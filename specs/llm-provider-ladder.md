# Spec: LLM provider ladder — Claude → OpenAI (GPT / codex) → DeepSeek

**Builder**: Claude Code · **Status**: Implemented (2026-11-01). Extends
[llm-cost.md](llm-cost.md) R2 (analysis routing) and R7 (CLI routing).

## Why

User decision 2026-11-01: besides the $200 Anthropic credits, $200 of
OpenAI API credits will be added. Spend order: Claude first, then GPT,
then DeepSeek. Today the ladder has two rungs (`claude-first`: Anthropic
until $190/UTC month, then DeepSeek).

## Design

### L1 — Ladder routing (generalises R2/R7)

`TRADINGAGENTS_LLM_ROUTING=ladder` (`claude-first` keeps today's behaviour
and never uses OpenAI). Rungs, in order:

1. **anthropic** — no exhausted marker and month spend < budget (R2);
2. **openai** — `OPENAI_API_KEY` set, no `llm_budget/openai/<YYYY-MM>.exhausted`,
   the month's balance **confirmed** and *confirmed balance − spend recorded
   since the confirmation* > margin (`TRADINGAGENTS_OPENAI_BALANCE_MARGIN_USD`,
   default $5);
3. **deepseek** — the floor.

The project key cannot read the balance (costs/usage/billing endpoints
need an Admin key, which the user cannot create), so the user confirms it:
`python -m pipeline.llm_budget confirm openai <USD>` writes
`llm_budget/openai/<YYYY-MM>.confirmed` (`balance_usd`, `confirmed_at`);
`status` shows spend, the remaining balance and whether the rung is
usable. An unconfirmed month skips the rung, and the Lark LLM line says
"OpenAI balance unconfirmed" (else "OpenAI left $X").

The decision is made per analysis job and once per CLI process. A
credit-exhausted failure (Anthropic "credit balance too low"; OpenAI
`insufficient_quota` / "exceeded your current quota") marks that
provider's month and reruns the run on the next rung — at most
anthropic → openai → deepseek, never the same rung twice. A CLI process
moves to DeepSeek on either provider's exhaustion (one-way).

### L2 — Analysis on OpenAI

`openai` profile: provider `openai`, quick `gpt-5.6-luna` (reasoning
effort low), deep `gpt-5.6-sol` (medium) for both judges (no Research
Manager override). New per-role knobs `openai_deep_reasoning_effort` /
`openai_quick_reasoning_effort` (env `TRADINGAGENTS_OPENAI_{DEEP,QUICK}_REASONING_EFFORT`)
override `openai_reasoning_effort` and are in the config digest; the
other profiles clear them, the `openai` profile clears the Anthropic,
DeepSeek and RM knobs. Structured output and usage capture verified live
(2026-11-01).

### L3 — Collection and evaluation on codex

On the openai rung every pipeline `claude -p` call is built as a
hermetic `codex exec`:
`codex exec --ignore-user-config --ephemeral --sandbox read-only
--skip-git-repo-check --json -c tools.web_search=true -m <model>
-c model_reasoning_effort=<effort> [--output-schema <file>] -`, prompt on
stdin, `CODEX_HOME=<state_dir>/codex_home`, the key only in the env
(`CODEX_API_KEY`, verified to authenticate without a login). Models per
role: collect `gpt-5.6-luna`, eval `gpt-5.6-sol`
(`TRADINGAGENTS_OPENAI_{COLLECT,EVAL}_MODEL`), so collector and evaluator
stay on different models (evaluator R2). Effort maps low/medium/high,
xhigh/max → high. The evaluator's `--json-schema` becomes an
`--output-schema` file (content-addressed under `codex_home/schemas`) in
OpenAI **strict** form — every object's properties all `required`,
`additionalProperties: false`, no `default` — since codex sends it strict
and the pydantic schema is rejected otherwise (`invalid_json_schema`,
measured 2026-11-01; the strict form validates live).

Quota detection also matches codex's own rewrite of `insufficient_quota`
("Quota exceeded. Check your plan and billing details.", `quota_exceeded`,
strings of codex-cli 0.164.0).

The JSONL events become the claude envelope shape: the last
`agent_message` is the result (parsed to `structured_output` when it is a
JSON object); `turn.completed` usage is summed and priced (tokens + $0.01
per web search call); `turn.failed`, or `error` events without a completed
turn, are an error result; a stream that ends before either has no
envelope. Usage lines carry `billing: openai-api`, the GPT model that
served the call, and the R11 search fields (`web_search` items:
`search` → queries, `open_page` → fetch URLs).

The brief's `generator` stays the configured backend's identity
(`claude-deep-search`), as on the DeepSeek route (R7); the provider and
model that served each call are in the usage log. If `codex` or `node` is
not on PATH the CLI skips the rung to DeepSeek (the scheduled jobs' PATH
is baked at install time).

### L4 — Ledger and prices

`gpt-5.6-sol` $4.00 in / $0.40 cached / $20.00 out, `gpt-5.6-luna` $0.20 /
$0.02 / $1.20 per 1M tokens (developers.openai.com/api/docs/pricing,
standard tier, 2026-11-01); no cache-write surcharge. Unknown `gpt-*`
models bill at the OpenAI ceiling (`priced: false`); OpenAI snapshot ids
(`-YYYY-MM-DD`) resolve to their alias. `month_spend(..., "openai")`
includes CLI lines billed `openai-api`; `since` restricts a fold to spend
after an instant (an unreadable timestamp counts — over-counting is
budget-safe).

## User decisions (2026-11-01)

- **Q1** OpenAI credits are **monthly**; November has only **$71** left.
  The balance must be confirmed before the pipeline first uses the rung.
  The project API key cannot read it (`/v1/organization/costs`, `/usage`,
  `/dashboard/billing/*` → 403; it reads only models, files, batches, `/me`).
  No Admin key can be created, so the user confirms the balance monthly
  (L1).
- **Q2** Models (official prices per 1M tokens, 2026-11-01, standard tier):
  quick roles and collection `gpt-5.6-luna` ($0.20 in / $0.02 cached /
  $1.20 out); Research Manager, Portfolio Manager and evaluation
  `gpt-5.6-sol` ($4.00 / $0.40 / $20.00). Web search $10 / 1K calls plus
  search content tokens at the model's input rate.
- **Q3** codex uses the **API key only**; no interactive login (the server
  has none today: `codex login status` → not logged in, and both
  `collect_backend` and `eval_backend` are `claude`, so codex is unused at
  runtime).

## Prior art (specs/README.md rule 6)

Covered by llm-cost.md (LiteLLM budget manager, LangChain fallbacks); the
codex hermetic flags to be verified against `codex exec --help` of the
installed version (0.164.0 on 2026-11-01).

## Acceptance criteria

1. With `ladder`, each rung is used in order; budget, marker, missing key
   and an unconfirmed or spent balance each skip a rung; DeepSeek is never
   skipped.
2. A quota error on the OpenAI rung moves the process to DeepSeek and
   writes the marker; the failed call is not retried on the same rung.
3. codex calls run hermetically (no user config, no session files, key
   never on argv) and log usage with `billing: openai-api`.
4. `claude-first` behaviour is unchanged.
