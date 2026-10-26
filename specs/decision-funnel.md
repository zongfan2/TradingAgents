# Spec: Decision funnel (per-slot exclusion record + webui page)

**Builder**: Claude Code · **Status**: P1 implemented (2026-10-25; user
signed off the plan), P2 implemented (2026-10-27), P3 open · Contracts: new `funnel` event (model:
`pipeline/contracts/funnel.py`) + snapshot schemas (this spec);
reads pool, brief/eval, ledger and orders contracts unchanged.

## Why

Every slot narrows a universe of candidates down to a handful of orders, but
most exclusion reasons are lost: they go to component stdout/stderr or the
logger, which the orchestrator discards after computing the status line. The
user wants to see, per slot, **where each ticker dropped out and why**, and
to keep that record for later strategy validation (threshold and filter
studies).

## Gaps this closes (verified on disk 2026-10-25)

| Stage | Today on disk | Lost today |
|---|---|---|
| Pool | core/opportunity/watch/removed, scores, `gate` | dropped nominees (e.g. MOH us 2026-10-23, score 3.5); new entrants cut by the opportunity cap or the watch cap; wrong-session/duplicate nominees; **which gate rule fired** (a fetch error and a structural fail are indistinguishable); nomination-failure reason |
| Collect | `collector.log` written/failed | failure reason; watch tickers are never collected (implicit); opportunity layer dropped on a stale pool |
| Evaluate | eval verdict + flagged claims | opportunity briefs skipped below the trigger threshold (counts only) |
| Trigger | — | **opportunity tickers not triggered** (no score / eval fail / brief `catalyst_score` < 7.0, not even logged); budget-guard drops; already-done skips |
| Decide | decision, `plan_valid` | plan violations / `plan_error`, ERROR text, run flags, **PM 5-tier rating** (only in `full_states_log_<date>.json`, overwritten by a same-day rerun) |
| Execute | skip reasons for attempted orders | **non-candidates** (HOLD, feeds arm, invalid plan, action mismatch, incomplete BUY plan, cn session); superseded attempts |

## Design

### R1 — Event log (recorded at the source)

`~/.tradingagents/funnel/<session>/<date>.events.jsonl`, append-only under
`locked_append`, one line per ticker decision a component makes:

```json
{"schema": 1, "written_at": "…Z", "session": "us", "date": "2026-10-23",
 "stage": "trigger", "component": "analysis_runner", "ticker": "HUM",
 "outcome": "excluded", "code": "below-threshold",
 "reason": "brief catalyst_score 6.0 < 7.0", "details": {"score": 6.0, "threshold": 7.0}}
```

- `outcome`: `passed` | `excluded` | `info`. `code` is a stable kebab-case
  code per stage (table below) for aggregation; `reason` is human text. An
  event with an unknown stage, outcome or code is dropped with a warning.
- Every emitter instance stamps a `run` id. A rerun (or a manual subset run)
  appends new events. Supersession rule (`funnel_events.current_outcomes`,
  the reference the snapshot builder uses): per **(stage, ticker)**, the
  **latest run with a `passed`/`excluded` event** decides; within that run
  the stage outcome is the strongest event (`passed` > `excluded` > `info`)
  and all of that run's events for the key are kept (both decide arms of a
  pair, carrying `details.arm`; a SELL's `cancel`s beside its close; a
  feeds-arm non-candidate beside the brief arm's order). A run with only
  `info` events (`already-done`, `superseded`, the adapter's rerun
  `dedupe`) never replaces a decision — except that an eval verdict
  (`pass`/`warn`/`fail`/`eval-error`) always decides `evaluate`, even as a
  core ticker's `info` (a `fail` withholds the brief);
  other runs' events are kept as notes. So a rerun supersedes only what it
  re-decided. Exception, `execute`: a **live** order an earlier run placed
  stays at the broker whatever a rerun emits (its feeds-arm non-candidate,
  `dedupe`), so the latest run with a live `submitted` (`details.dry_run`
  false) decides — a rerun never downgrades a placed order. Dry-run
  submissions place nothing and follow the general rule, so a later live
  run's skip replaces them.
- Emitters are best-effort: an event-write failure logs a warning and never
  changes the component's behavior, exit code or outputs. Each component's
  funnel helper is additionally wrapped (`funnel_events.best_effort`) so a
  call-site error (argument binding, formatting) cannot escape either.
- No existing contract changes: pool files, `DecisionRecord` (`extra=forbid`)
  and `OrderRecord` stay as they are. Everything new goes in the event log.

| Stage (component) | `code` values |
|---|---|
| `nominate` (pool_builder) | `nominated`, `wrong-session`, `duplicate`, `nomination-failed` (ticker `null`, `info`) |
| `pool` (pool_builder) | `core`, `opportunity-enter`, `opportunity-stay`, `watch`, `carried-forward`, `dropped-low-score`, `dropped-gate-fail`, `cap-truncated`, `watch-cap`, `removed-low-score-streak`, `removed-gate-fail-streak`, `now-core`; `details.gate_rule`: `insufficient-bars`, `liquidity`, `bollinger-pass`, `bollinger-weekly-broken`, `bollinger-daily-extended`, `bollinger-both`, `volume-demotion`, `fetch-error`, `gate-error`; `details.thresholds` (entry/exit) |
| `collect` (ticker_collector) | `written`, `exists`, `failed` (+ reason; `info` for core — a core ticker is analysed without its brief — `excluded` for opportunity), `not-collected-watch`, `opportunity-dropped-stale-pool` |
| `evaluate` (evaluator) | `pass`, `warn`, `fail` (`info` for core — the brief is withheld but the ticker runs; `excluded` for opportunity), `eval-error`, `skipped-below-threshold`, `skipped-absent` |
| `trigger` (analysis_runner) | `core`, `core-pair`, `catalyst`, `no-score`, `eval-fail`, `below-threshold`, `budget-dropped`, `already-done`, `macro-fail-feeds-only`; manual runs emit nothing |
| `decide` (analysis_runner) | `BUY`, `SELL`, `HOLD`, `ERROR`; `details`: `arm`, `run_id`, `pair_id`, `pm_rating` (raw five-tier rating, now also in the child's result payload), `plan_valid`, `violations[]`, `error`, `flags`, `execute_eligible` |
| `execute` (execution_adapter) | non-candidates `hold`, `error`, `feeds-arm`, `no-plan`, `plan-invalid`, `action-mismatch`, `incomplete-plan`, `non-us-session` (from `non_candidate_reason`, the single source of the candidate filter); `superseded` (`info`); every order skip reason by its code (`stale-quote`, `total-risk`, `submit-error`, …) — except `dedupe` (`info`: an earlier run already placed the order); `submitted` (also for `recovered-intent`); `rejected` (`excluded`: the broker answered the submit with status rejected); `cancel` (`info`); every execute event carries `details.arm` |

### R2 — Component output archive (safety net)

The orchestrator saves each component's final stdout JSON summary line and
the last 64 KB of stderr to
`~/.tradingagents/funnel/<session>/<date>/<component>.out.json`, so a reason
no emitter covers is still kept. Overwritten on a rerun of the same
component; failures to write are log-only.

### R3 — Funnel snapshot (one file per slot)

`python -m pipeline.funnel build --session S --date D` (`pipeline/funnel.py`)
writes `~/.tradingagents/funnel/<session>/<date>.json` (atomic, sorted keys,
no build timestamp — identical inputs give a byte-identical file). Contract
model: `pipeline/contracts/funnel.py` `FunnelSnapshot` — the writer
validates strictly before writing, readers (P3) use `parse_lenient`; a
stage is `unknown` exactly when its `code` is null.

- `context`: macro verdict (revision-bound, the runner's rule), pool
  (`as_of_date` of the pool the collectors read, `carried_forward`, read
  error), `nomination_failed`, `thresholds` (trigger from the slot's
  `config_snapshots`, entry/exit from pool events, caps from the pool
  contract; `thresholds.source` says `config-snapshot` / `events` /
  `default` per value), `execute_ran`, the event count, and `inputs`: a
  sha256 per file read — for the shared append-only files
  (`decisions.jsonl`, `orders.jsonl`, `collector.log`) and the slot's own
  event log only the slot's kept rows/events are hashed (manual decision
  rows, which can shadow the slot's decision, under `#manual`), so later days' appends never change an old snapshot.
- `counts`: tickers with an entry per stage, plus `final` counts.
- `tickers[]` (sorted): one entry per ticker ever considered (nominees, pool
  layers incl. `removed`, decisions) with `layer` (from the pool the
  components served — the same-day one or a warn-stale one — else the core
  yaml fallback), `furthest_stage`,
  `final`, `stop` (the first `excluded` stage **after the last stage the
  ticker passed** — an exclusion it went on past, e.g. a core collect
  failure recorded before the core rule, is not its stop: stage, code,
  reason, provenance; `null` if none), `provenance` (set over stages) and
  `stages`: per stage `outcome`, `code`, `reason`, `provenance`, `details`
  and the `events` behind it (both decide arms of a pair; every execute row
  and the superseded attempts).
- `final`: `order-live` / `order-dry` (execute `passed`), `excluded` (a
  stop), `pending` (us, execute not run yet, and some arm is an execution
  candidate — a ticker with none, e.g. brief HOLD + feeds BUY, is excluded
  at execute by `non_candidate_reason` already), `unknown`
  (no stop, no order — e.g. a core ticker without a decision row).
- Per (stage, ticker) a **recorded** R1 result (reduced by
  `current_outcomes`) wins; otherwise the stage is **reconstructed** from
  files by a rule the component applies deterministically, or **unknown**
  with `code: null` — a code is never invented. Reconstruction rules:
  nominate — nomination cache (wrong session by `session_for_ticker`, a
  duplicate keeps its first nomination); pool — layers of the same-day pool
  file (`opportunity-enter` when `entered_on` is the slot date, else
  `-stay`; `carried-forward` on a carried pool; without a usable pool —
  none, or older than the staleness cap — the core layer is
  `core.<session>.yaml` as the components read it (as it is *now*: a
  backfill of an old degraded slot uses today's list, hashed in `inputs`)
  and a too-old pool's opportunity layer is collect
  `opportunity-dropped-stale-pool`), `removed[].reason` prefixes
  (`score<` / `technical gate fail for` / `opportunity cap`), a nominee
  missing from the pool is `unknown` (low score, gate fail and watch cap
  are indistinguishable without the gate); collect — the slot's
  `collector.log` rows (`written`; `skipped` on a rerun ⇒ `exists`;
  `failed`), watch is
  `not-collected-watch`, a brief without a log row is `unknown`; evaluate —
  the revision-bound eval verdict, `skipped-absent` without a same-day
  brief, `skipped-below-threshold` for an opportunity brief under the
  trigger; trigger — the decision rows (`core`, `core-pair`, `catalyst`,
  `macro-fail-feeds-only` from the row's own macro verdict) or, for an
  untriggered opportunity ticker, `no-score` / `eval-fail` /
  `below-threshold` from its same-day brief (otherwise `unknown`: budget
  guard or run failure); decide — latest attempt per (ticker, arm), the
  strongest arm decides, violations/PM rating/error text are not in the
  ledger; execute — cn rows are `non-us-session`; us only once the
  execute step ran (order rows or execute events exist): non-candidates by
  `non_candidate_reason`, candidates by their order rows (a live
  submission sticks, else the arm's latest deciding row; brief arm first),
  superseded attempts as `info`. Manual runs are left out — their decision
  rows, order rows and execute events — so a manual submit on the slot day
  never marks the scheduled execute step as run (`pending` stays). They do
  share the attempt counter, though: when a manual attempt is the latest
  for a (ticker, arm) **and was decided before an execute run** (some slot
  submit row — `intent`/`submitted`/`skip`, not refresh — or execute event
  is later than its `decided_at`), that run
  superseded the slot's row and skipped the manual one, so the arm's
  execute stage is `manual-shadowed` (`excluded`,
  `details.manual_run_id`), overriding the recorded `superseded`. A manual
  rerun after execution shadows nothing, and a live order the slot's row
  placed (order row or recorded event) always stands. Execute
  events, like order rows, count only when written on the slot day
  (session-local): a later-day re-execution under the slot's run_id never
  changes the old snapshot.
- Built in-process by the orchestrator, recorded as status component
  `funnel`: at the end of every slot (cn and us) and again after the us
  `execute` job. A build failure is a `warn` on `funnel` (its error is
  appended to the execute notification) — never a slot or execute failure;
  the module is imported lazily so even an import error cannot stop a slot.
- `python -m pipeline.funnel backfill --since 2026-10-14 [--until D]
  [--session S]` rebuilds every slot with inputs (a same-day pool file, an
  event log or a decision row); one slot's failure is reported and the
  rest still build.

### R4 — Webui page 6 "决策漏斗"

- `GET /api/pipeline/funnel/dates`, `GET /api/pipeline/funnel/{session}?date=`,
  `GET /api/pipeline/funnel/aggregate?session=&since=` (counts by
  stage × code over dates; per-ticker exclusion frequency).
- Page: date/session picker; context strip (macro, thresholds); stage counts
  as a horizontal funnel; a table of tickers with stage reached, code,
  reason, detail, provenance badge, filterable by stage/code; each row links
  to the existing brief and decision views; CSV export of the table.
- Read-only. Missing files render "not yet run" (webui R1).

### R5 — Remote viewing over Tailscale (no public exposure)

The server is already in the user's tailnet (`desktop-p5u8dva.tailc36a4b.ts.net`).
`tailscale serve` proxies a tailnet-only URL to `127.0.0.1:8321`:

- New env `TRADINGAGENTS_WEBUI_ALLOWED_HOSTS` adds the tailnet name to the
  Host allowlist (webui R4).
- **Write endpoints** (POST: halt toggle, manual trigger, config) accept only
  loopback Host names, so the tailnet URL is read-only; the halt switch
  stays reachable via `ssh dev`.
- Works from a phone with the Tailscale app; the SSH tunnel keeps working.
- AWS is not needed. A public, no-Tailscale link would mean publishing a
  read-only static export (S3 + CloudFront + auth); out of scope unless
  asked.

## Prior art (specs/README.md rule 6)

- **P1 (event log)** predates rule 6 and adds no mechanism from the rule's
  list: it is an internal append-only JSONL log on the existing
  `locked_append` (the same primitive as `orders.jsonl`), with no external
  integration. No survey was done; none is claimed.
- **P2 (snapshot builder)** is a new component but adds no mechanism from
  the rule's list: a pure, offline fold of files the pipeline already
  writes, reusing the components' own rules and readers (`read_pool`,
  `load_decision_records`, `non_candidate_reason`, `current_outcomes`,
  `read_core_yaml`), no external integration or dependency. No survey was
  done; none is claimed.
- **P3 (R5 Tailscale serve)** is an external integration: survey prior art
  (Tailscale's own serve/funnel docs and 2–3 maintained projects exposing a
  loopback app over a tailnet) and record it here before P3 is built.

## Rollout

1. **P1 — record**: R1 emitters + R2 archive. Not merged before the first
   live paper session (Mon 2026-10-26 09:45 ET) has run, since it touches the
   adapter; that day is covered by backfill.
2. **P2 — snapshot**: R3 builder, orchestrator hook, backfill from 10-14.
3. **P3 — page**: R4 page + aggregate + CSV, R5 Tailscale serve.

Each phase: deterministic gate + Claude review (new component, contracts,
orchestrator, adapter, webui write-endpoint security).

## Acceptance criteria

1. Every exclusion in the gap table emits exactly one R1 event with a code
   from the table, from fixture slots covering each code; an event-write
   failure changes no component output or exit code.
2. The snapshot of a fixture slot lists every nominated, core and pool
   ticker exactly once with the right furthest stage and code; identical
   inputs give a byte-identical snapshot.
3. Backfill of a fixture slot without events marks the affected stages
   `reconstructed`/`unknown`, never invents a code.
4. A funnel build failure is a `warn` and never fails a slot or execute.
5. Webui: pages render from fixtures; a tailnet Host can GET but every POST
   from a non-loopback Host is rejected; a foreign Host is still 400.

## Later (not in this spec)

Forward 1/5/20-day returns for every funnel ticker, excluded ones included,
to compare cohorts (e.g. tickers cut by the 7.0 trigger vs those that ran).
