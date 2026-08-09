# Spec: WebUI Pages (pipeline v2)

**Builder**: Claude Code · **Status**: Open · Contracts: all (read-only
renderer; two write actions listed below). Extends the existing local
`webui/server.py` app.

## Pages / additions

### 1. Status banner (home)

Renders both `pipeline_status.<session>.json` files: per-component red/green
(`ok` green, `warn` amber, `failed`/`timeout` red, `skipped` grey), last slot
date/time per session, and a staleness rule — a session with no slot started
by `slot_time` + 1h on its local weekday renders red. Shows the latest
end-of-slot summary line.

### 2. Pool view

Renders the latest pool file per session: all four lists (core, opportunity,
watch, removed) with scores, catalyst types, rationale, gate verdicts and the
Bollinger snapshot values. Core entries display their informative score when
present. `carried_forward: true` renders a visible warning badge.

### 3. Briefs view

Lists macro and ticker briefs by date/session with their eval verdicts
(`pass`/`warn`/`fail`/missing); clicking renders the markdown and the eval's
flagged claims.

### 4. A/B aggregates

Computes the metrics **exactly as defined in the ledger contract's "A/B
aggregate definitions" section** (direction agreement, hit rates, excess
return, plan quality, eval weighting) on demand from the three ledger files,
grouped by arm with an optional preset filter, plus a paired-rows-only toggle.
No aggregate is ever persisted.

### 5. Decisions & orders

Table of decision rows joined with their order state (execution derived per
the ledger contract join rule) and outcomes; filter by date/session/ticker/arm.

## Write actions (the only two)

- **Manual analysis trigger**: a button that shells out to the analysis
  runner for a chosen ticker (`trigger: manual`). The page states that
  manual runs are never auto-executed (adapter S5).
- **Execution halt toggle**: creates/removes `~/.tradingagents/EXECUTION_HALT`
  and shows its current state prominently on every page while halted.

The webui never writes briefs, pools, ledger rows, or status files, and never
talks to the broker.

## Requirements

- **R1**: all reads tolerate missing files (pre-first-run state renders as
  "not yet run", never a 500).
- **R2**: aggregate computation is a pure function over ledger rows, unit-
  tested against fixture ledgers with known expected numbers (including the
  eval-weighted and paired-only variants and ambiguous-replay exclusion).
- **R3**: no external network calls; everything renders from local files.
- **R4**: the server binds `127.0.0.1` only — the write actions (manual
  trigger, halt toggle) must not be reachable from the network.

## Acceptance criteria

1. Fixture status/pool/ledger files render each page; staleness and
   carried-forward badges appear under the right fixtures.
2. Aggregate numbers match hand-computed fixtures for both arms, with and
   without eval weighting and pairing filters.
3. Halt toggle round-trips the file and is visible from every page while on;
   manual trigger invokes the runner with the right arguments (subprocess
   faked in tests).
