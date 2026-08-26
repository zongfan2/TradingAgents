# Spec: Pool Builder

**Builder**: Claude Code · **Status**: Open · Contract:
[pool-data-contract.md](pool-data-contract.md)

## Goal

Once per session slot, produce the day's tiered pool file: copy the
user-maintained core layer, nominate opportunity candidates via deep search,
confirm them with a deterministic technical gate (daily + weekly Bollinger),
and apply enter-fast/exit-slow hysteresis. Deep search finds narrative;
price structure must confirm it.

## Stages

1. **Load** — read `core.<session>.yaml` (missing file ⇒ empty core + warn) and
   the most recent pool file for the session dated **strictly before** the run
   date (hysteresis state carries across weekends, holidays, and failed days;
   a same-day rerun never reads its own output; no prior file at all ⇒ fresh
   state).
2. **Nominate** (deep search, subscription CLI): prompt
   `prompts/pool_nomination.md` rendered with `{{DATE}}`, `{{SESSION}}`, the
   current opportunity/watch lists, and the layer caps. The model scans the
   session's market for catalyst-driven candidates (earnings, product,
   regulatory, M&A, guidance, unusual flow) and re-scores existing members.
   Output: JSON list of `{ticker, score, catalyst_type, rationale, citations}`.
   Structural validation + one retry, mirroring the macro collector's R3
   pattern. Core tickers are also re-scored (score is informative for core —
   it never gates coverage).
3. **Technical gate** (deterministic, no LLM): for each nominated non-core
   ticker, fetch daily OHLCV via the existing yfinance vendor, resample to
   weekly, compute Bollinger bands (20, 2σ) on both frames, then apply the
   configured rule set. Default rules (config `pool_gate_rules`, user-tunable —
   these encode strategy, not correctness):
   - **Liquidity floor (hard veto, checked first)**: 20-day average daily
     dollar volume (mean of close × volume, listing currency) ≥
     `min_avg_dollar_volume` — per-session defaults `us: 20_000_000` (USD),
     `cn: 100_000_000` (listing currency: HKD for .HK, CNY for .SS/.SZ; one
     shared cn threshold, tunable). Below the floor ⇒ `fail` regardless of
     everything else: an illiquid name breaks the TradePlan's price
     assumptions at execution time.
   - Bollinger structure: weekly close ≥ weekly lower band (trend not
     broken) AND daily close ≤ daily upper band × 1.02 (not chasing an
     overheated break) ⇒ structure ok; exactly one holds ⇒ `watch`; neither
     holds ⇒ `fail`.
   - **Volume confirmation (soft demotion)**: 5-day average volume ≥ 20-day
     average volume × `volume_confirm_ratio` (default 1.2). A structurally
     passing candidate without the volume confirmation is demoted to `watch`
     — a catalyst narrative with no volume response is either unseen by the
     market or already priced in; volume alone never hard-fails.
   - `fail` also when price history < 60 trading days.
   Record the full snapshot (bands, avg dollar volume, volume ratio) in the
   pool entry regardless of outcome.
4. **Hysteresis + caps** — apply the contract's lifecycle rules and caps
   (including the absent-from-nomination decay rule and the
   gate-fail-high-score ⇒ watch routing); update `low_score_streak` /
   `gate_fail_streak`; move exits to `removed` with a reason.
5. **Write** — validate against the contract, atomic write, append one line
   (date, session, counts per layer, entries/exits) to `pools/builder.log`.

## Requirements

- **R1 — Core is read-only truth**: the builder never edits
  `core.<session>.yaml` and never drops a core ticker from the pool file, even
  when the technical gate would fail it (the gate applies to the opportunity
  path only).
- **R2 — Deterministic gate**: given the same OHLCV, the gate's verdict is
  reproducible; all thresholds come from config, defaults documented here.
- **R3 — Idempotent**: same-day rerun overwrites (hysteresis is computed from
  the most recent pool file dated strictly before the run date — never from
  today's own output — so a rerun is safe); `--force` re-runs the nomination
  search, otherwise a same-day rerun reuses the day's nomination output if
  cached.
- **R4 — Degrade, don't die**: nomination failure after retry ⇒ the builder
  itself writes a pool file carrying the opportunity/watch layers forward from
  the most recent prior pool file (streaks untouched), sets
  `"carried_forward": true`, warns on stderr, and exits 0 (surfaced as `warn`
  status) — ticker collection still covers known names. Only an unreadable
  core file or a write failure is a non-zero exit; on such a hard crash,
  downstream consumers read the most recent prior pool file.
- **R5 — Budget**: at most 1 deep search per slot; re-scoring rides in the
  same prompt as nomination.

## Non-goals

- No position awareness beyond the core file (holdings sync with the broker is
  the execution adapter's world; the user curates core manually).
- No intraday updates; no fundamental screening (a future gate stage).

## Acceptance criteria

1. Offline tests: gate rules on fixture OHLCV (pass/watch/fail each covered,
   including the <60-bars case); weekly resample correctness; hysteresis
   transitions (enter, 3-pool decay exit, 2-pool gate-fail exit, re-entry,
   absent-from-nomination decay, Friday→Monday gap carrying streaks intact);
   carried-forward fallback on nomination failure; cap truncation drops lowest
   scores and logs them.
2. Nomination validation rejects malformed model output and retries once.
3. A full dry run against recorded fixtures produces a contract-valid pool
   file; core entries mirror the yaml verbatim.
