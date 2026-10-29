# Spec: LLM cost — usage ledger, Claude-first routing, run-volume cuts

**Builder**: Claude Code · **Status**: Phase 1 implemented (R1–R4,
2026-10-29; user decisions recorded below), Phases 2–3 open · Contracts: new `llm_usage.jsonl` line schema (this spec);
the decision ledger is unchanged (`preset` already names the run's profile).
Changes upstream agent behavior (DeepSeek thinking, prompt order) — this
spec is the authorization AGENTS.md requires.

## Why

DeepSeek bill for the month to 2026-10-29: **$62.54**, 10,280 requests
(9,287 Flash, 993 V4-Pro), ~160 M tokens, low cache hits, high output.
Reconciled against the ledger: 509 runs (993 Pro calls = 2 per run) —
≈ $0.12, ≈ 20 calls and ≈ 314 K tokens per run. Causes (verified in code):

1. **Thinking on everywhere.** DeepSeek V4 defaults to thinking at effort
   high (api-docs.deepseek.com/guides/thinking_mode); the client never sends
   `thinking: disabled`, so all ~18 Flash calls (analyst tool loops,
   debaters, trader) bill reasoning as output.
2. **Prompts defeat the prefix cache.** Bull/bear, the three risk debaters,
   the trader and both managers open with a role-specific preamble and
   *then* embed the same ~60 K characters of reports — the shared block
   never forms a common prefix.
3. **Runs that cannot trade.** cn: 234 runs (46%) — the adapter is us-only.
   us feeds arm: 71 (14%) — shadow, never executed. Core tickers are
   re-analysed every slot: 481 of 509 runs, 353 of them HOLD.

The user also has **$200/month of Anthropic credits** to use before DeepSeek.

## User decisions (2026-10-29)

- Claude profile: quick role `claude-haiku-5-5` effort **low**; deep role
  (Research Manager, Portfolio Manager) `claude-opus-5-5` effort **medium**.
- Switch rule: **calendar-month budget** — Anthropic until month-to-date
  spend reaches **$190**, or immediately on a "credit balance too low"
  error; DeepSeek for the rest of the month.
- cn: **catalyst-only** analysis (no daily cn core runs).
- A/B feeds arm: **paused** (`TRADINGAGENTS_AB_PAIRING=off`) after a summary
  of the existing pairs.

## Design

### R1 — Usage ledger (measure first)

- `run_one` attaches a LangChain callback that sums, per model,
  `usage_metadata` from every LLM call: input, output, cache-read,
  cache-write and reasoning tokens, and the call count. The result payload
  gains `usage: {<model>: {...}}` (best-effort: a callback failure records
  nothing and never fails the run).
- The analysis runner appends one line per run to
  `~/.tradingagents/llm_usage.jsonl` (`locked_append`, append-only):
  `{schema, written_at, run_id, date, session, ticker, arm, profile,
  provider, models: {<model>: {calls, input, output, cache_read,
  cache_write, reasoning, cost_usd}}, cost_usd, priced}`.
- `cost_usd` from a price table in `pipeline/llm_prices.py` (USD per 1 M
  tokens; `priced: false` with cost 0 for an unknown model, never a guess):
  `claude-opus-5-5` 4 / 20 (cache read 0.20, cache write 1.25×),
  `claude-haiku-5-5` 0.10 / 0.50 (prompt ≤ 100 K; 0.50 / 2.50 above),
  DeepSeek per the user's console (configurable; Flash 0.14 / 0.28,
  cache hit 0.0028).
- An ERROR run that spent tokens still writes its line (spend is spend).

### R2 — Claude-first routing with a monthly budget

- Profiles (`pipeline/llm_profiles.py`): `claude` (above) and `deepseek`
  (deep `deepseek-v4-pro`, quick `deepseek-flash` — the current name;
  `deepseek-v4-flash` is a retired alias billed as Flash). The profile sets
  the child's `TRADINGAGENTS_LLM_PROVIDER` / `_DEEP_THINK_LLM` /
  `_QUICK_THINK_LLM` / effort env and becomes the ledger `preset`.
- `TRADINGAGENTS_LLM_ROUTING`: `claude-first` (default once deployed),
  `claude`, `deepseek`. `TRADINGAGENTS_ANTHROPIC_MONTHLY_BUDGET_USD`
  (default 190).
- Before each run (not per slot), the runner sums this calendar month's
  (UTC) `cost_usd` over `provider == anthropic` lines. Below the cap and no
  exhaustion marker → `claude`; otherwise `deepseek`. The spend is a pure
  fold over the append-only ledger — no mutable budget state (LiteLLM's
  BudgetManager shows the lost-update and file-rewrite races that avoids).
  Overspend is bounded by the runs already in flight (concurrency 2 ×
  ≈ $0.5), inside the $10 margin.
- A child failing with Anthropic's "credit balance is too low" error writes
  `~/.tradingagents/llm_budget/<YYYY-MM>.exhausted` (atomic; its existence is
  the state) and the job is retried once on `deepseek` (next attempt
  number — the ledger's latest-attempt rule makes it current). Other errors
  are not retried here.
- The end-of-slot notification adds month-to-date Anthropic spend and the
  profile used.

### R3 — Claude compatibility (required for R2)

- Per-role effort: `TRADINGAGENTS_ANTHROPIC_DEEP_EFFORT` /
  `_QUICK_EFFORT` (fallback `TRADINGAGENTS_ANTHROPIC_EFFORT`).
- `_supports_effort` accepts Haiku ≥ 5.5 (Haiku 5.5 takes effort; older
  Haiku still 400).
- Structured output on Claude 5.5-family models uses
  `method="json_schema"`: langchain-anthropic's default
  `function_calling` forces `tool_choice`, which Opus/Sonnet 5.5 reject with
  a 400 (the managers then silently fell back to free text).
- Model catalog lists `claude-opus-5-5`, `claude-sonnet-5-5`,
  `claude-haiku-5-5`.

### R4 — DeepSeek quick role without thinking

The `deepseek` profile sends `extra_body={"thinking": {"type":
"disabled"}}` on the **quick** LLM only; the deep role (two judging calls)
keeps thinking. `TRADINGAGENTS_DEEPSEEK_QUICK_THINKING=enabled` restores it.
Tool calls without thinking need no `reasoning_content` round-trip.

### R5 — Cache-friendly debate prompts

Bull, bear, the three risk debaters, trader and both managers render the
shared material first — instrument context and the four reports, byte-
identical across those calls — and their role instructions after it. On
Anthropic the shared block carries `cache_control` (ephemeral); DeepSeek
caches common prefixes automatically. Behavior is otherwise unchanged
(same content, different order); measured with R1 before/after.

### R6 — Run volume

- **cn catalyst-only**: `TRADINGAGENTS_CORE_ANALYSIS_SESSIONS` (default
  `cn,us`; set `us`). A session not listed runs no core jobs; each skipped
  core ticker emits trigger `core-session-off` (excluded) in the funnel.
- **A/B paused**: `TRADINGAGENTS_AB_PAIRING=off` (existing setting).
- **Core skip-unchanged** (us core): skip a core ticker's run when *all*
  hold — no open position or open order in it (positions.json); its latest
  scheduled decision was HOLD; that decision is ≤ 4 trading days old (so
  every core ticker gets a full run at least weekly); today's brief has no
  catalyst ≥ the trigger threshold and its eval is not newer-and-different.
  Funnel trigger code `core-unchanged` (excluded) with the age and reason.
  `TRADINGAGENTS_CORE_SKIP_UNCHANGED=on|off` (default off until deployed).

## Prior art (specs/README.md rule 6)

- **LiteLLM** — https://github.com/BerriAI/litellm @ `8d441ebc151c`, license
  not machine-declared (NOASSERTION; nothing copied). `budget_manager.py`:
  per-user spend with lazy period reset, persisted by rewriting a JSON file
  from background threads without a lock. Known issues avoided: lost
  updates, concurrent file rewrites, check-then-act overspend, timestamps
  fixed at import — hence the append-only ledger fold, a margin under the
  credit cap, and the provider's own credit error as the hard stop.
- **LangChain** — https://github.com/langchain-ai/langchain @ `864754c5e293`,
  MIT. `Runnable.with_fallbacks` (retry on another model on exception) and
  `usage_metadata` / callback token accounting. Taken: usage via callbacks;
  not taken: per-call fallback — switching provider mid-graph would mix two
  models inside one decision, so the switch happens per run.
- langchain-anthropic 1.5.4 (installed) — `with_structured_output` default
  `function_calling` forces `tool_choice`; `json_schema` uses native
  structured output (R3).

## Rollout

1. Phase 1: R1 + R2 + R3 + R4 (R4 is part of the `deepseek` profile), the
   A/B summary, then `AB_PAIRING=off` (done 2026-10-29). Verified live: Opus
   5.5 returns a `PortfolioDecision` via `json_schema`; Haiku 5.5 accepts
   effort `low`; `deepseek-flash` with thinking disabled reports 0
   reasoning tokens (33 vs 65 output tokens on the same one-line prompt).
2. Phase 2: R6.
3. Phase 3: R5, measured with R1.

Each phase: deterministic gate + Claude review (trading decisions' model
and inputs change; new ledger; scheduling of runs).

## Acceptance criteria

1. Every completed or failed run with token usage appends exactly one
   usage line; a callback or write failure never fails the run.
2. Month-to-date Anthropic spend is a pure function of the ledger; at or
   above the cap, or with the exhaustion marker, runs use `deepseek`; a
   credit-exhausted failure writes the marker and retries once on DeepSeek.
3. The managers' structured output succeeds on `claude-opus-5-5` without a
   forced tool choice; Haiku 5.5 receives `effort`.
4. With R6 on, cn slots run no core jobs, skipped tickers carry funnel
   codes, and held positions are never skipped.
5. R4/R5 change no decision-relevant content (same reports, same
   instructions), verified by prompt-content tests.
