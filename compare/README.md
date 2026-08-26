# LLM 后端对比（DeepSeek V4 vs Sonnet 5 订阅 vs GPT-5.6 Luna）

三个 preset 在同一套 TradingAgents 流水线上跑同一 ticker + 日期，仅更换 LLM 后端：

| Preset | 后端 | 模型 (deep / quick) | 前置条件 |
|---|---|---|---|
| `deepseek` | DeepSeek 官方 API | deepseek-v4-pro / deepseek-v4-flash | `.env` 填 `DEEPSEEK_API_KEY` |
| `claude-sub` | 本地代理 → Claude Pro/Max 订阅 | sonnet / sonnet（= Sonnet 5） | claude CLI 已登录 + 代理跑在 :3456 |
| `luna` | OpenAI 按量 API（占位，待提供 key） | gpt-5.6-luna / gpt-5.6-luna | `.env` 填 `OPENAI_API_KEY` |

## 用法

```bash
python compare/run.py --preset deepseek   --ticker NVDA --date 2026-07-18
python compare/run.py --preset claude-sub --ticker NVDA --date 2026-07-18
python compare/run.py --preset luna       --ticker NVDA --date 2026-07-18
```

结果与反思记忆按 preset 隔离在 `~/.tradingagents/compare/<preset>/`（logs = 报告，memory = 决策日志），避免一个后端的"经验"污染另一个后端的下一次运行。

### 宏观信息源 A/B（`--macro-source`）

```bash
python compare/run.py --preset deepseek --ticker NVDA --date 2026-07-28 --macro-source brief
```

- `feeds`（默认）：上游原行为——5 条固定宏观 query + FRED 指标
- `brief`：读取离线 deep search 日报（`~/.tradingagents/macro_briefs/`，契约见
  [specs/macro-brief-data-contract.md](../specs/macro-brief-data-contract.md)）；
  运行前会预检简报存在，缺失则在花任何 LLM token 之前退出
- 两臂共有：个股新闻、`search_news` 自主搜索、Polymarket——只有宏观来源这一个变量在动

## Claude 侧代理：claude-max-api-proxy

以子进程方式驱动**官方 claude CLI**（复用其 keychain 登录态），不提取 OAuth token——这是
Anthropic 2026-02-19 消费者条款更新后仍属受支持的官方编程用法（`claude --print`）。
代理装在 `~/Projects/claude-max-api-proxy`，暴露 OpenAI 兼容接口于 `http://localhost:3456/v1`。

```bash
cd ~/Projects/claude-max-api-proxy && npm start   # 启动（跑对比前需在跑）
curl -s http://localhost:3456/v1/models           # 验证
```

注意：代理每个请求 spawn 一次 CLI，单轮延迟明显高于直连 API，属正常现象；模型名用别名
`sonnet`（未知名字会被静默回落到 Opus，勿写全名）。

> 曾评估过用 codex-proxy 把 ChatGPT 订阅也转成本地端点（GPT-5.6 Luna），因其走非官方
> ChatGPT 后端接口、有账号风险，且 Luna 按量价格已很低（$1/$6 每百万 token），决定 Luna
> 直接走官方 API 占位，key 之后补。

## 对比时的注意事项

- **档位不对等**：v4-pro 是 DeepSeek 旗舰，Sonnet 5 是中档，Luna 是快速低价档。要对齐档位可
  编辑 preset（如 deep 全用各家旗舰，quick 全用各家快速档）。
- **数据面完全相同**：三个 preset 共用 yfinance 等数据源，差异只来自 LLM。
- **同日重复跑仍有随机性**：推理模型采样不保证可复现（见根 README "Reproducibility"）。
  建议每个 preset 跑 2–3 次看决策稳定性，而非单次定胜负。
- **`luna` preset 会打一条 RuntimeWarning**（模型不在 catalog，"Continuing anyway"），预期内。
