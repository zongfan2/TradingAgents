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
(`auto_adjust=False`, the pool builder gate's source). Sessions in the last
3 with |change| ≥ 5% are marked ⚠. Non-positive or NaN closes are ignored.
Any failure (or fewer than 2 sessions) renders a fixed "unavailable" line;
it never raises and never blocks a brief or an evaluation. Tests are
offline: an autouse fixture makes the fetch fail.

The block carries its rules: the closes are authoritative — never
contradict them, do not search for past closes or daily moves — and each ⚠
session gets the **first** searches (one targeted search per ⚠ session for
its driver), is explained in Company Developments (48h) with a cited driver
or written as "driver unverified" only if that search found nothing, and is
never described in the opposite direction or omitted.

### P2 — Collector

`prompts/ticker_deep_search.md` gains a "Verified price tape" section
(`{{PRICE_TAPE}}`) after the seed. The collector renders it per job with the
brief's `as_of`.

### P3 — Evaluator

Ticker evaluations get the same tape (`as_of` = the brief's) as a reference
block. A statement that contradicts the tape (wrong close, wrong direction,
a rebound described as a decline) is a **major** factual_accuracy flag; a ⚠
move neither explained nor marked "driver unverified" is a **major**
coverage flag; the evaluator does not search for these prices. Macro briefs
are unchanged.

Citation severity: a cited URL that does not contain the claim is now
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
   tape; macro evaluation prompts carry none.
3. The evaluator prompt states the tape and citation severity rules.
4. No test reaches the network.
