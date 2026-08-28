# Spec: Orchestrator (scheduling, settle, status, notifications)

**Builder**: Claude Code · **Status**: Open · Contracts: all (invoker); writer
of per-session status files and `ledger/outcomes.jsonl` (settle step)

## Goal

One static schedule entry per session drives the whole slot: settle → macro
collect/eval → pool build → ticker collect/eval → analysis → execution →
notify. Dynamic behavior lives in data (the pool file), never in scheduler
state — no schedule entries are ever created or deleted by code.

## Scheduling

- Config is the single source of truth: `sessions.cn.slot_time` /
  `sessions.us.slot_time` (default "08:30", market-local). The install script
  **generates** the launchd plists and the wrapper's acceptance window from
  config — nothing is hard-coded twice; changing `slot_time` requires
  re-running the install script, which the script itself prints as a reminder
  (`--print-schedule` shows the effective fire times).
- Generated jobs — **both sessions are treated identically; the host's
  timezone is never assumed** (this machine is America/Chicago, where 08:30
  Asia/Shanghai lands the *previous* local evening):
  - For each session, the install script converts `slot_time` in the
    session's IANA tz (`cn` = Asia/Shanghai, `us` = America/New_York) into
    every distinct host-local fire time across host-DST × market-DST
    combinations (≤ 4 candidates, deduped) and emits one launchd entry per
    candidate.
  - The wrapper runs only when the current time **in the session's timezone**
    is within `slot_time` ± 15 min, else exits 0.
  - **Slot identity is session-local**: the slot's `date` (used for
    filenames, ledger rows, and the weekday gate) is the current date in the
    session's timezone at fire time — a cn slot firing Sunday 18:30/19:30
    Chicago time IS Monday's Shanghai session.
  - **watchdog**: fires hourly; if a session's expected slot has not *started*
    by `slot_time` + 1h on its weekday (per the status file), send one
    notification for that session/day. This catches sleep/wake-coalesced
    launchd fires that land outside the wrapper window — a missed slot is
    never silent.
- Weekdays only (weekday computed in the session's timezone). Holiday
  **scheduling** calendars are a non-goal: a holiday run produces briefs and
  analyses harmlessly, and the adapter's S8 market-state guard skips every
  new submission on a closed market (D17). Previously placed GTC orders
  resting at the broker are broker behavior, not new submissions.
- The user accepted that the cn slot's analysis tail may finish shortly after
  the 09:30 open.

## Slot sequence

| # | Step | On failure |
|---|---|---|
| 0 | settle step (see below) | continue |
| 1 | macro collector `--session S` | continue — consumers degrade to newest older brief with warnings |
| 2 | macro evaluator (overlaps 3–5; **join barrier before 6**) | continue — verdict `missing`, flagged |
| 3 | pool builder | builder self-handles nomination failure (carried-forward pool); on hard crash, downstream reads the most recent prior pool file |
| 4 | ticker collectors (fan-out, concurrency-capped) | per-ticker isolation (collector R6) |
| 5 | ticker evaluators — every collected **core** brief + every would-trigger opportunity brief | continue, `missing` gating |
| 6 | analysis runner (incl. A/B pairs) | per-run isolation (runner R3) |
| 7 | execution adapter `submit` (`us` slot only) | per-order isolation (adapter R4) |
| 8 | notifications + final status write | — |

Steps 1 and 3 run in parallel (independent). **Step 6 does not start until the
macro evaluator process has terminated** (verdict written, fail, crash, or
timeout — the last two record `missing`); step 7 additionally requires that
the macro eval outcome is recorded in the status file. A late verdict can
therefore never be bypassed by timing.

## Settle step (0)

Never talks to the broker itself. Sequence:

1. Invoke `execution_adapter refresh` (the sole broker gateway) — order-status
   refresh rows, stale-entry cancels, close re-submissions per the adapter
   spec.
2. For decision records aged ≥ 1/5/20 trading days: compute close-to-close
   returns and benchmark returns (yfinance; benchmark per the existing
   `benchmark_map`), run the plan replay per the ledger contract, emit/refresh
   the `outcomes.jsonl` record. Trading-day age uses the ticker's own price
   calendar (a date with a bar ⇒ it traded), sidestepping holiday tables.

## Status files (`~/.tradingagents/pipeline_status.<session>.json`)

One file per session — concurrent cn/us slots never clobber each other; the
webui reads both.

```json
{
  "slot": {"date": "2026-08-03", "session": "us", "started_at": "...", "finished_at": null},
  "components": {
    "macro_collector": {"status": "ok", "started_at": "…", "finished_at": "…", "duration_s": 412, "error": null},
    "macro_evaluator": {"status": "warn", "started_at": "…", "finished_at": "…", "duration_s": 300, "error": "verdict=fail: 2 flagged claims"},
    "pool_builder": {"status": "failed", "started_at": "…", "finished_at": "…", "duration_s": 88, "error": "one-line reason"}
  },
  "history": []
}
```

- `status` ∈ `ok` | `warn` | `failed` | `timeout` | `skipped`, plus the
  transient `running` for the currently in-flight component only (the
  write-as-you-go status file records a component as `running` with
  `finished_at: null` while its subprocess runs, so a kill mid-slot leaves a
  readable file naming it). A slot whose `finished_at` is set never contains
  `running` — consumers seeing it should render "in progress", not an error.
  An evaluator that completes and writes a `fail` **verdict** is `warn` here
  (the component worked; the content failed) — component `failed` means the
  process crashed or was refused, and gating then treats the verdict as
  `missing`.
- `history` holds the last 20 completed slot objects (same shape as `slot` +
  `components`).
- Atomic rewrite after every component. The webui renders red/green per
  component; a session with no slot started by `slot_time` + 1h on a weekday
  renders red (staleness).

## Notifications

- macOS notification (`osascript`) on: any component `failed`/`timeout`, any
  eval `fail` verdict, any broker `rejected`/`canceled` surfaced by refresh,
  the watchdog's missed-slot alert, and (always) an end-of-slot summary —
  plans produced, orders submitted/dry-run, pairs run, failures.
- Dedup: at most one notification per (component, slot, reason); the watchdog
  fires at most once per session/day.
- An `osascript` failure degrades to log-only — notifications can never fail
  a slot.
- Config `notifications_enabled` (default true).

## Requirements

- **R1**: every component runs as a subprocess with a per-component timeout
  from config. Defaults: macro collector 30 min, ticker fan-out 45 min
  aggregate, macro evaluator 20 min, ticker-evaluator fan-out 60 min aggregate
  (evaluations run concurrently under the same concurrency cap as ticker
  collection — up to ~15 evals must fit), analysis runner 120 min,
  adapter/settle 15 min. Timeouts are per step invocation (the fan-out steps
  get one aggregate budget each); a timeout counts as `timeout` and the slot
  continues per the table.

- **R2**: slot mutual exclusion via an `O_CREAT|O_EXCL` lockfile
  `~/.tradingagents/locks/<session>-<date>.lock` (stale locks older than 12h
  are broken with a warning) — atomic, no status-file TOCTOU. Concurrent
  cn/us overlap is allowed (disjoint data + separate status files).
- **R3**: `--only <component>` and `--from <step>` flags for manual recovery;
  every component stays independently runnable by hand. Recovery notes: rerun
  analyses append new decision rows with incremented `<n>` (ledger contract);
  the adapter's ticker/day dedupe (S4) makes recovery submission-safe.
- **R4**: logs to `~/.tradingagents/orchestrator.log`, one line per component
  per slot (machine-parseable prefix + human tail); size-based rotation (keep
  the last 5 × 10 MB). Logs and status files never contain secrets (adapter
  S7 applies system-wide).

### Runtime concurrency policy (P0)

The analysis runner uses `analysis_job_concurrency`, default 2, overridden by
`TRADINGAGENTS_ANALYSIS_JOB_CONCURRENCY`. Jobs are the concurrency unit; the
arms within each job run sequentially. Ledger writes are immediate and
flock-protected. The parent thread emits completed-job lines, while the final
summary is assembled in planned-job order.

## Non-goals

- No cron support documented beyond launchd (macOS is the target machine).
- No retries beyond each component's own retry logic (manual `--from` covers
  recovery).

## Acceptance criteria

1. Fake-component tests: ordering, 1∥3 parallelism, the evaluator join
   barrier before step 6, failure continuation matrix (each table row),
   per-component timeouts, lockfile exclusivity + stale-lock breaking,
   `--only`/`--from`.
2. Install script: generated plists cover all host-DST × market-DST
   renderings on an America/Chicago host for BOTH sessions (cn: 18:30 CST /
   19:30 CDT previous local day; us: 07:30 both, differing across the
   mismatched DST-transition weeks); the wrapper accepts `slot_time` ± 15 min
   evaluated in the session's tz and rejects other hours; the slot date and
   weekday gate resolve in the session's tz (Sunday-evening Chicago fire ⇒
   Monday cn slot); `--print-schedule` matches the generated files;
   double-fire dedupe via the lockfile.
3. Watchdog: a fixture day with no started slot by `slot_time`+1h notifies
   exactly once; a started slot suppresses it.
4. Settle: fixture ledger + prices produce contract-valid outcomes rows;
   plan-replay first-touch and ambiguity cases covered; settle performs zero
   broker calls when the adapter refresh is stubbed out (verified by the fake).
5. Status file survives kill mid-slot (readable, shows the failed component);
   cn and us slots running concurrently leave both files intact.
