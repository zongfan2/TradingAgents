# Eval-effort study (2026-10-14) — D20 evaluator on Opus 5.5: `medium` vs `high`

**Decision:** `claude_eval_effort` defaults to `medium`. `TRADINGAGENTS_CLAUDE_EVAL_EFFORT=high`
restores the more expensive setting.

## Question

D20 pins the evaluator to Claude Opus 5.5. Is effort `medium` enough, or does the
evaluator need `high`? The decision-relevant output is the `fail` verdict. It needs a
`fabrication` flag or `factual_accuracy` < 5, and it is the only verdict that gates
analysis/execution (analysis-runner.md). `warn` gates exactly like `pass`.

## Method

- **Natural sample:** all 41 briefs from 2026-10-12 and 2026-10-13 that had a production
  eval: 3 macro and 38 ticker, cn and us. Each was re-evaluated on Opus 5.5 at `medium` and
  at `high`. 8 random briefs got a second `high` run (`high_rep`) to measure run-to-run noise.
- **Planted fabrications:** 8 briefs (1 macro, 7 ticker) got one false, market-moving
  sentence inside a bullet that already carried a citation. Tickers got a CEO resignation
  over an 18% revenue overstatement; the macro brief got a fictitious 50 bp PBoC LPR cut.
  Each was evaluated once at `medium` and once at `high`.
- **Isolation:** evaluator CLI (D20 hermetic flags) from the `claude-backends` worktree. Each
  arm used its own scratch copies, so production briefs and evals were only read. Cost and
  token counts come from the D20 usage log.

## Results

### Natural sample (n = 41)

| | pass / warn / fail | flagged claims (minor / major) |
|---|---|---|
| `medium` | 31 / 10 / 0 | 175 (162 / 13) |
| `high` | 27 / 14 / 0 | 184 (169 / 15) |
| production (unpinned CLI default) | 30 / 11 / 0 | 147 |

Is the `medium`/`high` difference larger than run-to-run noise? No:

| comparison | verdict agreement | more major flags: first / second / equal |
|---|---|---|
| `high` vs `medium` (41) | 31/41 | 9 / 7 / 25 |
| `high` vs `high_rep` (8) | 4/8 | 1 / 3 / 4 |

- Two `high` runs disagree with each other at least as often as `high` disagrees with `medium`.
- Several identical claims were flagged major by `medium` on one brief and by `high_rep` on
  another. The pass/warn boundary is inherently noisy, but it does not gate anything.

### Planted fabrications (n = 8)

| | planted claim flagged | flagged as `fabrication` | verdict `fail` |
|---|---|---|---|
| `medium` | 8/8 | 8/8 | 8/8 |
| `high` | 8/8 | 8/8 | 8/8 |

### Cost per evaluation (API-equivalent list price; subscription quota in practice)

| | median cost | median time | median turns | median output tokens |
|---|---|---|---|---|
| `medium` | $0.23 | 60 s | 15 | 3.8K |
| `high` | $0.38 | 97 s | 18 | 5.1K |

At about 20 evaluations a day, `medium` uses about $4.7 a day of quota against $7.7 for `high`.

## Collector smoke (same day; D20 collect default Sonnet 5.5 / `medium`)

These are live runs into scratch dirs for cn 2026-10-14, compared with that day's codex briefs:

- **Macro brief:** first-attempt pass, 18 sources, 888 words (codex: 23 / 1082). $0.54, 125 s.
- **Ticker briefs (0700.HK, 300750.SZ):** first-attempt pass, 9 and 10 sources (codex: 9 and 13).
  Catalyst scores 3.0 and 7.0, identical to codex. About $0.28 and 50 s each.
- **Pool builder, seeded with the prior production pool:**
  - `medium`: opportunity 5 / watch 7, $0.24, 49 s.
  - `high`: 5 / 10, $1.03, 182 s.
  - codex: 5 / 10. Both Claude runs exited the same names as codex at least partly.
  - Without a prior pool, `medium` re-scored the core list and nominated no new names. That
    run was unrealistic, because production always has history.

## Caveats

- The sample covers two days of one market regime.
- The planted errors were blatant. Subtle numeric errors were not tested.
- The production reference evals ran on the unpinned CLI default model (most likely
  Fable 5.1 / `xhigh`) with the full interactive setup loaded.
- The replicate sample is small (n = 8).
- Re-check if `fail` verdicts drop in production after the switch: compare the weekly fail
  rate before and after, and the `outcome` / cost trend in `claude_usage.jsonl`.
