# Spec: Decision funnel (per-slot exclusion record + webui page)

**Builder**: Claude Code · **Status**: Draft (2026-10-25, awaiting user
sign-off) · Contracts: new `funnel` event + snapshot schemas (this spec);
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
  code per stage (table below) for aggregation; `reason` is human text.
- Emitters are best-effort: an event-write failure logs a warning and never
  changes the component's behavior, exit code or outputs.
- No existing contract changes: pool files, `DecisionRecord` (`extra=forbid`)
  and `OrderRecord` stay as they are. Everything new goes in the event log.

| Stage (component) | `code` values |
|---|---|
| `nominate` (pool_builder) | `nominated`, `wrong-session`, `duplicate`, `nomination-failed` (+ `carried_forward`) |
| `pool` (pool_builder) | `core`, `opportunity-enter`, `opportunity-stay`, `watch`, `dropped-low-score`, `dropped-gate-fail`, `cap-truncated`, `watch-cap`, `removed-low-score-streak`, `removed-gate-fail-streak`; `details.gate_rule`: `insufficient-bars`, `liquidity`, `bollinger-*`, `volume-demotion`, `fetch-error`, plus the thresholds in force |
| `collect` (ticker_collector) | `written`, `exists`, `failed` (+ reason), `not-collected-watch`, `opportunity-dropped-stale-pool` |
| `evaluate` (evaluator) | `pass`, `warn`, `fail`, `skipped-below-threshold`, `skipped-absent` |
| `trigger` (analysis_runner) | `core`, `core-pair`, `catalyst`, `no-score`, `eval-fail`, `below-threshold`, `budget-dropped`, `already-done`, `macro-fail-feeds-only` |
| `decide` (analysis_runner) | `BUY`, `SELL`, `HOLD`, `ERROR`; `details`: `arm`, `run_id`, `pm_rating`, `plan_valid`, `violations[]`, `error` |
| `execute` (execution_adapter) | non-candidates `hold`, `feeds-arm`, `plan-invalid`, `action-mismatch`, `incomplete-plan`, `superseded`; every existing skip reason (`stale-quote`, `total-risk`, …); `submitted` (`details`: live/dry, broker status, qty, prices) |

### R2 — Component output archive (safety net)

The orchestrator saves each component's final stdout JSON summary line and
the last 64 KB of stderr to
`~/.tradingagents/funnel/<session>/<date>/<component>.out.json`, so a reason
no emitter covers is still kept. Overwritten on a rerun of the same
component; failures to write are log-only.

### R3 — Funnel snapshot (one file per slot)

`python -m pipeline.funnel build --session S --date D` writes
`~/.tradingagents/funnel/<session>/<date>.json` (atomic, deterministic for
the same inputs):

- `context`: macro verdict, pool `carried_forward`, the thresholds in force
  (trigger 7.0, entry 6.0, exit 4.0, caps, from `config_snapshots`), input
  file hashes.
- `counts`: tickers reaching each stage.
- `tickers[]`: one entry per ticker ever considered: `furthest_stage`,
  `final` (`order-live` / `order-dry` / `excluded`), the `code` + `reason` at
  the stage where it stopped, `provenance` (`recorded` from R1 events,
  `reconstructed` from pool/brief/ledger/order files, `unknown`), and the full
  per-stage trail (both arms for paired runs).
- Built automatically at the end of a cn slot and after the us `execute`
  (also at the end of the us slot, rebuilt after execute). A build failure is
  a `warn` on a new status component `funnel`, never a slot failure.
- `python -m pipeline.funnel backfill --since 2026-10-14` rebuilds past
  slots from files alone; stages without events read `reconstructed` or
  `unknown` (e.g. which gate rule fired, why a plan was invalid).

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
