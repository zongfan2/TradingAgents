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
| `RUNBOOK.md` | Operational runbook for pipeline v2: schedule install, manual slots, dry-run ramp, A/B campaign, troubleshooting |
| `pipeline/` | Offline pipeline v2: contract models/validators (`pipeline/contracts/`), shared utils, collectors/evaluator/orchestrator — specs are the source of truth |
| `prompts/` | Prompt templates used by the offline collectors (deep-search briefs) |
| `tests/` | Pytest suite (`unit` / `integration` / `smoke` markers) |

## Build & test

```bash
.venv/bin/python -m devtools.verification.offline --only all  # required deterministic gate

# Troubleshooting individual deterministic checks
.venv/bin/python -m pytest tests/ -q -m "not integration"
.venv/bin/python -m ruff check .
git diff --check
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

## Codex-Claude hybrid workflow

- **Codex — primary builder** owns production edits, tests, specs, contract-first
  changes, deterministic verification, and final integration.
- **Claude Code — independent verifier** reviews coherent risky changes from an
  isolated worktree and returns reports or patches for Codex to assess; it does
  not edit the primary worktree.

Run the deterministic gate for every completed change and before handoff:

```bash
.venv/bin/python -m devtools.verification.offline --only all
```

Claude review is an additional required gate whenever a change involves any of:

- contracts, interfaces, schemas, `pipeline/contracts/`, or shared data-contract specs;
- trading or portfolio safety, including orders, positions, quote freshness, or market state;
- data integrity, storage, replay, migration, retention, data loss, duplicate writes, overwrites, revision binding, or audit chains;
- concurrency, idempotency, scheduling, locks, retries, timeouts, session dates, timezone, or DST behavior;
- a nondeterministic, recurring, or flaky bug;
- silent-bad-data risk: an apparent success while bad data proceeds downstream;
- a new component or end-to-end feature; or
- the final material merge/PR checkpoint.

A complex bug requires Claude review when any mandatory trigger applies or when
**two or more** scored factors apply: its root cause crosses two or more
components or processes; it changes three or more logic files; unit tests alone
cannot establish the fix; fallback, retry, gating, or exit-code semantics
change; it depends on an external CLI, network, model, or third-party service;
the root cause remains unclear after roughly 30 minutes; multiple plausible
fixes have architectural trade-offs; or it can affect normal paths that did not
show the original symptom.

After the deterministic gate passes, run and then check the SHA-bound review
report (reports live under `~/.tradingagents/verification/`):

```bash
.venv/bin/python -m devtools.verification.claude_review run --base HEAD~1 --head HEAD --acceptance "describe the accepted behavior" --risk "state the mandatory trigger or score" --original-symptom "describe the bug, or none"
.venv/bin/python -m devtools.verification.claude_review check --head HEAD
```

Only the user may authorize a waiver, explicitly for the exact bound revision.
Use `claude_review waive` only with that authorization; its report remains
visibly `waived`, never `pass`, and becomes stale if the revision changes.

This development-review policy is separate from runtime D19: Codex performs
collection and Claude performs evaluation. Do not rewrite historical `Builder`
fields in component specs; those record existing delivery ownership, while this
guide governs future work.
