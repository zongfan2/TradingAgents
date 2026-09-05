# TradingAgents 结构化交易简报与 Lark 发布设计

**日期：** 2026-08-31

**修订：** 2026-09-05，纳入用户批准的执行安全、恢复与交付审查意见，并统一 Lark 阅读长度与发送预算

**状态：** 设计修订；尚未实现。用户批准修订方向不等于实现、执行入口或生产交付已经验收

**范围：** 从精确绑定分析 attempt 的决策、订单事件和上游状态确定性生成 JSON/Markdown 简报，并通过 Lark incoming webhook 发布。发布器保持只读交易边界；agent 提交能力另受本设计列出的执行安全前置门禁约束

## 1. 背景与目标

完整多智能体报告适合研究复核，但过长，不适合在 slot 结束后快速识别可行动计划。现有 `decisions.jsonl` 已保存经过确定性校验的 `TradePlan`，`orders.jsonl` 已保存 dry-run、提交、成交刷新和跳过原因；重新让 LLM 压缩完整报告既增加费用，也可能改写已经验证过的价格点位。

本功能新增一个 slot 级结构化交易简报发布器，目标是：

1. 每个 CN/US slot 的分析 attempt 结束后都产生一份简报；区分没有推荐、分析不完整、没有新提交和执行情况未知，不笼统写“本次无交易”。
2. 简报包含 agent 所需的 `run_id`、方向、入场区间、提交限价、止损、目标位、风险预算、失效条件、阻止原因和执行结果。
3. 机器消费者只使用 `run_id` 通过受控入口重新读取权威账本；不得从 Lark 文本拼装订单，也不得把旧简报当作当前执行授权。
4. 点位完全来自已保存且通过现有验证器的 `TradePlan`，不增加 LLM 调用，不生成新价格。
5. US 保留 Alpaca PAPER 及现有门禁，并在开放 agent 提交前补齐计划时效和被替代检查；CN/HK 在没有已验收执行适配器时不得成为可执行候选。
6. 通过名为 `TradingAgent Bot` 的 Lark incoming webhook 将人类可读版本发送到目标会话。
7. Lark 以 1000–1800 字符为常规阅读目标、2500 字符为软上限，最终请求体不超过 18,000 字节；不为压缩通知而裁剪权威数据或隐藏安全状态。

## 2. 非目标

- 不保证市场成交、精确止损成交价或最大实际亏损。符合限价不等于一定成交，未成交不追单。
- 不从 Lark 消息直接下单，不实现消息监听、自动消费群消息、交互卡片或点击下单。发布消息不代表已实现 agent 交易闭环。
- 不新增市价单、开空、部分止盈、拆仓、多目标自动退出或自由格式订单。
- 不为 CN/HK 接入券商。本轮只定义在未来 Futu OpenD 模拟盘适配器通过同等级验收后如何解除 `research_only`。
- 发布器不改变 `TradePlan`/`DecisionRecord` v1 schema，不降低 PAPER-only、kill switch、仓位、行情或重复订单门禁。新增执行前置检查属于独立工作包，不得藏在渲染逻辑中。
- 不承诺 webhook 严格 exactly-once；本地互斥和发送记录只能提供第 9 节定义的有限去重保证。
- 不把 webhook、API key 或其他秘密写入仓库和运行产物。

## 3. 方案选择

### 3.1 采用：确定性发布器

发布器只读取精确的 slot `run_ids`、`DecisionRecord`、订单事件和 pipeline 组件状态，生成规范 JSON，再由同一规范对象渲染 Markdown 和 Lark 文本。

优点：点位与执行适配器一致；零额外模型费用；输出可测试、可重放、可审计。缺点：叙述不如 LLM 摘要自然，但简报首先是操作与审计接口，不是研究文章。

### 3.2 不采用：再次调用 LLM 压缩报告

该方案文字自然，但可能改变数字或遗漏门禁，产生新的模型费用和不可重复输出，不允许作为 agent 的交易输入。

### 3.3 延后：确定性点位加 LLM 市场叙述

后续可以增加明确标记为“非权威叙述”的文字层，但它不得进入规范 JSON，也不得影响 eligibility、点位或订单。

## 4. 权威边界与安全不变量

1. `decisions.jsonl` 中由 `run_id` 定位的 `DecisionRecord` 是计划权威源。
2. `orders.jsonl` 是本地执行事件权威源；券商仍是订单和持仓的最终事实源。
3. JSON 简报是派生快照，不是新的决策账本，也没有下单权限。
4. Markdown 与 Lark 是 JSON 简报的确定性渲染，不得包含 JSON 中不存在的交易数字。
5. `run_id` 是定位符而不是权限凭证。agent 只提交该定位符；受控执行入口必须重新检查记录唯一性、时效、是否被替代和全部现有门禁。该能力在独立工作包通过前不可宣称可用。
6. 缺少精确 `run_ids` 时不得按日期扫描账本猜测；简报标记“分析来源不完整，无法完整确认本次结果，不可据此发起新交易”。这不代表此前没有订单，发布器也不能撤销此前订单。
7. `plan_valid: false`、`HOLD`、`ERROR`、feeds shadow、manual trigger、eval 阻止状态或未配置市场执行适配器均不得被渲染成可执行计划。
8. Lark 成功与否不能改变交易决策、订单或执行结果。
9. 历史简报、历史 skip 和 delivery 状态不能改写当前券商事实；消息重试不能重新运行 analysis 或 execution。

### 4.1 两个独立验收门槛

- **A：发布器门槛。** 精确归属、不可变快照、状态展示、交付恢复和离线/独立审查通过后，可以启用通知；不因此启用交易。
- **B：agent 提交门槛。** 第 7.4 节的受控入口及第 8 节执行策略完成正式 spec、代码、离线测试和 SHA 绑定独立审查后，才可开放 agent 提交。A 可先完成；B 未通过时，原本可以请求新提交的 US 计划改为 `next_action=review_required`，原因 `agent-execution-not-accepted`；既有订单仍按第 7.5 节观察，不因此变成可重提。
- 本设计不关闭或改写既有调度交易；其已发生结果如实展示。B 的状态来自已验收的部署能力，不得由 Lark enabled 或一个自报通过的运行时开关推断。

## 5. 架构与数据流

```text
persist analysis_attempt_id + incomplete manifest
                 │
                 ▼
analysis_runner final summary (exact run_ids)
                 │
                 ▼
       sealed attempt manifest
                 │
                 ▼
execution_adapter (US) / expected skip (CN)
                 │
                 ▼
   freeze upstream/order snapshot
                 │
                 ▼
       trade_brief publisher
         ├── canonical JSON
         ├── deterministic Markdown
         └── durable delivery record → Lark text webhook
```

新增 `trade_brief` 为 orchestrator 的最后一个组件。顺序固定为：

```text
analysis_runner → execution_adapter → trade_brief
```

发布器在 execution adapter 之后运行，只展示“截至观测时间的本地订单事件”，不主动查询券商，也不承诺捕获随后发生的成交。CN 的 execution adapter 预期为 `skipped: us slot only`，不阻止简报生成。

### 5.1 Attempt 身份与精确 manifest

orchestrator 在启动一次 analysis 调用之前持久化随机 UUID `analysis_attempt_id` 及 `complete=false` 的 manifest；只有身份持久化成功才可启动 analysis。不能以秒级时间戳或可被 `--only/--from` 重写的 status.started_at 充当身份。

analysis runner 的最终 stdout summary 已包含本次调用写入账本的有序 `run_ids`。orchestrator 必须保留完整 summary，在 analysis component 结束后校验并原子封存 manifest。字段至少包含：

- schema version；
- analysis_attempt_id、session、slot date、固定的 attempt_started_at，以及可选 parent_attempt_id；
- analysis component status；
- planned、completed、errors、invalid_plans、pairs、skipped、dropped；
- 无重复的有序 `run_ids`、完整 summary 的内容摘要；
- lifecycle（active/sealed）、complete、完整性原因、封存时间及 manifest 内容摘要。

summary 合同必须检查必填字段、严格类型、非负计数、`planned=completed+errors`、`invalid_plans<=completed`、`completed<=len(run_ids)<=planned` 和 pairs 的合法上界。不得将缺失计数默认为零，也不得用“去重”掩盖重复 ID。每个 ID 必须唯一解析为相同 session/date 的合法记录；异常整体视为 incomplete。

若 analysis runner timeout、崩溃或没有可解析 summary，manifest 显式记录 `complete: false` 和空 `run_ids`。发布器不得回退到日期扫描。

这里的 `complete` 只表示 run ID 来源完整且可验证，不表示每个计划都成功：一个可解析的 summary 可以同时包含 `errors > 0` 和部分成功写入的 `run_ids`，发布器必须保留这些错误并只处理 summary 明确列出的记录。

特别地，落账后日志处理失败的记录可能既有 run_id 又计入 errors，因此不能要求 `len(run_ids)=completed`。skipped/dropped 只表示覆盖和预算情况，不得伪装为 HOLD 或本 attempt 的成功计划。

### 5.2 恢复、重跑与状态快照

- 重新调用 analysis（包括补跑或 force）产生新 attempt；本次 run_ids 只描述本次新增记录。可以绑定 parent_attempt_id 解释恢复关系，但首版不合并父 attempt 的候选，不回扫当日账本补齐跳过的股票。
- 单独重试生成或发送必须显式选择已有 attempt/brief ID，不启动 analysis，不创建新分析身份。不存在、归属不符或损坏的 manifest 必须报错，不能退回同日旧文件。
- 每个 attempt 使用独立路径；封存后不可覆盖。进程中断遗留的 incomplete manifest 只可形成诊断简报，不能假装拥有完整 run_ids。
- active manifest 只允许 inspect，不生成 canonical/诊断 revision；生成必须等待 manifest 封存且 execution 结束状态快照已固定，防止过早预览占用正式 r1。恢复者须取得对应协调锁并确认原进程不再拥有该 attempt，才可将中断的 active manifest 封存为 incomplete。若分析已封存 complete 而 execution 中断，则保留精确 run_ids，执行结果标记 unknown，不将它变成“没有订单”。
- 在 execution 结束后、publisher 开始前冻结上游组件状态和订单账本一致性读取截止点。截止点、文件前缀/记录摘要必须可验证；读取到尾部半行、损坏记录或不一致数据时明确 unknown/incomplete，不得静默跳过。
- 简报的 `upstream_status` 只描述这次 attempt 的上游组件，不包含正在运行的 publisher，更不是尚未 finalize 的 slot 最终状态。恢复发布使用原快照，不能用重置后的全局 status 消除原告警。
- manifest、上游快照、brief ID/摘要和 delivery 结果在 status/history 中可关联。发布状态与交易事实分别保存。

## 6. 规范简报模型

规范 JSON 使用版本化 Pydantic contract，顶层结构为：

```json
{
  "schema_version": 1,
  "analysis_attempt_id": "653a92f7-cd4e-48d9-b012-e37cc678a316",
  "brief_id": "2026-08-31-us-653a92f7-cd4e-48d9-b012-e37cc678a316-r1",
  "revision": 1,
  "session": "us",
  "date": "2026-08-31",
  "attempt_started_at": "2026-08-31T09:30:02Z",
  "generated_at": "2026-08-31T12:05:46Z",
  "source_run_ids": [],
  "source_complete": true,
  "coverage": {"planned": 0, "completed": 0, "errors": 0, "invalid_plans": 0, "pairs": 0, "skipped": [], "dropped": []},
  "upstream_status": "ok",
  "candidates": [],
  "holds": [],
  "warnings": []
}
```

以上为结构示例；正式合同还必须包含 manifest/来源记录摘要、渲染版本、非敏感执行策略版本、上游组件快照和订单观测截止点。每个 candidate 至少包含：

- `run_id`、ticker、session、arm、trigger、decision；
- `eligibility`: `ready | research_only | blocked`，替代旧草案将 readiness 与执行状态混合的定义；
- `execution_state` 和 `next_action`，两者不得由 eligibility 单独推导；
- conviction、entry zone、planned_limit_price、stop、target 1、target 2 reference；
- market/venue、currency、价格精度、risk_pct 及其单位、horizon_days、invalidation、source levels；
- 计划适用交易日、execution_expires_at、superseded 状态及策略版本；缺少可验证策略时不得伪造有效期；
- macro/ticker eval verdict、plan validity；
- blockers（稳定的机器码加人类说明）；
- 按订单 ID 关联的历史尝试、订单角色、paper/dry-run 标识、requested_qty、filled_qty、filled_avg_price、实际 submitted limit/stop/target、broker status、execution_observed_at（有则记录，未知用 null 而非 0）。

`brief_id` 由 session、slot date、analysis_attempt_id 和 publication revision 确定。重试保持相同 ID；显式刷新快照创建新 revision 并绑定前一版本。旧版本保持历史含义，agent 不得以旧快照的 ready 代替当前检查。

## 7. 计划资格、订单事实与下一步动作

三个维度独立建模：eligibility 表示快照时的计划资格；execution_state 表示已观测订单事实；next_action 表示可进行的操作。ready 不是提交许可，已提交/成交的计划即使静态合格，也不能提示重复提交。

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
- 已知过期或被替代的计划不合格；未知的时效/能力检查进入 next_action 的人工处理门禁。

`ready` 只表示快照时满足静态条件，不保证提交或成交。即使 next_action 允许请求检查，提交时仍必须通过第 7.4 节，以及 PAPER account、execution enabled、kill switch、ticker/day 去重、市场状态、资产可交易性、行情新鲜度、买力、持仓、总风险、订单数量和加仓规则。

### 7.2 `research_only`

在尚未配置并通过验收的 CN/HK 执行适配器时，原本合格的 CN/HK BUY/SELL 计划为 `research_only`；若另有数据或 eval 错误，则按优先级标记 blocked 并保留 adapter 原因。这不是永久禁止 CN/HK 交易；未来 Futu OpenD 模拟盘适配器实现手数、价格档位、A 股 T+1、港股规则、市场时段、币种、仓位和 kill switch，并通过合同、离线门禁及独立审查后，才可解除限制。

### 7.3 `blocked`

以下任一条件产生 `blocked`：

- plan invalid 或决策为 ERROR；
- feeds arm；
- manual trigger 未走显式执行流程；
- macro/ticker eval 为 fail；
- eval missing 且 `execute_on_missing_eval=false`；
- manifest 不完整或记录无法按 `run_id` 唯一解析；
- 计划与 decision action 不一致；
- 已知计划过期或已被较新分析替代。

HOLD 单独聚合，不进入 candidates，也不显示伪造的价格点位。

### 7.4 Agent 提交的独立前置工作包

当前 `run_submit(..., run_id=...)` 直接选择原 record/date，未复用普通入口的 latest-attempt 筛选。因此仅在 Lark 中标注 run_id 不能完成安全接入。正式执行 spec 必须先约束以下行为，再在共享执行边界实现，而不是仅在 agent 的提示词或 UI 中检查：

1. **入口。** 首版由本地受控查询入口提供 run_id 对应的计划和最新状态，提交复用加固后的 execution_adapter `submit --run-id`。进程权限与既有 execution 开关提供授权；run_id 本身不授权。只接收 run_id，不接收调用者提供的价格、数量、忽略风控参数或自由格式指令。B 未验收前只开放查询，不开放 agent 提交。
2. **来源。** 对自动简报消费只接受完整封存 manifest 中唯一绑定的自动 brief/core-or-catalyst 记录；拒绝 manual、feeds、跨市场、缺失或矛盾的归属。读取损坏、重复记录和无法判断当前版本时 fail closed。
3. **有效期。** 本修订为 B 选择的新增保守策略是：新提交仅适用于 record.date 对应的美国交易日，并须早于该日正常交易时段收盘；按 America/New_York 当前日期及交易日历处理假日、提前收盘与 DST，市场状态门禁仍生效。跨日旧 BUY/SELL 均拒绝，不能用 record.date 替换真实当前时间。execution_expires_at 等于该日收盘时间，horizon_days 不延长提交有效期；若未来要求更短期限，应另作版本化策略变更。
4. **被替代。** 执行前检查相同 date/session/ticker/arm 的更高账本尝试号（run_id 的 n，与 analysis_attempt_id 不同），必须在过滤 BUY/SELL 前判断；新 HOLD、SELL、ERROR、manual 或无效计划都不能使旧 BUY 重新成为最新可执行计划。发现较新记录则拒绝旧 run；若有已登记且正在分析该标的的新 attempt，则在其明确结束前暂停旧计划新提交。读取后续同键记录仅用于失效判定，不得把它们补入原 manifest 的候选集合。
5. **同一执行边界。** 查询结果不是锁或授权，提交必须重新读取并检查；使用与分析/执行写入一致的协调规则，防止检查后又发布新版本的竞态。锁范围、顺序与并发验收由该工作包正式 spec 定义，未验收不得开放 agent 提交。
6. **既有订单。** 到期/替代检查约束新提交，不能阻断既有订单的查询、refresh、恢复或保护性维护。GTC 已挂订单的取消仍属于执行适配器已有生命周期；本发布器不自动取消，不把提交有效期误称为自动撤单时间。

拒绝原因采用稳定机器码，例如 `plan-expired`、`plan-superseded`、`analysis-in-progress`、`source-unverified`；不能通过刷新简报时间消除这些原因。

### 7.5 订单归并与 next_action

- 先按 client_order_id 和订单角色归并事件，再关联 run_id；同 ticker 的不同 run 共享去重 ID 时，展示原订单来源与本次 dedupe 原因，不能把旧订单说成本 run 新下的单。
- 区分 entry、close 和保护腿，不能把止盈腿成交笼统解释为入场成交。当前账本无法证明的 parent/leg 关系和保护有效性标记 unknown，不根据 parent filled 推断整个交易结束。历史 skip 是尝试结果，不能覆盖后续 submitted/filled；后续 dedupe skip 也不能把已成交订单降级为 blocked。
- `execution_state` 至少区分 `not_attempted`、`dry_run`、`pending`、`partially_filled`、`filled`、`rejected`、`canceled`、`expired`、`unknown`。broker_status 优先于事件名称；`kind=submitted, broker_status=rejected` 必须为 rejected。dry-run 不覆盖真实 PAPER 订单。
- 无 refresh 不能证明未成交。requested_qty 与 filled_qty 分别显示，例如“申请 10，已观测成交 5”；没有完整观测时不得将未知数量写成 0。execution_observed_at 是所用事件时间，不是渲染时间。
- next_action 按以下顺序确定：涉及当前订单/提交事实的 unknown、未解决 intent、拒单或需恢复时为 `review_required`；已知 pending/部分成交为 `wait`；已有已完成、取消或过期订单且无需新提交为 `none`；无上述订单事实但静态 blocked/research_only/HOLD 为 `none`；其余计划在 B 未通过、缺少已验收的时效/失效策略、必要来源数据缺失或策略要求人工判断时为 `review_required`；仅剩余合格且无现存/不确定订单的计划为 `request_execution_check`。它仍不是保证下单的指令，所有原因均保留。
- 可由受控入口完成但尚未进行的实时行情等动态检查，不阻止 `request_execution_check`；入口必须获取当前数据并通过全部门禁才能提交。不能把“尚未检查”写成“已经通过”，也不能让已知过期、被替代或失效的计划请求新提交。
- `wait`/`none` 只表示观察或无需新提交，不能自动调用 execution_adapter refresh：现有 refresh 会撤单和重提 SELL，并非只读。恢复和维护仍走明确授权的适配器路径，publisher 不创建订单。
- 临时报价或市场关闭等 skip 保留 `last_attempt_reason`；以后可以重新请求门禁检查，但不能将过期、替代、拒单或未解决 intent 当成可自动重试的新交易。
- 计数分别报告静态合格、可请求检查、待成交、已观测成交和未知数量，不再用 READY 总数表示“可新增交易数量”。

## 8. 点位与订单语义

### 8.1 BUY

- Lark 与 Markdown 显示完整 `entry_zone`。
- `limit_price = entry_zone[1]`，即计划允许的最差买入价格；成交可以更优，但不能高于该上限。
- entry zone 不是现有适配器已经实现的完整触发区间：当前上沿限价不能阻止跌破下沿后买入。发布器必须说明这一点，不能暗示已验收“只在区间内成交”。
- B 工作包必须明确并测试入场下沿/stop 失效规则；本修订选择的新增保守策略为新提交前有效实时 ask 低于 entry_zone[0] 或不高于 stop 时拒绝新 BUY，报价缺失、非法或过期也拒绝，不移动点位、不追单。这是提交前检查，不保证挂单后的成交价永远落在区间内；在途失效和撤单仍由独立执行策略处理。
- 数量在提交时由账户 equity、`risk_pct` 和 `limit_price - stop` 动态计算，并受现有 notional、gross exposure、position count、buying power 和 total risk 上限约束。
- 使用现有 GTC bracket：入场限价、保护止损和 `targets[0]` 第一目标。stop 是触发价而非保证成交价。
- `targets[1]` 仅标记为 `target_2_reference`，不自动拆仓或部分止盈。
- 限价未触及则保持未成交或按现有 stale-entry 规则取消，禁止切换市价追单。
- `risk_pct` 的单位为账户 equity 的百分比风险预算，不是仓位占比，也不是实际亏损保证；计划价格与实际委托/成交价格分字段展示。
- 自由文本 invalidation 只展示，不让模型临场解释为交易指令，也不宣称全部已自动检查。若新执行策略依赖无法机器核验的失效条件，则转 `review_required`，不得自行发明价格或覆盖条件。

### 8.2 SELL

- SELL 只关闭已有多头，不开空。
- 有 entry zone 时使用既有限价语义 `entry_zone[0]`；否则由适配器按既有 bid 折价和精度规则，在通过行情新鲜度检查后生成 marketable limit。发布器不拉行情或预填该动态价格；未提交时 planned_limit_price 可为 null，注明 `price_source=execution_quote`。
- 取消保护腿、可卖数量、一次重提和未成交升级仍由现有执行适配器负责。

所有价格必须携带市场、币种与来源，不能仅凭 ticker 推测；缺失或冲突时保留未知并阻止机器消费。精度沿用已验收市场适配器/计划合同，格式化不得把非法点位四舍五入成“合法订单”，不得对所有市场统一假设两位小数。

## 9. 本地存储、修订与去重

运行数据保存在 `~/.tradingagents/trade_briefs/`，不进入仓库：

```text
trade_briefs/
├── manifests/<session>/<date>/<analysis-attempt-id>.json
├── snapshots/<analysis-attempt-id>/<revision>.json
├── <session>/<date>/<brief-id>.json
├── <session>/<date>/<brief-id>.md
├── delivery/<destination-id>/<brief-id>.json
└── locks/
```

所有路径从配置 state_dir 派生，测试使用隔离目录，不硬编码用户主目录。attempt、revision、destination ID 必须通过路径安全验证。

### 9.1 不可变快照与产物恢复

- 每个 attempt/revision 的生成由进程间锁串行化。manifest 在初始 incomplete 到终态封存期间原子更新；封存后不覆盖，同日新 attempt 不替换旧 attempt 文件。
- 首次生成固定 generated_at、来源摘要、上游状态、订单截止点、渲染版本和消息内容。先持久化可恢复的来源快照，再原子写 canonical JSON；同 ID 重试不能重新采样时间、账户、订单或配置。
- canonical JSON 已存在则校验并直接复用；缺失 Markdown 或消息产物从它及绑定渲染版本补齐。既有内容不同、来源摘要不符或旧渲染版本不可用时 fail closed，不覆盖、不默默升级。
- 显式刷新事实创建新 revision；补发现有内容不创建新 revision。所有机器消费者仍须查询当前权威状态，历史 JSON 不原地更新。

### 9.2 发送记录与有限去重

以 `(destination_id, brief_id)` 为交付键，使用进程间锁和原子持久化发送记录。destination_id 为非敏感逻辑目标标识，不是 webhook；切换群目标必须显式切换标识，不能沿用旧成功记录或向新目标自动重放历史简报。

状态为 `pending → sending → sent | failed | unknown`：

- 发送前持久化 pending、不可变消息摘要、消息版本及尝试计数；发 HTTP 前必须持久化 sending。
- 仅明确的 HTTP 与业务成功回执才能记录 sent；相同消息摘要的 sent 直接跳过。成功写回前崩溃或写盘失败仍不能断言未发送。
- 只有可证明请求尚未发送或远端明确拒绝接收，才记录 failed 并按第 10 节决定是否重试。
- 读取响应超时、发送后断连、模糊 5xx/协议响应、遗留 sending 均进入 unknown。不得自动重发 unknown，即使这可能漏送；本地告警保留不确定事实。
- 显式人工重发 unknown 必须注明可能重复，保留原 brief_id/消息内容并追加交付尝试记录。不得清空记录冒充首次发送；sent 不自动重复发送。
- 记录发送时间、消息摘要、HTTP/业务码和脱敏错误分类，不保存 webhook、签名、密钥或完整远端响应。送达仅指服务端确认接收，不代表用户已读或 agent 已执行。

本地锁防并发重复，sent 记录防已确认成功重发；两者都不能消除远端接收与本地落盘之间的不确定窗口，不承诺严格 exactly-once。

## 10. Lark 发布

配置：

- `TRADINGAGENTS_LARK_ENABLED`，默认 false；
- `TRADINGAGENTS_LARK_WEBHOOK_URL`，仅来自 `.env`；
- 可选 `TRADINGAGENTS_LARK_SIGNING_SECRET`，仅来自 `.env`，在机器人启用签名校验时必须配置；
- 非敏感 destination_id 与必需关键词（如机器人已配置），以及受外层预算限制的连接/读取超时；

webhook 和签名秘密必须设置为不参与 dataclass repr，不进入 config digest、状态文件、stdout、stderr、异常正文或测试快照。日志只显示配置是否存在与脱敏分类。仅接受预期 Lark HTTPS webhook 主机/路径，不跟随重定向。不得在文档中保存真实 endpoint。

首版使用 Lark incoming webhook 的普通文本 payload，优先可靠和可读，不引入交互卡片。消息结构：

1. brief_id、session/date、观测时间、upstream 状态和数据完整性；
2. 静态合格/可请求检查/待成交/已观测成交/未知计数，以及候选的 run ID、点位、风险、execution_state 和 next_action；
3. research-only/blocked candidates 及原因；
4. HOLD ticker 聚合；
5. component warnings；
6. “仅为历史快照，agent 必须通过受控入口重新检查 run_id；不保证成交，不追单”的固定说明。

完整数据下没有合格计划可写“无新增合格推荐”；已有订单但无新提交请求写“无新增提交，已有订单见状态”；来源不完整写“无法完整确认本次结果”。不能从候选数为零推导“没有交易发生”。消息中的价格和数量全部来自规范 JSON。

### 10.1 统一长度预算与确定性压缩

长度限制只作用于人类阅读的 Lark 渲染，不裁剪上游完整报告、canonical JSON 或本地完整 Markdown。JSON 保留全部候选、点位、门禁与订单字段；Markdown 可以分节，但不能为了符合通知长度丢掉条目。

| 层级 | 预算 | 处理规则 |
|---|---|---|
| 常规阅读目标 | 1000–1800 字符 | 信息较少时可以更短，不为达到下限填充文字 |
| 阅读软上限 | 2500 字符 | 超过目标先压缩说明；必要字段仍超出软上限时允许完整发送，不删候选凑字数 |
| 发送硬上限 | 18,000 字节 | 最终请求体超过此值则降级为 summary_only，不能发送截断的交易条目 |

- **字符口径。** 对确定性渲染完成、安全转义处理后的 `content.text` 按 Unicode code point 计数（Python `len(text)`）；中文、英文、数字、标点、空格和换行均计入，组合 emoji 可能占多个 code point。该数不是中文汉字数、英文词数或模型 token 数，也不是 JSON 转义后的长度。
- **字节口径。** Lark 官方规定请求体不超过 20 KB；内部硬上限取 18,000 字节。发送前检查最终序列化 JSON 的 UTF-8 字节数，包括 JSON 转义、签名和 timestamp 开销，不用“每字三个字节”推算。
- **常规排版。** 标题/统计/观测时间约 150–250 字符；每条 BUY/SELL 候选参考 200–300 字符；HOLD 聚合、告警和固定说明参考 100–200 字符。这些都是排版预算，不是字段截断上限；零候选通知一般可控制在 200–500 字符。

超出常规目标时，按以下固定顺序压缩，不增加 LLM 调用，也不按 token 截断：

1. 去掉重复标题、空行和装饰；共用的市场、币种、时区、策略说明和固定免责声明移到组标题/页尾一次展示，保持每条候选的解释范围明确。
2. 压缩非安全性的研究叙述、来源长说明和重复历史尝试描述；使用固定短标签。原文仍留在本地完整产物；不改变数字、符号、单位或安全条件的含义。
3. HOLD 按市场聚合 ticker；相同阻止码/组件告警共用一份短说明，每个受影响候选仍保留完整原因码和明确关联。不把多个不同故障折叠为一个模糊“失败”。
4. 用紧凑完整条目列出所有 BUY/SELL 候选，不采用 Top-K、静默省略尾部候选或只展示成功项。每条必须原样保留完整 run_id，并保留适用的方向、原始有效点位、币种、风险单位、执行有效期/未知标记、阻止原因、execution_state 和 next_action；若有订单则区分申请/成交数量及观测时间。HOLD/无效计划不生成伪造点位。
5. 如果这些必要内容仍超过 2500 字符、但请求体不超过 18,000 字节，则发送完整紧凑版本，记录 `over_soft_limit=true`。不能为了达标截去 run_id、点位、有效期或安全条件；仅超过阅读软上限不等同于发送失败。

安全性 invalidation 文本必须完整保留，可以对相同条件共用完整说明并明确关联，不得截成似乎完整的半句。它属于必要内容，超过软上限仍保留；若因此超过字节硬上限，整份通知按下述 summary_only 降级，不以人工复核标签替代缺失条件，也不由渲染器改写 canonical 的资格、next_action 或执行事实。

压缩后请求体仍超过硬上限时，发送固定格式的 `summary_only` 通知：目标不超过 500 字符，只包含 brief_id、session/date、观测时间、来源完整性、规范计数、关键告警及“完整点位未送达，不可据此提交，请按 brief_id 查本地完整简报”。不发送部分候选或截断点位；canonical/Markdown 保留全部内容，组件报 warn。首版不分多条消息，避免部分送达被误认为完整；降级通知也必须通过最终字节检查，否则本次不发送并记录明确失败。

渲染版本固定排序、短标签、去重和压缩顺序；在首次生成时绑定 `delivery_variant=full|compact|summary_only`、text_chars、over_soft_limit 和消息摘要。发送记录另存每次最终 payload_bytes。相同 brief/destination 的重试复用原文本与 variant；预算或模板变更需要显式新 revision，不能在补发时偷偷重新裁剪。若重试时最终认证包装使已冻结消息超过硬限，则发 HTTP 前拒绝该次发送，记录 `payload-too-large`；不能改换 summary_only 冒充同一已冻结交付。

### 10.2 签名与安全渲染

- 报告自由文本须脱敏并处理 `<at ...>` 等标记，不能意外触发群提醒或形成 agent 指令。限长只适用于非安全叙述和错误摘要；安全条件不得语义截短，超预算按第 10.1 节整体降级。run_id 等结构字段也须校验并保持完整。
- 启用签名时按官方算法生成 timestamp/sign，每次明确允许的 HTTP 尝试重新签名；稳定消息摘要不包含变化的签名/认证时间戳。签名过期不通过改写 brief generated_at 解决。

### 10.3 回执与重试分类

- 成功必须同时满足 HTTP 2xx 和合法业务响应 `code=0`。兼容历史 StatusCode 的规则必须明确测试；缺失、矛盾或不可解析回执按 unknown，不将 HTTP 200 一概算成功。
- 明确未发送的连接失败、明确限流拒绝（HTTP 429 或业务限流码 11232）可在预算内短退避后最多重试一次；尊重 Retry-After，超过剩余预算则保留失败供显式补发。
- 关键词、IP 白名单、签名/鉴权、payload 等明确拒绝不自动重试；提示修正配置。429 是一般 4xx 不重试规则的明确例外。
- 请求可能送达后的 timeout、连接重置或非明确拒绝的 5xx 按 unknown，不适用自动重试。外层 component timeout 也必须能在恢复时将遗留 sending 识别为 unknown。

接口依据：[Lark Custom bot usage guide](https://open.larksuite.com/document/client-docs/bot-v3/add-custom-bot)（2026-09-05 核对；包含请求体限制、业务回执、限流、安全设置和普通文本 @ 行为）。实现时按该官方合同编写 fake transport 用例，不访问真实 webhook。

## 11. 失败与状态语义

- 本地产物完整且 Lark 明确成功：trade_brief component `ok`；上游失败仍独立保留，不能被发布成功洗成整个 slot 成功。
- 本地产物成功但 Lark disabled：本地成功；组件按配置明确记录为 `ok`，摘要注明 delivery disabled。
- Lark 明确失败、unknown 或 summary_only：组件 `warn`，分别保留失败、不确定或降级原因。只有第 10.3 节列出的确定拒绝/未发送情形可以自动重试。完整紧凑消息仅超过 2500 字符软上限而未超过字节硬上限时不因此报 warn，但须保留长度指标。
- manifest incomplete：生成来源不完整告警简报并尝试发送，组件至少 `warn`；不能枚举未知候选为“全部 blocked”，不得扫描账本猜测，也不宣称未发生交易。
- 简报自身 contract、持久化或产物唯一性失败：组件 `failed`；slot 已发生的分析或订单不回滚，但最终状态必须如实暴露发布失败。上游输入损坏若能形成合法 incomplete 诊断则按上一条 warn；不得吞掉诊断产物自身的失败。
- trade_brief 自身失败不能触发任何交易，也不能修改 decisions/orders/outcomes。
- 若组件被外层杀死，则 status 如实为 timeout；下一次只允许显式恢复产物或交付，不重跑整条 pipeline。即使 Lark 不可用，失败仍须在本地 status/history/交付记录中可见。

## 12. 代码与规格边界

本次仅修订设计文件。实现拆成两个工作包，先新增/更新正式 specs，再改代码：

### A. 简报与发布

- 新增 `specs/trade-brief-publisher.md`，定义 attempt manifest、严格 summary、快照、三维状态合同、存储和 delivery 状态机；
- 更新 `specs/orchestrator.md`，增加最后一步及状态映射；
- 仅在必要时补充 `specs/trade-plan-ledger-contract.md` 的派生消费者说明，不改变 v1 ledger schema；
- 新增 `pipeline/contracts/trade_brief.py`；
- 新增 `pipeline/trade_brief.py`；
- 修改 `pipeline/orchestrator.py` 和 `pipeline/config.py` 完成 manifest 与组件编排；
- 如需要持久化完整 summary/attempt 元数据，修改 `pipeline/analysis_runner.py` 及其正式 spec，不重新调用模型来补归属；
- 更新 `specs/README.md` 和 `RUNBOOK.md`，定义按 attempt/brief ID 的 inspect、no-send、补产物和 send-only 恢复入口；具体 CLI 拼写由正式 spec 统一，不把本设计中的接口名称说成现有命令；
- 新增离线 tests；不在测试中访问 Lark、券商、模型或其他网络。

### B. Agent 执行安全前置

- 更新 `specs/execution-adapter.md` 及必要的共享合同说明，定义有效期、superseded、入场失效、受控查询/提交入口和分析/执行协调规则。
- 修改 `pipeline/execution_adapter.py` 及所需的最小共享协调代码；自动消费者不能绕过检查直调旧入口。该工作包必须单独验证，不由 publisher 模拟风控通过。
- 若确需 schema 扩展，先版本化合同，不能为了遵守原草案“不改 schema”而将必要安全数据藏在自由文本中。
- A 可以先交付通知；B 未完成时不开放 agent 提交，不将发布器成功标成交易闭环完成。

## 13. 测试与审查

最低覆盖（A/B 各自实现的条目必须有确定性测试）：

1. manifest 只包含本 slot 精确 run IDs，不混入历史、manual 或并发外部记录；
2. incomplete、缺字段、计数不符、重复 ID、归属冲突和账本损坏 fail closed；落账后日志失败仍保留合法 ID；
3. eligibility/research-only/blocked/HOLD、execution_state、next_action 三维分类矩阵；
4. BUY/SELL 点位、target 1 实际语义、target 2 reference；
5. 先 skip 后成功、成功后 dedupe、拒单却有 submitted 事件、部分成交、订单腿、共享 CID、未知 refresh 和 dry-run/PAPER 优先级；
6. CN/HK 在适配器未验收时不能 ready，数据错误不能被 research_only 掩盖；B 未通过时 US 也不能 request_execution_check；
7. 新分析/补跑/force 创建新 attempt；active attempt 只能 inspect，确认原进程结束后才能封存中断诊断；发布重试复用旧 attempt；JSON 已写但 Markdown 未写、订单新增后重试、渲染版本变化、并发生成与显式修订；
8. webhook 明确成功/拒绝、HTTP 与业务限流、签名错误、读取超时、模糊 5xx、成功后 marker 落盘前崩溃、遗留 sending、并发补发及 unknown 不自动重试；
9. webhook 不出现在 repr、日志、status、config snapshot 或异常；
10. orchestrator 顺序、CN expected skip、US execution result 和 component outcome mapping；
11. fixture end-to-end 从 analysis summary 到 JSON/Markdown/Lark fake transport；
12. no-send 不产生外部写入；send-only 不能启动 analysis/execution；旧 slot 无 manifest 时不按日期拼出预览；
13. 旧 BUY 被新 HOLD/SELL/ERROR 替代、分析正在更新时旧 run 请求、跨日旧 BUY/SELL、节假日/提前收盘/DST、查询后版本改变的竞态；
14. 提交有效期不阻断既有订单对账/保护，未知 intent 不被自动重提，BUY 跌破下沿或 stop 不新提交；
15. 中英文/组合 emoji/空格换行的字符计数；目标上沿 1800、软上限 2500 字符和硬上限 18,000 字节各自的等于/超出边界；JSON 转义和签名开销计入字节但不误计入正文字符；压缩后必要字段完整、多候选不做 Top-K、短通知不凑字数、超硬限只发 summary_only、自由文本不能触发 @；
16. 恢复发布保留原上游失败，零候选不能证明零交易，incomplete 与订单 unknown 均不显示伪造的零数量。
17. 相同输入与渲染版本的文本、压缩 variant、字符计数和摘要稳定；预算变更不改变既有消息的补发内容；summary_only 自身超硬限或重试认证包装使冻结消息超硬限时不发送；安全失效条件不能被截短，首次生成时必要条件导致超硬限则整体降级，不修改 canonical 的 next_action。

完成实现后必须运行：

```bash
.venv/bin/python -m devtools.verification.offline --only all
```

本功能触发合同、交易安全、数据完整性、外部网络、新组件、编排和最终合并等强制审查条件。确定性门禁通过后必须运行 Claude 独立审查，并检查绑定到精确 HEAD SHA 的 pass 报告。

## 14. 发布步骤

1. 默认保持 Lark disabled，完成 A 的全部离线测试和审查；B 单独验收。
2. 使用 fixture 或部署后首次具有完整 manifest 的真实 slot 执行 no-send 预览。历史 slot 只有在已有完整、可验证的原始 summary 和归属证据时才可显式导入；否则只做来源不完整诊断，不为了预览补跑付费分析或回扫日期猜 ID。
3. 向用户展示即将发送的完整消息、text_chars、最终 payload_bytes、压缩 variant、三维状态和送达策略；复核阅读长度、不丢必要字段以及 unknown 不自动重发的取舍。
4. 用户确认内容后，将 webhook 仅写入本地 `.env` 并启用。
5. 发送一条明确标记为测试/历史快照的简报，验证目标会话、TradingAgent Bot 身份、格式、服务端回执和持久化交付记录；测试消息不赋予执行权限。
6. 保持 execution adapter 的原有 PAPER-only/default-off 规则；Lark 启用不等于交易启用。
7. 下一次自动 slot 观察本地产物、Lark delivery、status 和重复发送行为。
8. 只有 B 的正式执行策略、受控入口及绑定部署 revision 的审查通过后才开放 agent 提交；不把 A 的通知验收当作 B 的验收。

## 15. 验收标准

- 每个结束的分析 attempt 都产生一个确定性、版本化、不可变的 JSON/Markdown 简报，或在缺少完整 manifest 时产生来源不完整告警；发生持久化故障则留下可见的失败，不伪报产物存在。
- 所有交易数字可追溯到 source run ID 的 validated TradePlan 或对应订单事件。
- A 验收证明简报/通知可靠，不证明 agent 提交可用。B 验收必须证明受控 run_id 入口拒绝过期/被替代/来源不明的计划并重新执行全部风控；Lark 文本不能绕过门禁。
- BUY 限价不超过 entry zone 上沿，target 1 是唯一自动止盈，target 2 明确为参考，未触及限价不追单。
- 没有已验收 CN/HK 适配器时，CN/HK 永远不会被错误标记为 ready；适配器验收后可按相同接口解除限制。
- 申请数量、已成交数量与未知状态不混淆；历史 skip 不覆盖交易事实，过期快照不能诱导重复提交。
- Lark 统一采用 1000–1800 字符目标、2500 字符软上限和 18,000 字节硬上限；压缩优先去掉重复叙述，不裁剪 agent JSON、不静默丢候选、不截断必要点位或安全字段，超硬限明确降级。
- 重试复用冻结产物，来源和 revision 可追溯；webhook/签名不泄露，已确认 sent 去重，unknown 可见且不自动重发，不承诺严格 exactly-once。
- 离线 deterministic gate 和 SHA 绑定的 Claude review 均通过。
