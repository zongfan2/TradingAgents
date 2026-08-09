# Trade Plan & Outcome Ledger Contract (v1)

The shared interface between the analysis runner (writer of decisions + plans),
the execution adapter (sole broker gateway; writer of `orders.jsonl` via its
submit and refresh entrypoints), the settle step (writer of `outcomes.jsonl` —
it never talks to the broker itself), and the webui (read-only renderer).

## Location

- Directory: `~/.tradingagents/ledger/`
  (env override `TRADINGAGENTS_LEDGER_DIR`, config key `ledger_dir`).
- Files: `decisions.jsonl`, `orders.jsonl`, `outcomes.jsonl` — append-only
  jsonl; each line is one record; writers append atomically (single-line
  `O_APPEND` writes). Nothing ever rewrites history: state changes are new
  rows, and "current state" is defined per file below.

## TradePlan schema (embedded in a decision record)

```json
{
  "action": "BUY",                  // BUY | SELL | HOLD
  "conviction": 0.7,                // 0-1
  "add_intent": false,              // BUY on a held ticker: true = deliberate add-on tranche
  "add_rationale": null,            // required non-empty when add_intent — the NEW information justifying the add
  "entry_zone": [280.0, 285.0],     // required for BUY; optional for SELL; absent for HOLD
  "stop": 268.0,
  "targets": [301.0, 315.0],        // 1-2 targets, ascending for BUY
  "horizon_days": 10,
  "invalidation": "daily close below the weekly mid-band",
  "sizing": {"risk_pct": 1.0},
  "source_levels": "entry = daily BOLL mid, stop = below daily lower band"
}
```

Semantics: `SELL` closes an existing long only (no short opening — non-goal).
`HOLD` carries no execution fields. A BUY on a ticker with an existing long
position is a **maintain** (no order) unless `add_intent: true` with a
non-empty `add_rationale` — persistence of a bullish view is not new
information; an add-on must cite what changed. `add_intent` plans pass the
same BUY level checks; `add_intent` on a flat ticker is treated as a plain
BUY. In a daily re-analysis loop this is what prevents "still bullish" from
silently compounding into an uncapped position. Plans come from the trader LLM, anchored to
the market analyst's technical levels, then checked by the deterministic
validator. Definitions used by the validator: **entry mid** = midpoint of
`entry_zone`; **last close** = most recent daily close on or before the
analysis date from the same OHLCV series the market analyst was served;
**ATR14** = 14-period average true range on daily bars of that series.

- BUY: `stop < entry_zone[0] ≤ entry_zone[1] < min(targets)`; entry mid within
  ±10% of last close; stop distance (entry mid − stop) within
  [0.5 × ATR14, 15% of entry mid]; `0.1 ≤ sizing.risk_pct ≤ 2.0`;
  `1 ≤ horizon_days ≤ 30`.
- SELL: schema + numeric hygiene only. Broker-state checks (position exists,
  sellable qty, open legs) are deliberately NOT here — the validator is
  broker-blind; they are the execution adapter's S5 duties.
- HOLD: trivially valid.
- Numeric hygiene (all actions): every price finite and > 0; `entry_zone[0] ≤
  entry_zone[1]`; targets strictly ascending, no duplicates; conviction in
  [0, 1]; NaN/Inf anywhere ⇒ invalid.
- Any violation ⇒ `plan_valid: false`: the decision stands as a directional
  opinion, the report is flagged, and the plan is never executable.

## decisions.jsonl record

```json
{
  "run_id": "2026-08-03-us-NVDA-brief-1",
  "pair_id": "2026-08-03-us-NVDA-a1",        // attempt-scoped: both arms of ONE pairing invocation share it; null if unpaired
  "date": "2026-08-03", "session": "us", "ticker": "NVDA",
  "arm": "brief",                            // brief | feeds  (arm bundle)
  "preset": "claude-sub",
  "trigger": "core",                         // core | catalyst | manual
  "catalyst_score": 7.5,                     // null for core runs without a brief
  "macro_eval_verdict": "pass",              // pass | warn | fail | missing
  "ticker_eval_verdict": "pass",
  "inputs": {                                // audit: exactly what this run consumed
    "macro_brief": {"path": "2026-08-03.us.md", "generated_at": "2026-08-03T12:35:00Z", "sha256": "…"},
    "ticker_brief": {"path": "NVDA/2026-08-03.md", "generated_at": "2026-08-03T12:41:00Z", "sha256": "…"},
    "pool": {"path": "us/2026-08-03.json", "generated_at": "2026-08-03T12:31:00Z", "sha256": "…"},
    "config_digest": "…"                     // resolvable, see below
  },
  "decided_at": "2026-08-03T13:02:11Z",      // UTC instant the run completed — anchors outcome returns
  "decision": "BUY",                         // BUY | SELL | HOLD | ERROR
  "plan": { ...TradePlan... },               // null when decision is HOLD-without-plan or ERROR
  "plan_valid": true
}
```

`inputs` entries are null for whatever the run did not consume (feeds arm ⇒
both briefs null; withheld/absent brief ⇒ null). Combined with the contracts'
archive-on-overwrite rule, every hash resolves to preserved content — a rerun
can never silently rewrite what an earlier decision saw. `config_digest` is
content-addressed: the first time a digest appears, the runner writes the
effective-configuration snapshot (resolved run config + prompt-template file
hashes) to `ledger/config_snapshots/<digest>.json`, so the digest always
resolves to the full configuration that produced the run.

- `run_id` = `<date>-<session>-<ticker>-<arm>-<n>` where `<n>` is 1 + the
  count of existing decision rows with the same `(date, session, ticker, arm)`.
  The count-and-append is performed under an exclusive `flock` on
  `decisions.jsonl` (single host), so concurrent slot and manual runs cannot
  mint duplicate ids.
- `pair_id` = `<date>-<session>-<ticker>-a<k>`, `<k>` = pairing attempt
  number. A rerun re-runs the **whole pair** and mints `a<k+1>` for both arms
  — a pair_id therefore always has exactly two rows, one per arm.
- Decisions record **intent only**; no execution state is stored here — the
  runner finishes before the adapter runs, and rows are never edited. Derived
  states, by joining `orders.jsonl` on `run_id`: **submitted** = a `submitted`
  row with `dry_run: false` (order reached the broker); **filled** = submitted
  plus a later `refresh` row with `broker_status` `filled`/`partially_filled`.
  Aggregates must use *filled*, not *submitted*, wherever fills matter.

## orders.jsonl record (execution adapter only — submit and refresh entrypoints)

```json
{
  "run_id": "2026-08-03-us-NVDA-brief-1",
  "written_at": "2026-08-03T13:05:00Z",
  "session": "us", "ticker": "NVDA",
  "kind": "submitted",          // intent | submitted | skip | cancel | refresh
  "dry_run": false,
  "reason": null,               // required for kind=skip (cap hit, dedupe, halt, short-block, ...)
  "client_order_id": "2026-08-03-us-NVDA-entry",
  "order_kind": "bracket",      // bracket | close ; null for skip/refresh
  "tif": "gtc",
  "qty": 12, "limit_price": 285.0, "stop_price": 268.0, "target_price": 301.0,
  "broker_status": "filled",    // refresh rows: latest broker state
  "filled_avg_price": 283.6, "filled_qty": 12
}
```

- Current state of an order = the row with the greatest `written_at` for its
  `client_order_id` (every row carries `written_at`, so ordering never depends
  on physical line position).
- `client_order_id`: entries use `<date>-<session>-<ticker>-entry` —
  deliberately **excluding** `<n>` and arm, so the broker itself rejects a
  duplicate entry for the same ticker/day and crash recovery can query by a
  deterministic id (entries are never re-submitted; a re-entry is a new date).
  Closes use `<date>-<session>-<ticker>-close-<attempt>` — the broker rejects
  duplicate client ids, so the once-only re-submission is a new attempt
  number. The logical operation (the close of run X) is the set of its
  attempt rows.
- `intent` rows are appended immediately before a live broker submit; a
  recovery pass finding an `intent` without a matching `submitted`/`skip` row
  must query the broker by `client_order_id` before any resubmission.

## positions.json (execution adapter `refresh` only — atomic snapshot, not append-only)

```json
{
  "as_of": "2026-08-04T12:20:00Z",
  "equity": 99985.65,
  "positions": [
    {"ticker": "NVDA", "qty": 12, "avg_entry": 283.6, "last_fill_at": "2026-08-03T14:32:00Z",
     "market_value": 3520.0, "unrealized_pl": 114.7, "tranches": 1,
     "open_orders": [{"client_order_id": "2026-08-03-us-NVDA-entry", "leg": "stop", "price": 268.0}]}
  ]
}
```

Written (atomic rewrite) by every adapter `refresh` from live broker state.
Sole purpose: **decision context** — the runner injects a held ticker's
snapshot into the trader prompt. The adapter itself never reads it (it checks
live broker state at submit time), and an `as_of` older than 1 trading day
means the runner injects "position data unavailable" instead of stale
numbers.

## outcomes.jsonl record (settle step only)

```json
{
  "run_id": "2026-08-03-us-NVDA-brief-1",
  "settled_at": "2026-08-10T12:05:00Z",
  "returns": {"d1": 0.004, "d5": 0.021, "d20": null},          // null until computable
  "benchmark_returns": {"d1": 0.001, "d5": 0.008, "d20": null},
  "plan_replay": {                                             // BUY with plan_valid only; else null
    "entry_hit": true, "stop_hit_first": false,
    "target_hit_first": true, "ambiguous": false, "realized_rr": 0.94
  },
  "paper": {"realized_pnl": 214.7, "closed": true}             // null when run never executed
}
```

- The settle step emits/refreshes one record per `run_id` as horizons become
  computable; the current record is the one with the greatest `settled_at`.
- **Return anchor (no lookahead, no off-by-one)**: `d0` = the first regular-
  session close **at or after `decided_at`** in the ticker's own price
  calendar (a cn run finishing after the 09:30 open anchors to that same
  day's close, which is still in its future). `returns.dh` =
  close(d0 + h trading days) / close(d0) − 1; benchmark identically on the
  benchmark's calendar.
- `plan_replay` (BUY plans only; SELL/HOLD ⇒ null), on daily OHLC bars
  **strictly after `decided_at`**: `entry_hit` = low ≤ `entry_zone[1]` within
  `horizon_days` of the anchor; then
  first-touch ordering of `stop` vs `targets[0]` on subsequent bars.
  `realized_rr` = (exit − entry) / (entry − stop) with entry = `entry_zone[1]`
  and exit = the first-touched level. Both touched in one bar ⇒
  `ambiguous: true`, excluded from R:R aggregates.

## A/B aggregate definitions (computed on demand — by the webui/report script — never stored)

Over decision rows joined with outcomes (and orders for execution status),
grouped by `arm` (and optionally `preset`):

- **Pair selection**: for each `(date, session, ticker)`, aggregates use only
  the **latest complete pairing attempt** (greatest `a<k>` with both arms
  present); earlier attempts are excluded everywhere.
- **direction agreement** (paired rows only): share of selected pairs whose
  two arms produced the same `decision`.
- **hit rate @ h** for h ∈ {d1, d5, d20}: share of BUY rows with
  `returns.h > benchmark_returns.h` (SELL rows: `<`; HOLD excluded).
- **excess return @ h**: mean of `returns.h − benchmark_returns.h`, signed
  positive for BUY, negated for SELL.
- **plan quality**: entry-hit rate; mean `realized_rr` over non-ambiguous
  replays; target-first share.
- **eval weighting**: all metrics reported twice — all rows, and excluding
  rows whose consumed brief eval verdict is `fail` (`warn` shown both ways).

Statistical caveats (rendered with the numbers, not buried):

- This is a **conditional comparison**: catalyst-triggered pairs enter the
  sample because the *brief* arm's `catalyst_score` fired, so triggered-pair
  results are conditioned on the brief arm's selection — label them so; only
  core-rotation pairs approximate an unconditional comparison.
- Same-ticker rows across days are not independent samples: aggregates are
  also shown clustered per ticker and stratified per date.
- "≥ 100 pairs" counts **deduplicated `(date, session, ticker)` pairs**
  (one per latest complete attempt, per the pair-selection rule).
- `returns` are close-to-close on the decision date's series (plan replay uses
  OHLC); realized paper P&L is reported as its own column, never mixed into
  the close-to-close metrics. Benchmarks come from the existing
  `benchmark_map` (SPY for US, ^HSI for .HK, index per suffix otherwise).

## Versioning

v1. Breaking changes bump the version and update every consumer in the same
change set.
