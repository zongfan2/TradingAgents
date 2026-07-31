# Spec: Macro Brief — Evaluator

**Builder**: Codex · **Status**: Open · Contract: [macro-brief-data-contract.md](macro-brief-data-contract.md)

## Goal

Score each day's brief for accuracy using **GPT-5.6 Terra** (via `codex exec`),
writing `YYYY-MM-DD.eval.json` per the contract. The eval serves two purposes:
(a) catch fabrication/staleness before the brief feeds trading analysis,
(b) provide a quality weight when A/B-comparing brief-arm vs feeds-arm runs.

## Dimensions (scores 0–10)

- **factual_accuracy** — spot-check concrete numbers and events (rates, index
  moves, named meetings) against independent searches, not just the brief's own
  citations.
- **citation_support** — fetch a sample (≥ 5, always including the most
  market-moving claims) of cited URLs and verify each supports the claim it
  anchors. An unreachable URL is `minor`; a URL that contradicts or does not
  contain the claim is `major`; a claim with a fabricated-looking source is
  `fabrication`.
- **coverage** — run an independent "what moved macro markets in the last 48h"
  search; score down for obvious events the brief missed (list them in notes).
- **timeliness** — is the content actually about the trailing 48h/week, or
  recycled older narrative?
- **consistency** — internal contradictions between sections / Impact lines.

## Requirements

- **R1**: input = brief path (default: today's); refuse to run on a file that
  fails contract structure — i.e. the contract's hard requirements 1–5 only,
  not word count or citation floor (that's a collector bug — exit distinctly).
- **R2**: model = `gpt-5.6-terra` through `codex exec` with web search enabled;
  the evaluator must NOT reuse the collector's backend session or context
  (independence).
- **R3**: output `YYYY-MM-DD.eval.json` per contract schema, atomic write,
  idempotent (`--force` to re-evaluate).
- **R4**: `verdict` rule: `fail` if any `fabrication` flag or
  factual_accuracy < 5; `warn` if any `major` flag or any score < 6; else `pass`.
- **R5**: on `fail`, also print the flagged claims to stderr and exit non-zero
  so a scheduling wrapper can alert.
- **R6**: append one line (date, verdict, min-score, flags count) to
  `macro_briefs/evaluator.log`.

## A/B usage (informative)

When comparing pipeline runs, join on `as_of_date`: a brief-arm decision made on
a `warn`/`fail` brief should be excluded or down-weighted; report eval scores
alongside decision outcomes.

## Acceptance criteria

1. Given a valid brief, produces schema-valid eval json; running twice without
   `--force` does not re-spend tokens.
2. A brief with a planted false claim (test fixture) yields a `fabrication` or
   `major` flag and non-pass verdict.
3. A structurally broken brief is refused with the distinct exit code.
