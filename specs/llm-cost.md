# Spec: LLM cost — usage ledger, Claude-first routing, run-volume cuts

**Builder**: Claude Code · **Status**: Phase 1 implemented (R1–R4,
2026-10-29), Phase 2 implemented (R6, 2026-10-31); R7/R8 implemented (2026-10-31); R9/R10
implemented (2026-10-31, R10 off until the Haiku-collection comparison); R5 deferred.
User decisions recorded below · Contracts: new `llm_usage.jsonl` line schema (this spec);
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

- `run_one` attaches a LangChain callback (`pipeline/llm_usage.py`
  `UsageCallback`) that sums, per model, `usage_metadata` from every LLM
  call: input, output, cache-read, cache-write and reasoning tokens, and the
  call count. The result payload gains `usage: {<model>: {...}}`; the
  running total is also written to stdout after **every** call
  (`USAGE_SENTINEL` line), so a run that fails or is killed on timeout still
  reports what it spent. Best-effort: a callback failure never fails a run.
- The analysis runner appends one line per run to
  `~/.tradingagents/llm_usage.jsonl` (`locked_append`, append-only):
  `{schema, written_at, run_id, date, session, ticker, arm, profile,
  provider, models: {<model>: {calls, input, output, cache_read,
  cache_write, reasoning, cost_usd}}, cost_usd, priced}`.
- `cost_usd` from the price table in `pipeline/llm_usage.py` (USD per 1 M
  tokens). A dated snapshot id (`-YYYYMMDD`) prices as its alias. An
  unlisted **Claude** model is budgeted at the most expensive listed Claude
  rate (flagged `priced: false`, warned) so the Anthropic budget never
  undercounts; any other unlisted model is `priced: false` with cost 0:
  `claude-opus-5-5` 4 / 20 (cache read 0.20, cache write 1.25×),
  `claude-haiku-5-5` 0.10 / 0.50 (prompt ≤ 100 K; 0.50 / 2.50 above),
  DeepSeek per the user's console (configurable; Flash 0.14 / 0.28,
  cache hit 0.0028).
- An ERROR run that spent tokens still writes its line (spend is spend).

### R2 — Claude-first routing with a monthly budget

- Profiles (`pipeline/llm_routing.py`): `claude` (above) and `deepseek`
  (deep `deepseek-v4-pro`, quick `deepseek-flash` — the current name;
  `deepseek-v4-flash` is a retired alias billed as Flash). The profile sets
  the child's `TRADINGAGENTS_LLM_PROVIDER` / `_DEEP_THINK_LLM` /
  `_QUICK_THINK_LLM` / effort env and becomes the ledger `preset`. Both
  profiles also **clear** the provider-agnostic knobs a legacy
  single-provider `.env` may carry (`LLM_BACKEND_URL`, `TEMPERATURE`, the
  shared `ANTHROPIC_EFFORT`, `OPENAI_REASONING_EFFORT`,
  `GOOGLE_THINKING_LEVEL`) with empty values — a relay URL must never send
  Claude requests elsewhere; the config digest records them as None.
- `TRADINGAGENTS_LLM_ROUTING`: `claude-first` (default once deployed),
  `claude`, `deepseek`. `TRADINGAGENTS_ANTHROPIC_MONTHLY_BUDGET_USD`
  (default 190).
- Before each **job** (not per slot; one choice covers both arms of a pair,
  so the budget crossing never splits an A/B pair), the runner sums this calendar month's
  (UTC) `cost_usd` over `provider == anthropic` lines. Below the cap and no
  exhaustion marker → `claude`; otherwise `deepseek`. The spend is a pure
  fold over the append-only ledger — no mutable budget state (LiteLLM's
  BudgetManager shows the lost-update and file-rewrite races that avoids).
  Overspend is bounded by the runs already in flight (concurrency 2 ×
  ≈ $0.5), inside the $10 margin.
- A child failing with Anthropic's "credit balance is too low" error writes
  `~/.tradingagents/llm_budget/<YYYY-MM>.exhausted` (atomic; its existence is
  the state) and that run is rerun once on `deepseek` (as are the job's
  remaining arms). This is the one way a pair can split across providers
  (first arm on Claude, second on DeepSeek): the later arm's run carries the
  flag `llm-profile-switched` (only when an earlier arm of the job really ran
  on another profile — a switch on the first arm keeps the pair on DeepSeek), and the pair's rows have different `preset`s, so
  a per-preset A/B aggregate never counts it as a complete pair. Only the final result is written to the decision ledger;
  the failed Claude attempt appears only in `llm_usage.jsonl` (`run_id`
  null). Other errors are not retried here.
- The per-profile config digest is resolved before the child starts, so a
  snapshot write failure fails the run before any token is spent.
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

### R5 — Cache-friendly debate prompts (deferred 2026-10-31)

**Deferred — measured, not worth it.** On 10-30 the Opus judge calls
(≈11.3K in / 1.7K out, ≈$0.08 each) read a prompt that no other call
shares, so caching saves nothing there; the Haiku roles would save
≈$2–3/month. Kept below as the design if volume grows.


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
- **Core skip-unchanged** (`analysis_runner.select_core_runs`): skip a
  core ticker's run when *all* hold — a positions snapshot exists, is not
  stale (the contract's 1-trading-day rule) and shows no position or open
  leg in it; its latest scheduled brief-arm decision (manual and ERROR rows
  ignored, latest attempt wins) is a HOLD at most 4 trading days old
  (weekdays since it, slot day included — so every core ticker gets a full
  run at least weekly); it has no scheduled row today yet (a same-day rerun
  is left to the already-done rule); today's brief exists, its eval is
  `pass`/`warn` and its catalyst score is below the trigger threshold (no
  brief, no score or a failed/missing eval is doubt). Any doubt runs the
  ticker. Funnel trigger code
  `core-unchanged` (excluded) with the age, score and threshold.
  `TRADINGAGENTS_CORE_SKIP_UNCHANGED=on|off` (default off). Both settings
  are part of the config digest.

### R7 — Claude CLI (collection + evaluation) inside the budget, DeepSeek Flash after

User decisions 2026-10-31: the Claude CLI steps (macro/ticker collectors,
pool nomination, evaluator) keep billing the Anthropic API key; once the
month's credits are spent they run on DeepSeek Flash.

- Measured 2026-10-30: the CLI spent $13.75 (4.5 M tokens incl. web search)
  against $4.03 for analysis; the console's $17.79 for the day is the sum.
  Before ANTHROPIC_API_KEY entered `.env` (2026-10-29) the CLI ran on the
  subscription, so older `claude_usage.jsonl` rows were never API-billed.
- Every `claude_usage.jsonl` line now records `billing`: `anthropic-api`
  (an Anthropic key in the CLI's env), `subscription` (none) or `deepseek`,
  and `billed_usd`: the CLI's `total_cost_usd` for `anthropic-api`, 0 for
  `subscription`, and the DeepSeek Flash price of its tokens for `deepseek`
  (the CLI's own figure is Anthropic-priced). Lines without `billing`
  (history) count as `subscription`.
- The monthly Anthropic spend that gates **both** analysis routing (R2) and
  the CLI is analysis spend (`llm_usage.jsonl`) plus `anthropic-api` CLI
  spend in the same UTC month.
- Route per component process (`pipeline/claude_cli.py`): decided at the
  first `claude -p` of the process with the R2 rule (`claude-first`: Claude
  below the budget and without the month's exhaustion marker; `claude` /
  `deepseek` pin; unset = legacy, never DeepSeek). DeepSeek mode points the
  process's own env at DeepSeek's Anthropic-compatible endpoint
  (`ANTHROPIC_BASE_URL=https://api.deepseek.com/anthropic`,
  `ANTHROPIC_API_KEY` = the DeepSeek key — never on argv) and pins every
  CLI model to `claude-sonnet-5-5`, which DeepSeek maps to `deepseek-flash`
  (a `claude-opus*` name would map to V4-Pro). Its server-side web search is
  supported (verified live 2026-10-31: a WebSearch call returned a dated
  headline with its URL, and a `--json-schema` call — the evaluator's mode —
  returned valid `structured_output`). Without a DeepSeek key the CLI stays on Anthropic
  (warned).
- A CLI error result reporting the credit balance as too low writes the
  month's exhaustion marker and switches the process to DeepSeek; that one
  call still fails (the components retry validation errors only), later
  calls run on Flash. The switch is one-way within a process. Overspend past
  the budget is bounded by one component's calls (~$7), inside the $10
  margin.

### R8 — Collect only what can be analysed

A session outside `TRADINGAGENTS_CORE_ANALYSIS_SESSIONS` (R6) collects and
evaluates no core-ticker briefs — those briefs had no consumer once cn
became catalyst-only (≈10 briefs + evals per day). Opportunity tickers are
collected and evaluated as before; the funnel records each skipped core
ticker at the collect stage as `not-collected-core-off` (excluded). The
setting is parsed once (`pipeline.config.core_analysis_sessions`) for the
runner, collector and evaluator. A core-off session with no usable pool has
nothing to collect: that is an empty, successful run, not the collector's
"no tickers" error.

### R9 — Research Manager on Sonnet 5.5

`research_manager_llm` (env `TRADINGAGENTS_RESEARCH_MANAGER_LLM`, default
unset = `deep_think_llm`) gives the Research Manager its own model, built
with the **deep** role's knobs (effort, thinking); the Portfolio Manager —
the call that issues the trade decision — always keeps `deep_think_llm`.
The `claude` profile sets `claude-sonnet-5-5` (≈$0.04 instead of ≈$0.08
per call, user decision 2026-10-31); the `deepseek` profile clears it, so
its Research Manager stays on `deepseek-v4-pro`. The key is part of the
analysis config digest. Takes effect at merge: it changes analysis only,
never brief collection or evaluation.

### R10 — Per-brief web-search cap for ticker collection

Web search bills $10 per 1K requests apart from tokens; on 10-30 the
ticker collectors ran ≈390 searches (≈$3.9 of $7.63). The ticker prompt's
research protocol is a `{{SEARCH_BUDGET}}` sentence:

- `TRADINGAGENTS_COLLECT_MAX_SEARCHES` unset, empty or `0` — the original
  protocol, byte for byte ("at least two distinct web searches" per
  section);
- `N ≥ 5` — "at most N web searches in total, at least one per section".
  Values that are not integers, or 1–4 (fewer than one per section), fail
  the collector before any job.

The cap is a prompt instruction (the CLI has no search-count flag), so
every `claude_usage.jsonl` line records `web_search_requests` and
`web_fetch_requests` from the envelope's `usage.server_tool_use` (junk
reads 0) to measure compliance. Macro and pool prompts are unchanged.
Rollout: off at merge; set to 8 after the Haiku-collection comparison
(≈2026-11-04), since it changes how briefs are collected.

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
3. Phase 3: R5 deferred (measured, not worth it); R9 live at merge; R10
   merged off, enabled after the Haiku-collection comparison.

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
6. R9: with the model set, the Research Manager runs on it with the deep
   knobs and the Portfolio Manager on `deep_think_llm`; unset (or equal to
   the deep model) both share one client.
7. R10: with no cap the rendered ticker prompt keeps the original protocol
   text; a cap is stated in the prompt; junk fails before any job; every
   CLI usage line carries the two server-tool counts.
