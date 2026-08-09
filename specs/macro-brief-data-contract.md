# Macro Brief Data Contract (v2)

The single shared interface between the collector (writer), the evaluator
(reader + `*.eval.json` writer), and the TradingAgents pipeline (reader).

v2 change (2026-08-03): briefs are **session-scoped** — two per day (`cn`, `us`)
instead of one overwritten file — so ledger rows reference immutable inputs and
the two sessions' information delta stays auditable. Brief structure is
unchanged from v1.

## Location & naming

- Directory: `~/.tradingagents/macro_briefs/`
  - Pipeline override: env `TRADINGAGENTS_MACRO_BRIEF_DIR` (config key `macro_brief_dir`).
  - Collector and evaluator honor the same env var.
- Brief file: `YYYY-MM-DD.<session>.md`, `session` ∈ `cn` | `us` — the date is
  the **as-of trading day** in the session's local calendar
  (`cn` = Asia/Shanghai, `us` = America/New_York).
- Evaluation file: `YYYY-MM-DD.<session>.eval.json`, same directory, written
  only by the evaluator.
- **Legacy v1 files** (`YYYY-MM-DD.md`, no session) remain readable as
  session-agnostic candidates, ranked per the selection rule below.
- Writers write **atomically** (temp file + rename). One brief per session per
  day; a same-day rerun replaces whole files, never appends — and before
  replacing, the writer moves the existing file to
  `archive/<original-name>.<generated_at>.md` (same for eval jsons). Archived
  revisions are immutable: ledger `inputs` hashes always resolve to preserved
  content.

## Brief format

Markdown, English, YAML frontmatter. Target length 800–1500 words (soft
quality target — the collector enforces hard bounds of 500–2500; word count is
NOT part of structural validity, which is defined by hard requirements 1–5
below):

```markdown
---
as_of_date: 2026-07-28
session: us                          # cn | us — must match the filename
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
4. Frontmatter fields all present; `as_of_date` and `session` match the
   filename (legacy v1 files are exempt from `session`).
5. Content covers the trailing ~48h with weekly context; no forward-dated
   claims — asserting outcomes of events that had not occurred as of
   `as_of_date` is a violation; listing *scheduled* future events with their
   dates (the Watchlist's job) is not.

`sources_count` must equal the number of distinct cited URLs in the body; the
collector verifies this by counting body URLs itself (≥ 8 required) — consumers
never trust the self-reported number. "Contract structure" (what the evaluator
refuses on) means hard requirements 1–5 only; word count and the citation floor
are collector quality gates.

## Evaluation format (`YYYY-MM-DD.<session>.eval.json`)

```json
{
  "as_of_date": "2026-07-28",
  "session": "us",
  "brief_sha256": "…",                 // hash of the exact brief revision evaluated
  "brief_generated_at": "2026-07-28T09:30:00Z",
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
- **Revision binding (normative)**: an eval is valid only for the brief
  revision whose content hash equals `brief_sha256`. Consumers hash the brief
  they are about to serve and treat any mismatch as verdict `missing` — a
  re-collected brief silently inheriting its predecessor's `pass` is exactly
  the failure this prevents. A hash mismatch is also the evaluator's signal
  to re-evaluate without `--force`.

## Staleness & session selection (pipeline behavior, normative)

Reading for a ticker of market M on analysis date D: among all briefs with
date ≤ D, rank by **date first** (newest wins); on a date tie prefer the
session matching M (`cn` for A-shares/HK, `us` otherwise), then the other
session, then legacy. Serving anything but a same-day-or-newest
session-matching brief notes the fallback in the served header. A gap over 3
calendar days adds a WARNING header. Consumers must surface the brief's
`as_of_date` and session; they never silently treat stale data as current.

## Versioning

This is v2. Breaking changes bump the version here and update every consumer in
the same change set. History: v1 (2026-07) single daily file, frozen until the
2026-08-03 session-scoping change.
