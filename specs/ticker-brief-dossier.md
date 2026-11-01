# Spec: Ticker brief dossier + daily delta (cross-day dedup)

**Builder**: Claude Code · **Status**: Draft (2026-11-01) — implement after
the Haiku-collection comparison (≈2026-11-04) and ≥5 trading days of
llm-cost R11 query logs as the baseline.

## Why

Measured on 309 ticker briefs (10-16..10-30, Sonnet collection):

- ≈13.4 web searches per brief (10-30: ≈390 searches / 29 briefs) yield
  10.3 distinct cited sources;
- of the 1 246 cited URLs that carry a date, **77 % are older than 7 days**
  (interim results, financing, listing plans, analyst targets), while the
  protocol's focus is the trailing 48 hours;
- only 26 % of those old URLs equal one cited the day before: the same
  background facts are re-found through *different* pages every day, so
  URL- or query-level dedup cannot remove the work;
- the evaluator (Opus, $4.73 on 10-30 — the largest single cost) re-verifies
  that same background every day.

The fix is structural: research the slow-moving background once, then
research only what changed.

## Design

### D1 — Dossier (contract change first: ticker-brief-data-contract)

`ticker_briefs/<TICKER>/dossier.md` — the background half of a brief:
same five sections, same citation rules, frontmatter `as_of_date`,
`generated_at`, `generator`, `sources_count`, plus `refresh_reason`.
Revisions are archived like briefs (never overwritten in place). It is
written by a **dossier run**: today's full research protocol (prompt
`ticker_dossier.md`, unchanged depth, no 48-hour focus) and evaluated by
the evaluator like a brief (`dossier.eval.json`). A dossier whose eval is
`fail` or missing is not usable.

### D2 — Delta brief

A daily brief for a ticker with a usable dossier is a **delta run**: the
prompt carries the dossier text and asks for searches on the trailing
48 hours and calendar changes only (cap: R10's `N`, default 5). The output
is the same five-section brief the analysis reads today:

- new bullets with fresh citations;
- background bullets copied **verbatim** from the dossier, each suffixed
  `[dossier YYYY-MM-DD]`;
- frontmatter `basis: delta`, `dossier_as_of`, and
  `dossier_conflict: true|false` (a fresh fact contradicts the dossier).

A ticker without a usable dossier gets today's full run (`basis: full`).
Downstream readers (`get_ticker_brief`) need no change.

### D3 — Refresh triggers (dossier run instead of delta)

1. no usable dossier (new opportunity ticker, failed eval);
2. dossier older than 7 calendar days;
3. the previous delta set `dossier_conflict: true`;
4. a dated catalyst in the dossier's calendar passed since it was written
   (earnings, AGM, lock-up expiry);
5. |1-day move| ≥ 8 % (threshold configurable) since the dossier.

At most one refresh per ticker per day; refreshes are spread so no slot
refreshes more than ⅓ of its tickers (stagger by ticker hash).

### D4 — Evaluation

- Dossier: full evaluation on each revision.
- Delta brief: the LLM grades **only the non-carried bullets**. Carried
  bullets are checked **deterministically**: each must match a dossier
  bullet byte for byte (after the suffix is stripped); any mismatch fails
  the brief's `consistency` and is listed as a flagged claim. This removes
  the daily re-verification of background from the Opus bill.

### D5 — Optional: no-news carry-forward

Before a delta run, a free check (yfinance news for the trailing 48 h) with
zero items and no calendar event today → the previous brief is carried
forward as today's (`basis: carried`, no LLM call). Off by default;
enabled only after D1–D4 have run a week.

## Expected effect (to be measured with llm-cost R11)

Per ticker per day: ≈4–5 delta searches + ≈12 dossier searches / 7 days
≈ 6–7 searches (from ≈13.4); evaluator input tokens on delta days roughly
halved. These are estimates; the acceptance gate is the measurement below.

## Risks

- **Error propagation**: a wrong dossier fact is copied daily. Mitigated by
  D1 (only eval-passed dossiers), D3.2 (7-day expiry), D3.3 (conflict
  refresh), and the evaluator still seeing carried bullets in context.
- **Missed slow change**: a background fact changes without a 48-hour news
  item — bounded by the 7-day expiry.
- **Quality comparison**: rolled out on half the tickers (hash split) for
  one week; compare eval verdicts, flagged claims, and catalyst scores with
  the full-run half before switching all.

## Prior art (specs/README.md rule 6)

To be completed before implementation (incremental summarisation /
cache-invalidation designs in maintained projects).

## Acceptance criteria

1. Contract updated first; dossier and delta briefs validate under the
   strict models; `basis: full` briefs are unchanged.
2. Every refresh trigger (D3) has a test; no ticker refreshes twice a day.
3. Carried bullets that differ from the dossier fail deterministically.
4. Over the trial week: searches per brief and evaluator tokens per brief
   fall, with eval `fail` rate and mean flagged claims no worse than the
   full-run half.
