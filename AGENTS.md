# Agent Guide — TradingAgents (research fork)

This is a local research fork of [TauricResearch/TradingAgents](https://github.com/TauricResearch/TradingAgents)
used to compare LLM backends (DeepSeek V4 / Claude Sonnet 5 / GPT-5.6) on one
multi-agent trading pipeline, and to build a **macro-brief subsystem** around it.
Multiple coding agents (Claude Code, Codex) work in this repo. **Specs are the
source of truth for cross-agent work: see [`specs/README.md`](specs/README.md).**

## Layout

| Path | What it is |
|---|---|
| `tradingagents/` | Upstream package (agents, dataflows, graph, llm_clients) — our local additions: `search_news` tool, macro-brief pipeline hooks |
| `cli/` | Upstream interactive CLI |
| `compare/` | Our comparison harness: per-backend env presets + `run.py` |
| `specs/` | Cross-agent specs; each spec names its builder and status |
| `pipeline/` | Offline pipeline v2: contract models/validators (`pipeline/contracts/`), shared utils, collectors/evaluator/orchestrator — specs are the source of truth |
| `prompts/` | Prompt templates used by the offline collectors (deep-search briefs) |
| `tests/` | Pytest suite (`unit` / `integration` / `smoke` markers) |

## Build & test

```bash
.venv/bin/python -m pytest tests/ -q          # full suite (offline, ~90s)
.venv/bin/python -m ruff check tradingagents tests compare
```

Environment lives in `.venv` (Python 3.14, `pip install -e .`). API keys are in
`.env` (never commit). Runtime data (briefs, reports, memory) lives under
`~/.tradingagents/` — never inside the repo.

## Conventions that matter here

- **Data access goes through the vendor registry** (`tradingagents/dataflows/interface.py`:
  `TOOLS_CATEGORIES` + `VENDOR_METHODS` + `route_to_vendor`). New data sources are
  new vendors; do not call fetchers directly from agent code.
- **Config via `DEFAULT_CONFIG`** (`tradingagents/default_config.py`), overridable
  by `TRADINGAGENTS_*` env vars declared in `_ENV_OVERRIDES` (str/int/float/bool only).
- Tools exposed to agents are `@tool` wrappers in `tradingagents/agents/utils/*_tools.py`,
  re-exported via `agent_utils.py`, and must ALSO be registered in the matching
  `ToolNode` in `tradingagents/graph/trading_graph.py` or calls fail at runtime.
- Tests are offline: mock network (see `tests/test_search_news.py` for the
  `yf.Search` pattern). Run `ruff` before finishing.
- Upstream behavior is not changed unless a spec says so.

## Division of labor (current)

- **Pipeline side** (macro-brief consumption in TradingAgents): built by Claude Code — see `specs/macro-brief-pipeline.md`.
- **Collector** (daily deep-search job producing briefs): to be built by Codex — see `specs/macro-brief-collector.md`.
- **Evaluator** (Claude-backed accuracy scoring by default; GPT-5.6 Terra selectable): to be built by Claude Code — see `specs/macro-brief-evaluator.md`.
- The shared interface between all three is `specs/macro-brief-data-contract.md`. **Change the contract first, then the components.**
