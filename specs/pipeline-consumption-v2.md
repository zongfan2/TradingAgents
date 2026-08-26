# Spec: Pipeline Consumption v2

**Builder**: Claude Code · **Status**: Open · Contracts:
[macro-brief-data-contract.md](macro-brief-data-contract.md) (v2),
[ticker-brief-data-contract.md](ticker-brief-data-contract.md)

Extends the implemented v1 consumption side
([macro-brief-pipeline.md](macro-brief-pipeline.md)) for session-scoped macro
briefs, ticker briefs, and the fully offline `brief` news arm.

## Changes

### 1. Session-aware macro reader

`get_macro_brief_local(curr_date, session)` implements the contract's
session-selection rule (candidates ranked by date first, then
session-match > other-session > legacy, fallbacks noted in the served header;
3-day staleness warning unchanged). The session derives from the ticker's
symbol suffix (`.SS`/`.SZ`/`.HK` ⇒ `cn`, else `us`) via a shared helper in
`symbol_utils`. The `@tool get_macro_brief(curr_date, ticker)` signature gains
an optional `ticker` argument — the analyst passes the instrument under
analysis so the vendor can derive the session; omitted ⇒ `us`.

### 2. Ticker brief tool

- New vendor category `ticker_brief` (vendor `local`, in `OPTIONAL_CATEGORIES`),
  method `get_ticker_brief` reading per the ticker contract's staleness rules
  (1-day gap warns, ≥ 2 days ⇒ DATA_UNAVAILABLE degrade).
- `@tool get_ticker_brief(ticker, curr_date)` in `macro_data_tools.py`
  (renamed concern: file stays, tool re-exported via `agent_utils`), registered
  in the news `ToolNode` (superset registration, as v1).

### 3. Arm switches

- Existing `macro_source` (`feeds`|`brief`) unchanged.
- New `ticker_source` (`feeds`|`brief`), config + `TRADINGAGENTS_TICKER_SOURCE`
  env override, same validation pattern (`_ENV_CHOICES`), default `feeds`
  (exact upstream behavior preserved).
- The A/B protocol always moves both switches together as a bundle
  (runner spec); independent switches exist for debugging.

### 4. Offline brief arm (news side)

`_select_macro_tools` generalizes to `_select_news_tools(macro_source,
ticker_source)`:

Each tool is governed by exactly one switch, so every combination is fully
determined:

| Tool | Governing switch | Present when |
|---|---|---|
| `get_news` (ticker news API) | `ticker_source` | = `feeds` |
| `get_ticker_brief` | `ticker_source` | = `brief` |
| `get_global_news` + `get_macro_indicators` | `macro_source` | = `feeds` |
| `get_macro_brief` | `macro_source` | = `brief` |
| `search_news` (autonomous queries) | both | either switch = `feeds` (removed only in the full brief bundle) |
| `get_prediction_markets` (Polymarket, keyless) | none | always, behind config `polymarket_enabled` (default true) |

The
brief-bundle system prompt directs: ground macro commentary in the macro
brief, company commentary in the ticker brief, cite both as-of dates, and
state explicitly when a brief is absent (DATA_UNAVAILABLE) instead of
improvising. Zero news-API calls occur in the full brief bundle at analysis
time; remaining external calls are yfinance market/fundamental data and
optional Polymarket.

### 5. Eval verdict surfacing

A small reader util exposes a brief's eval verdict (`pass|warn|fail|missing`)
to the runner for gating and to the report header (rendered by the existing
report saving path). The pipeline itself never blocks on eval — gating policy
lives in the runner.

## Config keys added

| Key | Default | Env override |
|---|---|---|
| `ticker_source` | `feeds` | `TRADINGAGENTS_TICKER_SOURCE` |
| `ticker_brief_dir` | `~/.tradingagents/ticker_briefs` | `TRADINGAGENTS_TICKER_BRIEF_DIR` |
| `polymarket_enabled` | `true` | `TRADINGAGENTS_POLYMARKET_ENABLED` |
| `data_vendors["ticker_brief"]` | `local` | — |

## Acceptance criteria

1. Tests mirror `test_macro_brief.py` for the ticker reader: newest-≤-date,
   1-day warn, 2-day absent, future-brief exclusion, routing registration,
   optional-category degrade.
2. Session selection: `.HK`/`.SS`/`.SZ` map to `cn`, others `us`; fallback
   header notes when serving cross-session/legacy briefs.
3. Tool exposure matrix above verified per switch combination;
   `feeds`/`feeds` reproduces the exact pre-change tool list.
4. Full suite + ruff green.
