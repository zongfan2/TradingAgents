# Design: Pipeline v2 — Scheduled Deep-Search Info Sources + Paper Execution

**Date**: 2026-08-03 · **Status**: Approved design, implementation open
**Supersedes**: extends the macro-brief subsystem (v1 specs) to a dual-market,
pool-driven, paper-executing daily pipeline.

## Goal

Replace per-run news/API fetching with **scheduled deep-search briefs** (macro
and per-ticker), gate and rank a **tiered stock pool** with technical
indicators, and close the loop with **structured trade plans executed on an
Alpaca paper account** — all running locally on subscription-backed CLIs with
near-zero marginal API cost.

## System overview

```
[08:30 Asia/Shanghai — cn slot]                [08:30 America/New_York — us slot]
        │                                                  │
        ▼                                                  ▼
┌─ orchestrator (per slot, static launchd entry) ─────────────────────────────┐
│ 0. settle job        — backfill outcomes, reconcile paper fills             │
│ 1. macro collector   — macro_briefs/YYYY-MM-DD.<session>.md                 │
│ 2. macro evaluator   — YYYY-MM-DD.<session>.eval.json                       │
│ 3. pool builder      — deepsearch nominate → BOLL gate → pools/<session>/   │
│ 4. ticker collectors — fan-out over pool, concurrency-capped                │
│ 5. ticker evaluators — only for analysis-triggering briefs                  │
│ 6. analysis runner   — core always + catalyst-triggered; A/B pairing;       │
│                        TradePlan + deterministic validator                  │
│ 7. execution adapter — us slot only, Alpaca PAPER, dry-run by default       │
│ 8. status + notifications throughout                                        │
└─────────────────────────────────────────────────────────────────────────────┘
```

## Decision log

| # | Decision | Rationale |
|---|---|---|
| D1 | All v2 components built by Claude Code first; Codex handoff later | User decision 2026-08-03; specs updated accordingly |
| D2 | Two market sessions: `cn` (A-shares+HK, 08:30 Asia/Shanghai) and `us` (08:30 America/New_York); IANA tz names only. Fire times are always converted from the session tz to the host tz **at install time** (08:30 Shanghai lands the previous local evening on a US host), slot dates/weekdays resolve in the session tz, and the installer must be re-run after any host timezone change (the host has already moved Chicago→Eastern once mid-project) | "CST"/"GMT" abbreviations are ambiguous; both markets wanted; assuming host tz would run cn 13–14h late (Codex review pt 2) |
| D3 | Two macro briefs per day, session-scoped files (contract v2) | Overwriting one file destroys the audit trail: ledger rows must reference immutable inputs for a clean A/B; enables session information-delta analysis |
| D4 | Static schedule entries + data-driven fan-out; NO dynamic creation/deletion of schedule tasks | Scheduler-state mutation is fragile (orphans, lost DAG ordering, no central concurrency cap). Pool membership in a JSON file gives the same lifecycle: dropped ticker ⇒ not collected next slot |
| D5 | Tiered pool: core (user-maintained file) + opportunity (deep-search) + watch; enter fast, exit slow (hysteresis) | Holdings must always be covered; opportunity churn must not thrash |
| D6 | Deterministic technical gate on opportunity candidates: daily + weekly Bollinger rules, configurable | Deep search finds narrative; price structure must confirm. Rules are strategy — parameterized, user-tunable |
| D7 | `brief` arm is fully offline on the news side: no get_news / get_global_news / search_news at analysis time. Polymarket kept behind a toggle; yfinance price/fundamental data unchanged | User requirement. Prices cannot come from deep search; Polymarket is keyless+free and orthogonal |
| D8 | Analysis triggers: core = every slot; opportunity = catalyst_score ≥ threshold (default 7.0) | Quota control without missing holdings risk |
| D9 | Trade plans: LLM proposes structured levels anchored to technical report; deterministic validator checks coherence against real prices | LLM-only numbers are sloppy; rules-only are rigid |
| D10 | Execution: Alpaca **paper** only, account prefix `PA` verified at startup, endpoint pinned, `execution_enabled=false` default (dry-run). US session only. No short opening. brief arm only | Paper account PA37D0102E73 verified 2026-08-03. Live trading is a structural non-goal |
| D11 | A/B = arm bundles (`feeds` bundle vs `brief` bundle), paired same-ticker/same-date runs: 3 rotating core pairs **per session slot** + all triggered tickers. Only brief bundle executes; feeds is shadow | One account cannot hold both arms' positions; pairing is the clean contrast |
| D12 | Eval gating (normative table in analysis-runner.md): macro `fail` → run forced to feeds + flagged (thus no auto-execution); ticker `fail` → auto-trigger suppressed, core runs proceed with the brief withheld, no auto-execution; `missing` → proceed flagged, execution blocked unless `execute_on_missing_eval`. A verdict counts only when the eval's `brief_sha256` matches the served brief revision | A fabricated brief must not silently feed decisions — and a stale `pass` must not transfer to a re-collected brief |
| D13 | Outcome ledger (jsonl) + settle backfill (1/5/20d vs benchmark) + plan replay + paper-fill reconciliation | Without an outcome ledger the A/B is vibes |
| D14 | Per-session status files + webui banner + macOS notifications on failure (webui-pages.md owns the rendering) | Collector failures must be loud; stale briefs must be visible |
| D15 | Legacy v1 macro briefs (`YYYY-MM-DD.md`) stay readable at lower priority | Cheap migration |
| D16 | Inputs are immutable: same-day rewrites archive the prior revision (`archive/<name>.<generated_at>`), and every decision row records path + generated_at + sha256 of the exact briefs/pool it consumed plus a config digest | A/B audit must be able to reproduce what a run saw; overwrite-in-place destroys that (Codex review 2026-08-03, pt 1) |
| D17 | Market-state guards before any live submit: broker calendar/clock, halt status, quote freshness (`max_quote_age`) — closed/halted/stale ⇒ skip, never submit | Old prices must not arm GTC orders on holidays/halts (Codex review 2026-08-03, pt 5) |
| D18 | Constrained add-ons: position snapshot (`positions.json`) feeds the trader; a BUY on a held ticker is a maintain unless `add_intent` + rationale; adds are pyramid-up only, ≤ 2 tranches, ≥ 3 trading days apart, per-ticker notional ≤ 20% | Daily re-analysis re-emits BUY for held names — without explicit add semantics, signal persistence compounds into an uncapped position; blanket skip forbids legitimate pyramiding (user decision 2026-08-05) |
| D19 | CLI-role swap: **codex performs all deep-search collection** (macro, pool nomination, ticker briefs — config `collect_backend`, default `codex`) and **claude performs evaluation** (config `eval_backend`, default `claude`, identity `claude-eval`); explicit `--backend` still overrides per invocation, and collector/evaluator backend independence is preserved (backends must differ; a match warns loudly) — just mirrored | Claude subscription's monthly cap was hit once and collection is the heavier consumer (user decision 2026-08-10) |

## Contracts (files are the only interfaces)

| Contract | File |
|---|---|
| Macro brief v2 | [macro-brief-data-contract.md](macro-brief-data-contract.md) |
| Pool v1 | [pool-data-contract.md](pool-data-contract.md) |
| Ticker brief v1 | [ticker-brief-data-contract.md](ticker-brief-data-contract.md) |
| Trade plan + ledger v1 | [trade-plan-ledger-contract.md](trade-plan-ledger-contract.md) |

## Cost model

Per slot: 1 macro deepsearch + 1 pool deepsearch + ≤15 ticker deepsearches
(concurrency 3) on the Codex subscription; evaluator runs on the Claude
subscription (D19 — collection is the heavier consumer, so it burns the Codex
quota); analysis runs on the preset backend (claude-sub preset =
subscription quota). Analysis-time external calls collapse to yfinance OHLCV /
fundamentals (free) + optional Polymarket (free). The only per-token billing
left is the optional `luna`/`deepseek` presets.

Capacity honesty: cn slot end-to-end ≈ 45–75 min with default caps; starting
08:30 means some analysis completes after the 09:30 open. Slot start time is a
config knob; user chose 08:30 default, accepting the tail.

## A/B protocol

- One axis at a time: info-source A/B fixes the LLM preset; backend comparison
  fixes the info source.
- Pairing: same ticker, same date, both bundles, all else equal. Default 3
  rotating core pairs per session slot + every triggered opportunity ticker.
- Ledger rows carry `arm` + `pair_id`; the webui aggregates: direction
  agreement, 1/5/20d hit-rate and excess return, plan-quality (entry-hit rate,
  realized R:R), eval-weighted views (drop rows whose brief eval = fail).
- Honest labeling (ledger contract "Statistical caveats"): triggered pairs are
  a **conditional comparison** (selection decided by the brief arm's catalyst
  score); same-ticker daily rows are not independent (cluster by ticker,
  stratify by date); pair counts deduplicate `(date, session, ticker)`.
- Read no conclusions before ≥100 pairs (~2–4 weeks at the default cadence
  across both slots). Sequential monitoring,
  not a one-shot significance test. First question answered: "is the brief
  bundle no worse, at near-zero info cost"; alpha attribution comes later.
- Early stop: brief eval fail-rate > 20% or anomalous direction disagreement →
  fix collection quality before resuming the count.

## Non-goals

- Live trading (execution adapter structurally refuses non-paper accounts).
- Short opening, options, intraday re-analysis, multi-machine deployment.
- Holiday **scheduling** calendars (weekday schedule; a holiday run produces
  briefs/analyses harmlessly) — but holiday/halt **submission** is prevented
  by the adapter's market-state guards (D17), not left to chance.
- CN/HK auto-execution (report + notification only; a future futu OpenD
  adapter may lift this — out of scope here).

## Config appendix (keys added by v2)

Scalars marked with an env var get rows in `_ENV_OVERRIDES` (and
`_ENV_CHOICES` where enum-valued) per the repo's config convention;
structured keys are file-config only.

| Key | Default | Type | Env override |
|---|---|---|---|
| `ticker_source` | `feeds` | enum feeds/brief | `TRADINGAGENTS_TICKER_SOURCE` |
| `ticker_brief_dir` | `~/.tradingagents/ticker_briefs` | str | `TRADINGAGENTS_TICKER_BRIEF_DIR` |
| `pool_dir` | `~/.tradingagents/pools` | str | `TRADINGAGENTS_POOL_DIR` |
| `ledger_dir` | `~/.tradingagents/ledger` | str | `TRADINGAGENTS_LEDGER_DIR` |
| `polymarket_enabled` | `true` | bool | `TRADINGAGENTS_POLYMARKET_ENABLED` |
| `sessions.{cn,us}.slot_time` | `"08:30"` | str (market-local) | — (install script consumes) |
| `pool_entry_threshold` / `pool_exit_threshold` | 6.0 / 4.0 | float | — |
| `analysis_trigger_threshold` | 7.0 | float | `TRADINGAGENTS_TRIGGER_THRESHOLD` |
| `pool_max_staleness_days` | 3 | int | — |
| `pool_gate_rules` | see pool-builder.md | structured | — |
| `ticker_collect_concurrency` | 3 | int | — |
| `ab_pairing` / `ab_core_pairs_per_slot` | `paired` / 3 | enum / int | — |
| `max_runs_per_slot` | 30 | int | — |
| `execution_enabled` | `false` | bool | `TRADINGAGENTS_EXECUTION_ENABLED` |
| `execute_on_missing_eval` | `false` | bool | `TRADINGAGENTS_EXECUTE_ON_MISSING_EVAL` |
| `per_order_notional_cap` / `max_gross_exposure` / `max_total_risk` | 0.15 / 1.0 / 0.05 | float (share of equity) | — |
| `max_tranches_per_ticker` / `add_spacing_days` / `per_ticker_notional_cap` | 2 / 3 / 0.20 | int / int / float | — |
| `pyramid_up_only` | `true` | bool | — |
| `max_open_positions` / `max_orders_per_slot` | 10 / 10 | int | — |
| `max_quote_age_minutes` | 15 | int | — |
| `notifications_enabled` | `true` | bool | — |
| `component_timeouts` | see orchestrator.md R1 | structured | — |
| `collect_backend` | `codex` | enum claude/codex (D19) | `TRADINGAGENTS_COLLECT_BACKEND` |
| `eval_backend` | `claude` | enum claude/codex (D19) | `TRADINGAGENTS_EVAL_BACKEND` |

## Implementation order

1. **P0** Contracts + spec updates (this change set)
2. **P0** Macro collector (dual-session) + orchestrator skeleton + alerting
3. **P0** Macro evaluator + eval gating
4. **P1** Pool builder (deepsearch + BOLL gate + hysteresis)
5. **P1** Ticker-brief collector (fan-out)
6. **P1** Pipeline consumption v2 (session-aware reader, get_ticker_brief, ticker_source, offline brief arm)
7. **P1** Analysis runner (triggers, pairing, TradePlan + validator)
8. **P1** Execution adapter (Alpaca paper, dry-run default)
9. **P1** Ledger + settle + webui status/aggregate pages
10. **P2** A/B runbook + threshold tuning
