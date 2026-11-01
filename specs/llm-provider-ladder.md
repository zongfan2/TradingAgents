# Spec: LLM provider ladder — Claude → OpenAI (GPT / codex) → DeepSeek

**Builder**: Claude Code · **Status**: Draft (2026-11-01) — waiting on the
user's OpenAI API key and the open decisions below. Extends
[llm-cost.md](llm-cost.md) R2 (analysis routing) and R7 (CLI routing).

## Why

User decision 2026-11-01: besides the $200 Anthropic credits, $200 of
OpenAI API credits will be added. Spend order: Claude first, then GPT,
then DeepSeek. Today the ladder has two rungs (`claude-first`: Anthropic
until $190/UTC month, then DeepSeek).

## Design

### L1 — Ladder routing (generalises R2/R7)

`TRADINGAGENTS_LLM_ROUTING=ladder` (new; `claude-first` keeps today's
behaviour). Rungs, in order: `anthropic`, `openai`, `deepseek`. A rung is
usable while (a) its key is set, (b) its period spend from the ledger is
below its budget (`TRADINGAGENTS_<PROVIDER>_BUDGET_USD`, Anthropic default
190), and (c) no exhausted marker exists (`llm_budget/<provider>/<period>.exhausted`).
DeepSeek is the floor (no budget). The decision is made per analysis run
and once per CLI process, as today; switches are one-way within a process,
and a credit-exhausted error (Anthropic: "credit balance too low";
OpenAI: HTTP 429 `insufficient_quota`) writes the marker and moves the
process to the next rung.

### L2 — Analysis on OpenAI

New `openai` profile for the graph: provider `openai`, quick and deep
models per open decision Q2, per-role reasoning effort
(`openai_reasoning_effort` split into deep/quick like R3), structured
output through the existing OpenAI client path. The Research Manager
override (R9) gets the OpenAI quick-or-deep choice from the same profile.
The `deepseek` profile clears every OpenAI knob, as it clears Anthropic's.

### L3 — Collection and evaluation on codex

The `codex` CLI backend already exists (`codex exec`, web search on) but
rides the user's interactive login and config. On the `openai` rung the
claude CLI steps switch to a **hermetic** codex call:

- `codex exec --ignore-user-config --ephemeral --sandbox read-only
  -c tools.web_search=true --skip-git-repo-check --json -m <model> -`,
  prompt on stdin, a scratch `CODEX_HOME`, the API key from the env only
  (never argv);
- `--json` JSONL events give token usage and the web-search calls, logged
  to the same usage ledger with `billing: openai-api` (R11 fields);
- the evaluator uses `--output-schema` for the judgment object;
- generator identity `codex-deep-search` (already in the contracts);
- independence (evaluator R2) stays at the model level: collector and
  evaluator use different pinned models.

### L4 — Ledger and prices

`llm_usage` gains `openai` prices (official list prices, recorded with
their date at implementation; no guessed figures) and per-provider period
spend. Unknown OpenAI models are billed at the most expensive listed
OpenAI rate (as Claude's ceiling rule).

## User decisions (2026-11-01)

- **Q1** OpenAI credits are **monthly**; November has only **$71** left.
  The balance must be confirmed before the pipeline first uses the rung.
  The project API key cannot read it (`/v1/organization/costs`, `/usage`,
  `/dashboard/billing/*` → 403; it reads only models, files, batches, `/me`).
  Mechanism pending: an Admin key for the costs API, or the local ledger
  plus a manual check.
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

1. With `ladder`, each rung is used in order; budget, marker, and missing
   key each skip a rung; DeepSeek is never skipped.
2. A quota error on the OpenAI rung moves the process to DeepSeek and
   writes the marker; the failed call is not retried on the same rung.
3. codex calls run hermetically (no user config, no session files, key
   never on argv) and log usage with `billing: openai-api`.
4. `claude-first` behaviour is unchanged.
