# Macro Brief Data Contract (v1)

The single shared interface between the collector (writer), the evaluator
(reader + `*.eval.json` writer), and the TradingAgents pipeline (reader).

## Location & naming

- Directory: `~/.tradingagents/macro_briefs/`
  - Pipeline override: env `TRADINGAGENTS_MACRO_BRIEF_DIR` (config key `macro_brief_dir`).
  - Collector and evaluator honor the same env var.
- Brief file: `YYYY-MM-DD.md` — the date is the **as-of trading day** the brief
  was generated for (generation-local calendar date).
- Evaluation file: `YYYY-MM-DD.eval.json`, same directory, written only by the evaluator.
- Writers write **atomically** (temp file + rename). One brief per day; a rerun
  the same day overwrites whole files, never appends.

## Brief format

Markdown, English, YAML frontmatter. Target length 800–1500 words (soft
quality target — the collector enforces hard bounds of 500–2500; word count is
NOT part of structural validity, which is defined by hard requirements 1–5
below):

```markdown
---
as_of_date: 2026-07-28
generated_at: 2026-07-28T09:30:00Z   # ISO 8601 UTC, actual generation time
generator: claude-deep-search        # or codex-deep-search / model id
sources_count: 14                    # number of distinct cited URLs
---

## Monetary Policy & Rates
...claims, each with an inline citation [Reuters](https://...)...
**Impact**: bearish — one-line rationale.

## Growth & Earnings
...

## Geopolitics & Trade
...

## Global Liquidity & FX
...

## Commodities & Supply Chains
...

## China & Asia
...

## Surprises & Watchlist
- unexpected events of the last 24h and what to watch next, with citations
```

Hard requirements (the evaluator scores against these; the collector validates
before writing):

1. All 7 `##` sections present, exact titles above, in order.
2. Every factual claim carries an inline markdown citation to its source URL.
3. Each of sections 1–6 ends with an `**Impact**:` line
   (`bullish` / `bearish` / `neutral` / `mixed` + one-line rationale).
4. Frontmatter fields all present; `as_of_date` matches the filename.
5. Content covers the trailing ~48h with weekly context; no forward-dated claims.

`sources_count` must equal the number of distinct cited URLs in the body; the
collector verifies this by counting body URLs itself (≥ 8 required) — consumers
never trust the self-reported number. "Contract structure" (what the evaluator
refuses on) means hard requirements 1–5 only; word count and the citation floor
are collector quality gates.

## Evaluation format (`YYYY-MM-DD.eval.json`)

```json
{
  "as_of_date": "2026-07-28",
  "evaluator": "gpt-5.6-terra",
  "evaluated_at": "2026-07-28T10:05:00Z",
  "scores": {
    "factual_accuracy": 8.5,
    "citation_support": 9.0,
    "coverage": 7.0,
    "timeliness": 8.0,
    "consistency": 9.5
  },
  "flagged_claims": [
    {
      "section": "China & Asia",
      "claim": "…",
      "issue": "citation does not mention the 13.4% figure",
      "severity": "major"
    }
  ],
  "verdict": "pass",
  "notes": "free text"
}
```

- Scores are 0–10 floats. `verdict` ∈ `pass` | `warn` | `fail`
  (fail = any fabricated claim, or factual_accuracy < 5).
- `severity` ∈ `minor` | `major` | `fabrication`.

## Staleness (pipeline behavior, informative)

The pipeline reads the newest brief with date ≤ the analysis date and prepends
a WARNING header when the gap exceeds 3 calendar days. Consumers must surface
the brief's `as_of_date`; they never silently treat stale data as current.

## Versioning

This is v1. Breaking changes bump the version here and update every consumer in
the same change set.
