# Spec: Analysis Runner

**Builder**: Claude Code · **Status**: Open · Contracts:
[trade-plan-ledger-contract.md](trade-plan-ledger-contract.md) (writer),
[pool-data-contract.md](pool-data-contract.md),
[ticker-brief-data-contract.md](ticker-brief-data-contract.md),
[macro-brief-data-contract.md](macro-brief-data-contract.md) (readers)

## Goal

Decide which tickers get a full multi-agent analysis this slot, run them (with
A/B pairing), turn the trader's output into a validated TradePlan, and append
decision records to the ledger. The runner spends the analysis-LLM budget;
everything upstream只是 preparing its inputs.

## Trigger rules

Per session slot, from the day's pool file:

- **core**: every member, every slot (holdings risk is never skipped).
- **opportunity**: members whose day's ticker brief has
  `catalyst_score ≥ analysis_trigger_threshold` (default 7.0). Brief absent or
  eval `fail` ⇒ no auto-trigger.
- **manual**: webui/CLI can trigger any ticker anytime (`trigger: "manual"`).
  Manual runs are never auto-executed (adapter S5); executing one requires an
  explicit adapter invocation.

## Eval gating (normative state table)

A verdict counts only when the eval's `brief_sha256` matches the hash of the
brief actually being served (contract revision binding); any mismatch — e.g.
a re-collected brief with a stale predecessor eval — is `missing`.

| macro eval | ticker eval | brief-arm run | feeds shadow (when paired) | auto-execution |
|---|---|---|---|---|
| pass/warn | pass/warn | runs | runs | eligible |
| pass/warn | missing | runs, flagged | runs | blocked unless `execute_on_missing_eval` |
| pass/warn | fail | runs with the ticker brief **withheld** (DATA_UNAVAILABLE) — core coverage never stops, contaminated input does | runs | blocked (skip reason `ticker-eval-fail`) |
| missing | pass/warn | runs, flagged | runs | blocked unless `execute_on_missing_eval` |
| fail | any | replaced by a feeds-arm run, flagged `macro-fail` | that run *is* the feeds arm | blocked — feeds never executes, so macro `fail` ⇒ no auto-execution that slot (deliberate) |

- `execute_on_missing_eval`: config, default **false** — a `missing` verdict
  means the evaluator broke (the orchestrator's join barrier otherwise
  guarantees a verdict), so execution defaults to requiring `pass`/`warn`.
- Catalyst auto-triggering additionally requires the ticker eval to be
  non-`fail` (trigger rules above); the table governs runs that happen anyway
  (core, manual).

## A/B pairing

- Arms are **bundles**: `brief` = macro brief + ticker brief + offline news
  side; `feeds` = upstream fixed feeds + live news APIs. One config axis;
  never mix per-switch (the `macro_source` / `ticker_source` switches exist
  for surgical debugging, but the protocol compares bundles).
- Pairing config `ab_pairing`: `off` | `paired` (default `paired` while the
  A/B campaign runs). Paired = run both arms for: (a) a rotating subset of
  core (`ab_core_pairs_per_slot` = k, default 3 **per session slot**),
  selected statelessly — sort the session's core tickers, take k wrapping
  from offset `(date.toordinal() mod ceil(len(core)/k)) × k` — every core
  ticker is paired within `ceil(len/k)` slots for ANY k/len combination (a
  plain `×k` stride skips tickers whenever len divides the stride), and
  rotation needs no cross-day state file; (b) every catalyst-triggered
  ticker. All other runs are `brief`-arm only. A rerun re-runs the **whole
  pair** and mints the next pairing attempt (`pair_id` `a<k+1>`, ledger
  contract) — single arms are never re-run in isolation.
- Both runs of a pair share `pair_id`, identical preset/config, and execute
  back-to-back. **Only the `brief` arm may reach the execution adapter**; the
  `feeds` arm is always shadow — it never reaches the adapter, so it never
  gains an `orders.jsonl` submitted row.

## TradePlan production

1. The trader prompt (upstream `trader.py`, extended) must output, after its
   free-text reasoning, a fenced JSON block matching the TradePlan schema,
   with levels explicitly anchored to the market analyst's technical report.
   For a held ticker the prompt includes the position snapshot (qty, avg
   entry, unrealized P&L, protective stops) from `positions.json` (ledger
   contract; `as_of` older than 1 trading day ⇒ "position data unavailable"
   is injected instead) and instructs: emit `add_intent` + `add_rationale`
   only on genuinely NEW information — a BUY without `add_intent` is a
   maintain and produces no order.
2. The runner parses the block (one repair-reprompt on parse failure), then
   runs the deterministic validator from the ledger contract (price/ATR checks
   use the same yfinance data the market analyst saw).
3. `plan_valid: false` ⇒ decision recorded as directional opinion; report
   flagged; never executed. HOLD ⇒ trivially valid, no levels.

## Ledger writes

One `decisions.jsonl` record per run, per the contract, written immediately
after the run completes (crash between runs loses at most the in-flight run).
`run_id` = `<date>-<session>-<ticker>-<arm>-<n>`, with `<n>` assigned per the
ledger contract (1 + count of existing rows for the same date/session/ticker/
arm — reruns and manual runs increment it, assigned under the contract's
`flock` rule). The runner also records `decided_at` (UTC run-completion
instant — the outcome-return anchor). Execution state is never written
here; it is derived by joining `orders.jsonl` (contract rule). The runner
computes the record's `inputs` block itself — sha256 of the exact brief/pool
files it served to the agents plus the config digest — at consumption time,
so the hashes describe what the run actually saw, not what is on disk later.

## Requirements

- **R1**: runs execute through the existing `TradingAgentsGraph` propagation
  (same code path as `compare/run.py`) with per-arm env/config injection;
  results/memory isolation per arm follows the compare-harness pattern so
  reflection memories never leak across arms.
- **R2**: budget guard: config `max_runs_per_slot` (default 30 — the default
  worst case is 10 core + 3 core-pair shadows + 5 triggered × 2 arms = 23,
  with headroom); when exceeded, drop in order: unpaired triggered runs
  (lowest catalyst_score first), then whole pairs (both arms together — never
  orphan one arm), and never core solo runs. Log every drop (no silent
  truncation).
- **R3**: a failed run (LLM error after retries) records a
  `decision: "ERROR"` ledger row and continues; the slot never aborts on one
  ticker.
- **R4**: emits a one-line JSON summary per run to stdout for the orchestrator
  (status + notification assembly).
- **R5**: CN/HK sessions produce plans too (for the ledger/plan-replay), but
  the execution adapter is never invoked for `session: cn` (Alpaca is
  US-only; CN/HK execution is a future adapter).

## Non-goals

- No portfolio-level optimization (per-ticker decisions only, v2).
- No intraday re-runs; no execution logic (adapter's spec).

## Acceptance criteria

1. Offline tests with a faked graph: trigger selection (core-all,
   threshold, eval-gated, manual), rotation fairness of core pairs, budget
   guard drop order, run_id/pair_id formats.
2. TradePlan parse→validate: fixture LLM outputs covering valid BUY, zone
   inversion, stop too tight/wide, entry too far from close, HOLD, parse
   failure + repair path.
3. Ledger rows validate against the contract schema; arm isolation of
   memory/results dirs verified.
