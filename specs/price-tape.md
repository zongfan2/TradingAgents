# Spec: Verified price tape for ticker briefs and their evaluation

**Builder**: Claude Code · **Status**: Implemented (2026-11-05)

## Why

Checked against the actual closes, the 2026-11-02/03 ticker briefs got their
own recent price action wrong, and the evaluator could only catch it by
searching:

- INTC 11-03: a "+2.4% pre-market bounce" on a day it closed **−8.80%**;
- SNDK 11-03: the post-earnings **−13.47%** session (Oct 30) left out, the
  Oct 29 regular-session drop called a reaction to results released after
  the close;
- MU 11-03: a "steady ~10% decline" through a +5.86% rebound to 983.23;
- AAPL 11-02: "could not verify" the 29/30 Oct closes;
- LITE 11-03: an −18.59% day while the brief still scored a results print
  already out as the pending catalyst.

Closes are not research: web search is an unreliable and expensive source
for them (each search is $0.01, and the collector is capped at 10).

## Design

### P1 — The tape

`pipeline.price_tape.render_tape(ticker, as_of)`: the last 10 daily closes
**strictly before** `as_of` (no partial day, no look-ahead) with the
session-over-session change, from yfinance daily history
(`auto_adjust=False`, the pool builder gate's source; yfinance still
split-adjusts every Close before a split, so any split in the fetched
~2 months, inside the window or after a past `as_of` (backfill, re-evaluation),
makes the tape unavailable rather than show rescaled closes). Sessions in the last
3 with |change| ≥ 5% (as shown, to 2 decimals) are marked ⚠; closes below 1
are shown to 4 decimals, others to 2. Non-positive or NaN closes are left out,
and the session after one shows no change (a move across the gap spans two
sessions); so does the session after a weekday with no bar at all (a
suspension or a holiday, which the weekday calendar cannot tell apart: one
change figure lost, never a two-session move shown as one).
Any failure, fewer than 2 sessions, or a tape whose newest session is not
the last weekday before `as_of` renders a fixed "unavailable" line: a tape
missing even one session (late data, an outage, a halt) would hide the most
relevant move behind "authoritative". yfinance lists a finished session with
a NaN close until its data is complete (measured 2026-11-05 00:28 Shanghai:
000001.SS and 600256.SS both at 11-04 = NaN), and data can lag for a ticker
and its whole market together, with no bar listed at all — so the rule is the
weekday calendar, not a comparison with the market's own data (a benchmark
check, tried in review, cannot see a market-wide lag and became redundant).
A holiday therefore costs the first tape after it (about ten days a year per
market): a missed tape, never one a session short whose missing session the
evaluator would later hold the brief to. It never raises and never blocks a
brief or an evaluation. Tests are offline: an autouse fixture makes the fetch
fail.

The collector's block (`for_writer=True`) carries the writer's rules. A
tape figure is cited as `[price tape]`, a valid citation without a URL (the
ticker-brief contract, hard requirement 2, allows it; it does not count
toward the ≥ 5 cited URLs). The closes are authoritative: never contradict
them, do not search for past closes or daily moves. Each ⚠
session gets the **first** searches (one targeted search per ⚠ session for
its driver), is explained in Company Developments (48h) with a cited driver
or written as "driver unverified" only if that search found nothing, and is
never described in the opposite direction or omitted.

### P2 — Collector

`prompts/ticker_deep_search.md` gains a "Verified price tape" section
(`{{PRICE_TAPE}}`) after the seed. The collector renders it per job with the
brief's `as_of`. The `[price tape]` citation rule (`{{TAPE_CITATION_RULE}}`) is
rendered only when the tape is: a writer without one is never told it may
cite it. Likewise the sourcing rule ("numbers must come from your searches")
gains the tape as a source for past closes and daily moves
(`{{TAPE_NUMBERS_RULE}}`) only when there is one.

### P3 — Evaluator

Ticker evaluations get the same tape facts (`as_of` = the brief's,
`for_writer=False`: the table and the ⚠ list, none of the writer's rules) as
a reference block with the evaluator's own rules; a `[price tape]` figure
that matches the tape is correctly cited. The ticker evaluator's citation
rules always state that `[price tape]` is a valid URL-less citation (checked
against the tape block when there is one, else by search), so a brief
collected with a tape is never penalised for it when the evaluator's own
fetch fails. The ticker eval report records `price_tape_newest` (the
tape's newest session, null without one) and `price_tape_sha256` (a digest
of its table rows; the collector logs the digest of its writer's tape), so a
verdict reached with or without a tape, or on a tape the writer did not
see, can be told apart. When the tape is unavailable the
block is left out entirely and logged — the evaluator then checks prices by
its own searches, as before. A statement that contradicts the tape (wrong close, wrong direction, a
rebound described as a decline) is a **major** factual_accuracy flag.

A ⚠ move neither explained nor marked "driver unverified" is a **major**
coverage flag only when the brief cites `[price tape]` at least once (any
case or spacing). The collector and the evaluator read market data at
different times (a cn session listed with a NaN close at collection may be
complete at evaluation), and that citation is the evidence that the writer
had a tape. A brief with no `[price tape]` citation, whether its writer had
no tape or had one and cited none of it, is scored on an unexplained ⚠ move
as an ordinary coverage miss; a contradiction is still major.

The evaluator does not search for the session closes and moves in the tape.
It still checks pre-market, after-hours, intraday and `as_of`-session moves,
which the tape does not hold, by search. Macro briefs
are unchanged.

Citation severity (ticker evaluations only): a cited URL that does not contain the claim is now
**minor** when the evaluator verified the claim elsewhere (and says so),
**major** when it could not verify it or found it wrong; a URL that
contradicts the claim stays major. Measured on 23 briefs, many Opus "major"
flags were right facts behind the wrong page.

## Measured (2026-11-04/05, live, scratch dir; Haiku collector, cap 10, Opus eval)

| brief (11-03) | before: verdict / factual / majors | after |
|---|---|---|
| INTC | warn / 7.0 / 3 | **pass / 8.0 / 0** (both ⚠ days reported, Oct 30 driver cited) |
| MU | warn / 6.0 / 4 | warn / 7.0 / **1** (no price error; both ⚠ drivers cited) |
| SNDK | warn / 7.5 / 3 | warn / 6.0 / 3 (prices right; driver attribution still weak) |

Price statements contradicting the tape: none in the three new briefs.
Collector cost per brief ≈ $0.13 with 10 searches (from ≈ $0.25).

## Acceptance criteria

1. The tape never includes the `as_of` session or later; ⚠ marks only the
   last 3 sessions at |change| ≥ 5%; failures render "unavailable".
2. The collector prompt and the ticker evaluation prompt carry the same
   tape when both reads agree (each reads at its own run time); macro evaluation prompts carry none.
3. The evaluator prompt states the tape and citation severity rules.
4. No test reaches the network.
