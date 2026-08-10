# Spec: Brief Evaluator (macro + ticker)

**Builder**: Claude Code · **Status**: Open · Contracts:
[macro-brief-data-contract.md](macro-brief-data-contract.md) (v2),
[ticker-brief-data-contract.md](ticker-brief-data-contract.md)

## Goal

Score briefs for accuracy using a web-search-enabled CLI backend — default
**`claude -p`** (identity `claude-eval`) per D19, with **GPT-5.6 Terra** (via
`codex exec`) selectable — writing
the eval json next to each brief per its contract. The eval serves two
purposes: (a) catch fabrication/staleness before a brief feeds trading
analysis (see gating below), (b) provide a quality weight when A/B-comparing
brief-arm vs feeds-arm runs.

## Scope

One evaluator handles both brief kinds; it detects the kind from the
frontmatter (`ticker` present ⇒ ticker brief) and applies the matching
contract's structural definition. Quota policy (enforced by the orchestrator,
not the evaluator): macro briefs are always evaluated; ticker briefs are
evaluated for **every collected core brief** (core runs happen — and may
execute — every slot, so D12 requires their inputs checked) and every
opportunity brief whose `catalyst_score` ≥ the analysis trigger threshold.

## Dimensions (scores 0–10)

- **factual_accuracy** — spot-check concrete numbers and events against
  independent searches, not just the brief's own citations.
- **citation_support** — fetch a sample (≥ 5, always including the most
  market-moving claims) of cited URLs and verify each supports the claim it
  anchors. Unreachable URL = `minor`; URL that contradicts or does not contain
  the claim = `major`; fabricated-looking source = `fabrication`.
- **coverage** — run an independent "what moved this market / this name in the
  last 48h" search; score down for obvious misses (list them in notes).
- **timeliness** — is the content actually about the trailing 48h/week?
- **consistency** — internal contradictions between sections / Impact lines.
  For ticker briefs this includes `catalyst_score` inflation: a score ≥ 7 with
  no dated catalyst inside ~2 weeks in the body is a `major` flag.

## Requirements

- **R1**: input = brief path; refuse to run on a file that fails its
  contract's hard structural requirements (that's a collector bug — exit
  distinctly).
- **R2**: backend selectable — `--backend claude|codex`, default from config
  `eval_backend` (env `TRADINGAGENTS_EVAL_BACKEND`), `claude` per D19. The
  `claude` backend runs `claude -p` with WebSearch/WebFetch (evaluator
  identity `claude-eval`); the `codex` backend runs model `gpt-5.6-terra`
  through `codex exec` with web search enabled (identity `gpt-5.6-terra`).
  Independence: the evaluator must NOT reuse the collector's backend session
  or context, and the eval backend must differ from `collect_backend` — a
  match is a loud stderr warning (never a failure).
- **R3**: output the eval json per the brief's contract schema (including
  `session` for macro, `ticker` for ticker briefs, and always
  `brief_sha256`/`brief_generated_at` of the exact revision evaluated),
  atomic write. Idempotency is **hash-scoped**: skip only when an existing
  eval's `brief_sha256` matches the current brief's hash; a mismatch (the
  brief was re-collected) re-evaluates without `--force`, archiving the stale
  eval per the contract. `--force` re-evaluates even on a hash match.
- **R4**: `verdict` rule: `fail` if any `fabrication` flag or
  factual_accuracy < 5; `warn` if any `major` flag or any score < 6; else `pass`.
- **R5**: a completed evaluation exits 0 **regardless of verdict** (the
  component worked; the content failed) — a `fail` verdict additionally prints
  the flagged claims to stderr, and the orchestrator alerts by reading the
  verdict from the eval json. Non-zero exits are reserved for the structural
  refusal (R1, distinct code) and crashes, which gating treats as `missing`.
- **R6**: append one line (date, session/ticker, verdict, min-score, flags
  count) to `macro_briefs/evaluator.log`.

## Gating (informative — the normative rules live in analysis-runner.md)

Summary: macro `fail` ⇒ the run is forced to the feeds arm and flagged; ticker
`fail` ⇒ the catalyst auto-trigger is suppressed, and on a core run the ticker
brief is **withheld** (DATA_UNAVAILABLE) while the run stays in the brief
bundle — the offline arm never falls back to live news APIs (D7). `missing`
⇒ proceed, tagged `"missing"` in the ledger row. A/B aggregation excludes
`fail` rows per the ledger contract's aggregate definitions.

## Acceptance criteria

1. Given a valid macro brief, produces schema-valid eval json; running twice
   on the same revision without `--force` does not re-spend tokens, while a
   re-collected brief (changed hash) triggers re-evaluation automatically and
   archives the stale eval.
2. Given a valid ticker brief, detects the kind and applies the ticker
   contract (section titles, catalyst_score consistency check).
3. A brief with a planted false claim (test fixture) yields a `fabrication` or
   `major` flag and a non-pass verdict.
4. A structurally broken brief is refused with the distinct exit code.
