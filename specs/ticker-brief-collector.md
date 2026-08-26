# Spec: Ticker Brief — Collector

**Builder**: Claude Code · **Status**: Open · Contracts:
[ticker-brief-data-contract.md](ticker-brief-data-contract.md),
[pool-data-contract.md](pool-data-contract.md) (reader)

## Goal

For every `core` ∪ `opportunity` member of the session's pool file, run a
per-ticker deep search and write the day's ticker brief per the contract.
Pool-driven fan-out: membership in the pool file IS the collection schedule —
a ticker dropped from the pool is simply not collected next slot; no schedule
state is created or deleted anywhere.

## Requirements

- **R1 — Input**: `--session cn|us` (+ optional `--date`, `--tickers` subset
  for manual runs). Reads the pool per the pool contract's reading rule
  (newest ≤ date, staleness warning, `pool_max_staleness_days` cap with
  core-yaml fallback); exits distinctly only when even the fallback yields no
  tickers.
- **R2 — Render**: `prompts/ticker_deep_search.md` with `{{DATE}}`,
  `{{TICKER}}`, `{{SESSION}}`, `{{GENERATOR}}`, and the pool entry's
  `catalyst_type`/`rationale` as the investigation seed. The prompt instructs
  the model to score `catalyst_score` per the contract's scale and to prefer
  primary sources / major wires, mirroring the macro prompt's rules.
- **R3 — Generate**: subscription CLI backend, selectable like the macro
  collector. **Concurrency-capped fan-out** (config
  `ticker_collect_concurrency`, default 3) — subscription rate limits are the
  shared budget; a failed ticker never aborts the others.
- **R4 — Validate before write**: the ticker contract's hard requirements
  (5 sections + Impact line, ≥ 5 body-counted citations, frontmatter/filename
  agreement, word bounds 250–2000) with the standard retry-once-then-fail.
- **R5 — Atomic + idempotent**: per-ticker temp+rename; existing same-day
  brief ⇒ skip unless `--force`.
- **R6 — Partial success is success**: exit 0 when ≥ 1 requested brief was
  written; report per-ticker outcomes on stdout as one JSON summary line
  (consumed by the orchestrator for status/notifications); non-zero only when
  every ticker failed or the pool file was unreadable.
- **R7 — Log**: one line per ticker (date, session, ticker, sources_count,
  catalyst_score, outcome) to `ticker_briefs/collector.log`.

## Non-goals

- No pool mutation (the builder owns the pool; the collector only reads it).
- No analysis triggering (the runner reads `catalyst_score` itself).
- No cross-ticker synthesis (portfolio-level context is the macro brief's job).

## Acceptance criteria

1. Fan-out over a fixture pool writes contract-valid briefs for all members,
   respects the concurrency cap (observable via an injected fake backend), and
   isolates a failing ticker (others still written, summary marks the failure).
2. Killing the process mid-run leaves no partial brief files.
3. Validation rejects a brief whose frontmatter ticker mismatches its
   directory, with retry firing exactly once.
4. `--tickers NVDA` collects only that subset; `--force` overwrites.
