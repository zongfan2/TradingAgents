# Spec: Macro Brief — Pipeline Side

**Builder**: Claude Code · **Status**: Implemented · Contract: [macro-brief-data-contract.md](macro-brief-data-contract.md)

## Goal

Let the TradingAgents news analyst consume the daily deep-search macro brief as
an alternative to the fixed macro feeds, switchable per run, so the two macro
information sources can be A/B tested with everything else held constant.

## Design

### A/B arms (config `macro_source`, env `TRADINGAGENTS_MACRO_SOURCE`, default `feeds`)

Arm values are validated: the env overlay normalizes (strip/lower) and rejects
anything outside `{feeds, brief}` at startup; `_select_macro_tools` re-validates
at node level so a programmatic misconfig raises instead of silently selecting
the wrong arm.

| Tool available to news analyst | `feeds` arm | `brief` arm |
|---|---|---|
| `get_news` (ticker news) | ✓ | ✓ |
| `search_news` (autonomous queries) | ✓ | ✓ |
| `get_prediction_markets` (Polymarket) | ✓ | ✓ |
| `get_global_news` (5 fixed macro queries) | ✓ | — |
| `get_macro_indicators` (FRED) | ✓ | — |
| `get_macro_brief` (cached deep-search brief) | — | ✓ |

Only the macro information source varies between arms; ticker news, autonomous
search, and prediction markets are common to both.

### Components

- `tradingagents/dataflows/macro_brief.py` — `get_macro_brief_local(curr_date)`:
  picks the newest `YYYY-MM-DD.md` in `macro_brief_dir` with date ≤ `curr_date`,
  prepends an as-of header, adds a WARNING header when the brief is more than
  3 calendar days older than the analysis date, raises `VendorNotConfiguredError`
  when no brief qualifies.
- Vendor registry: new category `macro_brief` (vendor `local`), method
  `get_macro_brief`. The category is in `OPTIONAL_CATEGORIES`: a missing brief
  degrades to the standard `DATA_UNAVAILABLE` sentinel instead of aborting a run.
- `@tool get_macro_brief(curr_date)` in `agents/utils/macro_data_tools.py`,
  re-exported via `agent_utils`, registered in the news `ToolNode`
  (superset registration: both arms' tools are executable; the arm only controls
  what is bound to the LLM).
- `news_analyst.py` — `_select_macro_tools(macro_source)` returns the arm's
  macro tools plus the matching prompt fragment; the system prompt tells the
  brief arm to ground macro commentary in the brief and cite its as-of date.
- `compare/run.py` — `--macro-source {feeds,brief}` sets the env var before
  config import; the `brief` arm pre-flights that a qualifying brief exists and
  aborts before any LLM spend if not.

## Config keys added

| Key | Default | Env override |
|---|---|---|
| `macro_source` | `feeds` | `TRADINGAGENTS_MACRO_SOURCE` |
| `macro_brief_dir` | `~/.tradingagents/macro_briefs` | `TRADINGAGENTS_MACRO_BRIEF_DIR` |
| `data_vendors["macro_brief"]` | `local` | — |

## Acceptance criteria

1. `tests/test_macro_brief.py` covers: newest-≤-date selection, future-brief
   exclusion, missing-brief error, stale warning, routing registration,
   optional-category degrade, tool exposure, arm selection. ✅
2. Full suite + ruff green. ✅
3. `--macro-source feeds` produces the exact pre-change behavior. ✅
