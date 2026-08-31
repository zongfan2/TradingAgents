# TradingAgents 结构化交易简报与 Lark 发布设计

**日期：** 2026-08-31

**状态：** 用户已批准交互设计，已通过规格自审，待用户复核书面版本

**范围：** 在每个 CN/US slot 完成后，从既有决策与订单账本确定性生成机器可读交易简报、Markdown 摘要，并通过 Lark incoming webhook 发布；不改变现有分析、风控或下单权威边界

## 1. 背景与目标

完整多智能体报告适合研究复核，但过长，不适合在 slot 结束后快速识别可行动计划。现有 `decisions.jsonl` 已保存经过确定性校验的 `TradePlan`，`orders.jsonl` 已保存 dry-run、提交、成交刷新和跳过原因；重新让 LLM 压缩完整报告既增加费用，也可能改写已经验证过的价格点位。

本功能新增一个 slot 级结构化交易简报发布器，目标是：

1. 每个 CN/US slot 结束后都产生一份简报；没有可行动计划时明确写“本次无交易”。
2. 简报包含 agent 所需的 `run_id`、方向、入场区间、提交限价、止损、目标位、风险预算、失效条件、阻止原因和执行结果。
3. 机器消费者只使用 `run_id` 重新读取权威账本；不得从 Lark 文本拼装订单。
4. 点位完全来自已保存且通过现有验证器的 `TradePlan`，不增加 LLM 调用，不生成新价格。
5. US 继续使用现有 Alpaca PAPER 执行适配器及全部门禁；CN/HK 在没有已验收执行适配器时强制为 `research_only`。
6. 通过名为 `TradingAgent Bot` 的 Lark incoming webhook 将人类可读版本发送到目标会话。

## 2. 非目标

- 不保证市场成交。系统只保证订单严格使用批准的限价语义；价格未触及则不成交、不追单。
- 不从 Lark 消息直接下单，不实现 Lark 交互卡片或点击下单。
- 不新增市价单、开空、部分止盈、拆仓、多目标自动退出或自由格式订单。
- 不为 CN/HK 接入券商。本轮只定义在未来 Futu OpenD 模拟盘适配器通过同等级验收后如何解除 `research_only`。
- 不改变 `TradePlan`、`DecisionRecord`、Alpaca PAPER-only、kill switch、仓位上限、行情新鲜度或重复订单规则。
- 不把 webhook、API key 或其他秘密写入仓库和运行产物。

## 3. 方案选择

### 3.1 采用：确定性发布器

发布器只读取精确的 slot `run_ids`、`DecisionRecord`、订单事件和 pipeline 组件状态，生成规范 JSON，再由同一规范对象渲染 Markdown 和 Lark 文本。

优点：点位与执行适配器一致；零额外模型费用；输出可测试、可重放、可审计。缺点：叙述不如 LLM 摘要自然，但简报首先是操作与审计接口，不是研究文章。

### 3.2 不采用：再次调用 LLM 压缩报告

该方案文字自然，但可能改变数字或遗漏门禁，产生新的模型费用和不可重复输出，不允许作为 agent 的交易输入。

### 3.3 延后：确定性点位加 LLM 市场叙述

后续可以增加明确标记为“非权威叙述”的文字层，但它不得进入规范 JSON，也不得影响 readiness、点位或订单。

## 4. 权威边界与安全不变量

1. `decisions.jsonl` 中由 `run_id` 定位的 `DecisionRecord` 是计划权威源。
2. `orders.jsonl` 是本地执行事件权威源；券商仍是订单和持仓的最终事实源。
3. JSON 简报是派生快照，不是新的决策账本，也没有下单权限。
4. Markdown 与 Lark 是 JSON 简报的确定性渲染，不得包含 JSON 中不存在的交易数字。
5. agent 只提交 `run_id`；执行适配器重新加载记录并重新执行所有静态和动态门禁。
6. 缺少精确 `run_ids` 时不得按日期扫描账本猜测，必须生成“分析不完整，本次禁止执行”的简报。
7. `plan_valid: false`、`HOLD`、`ERROR`、feeds shadow、manual trigger、eval 阻止状态或未配置市场执行适配器均不得被渲染成可执行计划。
8. Lark 成功与否不能改变交易决策、订单或执行结果。

## 5. 架构与数据流

```text
analysis_runner final summary (exact run_ids)
                 │
                 ▼
       atomic slot-run manifest
                 │
                 ▼
execution_adapter (US) / expected skip (CN)
                 │
                 ▼
       trade_brief publisher
         ├── canonical JSON
         ├── deterministic Markdown
         └── Lark text webhook
```

新增 `trade_brief` 为 orchestrator 的最后一个组件。顺序固定为：

```text
analysis_runner → execution_adapter → trade_brief
```

发布器在 execution adapter 之后运行，因此 US 简报可以显示真实的 `dry_run`、`submitted`、`filled`、`unfilled` 或 `blocked` 状态。CN 的 execution adapter 预期为 `skipped: us slot only`，不阻止简报生成。

### 5.1 精确 slot-run manifest

analysis runner 的最终 stdout summary 已包含本次运行成功写入账本的有序 `run_ids`。orchestrator 必须解析完整 summary，而不只保留计数，并在 analysis component 结束后原子写入 manifest。manifest 至少包含：

- schema version；
- session、slot date、slot started_at；
- analysis component status；
- planned、completed、errors、invalid_plans、pairs；
- 有序且去重的 `run_ids`；
- manifest generated_at。

若 analysis runner timeout、崩溃或没有可解析 summary，manifest 显式记录 `complete: false` 和空 `run_ids`。发布器不得回退到日期扫描。

这里的 `complete` 只表示 run ID 来源完整且可验证，不表示每个计划都成功：一个可解析的 summary 可以同时包含 `errors > 0` 和部分成功写入的 `run_ids`，发布器必须保留这些错误并只处理 summary 明确列出的记录。

## 6. 规范简报模型

规范 JSON 使用版本化 Pydantic contract，顶层结构为：

```json
{
  "schema_version": 1,
  "brief_id": "2026-08-31-us-20260831T093002Z",
  "session": "us",
  "date": "2026-08-31",
  "slot_started_at": "2026-08-31T09:30:02Z",
  "generated_at": "2026-08-31T12:05:46Z",
  "source_run_ids": ["2026-08-31-us-CRWD-brief-1"],
  "pipeline_status": "ok",
  "candidates": [],
  "holds": [],
  "warnings": []
}
```

每个 candidate 至少包含：

- `run_id`、ticker、session、arm、trigger、decision；
- `readiness`: `ready | research_only | blocked`；
- conviction、entry zone、limit price、stop、target 1、target 2 reference；
- risk percentage、horizon days、invalidation、source levels；
- macro/ticker eval verdict、plan validity；
- blockers（稳定的机器码加人类说明）；
- execution state、qty、limit/stop/target、broker status 和 skip reason（有则记录）。

`brief_id` 由 session、slot date 和 slot started_at 确定，同一次 slot 重试保持稳定，不同强制重跑产生新 ID。

## 7. Readiness 分类

分类优先级固定为 `blocked > research_only > ready`。例如 CN 计划同时存在 eval missing 时应标记为 `blocked` 并列出 eval 与 adapter 两项原因，而不是用 `research_only` 掩盖数据质量问题。

### 7.1 `ready`

只有同时满足以下静态条件的计划才可标记为 `ready`：

- 已配置并通过验收的该市场执行适配器；
- `arm=brief`；
- `decision` 与 `plan.action` 同为 BUY 或 SELL；
- `plan_valid=true`；
- trigger 为 core 或 catalyst；
- macro/ticker eval 均满足现有执行门禁；
- 不是 manual run。

`ready` 只表示可以交给执行适配器检查，不保证提交或成交。agent 按 `run_id` 调用适配器后，仍需通过 PAPER account、execution enabled、kill switch、ticker/day 去重、市场状态、资产可交易性、行情新鲜度、买力、持仓、总风险、订单数量和加仓规则。

### 7.2 `research_only`

在尚未配置并通过验收的 CN/HK 执行适配器时，所有 CN/HK BUY/SELL 计划强制为 `research_only`。这不是永久禁止 CN/HK 交易；未来 Futu OpenD 模拟盘适配器实现手数、价格档位、A 股 T+1、港股规则、市场时段、币种、仓位和 kill switch，并通过合同、离线门禁及独立审查后，才可解除该状态。

### 7.3 `blocked`

以下任一条件产生 `blocked`：

- plan invalid 或决策为 ERROR；
- feeds arm；
- manual trigger 未走显式执行流程；
- macro/ticker eval 为 fail；
- eval missing 且 `execute_on_missing_eval=false`；
- manifest 不完整或记录无法按 `run_id` 唯一解析；
- 计划与 decision action 不一致；
- 执行适配器已经记录动态 skip reason。

HOLD 单独聚合，不进入 candidates，也不显示伪造的价格点位。

## 8. 点位与订单语义

### 8.1 BUY

- Lark 与 Markdown 显示完整 `entry_zone`。
- `limit_price = entry_zone[1]`，即计划允许的最差买入价格；成交可以更优，但不能高于该上限。
- 数量在提交时由账户 equity、`risk_pct` 和 `limit_price - stop` 动态计算，并受现有 notional、gross exposure、position count、buying power 和 total risk 上限约束。
- 使用现有 GTC bracket：入场限价、保护止损和 `targets[0]` 第一目标。
- `targets[1]` 仅标记为 `target_2_reference`，不自动拆仓或部分止盈。
- 限价未触及则保持未成交或按现有 stale-entry 规则取消，禁止切换市价追单。

### 8.2 SELL

- SELL 只关闭已有多头，不开空。
- 有 entry zone 时使用既有计划限价语义；否则执行适配器仅可依据通过新鲜度检查的实时 bid 生成 marketable limit。
- 取消保护腿、可卖数量、一次重提和未成交升级仍由现有执行适配器负责。

## 9. 本地存储、修订与去重

运行数据保存在 `~/.tradingagents/trade_briefs/`，不进入仓库：

```text
trade_briefs/
├── manifests/<session>/<date>.json
├── archive/manifests/<session>/<date>.<generated-at>.json
├── <session>/<date>/<brief-id>.json
├── <session>/<date>/<brief-id>.md
└── delivery/<brief-id>.json
```

- manifest 使用原子写入；同日 force rerun 前将旧 manifest 归档。
- 规范 JSON 和 Markdown 以 immutable `brief_id` 命名；已存在且内容相同视为幂等成功，内容不同则 fail closed，不覆盖。
- delivery marker 只在 Lark 返回成功后原子写入。存在成功 marker 时跳过重复发送。
- marker 记录 brief ID、发送时间、HTTP status 和非敏感消息摘要，不保存 webhook 或完整远端响应。

## 10. Lark 发布

配置：

- `TRADINGAGENTS_LARK_ENABLED`，默认 false；
- `TRADINGAGENTS_LARK_WEBHOOK_URL`，仅来自 `.env`；
- 可选的短连接/读取超时，受 trade_brief component 外层预算限制。

webhook 字段必须设置为不参与 dataclass repr，不进入 config digest、状态文件、stdout、stderr、异常正文或测试快照。日志只能显示 `configured=true/false`。

首版使用 Lark incoming webhook 的普通文本 payload，优先可靠和可读，不引入交互卡片。消息结构：

1. session/date、pipeline 状态、ready/research-only/blocked/HOLD 计数；
2. READY candidates 的 run ID、点位、风险和执行状态；
3. research-only/blocked candidates 及原因；
4. HOLD ticker 聚合；
5. component warnings；
6. “agent 只使用 run_id、限价未触及不追单”的固定安全说明。

如果没有 READY candidates，仍发送“本次无可执行交易”。消息中的价格和数量全部来自规范 JSON。

发送成功必须同时满足 HTTP 2xx 和 Lark 响应体的业务成功状态；HTTP 200 但响应体表示拒绝、关键词校验失败或其他错误时仍按失败处理。远端响应正文不得原样进入日志。渲染器设置保守字符上限，READY 计划保留完整点位，blocked/research-only/HOLD 使用紧凑行，component error 只保留脱敏短摘要，避免超出 webhook 消息限制。

## 11. 失败与状态语义

- 本地产物成功且 Lark 成功：trade_brief component `ok`。
- 本地产物成功但 Lark disabled：本地成功；组件按配置明确记录为 `ok`，摘要注明 delivery disabled。
- 本地产物成功但 Lark timeout/429/5xx：短退避后最多重试一次；仍失败则 `warn`，保留产物且不影响既有交易结果。
- Lark 4xx：不重试，组件 `warn`，错误信息必须脱敏。
- manifest incomplete：生成 blocked-all 告警简报并尝试发送，组件 `warn`；不得扫描账本猜测。
- 本地 contract、持久化或唯一性失败：组件 `failed`；slot 已发生的分析或订单不回滚，但最终状态必须如实暴露发布失败。
- trade_brief 自身失败不能触发任何交易，也不能修改 decisions/orders/outcomes。

## 12. 代码与规格边界

实现阶段先新增/更新正式 specs，再改代码：

- 新增 `specs/trade-brief-publisher.md`，定义 manifest、brief contract、readiness、存储和 delivery 语义；
- 更新 `specs/orchestrator.md`，增加最后一步及状态映射；
- 仅在必要时补充 `specs/trade-plan-ledger-contract.md` 的派生消费者说明，不改变 v1 ledger schema；
- 新增 `pipeline/contracts/trade_brief.py`；
- 新增 `pipeline/trade_brief.py`；
- 修改 `pipeline/orchestrator.py` 和 `pipeline/config.py` 完成 manifest 与组件编排；
- 新增离线 tests；不在测试中访问 Lark、券商、模型或其他网络。

## 13. 测试与审查

最低覆盖：

1. manifest 只包含本 slot 精确 run IDs，不混入历史、manual 或并发外部记录；
2. incomplete summary fail closed；
3. READY/research-only/blocked/HOLD 分类矩阵；
4. BUY/SELL 点位、target 1 实际语义、target 2 reference；
5. orders join 后的 dry-run/submitted/filled/unfilled/skip 展示；
6. CN 在适配器未验收时始终 research-only；
7. 同日 force rerun revision、immutable output 和 delivery 去重；
8. webhook success、timeout、429、4xx、5xx 与单次重试；
9. webhook 不出现在 repr、日志、status、config snapshot 或异常；
10. orchestrator 顺序、CN expected skip、US execution result 和 component outcome mapping；
11. fixture end-to-end 从 analysis summary 到 JSON/Markdown/Lark fake transport；
12. `--no-send` 或等价预览模式不产生外部写入。

完成实现后必须运行：

```bash
.venv/bin/python -m devtools.verification.offline --only all
```

本功能触发合同、交易安全、数据完整性、外部网络、新组件、编排和最终合并等强制审查条件。确定性门禁通过后必须运行 Claude 独立审查，并检查绑定到精确 HEAD SHA 的 pass 报告。

## 14. 发布步骤

1. 默认保持 Lark disabled，完成全部离线测试和审查。
2. 对最近一个完成的 slot 执行只生成、不发送的预览。
3. 向用户展示即将发送的完整消息和 readiness 分类。
4. 用户确认内容后，将 webhook 仅写入本地 `.env` 并启用。
5. 发送一条真实测试简报，验证目标会话、TradingAgent Bot 身份、格式和去重 marker。
6. 保持 execution adapter 的原有 PAPER-only/default-off 规则；Lark 启用不等于交易启用。
7. 下一次自动 slot 观察本地产物、Lark delivery、status 和重复发送行为。

## 15. 验收标准

- 每个完成的 slot 都产生一个确定性、版本化、不可变的 JSON/Markdown 简报，或在缺少完整 manifest 时产生 blocked-all 告警简报。
- 所有交易数字可追溯到 source run ID 的 validated TradePlan 或对应订单事件。
- agent 可以仅凭 `run_id` 调用现有适配器，并且适配器重新执行全部风控；Lark 文本不能绕过门禁。
- BUY 限价不超过 entry zone 上沿，target 1 是唯一自动止盈，target 2 明确为参考，未触及限价不追单。
- 没有已验收 CN/HK 适配器时，CN/HK 永远不会被错误标记为 ready；适配器验收后可按相同接口解除限制。
- webhook 不泄露，失败可见但不会更改交易事实，成功发送幂等去重。
- 离线 deterministic gate 和 SHA 绑定的 Claude review 均通过。
