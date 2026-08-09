# Specs Index

Cross-agent specifications for the trading pipeline. Any agent (Claude Code,
Codex) picking up a task reads the **data contract first**, then its component
spec. Contracts are the only shared interfaces — components never depend on
each other's internals. The overall v2 design and decision log live in
[2026-08-03-pipeline-v2-design.md](2026-08-03-pipeline-v2-design.md).

## Contracts

| Contract | Interface | Status |
|---|---|---|
| [macro-brief-data-contract.md](macro-brief-data-contract.md) | Daily macro brief files + eval json | **v2** |
| [pool-data-contract.md](pool-data-contract.md) | Tiered stock pool json + hysteresis state | **v1** |
| [ticker-brief-data-contract.md](ticker-brief-data-contract.md) | Per-ticker brief files + eval json | **v1** |
| [trade-plan-ledger-contract.md](trade-plan-ledger-contract.md) | TradePlan schema + decisions/orders/outcomes jsonl | **v1** |

## Components

| Spec | Component | Builder | Status |
|---|---|---|---|
| [macro-brief-pipeline.md](macro-brief-pipeline.md) | Macro consumption (`get_macro_brief`, `macro_source` switch) | Claude Code | **Implemented (v1)** — v2 deltas in pipeline-consumption-v2 |
| [pipeline-consumption-v2.md](pipeline-consumption-v2.md) | Session-aware readers, `get_ticker_brief`, `ticker_source`, offline brief arm | Claude Code | Open |
| [macro-brief-collector.md](macro-brief-collector.md) | Scheduled deep-search macro briefs (dual session) | Claude Code (Codex handoff later) | Open |
| [macro-brief-evaluator.md](macro-brief-evaluator.md) | GPT-5.6 Terra accuracy scoring of briefs (macro + ticker) | Claude Code (Codex handoff later) | Open |
| [pool-builder.md](pool-builder.md) | Deep-search nomination + Bollinger gate + hysteresis | Claude Code | Open |
| [ticker-brief-collector.md](ticker-brief-collector.md) | Pool-driven per-ticker deep-search briefs | Claude Code | Open |
| [analysis-runner.md](analysis-runner.md) | Trigger rules, A/B pairing, TradePlan + validator, ledger writes | Claude Code | Open |
| [execution-adapter.md](execution-adapter.md) | Alpaca **paper** bracket orders, fail-closed guards | Claude Code | Open |
| [orchestrator.md](orchestrator.md) | Per-session scheduling, ordering, settle job, status file, notifications | Claude Code | Open |
| [webui-pages.md](webui-pages.md) | Status banner, pool/briefs views, A/B aggregates, halt toggle | Claude Code | Open |

## System overview

```
[08:30 Asia/Shanghai — cn]                [08:30 America/New_York — us]
   macro collector ─▶ macro_briefs/YYYY-MM-DD.<session>.md ─▶ evaluator ─▶ eval.json
   pool builder    ─▶ pools/<session>/YYYY-MM-DD.json
   ticker collectors ─▶ ticker_briefs/<TICKER>/YYYY-MM-DD.md (─▶ evaluator)
   analysis runner ─▶ ledger/decisions.jsonl  (feeds vs brief A/B pairs)
   execution adapter (us, paper, opt-in) ─▶ ledger/orders.jsonl
   settle job ─▶ ledger/outcomes.jsonl        webui ─▶ status + aggregates
```

## Rules

1. **Contract first**: a change to file location, naming, frontmatter, or schema
   lands in the contract spec before any component changes.
2. Components communicate **only through files named in the contracts** — the
   collectors never read pipeline state; the pipeline never writes to brief or
   pool directories; the evaluator writes only `*.eval.json`; only the
   execution adapter talks to the broker.
3. Each spec lists acceptance criteria; a component is done when they all pass.
4. Secrets (Alpaca keys, LLM keys) live in `.env` only — never in specs, code,
   pool files, the ledger, logs, or status files.
5. Every contract schema ships as a machine-readable model (Pydantic) with a
   strict validator: writers reject unknown fields, readers warn on them;
   contract compatibility tests live next to the models. Prose in a contract
   spec and its model must never disagree — the model follows the spec.
