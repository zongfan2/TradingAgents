# Ticker Brief Data Contract (v1)

The shared interface between the ticker-brief collector (writer), the ticker
evaluator (reader + `*.eval.json` writer), and the TradingAgents pipeline
(reader). Mirrors the macro-brief contract; differences are called out.

## Location & naming

- Directory: `~/.tradingagents/ticker_briefs/<TICKER>/`
  - Env override for the base dir: `TRADINGAGENTS_TICKER_BRIEF_DIR`
    (config key `ticker_brief_dir`).
  - `<TICKER>` is the pipeline symbol verbatim (`NVDA`, `0700.HK`, `600519.SS`).
- Brief file: `YYYY-MM-DD.md` — the session trading day it was generated for.
- Evaluation file: `YYYY-MM-DD.eval.json`, same directory, written only by the
  evaluator. Full schema (macro eval schema plus identity fields):

  ```json
  {
    "as_of_date": "2026-08-03",
    "ticker": "NVDA",
    "session": "us",
    "brief_sha256": "…",
    "brief_generated_at": "2026-08-03T12:41:00Z",
    "evaluator": "gpt-5.6-terra",
    "evaluated_at": "2026-08-03T13:00:00Z",
    "scores": {"factual_accuracy": 8.0, "citation_support": 8.5,
               "coverage": 7.5, "timeliness": 8.0, "consistency": 9.0},
    "flagged_claims": [{"section": "Risks", "claim": "…", "issue": "…", "severity": "minor"}],
    "verdict": "pass",
    "notes": "free text"
  }
  ```

  The macro contract's **revision binding** rule applies identically: an eval
  is valid only when `brief_sha256` matches the brief being served; mismatch
  ⇒ `missing` and re-evaluation.
- Atomic writes; one brief per ticker per day; a same-day rerun replaces the
  file after moving the existing one to `archive/<name>.<generated_at>.md`
  (immutable revisions, same as the macro contract).
- **Session uniqueness invariant**: a ticker belongs to exactly one session,
  derived from its symbol suffix (`.SS`/`.SZ`/`.HK` ⇒ `cn`, else `us`), and
  only that session's slot ever collects it — so `YYYY-MM-DD.md` is unique per
  ticker/day by construction. The frontmatter `session` is denormalized
  metadata, not a partition key; no session component is needed in the name.

## Brief format

Markdown, English, YAML frontmatter. Target 400–1200 words (collector enforces
hard bounds 250–2000; word count is NOT part of structural validity):

```markdown
---
as_of_date: 2026-08-03
ticker: NVDA
session: us
generated_at: 2026-08-03T12:40:00Z
generator: claude-deep-search
sources_count: 7
catalyst_score: 7.5          # 0-10 float, see scale below
catalyst_type: earnings      # earnings|product|regulatory|M&A|guidance|flow|macro-exposure|other
catalyst_window: 2026-08-27  # date or date range of the primary catalyst; "none" if none
---

## Company Developments (48h)
...claims, each with an inline citation [Reuters](https://...)...

## Catalysts & Calendar
...upcoming dated events: earnings, product launches, regulatory decisions...

## Supply Chain & Competitors
...

## Institutional Views & Positioning
...analyst actions, notable flows, short interest changes...

## Risks
...

**Impact**: bullish — one-line rationale for the marginal 48h read on this name.
```

Hard requirements (evaluator scores against these; collector validates before
writing):

1. All 5 `##` sections present, exact titles above, in order, followed by a
   final `**Impact**:` line (`bullish|bearish|neutral|mixed` + one-line
   rationale).
2. Every factual claim carries an inline markdown citation to its source URL;
   ≥ 5 distinct cited URLs counted from the body (`sources_count` must equal
   that count — consumers never trust the self-reported number).
3. Frontmatter fields all present; `as_of_date` matches the filename; `ticker`
   matches the directory name.
4. Content covers the trailing ~48h with weekly context; no forward-dated
   claims (the catalyst calendar lists *scheduled* future events, which is not
   a forward-dated claim).
5. `catalyst_score` reflects the strength/imminence of tradeable catalysts:
   0–3 nothing actionable · 4–6 notable but not imminent · 7–8 strong dated
   catalyst inside ~2 weeks · 9–10 imminent, high-impact (≤ 48h). The score
   drives analysis triggering, so inflation is a quality defect the evaluator
   flags.

## Staleness (pipeline behavior, normative)

Reading for analysis date D: newest brief with date ≤ D.
- gap = 0: serve as-is.
- gap = 1 calendar day: serve with a prominent WARNING header.
- gap ≥ 2 calendar days: treat as **absent** (`VendorNotConfiguredError` →
  standard DATA_UNAVAILABLE degrade). Ticker news decays faster than macro;
  a fresh weekday collection makes gap ≥ 2 a failure signal, not a weekend
  artifact.

## Versioning

v1. Breaking changes bump the version and update every consumer in the same
change set.
