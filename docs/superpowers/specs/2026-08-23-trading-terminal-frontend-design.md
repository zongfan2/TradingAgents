# TradingAgents 现代机构级交易终端设计

**日期：** 2026-08-23

**状态：** 已通过规格自审，待用户复核

**范围：** 重构现有 `webui` 前端；保留现有 FastAPI、Pipeline v2 与数据契约

## 1. 背景

TradingAgents 已具备完整的本地 Web API 和两套原生 HTML/JavaScript 页面，覆盖行情、多智能体分析、分层报告、股票池、简报评估、A/B 聚合、决策与订单、执行熔断和配置。当前界面能够操作这些功能，但主页与 Pipeline 页面相互割裂，工程状态和交易工作流混在同一层级，难以支持开盘前的快速判断，也难以继续扩展。

现有前端约 1,600 行 HTML/CSS/JavaScript，包含近 80 个直接操作的 DOM 标识。继续在大型原生脚本中增加路由、共享状态、表格、轮询和响应式交互，会提高维护成本。因此本次将前端重构为 React + TypeScript 应用，同时保持 Python 后端和既有文件契约稳定。

## 2. 产品定位

产品是面向主动交易者的“AI 投资研究指挥台”，首要任务是帮助用户在开盘前快速浏览市场环境、候选机会和风险警报，并从机会直接进入可解释的多智能体研究和纸面交易计划。

研究员和开发者是第二类重要用户。系统必须保留 Agent 过程、输入版本、简报质量、模型 A/B、流水线健康度和运行诊断，但这些工程信息进入独立的“系统”区域，不干扰交易者主线。

### 2.1 核心原则

1. 每个一级页面只回答一个核心问题。
2. 首屏显示摘要和待处理事项，详细内容通过任务型页面和 Tab 展开。
3. 数据、时间和风险语义优先于装饰性 AI 表达。
4. AI 判断必须展示证据、分歧、置信度和输入新鲜度，而不是只显示 BUY/SELL 标签。
5. 当前仅支持 PAPER 执行；界面不得暗示已经具备实盘能力。
6. `EXECUTION_HALT`、市场时段、PAPER 模式和数据过期状态始终可见。

## 3. 参考模式与原创方向

产品参考以下模式，但不复制具体界面：

- Composer：降低从投资意图到策略理解的门槛。
- Danelfin：用可扫描的评分和分项信号帮助发现机会。
- TrendSpider：把研究、信号、图表和执行组织在同一工作台。
- 同类多智能体交易产品：公开 Agent 分工、辩论和最终决策链。

最终方向是“现代机构工作台”：信息密度高于消费型券商应用，但低于传统 Bloomberg 终端；使用清晰模块、任务型 Tab 和渐进披露避免拥挤。

## 4. 信息架构

桌面端使用左侧一级导航，移动端使用底部导航。一级结构固定为：

`总览 → 机会 → 研究 → 交易 → 系统`

全局应用外壳包含：

- 产品标识和一级导航；
- 全局 ticker 搜索；
- 当前 US/CN 市场上下文与时段；
- PAPER 状态；
- `EXECUTION_HALT` 状态和入口；
- 全局数据更新时间与严重风险提示。

### 4.1 总览：今天发生了什么

总览服务开盘前工作流，只保留决策所需摘要：

- US/CN 时段和 Pipeline 健康状态；
- 全局风险警报与简报质量警报；
- 今日机会 Top 5；
- 最新 Agent 决策和需要处理的动作；
- 宏观简报三条关键结论；
- 当前 PAPER 持仓和未完成订单摘要。

点击机会、风险、决策或订单时进入对应详情页面，不在总览堆叠完整表格和报告。

### 4.2 机会：应该研究什么

“机会”是完整 AI 选股雷达，总览只复用其 Top 5 摘要。

- 第一层 Tab：`US` / `CN`；
- 第二层 Tab：`高优先级` / `Core` / `Watch` / `Removed`；
- 支持按 AI 分数、催化剂、技术 Gate、更新时间和风险排序筛选；
- 表格主列为 ticker、AI 分数、催化剂、技术 Gate、风险、更新时间；
- `carried_forward`、数据过期和缺少评估必须显著标记；
- 支持紧凑与舒适两种显示密度；
- 点击一行进入该 ticker 的研究工作区。

“高优先级”由当前 Opportunity 层和有效 Gate 组合而来，不在前端重新实现选股业务规则。

### 4.3 研究：为什么形成这个判断

研究页以 ticker 和分析日期作为 URL 状态，包含：

- `投资摘要`：行情、最终判断、置信度、TradePlan、关键证据和风险；
- `多空辩论`：Bull/Bear 观点、分歧点和研究经理结论；
- `Agent 报告`：技术、情绪、新闻、基本面、交易员、风险和组合经理报告；
- `数据与来源`：宏观/个股简报版本、评估结论、引用、时间和 SHA-256。

页面必须展示分析所处阶段和可恢复的 `run_id`。行情图和最终 TradePlan 位于摘要 Tab，不要求交易者阅读完整报告后才能行动。

### 4.4 交易：接下来做什么

交易页包含四个 Tab：

- `交易计划`：AI 决策历史和 TradePlan；
- `订单记录`：提交、成交刷新、取消、跳过、失败和 dry-run 事件；
- `当前持仓`：数量、成本、市值、浮盈亏、分批次数和未完成订单；
- `结果复盘`：D1/D5/D20 收益、超额收益、计划回放和 Paper P&L。

所有记录以 `run_id` 串联，可从订单和结果返回产生它的 Agent 研究与输入版本。第一版不增加现有后端尚未支持的自由下单或实盘切换。

### 4.5 系统：系统是否可信地工作

系统页面向研究员和开发者，包含：

- `流水线`：US/CN slot、组件状态、耗时、错误和过期规则；
- `简报评估`：宏观和个股简报、pass/warn/fail、问题论断；
- `A/B 对比`：brief/feeds、配对过滤、preset 和统计注意事项；
- `配置`：LLM、数据源、推理强度、报告语言和 API key 配置状态。

详细日志和大型 JSON 默认折叠。危险操作仍受全局确认层保护。

## 5. 视觉系统

视觉采用冷静的现代机构终端，而非 Robinhood 式消费界面或霓虹 AI 风格。

- 背景：深石墨黑；卡片：略暖的深灰；边框低对比但清晰。
- 主文字：高对比中性白；辅助文字使用两级灰色。
- 青绿色：上涨、通过和主要正向操作。
- 蓝色：正在运行、选择态和中性系统进度。
- 琥珀色：风险、过期、warn 和需要关注。
- 红色：下跌、失败、减仓、高风险和 HALT。
- 中文正文使用系统无衬线字体；价格、百分比、时间和 ID 使用等宽数字。
- 不使用装饰性机器人图标表达 AI；用 Agent 共识、证据和阶段表达 AI。
- 同一页面最多四个内容 Tab，复杂数据按任务拆分而非全部平铺。

桌面端支持多栏工作区；平板收敛为双栏；手机端使用底部导航、横向指标条、卡片化表格行和全屏详情。所有触控目标至少 44px，颜色不是唯一状态信号。

## 6. 前端技术架构

### 6.1 技术选型

- React + TypeScript；
- Vite 负责开发和静态构建；
- React Router 管理五个一级页面及 ticker/run 深链接；
- Vitest + React Testing Library 负责单元与组件测试；
- FastAPI 继续负责 API、Agent、行情、文件契约和交易逻辑；
- 不使用 Next.js，因为应用不需要 SEO、SSR 或 Node.js 服务端运行时。

React 应用位于 `webui/frontend/`。本地开发时 Vite 将 `/api` 代理到 FastAPI；生产构建输出静态文件，初期可由 FastAPI 提供，AWS 阶段改由 S3/CloudFront 提供。

### 6.2 模块边界

- `AppShell`：一级导航、搜索、市场上下文、PAPER/HALT 和全局状态；
- `StatusStrip`：更新时间、流水线健康度和风险警报；
- `OpportunityRadar`：排名、分层、筛选、排序和显示密度；
- `TickerWorkspace`：ticker 路由、行情、摘要和研究 Tab；
- `AgentConsensus`：Agent 观点、置信度、分歧和决策链；
- `TradePlanCard`：方向、入场区间、止损、目标、风险和失效条件；
- `DataTable`：股票池、订单、Ledger 和评估结果的共用表格能力；
- `AsyncBoundary`：加载、空数据、过期、权限和失败状态；
- `ConfirmAction`：HALT、发起分析及未来交易操作的确认界面；
- `api`：类型化请求、错误归一化和响应解析；
- `domain`：与 Pydantic 契约对应的 TypeScript 类型，不包含 UI 逻辑。

## 7. 数据流与状态

- 组件不得直接散落拼接 API 地址，统一通过类型化 API 层访问。
- 总览并行读取状态、股票池、简报摘要、最新决策和持仓摘要；每张卡独立失败。
- 只有正在运行的分析与 Pipeline 状态自动轮询；其他数据进入页面或手动刷新时加载。
- 市场、Tab、ticker、日期和筛选条件写入 URL，可刷新、收藏和分享。
- 自选列表、显示密度和非敏感视图偏好可写入 `localStorage`。
- 交易计划、订单、持仓、报告和运行状态不得只保存在浏览器。
- 所有数据块显示生成时间或更新时间；过期数据使用显式状态，不能伪装成实时数据。
- 发起分析返回 `run_id` 后进入可恢复任务页。本地阶段仍受现有进程内运行状态边界限制，AWS 阶段迁移到持久 RunStore。

若总览组合现有接口造成明显的重复读取，再增加一个轻量只读聚合 API。聚合 API 只编排既有领域函数，不复制选股、评估或 Ledger 规则。

## 8. 数据存储

### 8.1 当前本地模式

运行数据默认位于 `~/.tradingagents/`，不进入仓库：

| 数据 | 位置 | 格式 |
|---|---|---|
| 宏观简报 | `macro_briefs/` | Markdown + YAML frontmatter |
| 个股简报 | `ticker_briefs/<TICKER>/` | Markdown + YAML frontmatter |
| 简报评估 | 简报同目录 | JSON，绑定原文 SHA-256 |
| 股票池 | `pools/<session>/` | 每日 JSON 快照 |
| Core 列表 | `pools/core.<session>.yaml` | YAML |
| AI 决策 | `ledger/decisions.jsonl` | 追加式 JSONL |
| 订单事件 | `ledger/orders.jsonl` | 追加式 JSONL |
| 交易结果 | `ledger/outcomes.jsonl` | 追加式 JSONL |
| 当前持仓 | `ledger/positions.json` | 原子覆盖 JSON 快照 |
| Pipeline 状态 | `pipeline_status.<session>.json` | JSON 快照 |
| Agent 报告 | `logs/reports/` | 分层 Markdown |
| 长期记忆 | `memory/trading_memory.md` | Markdown |

交易审计链为 `decisions → orders → outcomes`，通过 `run_id` 连接。券商仍是实际成交事实源；本地 Ledger 保存经过系统处理的审计记录。

### 8.2 存储抽象

本轮前端开发不立即引入云数据库，但后端演进必须保持以下边界：

- `ArtifactStore`：简报、报告、评估、股票池和归档产物；本地文件实现可替换为 S3。
- `LedgerStore`：决策、订单、结果和持仓；本地 JSONL/JSON 实现可替换为 PostgreSQL。
- `RunStore`：分析任务和进度；本地进程内实现可替换为 PostgreSQL。

前端只依赖 API，不感知具体存储实现。

### 8.3 AWS 生产模式

- React 静态产物：S3 + CloudFront；
- FastAPI：ECS Fargate Service；
- Agent 和定时 Pipeline：独立 ECS Tasks；
- 调度：EventBridge Scheduler；
- 长任务解耦：SQS；
- 简报、报告、评估、股票池快照和归档：S3；
- `trade_decisions`、`order_events`、`trade_outcomes`、`positions`、`analysis_runs`、用户和自选：RDS PostgreSQL；
- API key 和券商密钥：Secrets Manager；
- 日志与告警：CloudWatch。

S3 对象在 PostgreSQL 中仅记录对象 key、SHA-256、生成时间和领域身份。PostgreSQL 中的订单事件保存券商订单 ID，以便与外部事实源对账。

## 9. 安全与错误处理

- 本地模式继续只绑定 `127.0.0.1`。
- API key 只返回是否已配置，绝不回显值。
- PAPER、市场时段和 HALT 固定显示，不能被页面 Tab 隐藏。
- 发起分析、启用/解除 HALT 和未来订单操作使用明确的确认层。
- 当前只显示 PAPER 文案，不提供 LIVE 切换。
- 一个接口失败只影响对应模块；页面保留其他已成功数据。
- 写操作失败时保留输入、显示可操作原因，并允许安全重试。
- 空文件、首次运行、过期、评估缺失、权限失败和网络失败是不同状态。
- 报告仍按 escape-first 方式安全渲染 Markdown，不允许不可信内联 HTML。
- AWS 阶段增加身份认证、最小权限 IAM、HTTPS、数据库私网访问和审计日志；这些不属于本地前端首版实现。

## 10. 迁移策略

1. 在 `webui/frontend/` 建立 React + TypeScript + Vite 工程和应用外壳。
2. 建立类型化 API 与领域模型，保持既有 FastAPI 路径和 Pydantic 契约不变。
3. 先实现总览和机会，复用现有状态、股票池、简报和 Ledger API。
4. 再迁移研究、交易和系统页面。
5. 新旧界面短期并存用于功能对照；React 覆盖验收后移除旧 `index.html`、`pipeline.html`、`app.js` 和 `pipeline.js`。
6. FastAPI 默认路由切换到 React 构建产物，本地仍保留一条服务启动命令。
7. AWS 存储和基础设施只预留接口，不在本轮前端重构中实现。

不得长期维护两套生产 UI；旧页面只作为迁移期回退。

## 11. 验证策略

### 11.1 前端测试

- AppShell 导航、深链接和移动导航；
- US/CN、分层 Tab、排序、筛选和密度切换；
- loading、empty、stale、warn、failed 和 missing 状态；
- OpportunityRadar 到 TickerWorkspace 的路径；
- 发起分析、轮询、刷新恢复和失败重试；
- HALT 显示、确认和 round-trip；
- TradePlan、订单事件、持仓和结果的 `run_id` 串联；
- 键盘导航、焦点、ARIA 标签和不依赖颜色的状态文本。

### 11.2 后端与契约测试

- 保留现有 `tests/test_webui.py`、`tests/test_webui_pipeline.py` 和聚合测试；
- 增加前端使用的 API 响应契约检查；
- 确认缺失文件仍返回可渲染空状态而非 500；
- 确认简报 hash binding、staleness、A/B 定义和订单 join 规则没有移入前端或发生变化。

### 11.3 验收流程

- `npm` 类型检查、前端测试和生产构建通过；
- Python WebUI/Pipeline 相关测试通过；
- 桌面、平板和手机关键页面完成浏览器交互验收；
- 总览、机会、研究、交易和系统均能在无运行数据的全新环境中正常显示；
- 旧 UI 移除前完成新旧功能清单对照。

## 12. 完成标准

1. 用户可以从总览在一分钟内发现今日 Top 5 机会、关键风险和待处理动作。
2. 完整 AI 雷达位于机会页，并支持市场、分层、排序和筛选。
3. 任一机会可以进入包含行情、共识、TradePlan、报告和来源的研究工作区。
4. 交易页完整展示决策、订单、持仓和结果历史，并可追溯到 `run_id`。
5. 系统页保留 Pipeline、评估、A/B 和配置等开发诊断能力。
6. PAPER、HALT、过期和失败状态在正确上下文中始终明确。
7. React 前端通过类型、组件、构建和浏览器关键流程验证。
8. 现有后端契约和离线测试保持通过。
9. 旧生产 UI 被新 UI 替代，不再维护两套实现。
10. 前端不依赖本地文件路径，可在未来直接部署到 S3/CloudFront 并连接 FastAPI API。

## 13. 非目标

- 实盘交易和 LIVE/PAPER 切换；
- 新券商接入；
- AWS 基础设施落地；
- 在本轮迁移中把所有本地文件立即转换为数据库；
- 自定义拖拽工作区；
- 自由文本 AI 聊天或自然语言策略生成；
- 重写 Pipeline v2、选股规则、简报评估或 A/B 统计定义。
