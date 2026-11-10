# Pool Data Contract (v1)

The shared interface between the pool builder (writer) and the ticker-brief
collector / analysis runner / webui (readers).

## Location & naming

- Directory: `~/.tradingagents/pools/<session>/` where `session` ∈ `cn` | `us`.
  - Env override for the base dir: `TRADINGAGENTS_POOL_DIR` (config key `pool_dir`).
- Pool file: `YYYY-MM-DD.json` — the session's trading day it was generated for.
- Core-layer source of truth (user-maintained, builder reads, **never writes**):
  `~/.tradingagents/pools/core.<session>.yaml` — a list of entries
  `{ticker, note?}`. Tickers use pipeline symbols (`NVDA`, `0700.HK`, `600519.SS`).
- Writers write atomically (temp file + rename). One pool per session per day;
  a same-day rerun replaces the whole file after moving the existing one to
  `archive/<name>.<generated_at>.json` (immutable revisions — ledger
  `pool_sha256` always resolves to preserved content).

## Pool file schema

```json
{
  "as_of_date": "2026-08-03",
  "session": "us",
  "generated_at": "2026-08-03T12:31:00Z",
  "generator": "claude-deep-search",
  "carried_forward": false,
  "core": [
    {"ticker": "NVDA", "note": "holding", "score": 6.8}
  ],
  "opportunity": [
    {
      "ticker": "AVGO",
      "score": 7.5,
      "catalyst_type": "earnings",
      "rationale": "one-line reason with the catalyst and timing",
      "citations": ["https://..."],
      "entered_on": "2026-08-01",
      "low_score_streak": 0,
      "gate_fail_streak": 0,
      "technical": {
        "gate": "pass",
        "boll_daily": {"close": 291.2, "mid": 285.1, "upper": 301.4, "lower": 268.8},
        "boll_weekly": {"close": 291.2, "mid": 262.0, "upper": 315.5, "lower": 208.4},
        "avg_dollar_volume_20d": 41200000.0,
        "volume_ratio_5d_20d": 1.35
      }
    }
  ],
  "watch": [
    {"ticker": "MRVL", "score": 6.2, "catalyst_type": "product",
     "rationale": "…", "citations": ["https://…"], "technical": {"gate": "watch"}}
  ],
  "removed": [
    {"ticker": "SMCI", "reason": "score<4.0 for 3 sessions", "last_score": 3.1}
  ]
}
```

Hard requirements:

1. `core` mirrors `core.<session>.yaml` tickers/notes verbatim (order
   preserved). The builder never drops or reorders the yaml's core entries;
   it MAY annotate them with informative `score`/`technical` fields
   (renderable, never gating — core coverage is unconditional). The one
   addition (user decision 2026-11-09): after the yaml entries, every open
   position of the session's market in the ledger's `positions.json` (a row
   with a filled quantity — the adapter writes one per broker position; a
   row carrying only open orders would count too) that the yaml does not list is
   appended, in ticker order, with the note `held position (auto-added while
   held)` — so a name bought from the opportunity layer is collected and
   analysed every slot until it is closed. Its opportunity entry folds into
   core (no cross-layer duplicate). Only in a session whose core is analysed
   (`core_analysis_sessions`; elsewhere core gets no brief and no analysis,
   so the held name keeps its opportunity coverage, which the normal exit
   rules can still end — acceptable while no core-off session trades). A missing snapshot adds
   nothing; an unreadable snapshot or config, or a symbol outside the ledger
   ticker charset, adds nothing for it with a stderr warning — never a failed
   build. When no pool file exists, consumers' fallback to
   `core.<session>.yaml` alone has no held names (the degraded mode the
   reading rule already accepts). A snapshot not refreshed on the run date
   (the session's calendar date; every slot's settle step refreshes it
   first) is still used, with a stderr warning: a position opened since its
   last refresh is then missing until the next good refresh. Rebuilding a
   past date uses the current snapshot, with a warning. The fold of a held
   name's opportunity/watch entry into core is case-insensitive. Symbols
   are the broker's position symbols (the namespace the runner's position
   context already assumes): a class share the broker and the yaml spell
   differently (BRK.B / BRK-B) would be listed twice — none is in any pool
   today.
2. Every `opportunity`/`watch` entry has `score` (0–10 float), `catalyst_type`
   (`earnings|product|regulatory|M&A|guidance|flow|macro-exposure|other`),
   `rationale`, ≥1 citation URL, and a `technical` block with `gate`.
3. `opportunity` entries additionally carry `entered_on`,
   `low_score_streak`, `gate_fail_streak` (ints ≥ 0) — hysteresis state lives
   in the pool file itself; the builder updates them from the most recent pool
   file dated **strictly before** the run date (so gaps don't reset streaks
   and a same-day rerun doesn't double-advance them).
4. Caps (config, defaults): `core` ≤ 10 (soft — warn only, core is
   user-controlled), `opportunity` ≤ 5, `watch` ≤ 10. The builder truncates
   `opportunity`/`watch` lowest-score-first and logs what was dropped.
5. All tickers valid pipeline symbols for the session's market; no duplicates
   within or across layers (core wins over opportunity, opportunity over watch).
6. `carried_forward` (optional, default false): true when nomination failed and
   the builder carried the opportunity/watch layers forward from the most
   recent prior pool file. Consumers surface it as a degraded-data warning.

## Lifecycle rules (normative for the builder)

- Streaks count **generated pool files**, not calendar days: hysteresis state
  loads from the most recent prior pool file for the session, so weekends,
  holidays, and failed days neither reset nor advance a streak.
- **Enter** opportunity: technical gate `pass` AND score ≥ `pool_entry_threshold`
  (default 6.0). Entry is immediate — opportunities are time-sensitive.
- **Exit** opportunity (to `removed`): `low_score_streak` ≥ 3 (score below
  `pool_exit_threshold`, default 4.0, in 3 consecutive generated pools) OR
  `gate_fail_streak` ≥ 2. Exit is slow — no daily thrash.
- An existing opportunity member **absent from the nomination output** keeps
  its last score and counts as below the exit threshold for that pool's
  `low_score_streak` (silence is decay, not deletion).
- `watch`: gate `watch`, or score in the [exit, entry) band, or gate `fail`
  with score ≥ entry threshold (strong narrative, broken structure — worth
  watching, not entering); never triggers collection or analysis; promoted
  only via the normal enter rule. Gate `fail` with score < entry threshold is
  dropped entirely.
- A ticker in `removed` may re-enter on a later day via the normal enter rule.

## Reading rule (consumers, normative)

Consumers read the **newest pool file with date ≤ the analysis date** for the
session. Gap ≥ 1 day ⇒ proceed with a prominent staleness warning; gap >
`pool_max_staleness_days` (default 3) ⇒ the pool is **absent**: the
opportunity layer is treated as empty, while core coverage falls back to
`core.<session>.yaml` directly — holdings never lose brief coverage because a
builder broke. The ledger's `inputs.pool` records the path/date actually
consumed, so a stale-pool run is visible in the audit trail.

## Consumers

- Ticker-brief collector: collects for `core` ∪ `opportunity` only.
- Analysis runner: core = always run; opportunity = run when the ticker brief's
  `catalyst_score` ≥ `analysis_trigger_threshold` (default 7.0).
- webui: renders all four lists with scores and gate snapshots.

## Versioning

v1. Breaking changes bump the version and update every consumer in the same
change set.
