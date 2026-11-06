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

`openai` profile: provider `openai`, quick `gpt-6-luna` (reasoning
effort low), deep `gpt-6.1-sol` (medium) for both judges (no Research
Manager override). New per-role knobs `openai_deep_reasoning_effort` /
`openai_quick_reasoning_effort` (env `TRADINGAGENTS_OPENAI_{DEEP,QUICK}_REASONING_EFFORT`)
override `openai_reasoning_effort` and are in the config digest when set
(left out when unset, so the other profiles' digests are unchanged); the
other profiles clear them, the `openai` profile clears the Anthropic,
DeepSeek and RM knobs. Structured output and usage capture verified live
(2026-11-01).

### L3 — Collection and evaluation on codex

On the openai rung every pipeline `claude -p` call is built as a
hermetic `codex exec`:
`codex exec --ignore-user-config --sandbox read-only
--skip-git-repo-check --json -c tools.web_search=true -m <model>
-c model_reasoning_effort=<effort> --disable <feature>… --cd <call_home>/work
[--output-schema <file>] -`, prompt on stdin. Isolation (the calls read
untrusted web pages, as the claude path's HERMETIC_FLAGS assume):

- every default-on feature beyond search is disabled — shell and
  unified exec, browser/computer use, images, plugins, apps, hooks,
  skills, sub-agents (`CODEX_DISABLED_FEATURES`); `code_mode_host` stays
  on because web search runs through it, and its JavaScript has no
  `require`/`fs`/`process`;
- the read-only sandbox refuses writes (`apply_patch`);
- the env is an allow-list (PATH, locale, TZ, TMPDIR, CA and proxy vars)
  plus `CODEX_API_KEY` (verified to authenticate without a login),
  `CODEX_HOME=HOME=<call_home>` (below); no other key reaches codex;
- every call gets its own throw-away `CODEX_HOME`
  (`codex_home/calls/<uuid>`, cwd its empty `work/`, never the repo):
  without `--ephemeral` codex writes the session rollout **and** its
  state/thread-history SQLite databases with the prompt and fetched pages
  there (measured: `--ephemeral` left no prompt text, the non-ephemeral
  run did), so the whole dir is deleted once the call is billed (L4) —
  concurrent calls never share one, so deletion never races a running
  codex; dirs a crash or launch failure left are pruned after 24 h by
  every CLI call, whatever its route.
  Only the content-addressed output schemas stay in `codex_home/schemas`.

All four probed live on 2026-11-01: no shell ("no shell tool
available"), `file://` opens and `id` fail, JS `require`/`fs`/`process`
are absent, writes are refused, and search still works. Models per
role: collect `gpt-6-luna`, eval `gpt-6.1-terra`
(`TRADINGAGENTS_OPENAI_{COLLECT,EVAL}_MODEL`), so collector and evaluator
stay on different models (evaluator R2). Effort maps low/medium/high,
xhigh/max → high. The evaluator's `--json-schema` becomes an
`--output-schema` file (content-addressed under `codex_home/schemas`) in
OpenAI **strict** form — every object's properties all `required`,
`additionalProperties: false`, no `default` — since codex sends it strict
and the pydantic schema is rejected otherwise (`invalid_json_schema`,
measured 2026-11-01; the strict form validates live).

Measured codex-cli 0.164.0 event semantics (2026-11-01, live): `codex
exec` runs one turn, so one `turn.completed` carries that turn's usage;
`output_tokens` already includes `reasoning_output_tokens` (102 incl. 14);
`input_tokens` includes `cached_input_tokens` and `cache_write_input_tokens`.
Every `web_search` item except an `open_page` action is billed as a search
(over-counting a non-search action is budget-safe). A routed run's usage under a model name the provider did
not report (`unknown`) belongs to the run's profile provider and, unpriced,
is budgeted at that provider's ceiling — never at $0. The only
`claude_command` callers are the collector path (`collect` role) and the
evaluator (`eval` role), pinned by a test.

API errors arrive on stdout as `error` / `turn.failed` events under
`--json` (measured with a 404 model error: five "Reconnecting…" `error`
events, then `turn.failed`); a quota 429 takes the same path.
Quota detection also matches codex's own rewrite of `insufficient_quota`
("Quota exceeded. Check your plan and billing details.", `quota_exceeded`,
strings of codex-cli 0.164.0).

The JSONL events become the claude envelope shape: the last
`agent_message` is the result (parsed to `structured_output` when it is a
JSON object); `turn.completed` usage is summed and priced (tokens + $0.01
per web search call); `turn.failed`, or `error` events without a completed
turn, are an error result. Any call that ends without `turn.completed`
(failed, or cut by a timeout/crash, including after transient
"Reconnecting…" errors) is billed at the searches seen + a $0.25 estimate
for its unreported tokens (over-count: the confirmed balance is never
over-reported); every launch site records a timed-out call's partial
stdout. Usage lines carry `billing: openai-api`, the GPT model that
served the call, and the R11 search fields (`web_search` items:
`search` → queries, `open_page` → fetch URLs).

The brief's `generator` stays the configured backend's identity
(`claude-deep-search`), as on the DeepSeek route (R7); the provider and
model that served each call are in the usage log.

The call runs codex's **native binary** directly: the npm `codex` is a
node launcher that spawns it, and a per-call `subprocess.run` timeout
would kill only the launcher, leaving the native process spending outside
the ledger. The binary is found the way the launcher finds it
(the host's target triple and platform package as codex.js maps them;
`node_modules/<package>/vendor/<triple>/bin/codex` under the launcher's
package dir or any ancestor, as Node resolution walks — nested or
hoisted — then the bundled `vendor/<triple>/…`; or `codex` on PATH already
being a native, non-script binary), with the
launcher's `CODEX_MANAGED_BY_NPM` / `CODEX_MANAGED_PACKAGE_ROOT` env. No
binary found (e.g. `codex` not on the scheduled jobs' PATH, which is
baked at install time) → the CLI skips the rung to DeepSeek.

### L4 — Ledger and prices

A codex model override without a listed price is budgeted at the OpenAI
ceiling, never at $0. Per 1M tokens, in / cached / out, prompts ≤ 272K
(developers.openai.com/api/docs/pricing, standard tier, 2026-11-01):
`gpt-6-luna` $0.10 / $0.01 / $0.50, `gpt-6.1-terra` $1.00 / $0.05 / $5.00,
`gpt-6.1-sol` $2.00 / $0.10 / $10.00 (previous generation, kept for old
lines: `gpt-5.6-sol` $4 / $0.40 / $20, `gpt-5.6-luna` $0.20 / $0.02 /
$1.20). Prompts over 272K bill at each model's long-context tier (×2
input/cached, ×1.5 output); no cache-write surcharge (writes bill as
input, at the tier's rate). The tier is a per-request rule. codex's
`turn.completed` usage sums the turn's requests, but its session rollout
(`codex_home/sessions/**/rollout-*-<thread_id>.jsonl`, `token_count`
events' `last_token_usage`) records each request: codex therefore runs
without `--ephemeral`, and after each call the rollout of its
`thread.started` id is read from the call's home, every request priced on
its own tier, and the call home deleted (failed turns too; rows are cached
per thread so a re-parse prices the same). A failed or cut turn bills its
finished requests exactly plus the estimate for the one in flight. The
rollout is used only when its requests add up exactly to the turn's
usage; otherwise — and for ledger rows of summed calls — the sum is judged
as one request (never under-bills, may over-count). Measured 2026-11-01:
a real AAPL brief made 9 requests, largest 48K, summed 286K (would have
read as long-tier); `gpt-6-luna`'s context window is 258,400 tokens, so
its requests can never reach the long tier. `gpt-6-astra` ($10 / $1 / $50) and
`gpt-6-sol` ($2 / $0.20 / $10) are listed though unused, so an override
prices exactly and the unlisted-model ceiling is the highest real rate
(`gpt-6-astra`). Unknown `gpt-*`
models bill at the OpenAI ceiling (`priced: false`); OpenAI snapshot ids
(`-YYYY-MM-DD`) resolve to their alias. `month_spend(..., "openai")`
includes CLI lines billed `openai-api`; `since` restricts a fold to spend
after an instant (an unreadable timestamp counts — over-counting is
budget-safe).

### L5 — A refused key steps down for the slot (2026-11-06)

2026-10-27..29: DeepSeek answered every analysis run with `401 - Authentication
Fails` for three slots (15–17 ERROR rows each, read only as "run errors 15" in
the summary). The ladder stepped down only on exhausted credits. Now an
authentication refusal (`llm_usage.is_auth_error`: `error code: 401`,
`authentication_error`, `authentication fails`, `invalid x-api-key`,
`invalid_api_key`, `incorrect api key`, `invalid api key`) under `ladder` or
`claude-first` marks the provider refused **for the rest of the slot only**
(the router's in-memory set; nothing is written for the month — a key is
fixed by the operator, not by a new month) and reruns the run on the next
rung: Anthropic → OpenAI (ladder, when usable) → DeepSeek; later jobs of the
slot start on the stepped-down rung. DeepSeek is the floor: a refusal there
(or on every rung) ends in the ERROR row, kind `auth`. Fixed routings
(`claude`, `deepseek`) never move. The runner summary gains `error_kinds`
(`auth` / `credit` / `timeout` / `other`), and the orchestrator alerts on a
slot whose runs mostly failed (specs/orchestrator.md Notifications).

One loop handles both rung failures, in any order (e.g. Anthropic out of
credits, then OpenAI refusing its key → DeepSeek). Its step-down skips every
rung the run already failed on and every provider refused earlier in the
slot, so the floor never climbs back to a refused key. A retried attempt's
usage is written in the loop and the final attempt once afterwards; when
nowhere is left the run is recorded on the profile it actually ran on.

## User decisions (2026-11-01)

- **Q1** OpenAI credits are **monthly**; November has only **$71** left.
  The balance must be confirmed before the pipeline first uses the rung.
  The project API key cannot read it (`/v1/organization/costs`, `/usage`,
  `/dashboard/billing/*` → 403; it reads only models, files, batches, `/me`).
  No Admin key can be created, so the user confirms the balance monthly
  (L1).
- **Q2** Models (revised the same day to the GPT-6 line — the first pick,
  `gpt-5.6-luna`/`gpt-5.6-sol`, came from a truncated model listing):
  quick roles and collection `gpt-6-luna`; Research Manager and Portfolio
  Manager `gpt-6.1-sol`; evaluation `gpt-6.1-terra` (user choice:
  cheaper than sol, still a different model from the collector). All
  three verified live on 2026-11-01: analysis structured output (luna,
  sol), codex search (luna), codex strict eval schema (terra). Not used:
  `gpt-6-astra` ($10 / $50, top tier) and `gpt-6-sol` (superseded by
  6.1). Web search $10 / 1K calls plus search content tokens at the
  model's input rate.
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
3. codex calls run hermetically (no user config, only web search, key
   never on argv, each in its own call home whose session files — rollout,
   state databases — are deleted once the call is billed) and log usage
   with `billing: openai-api`, each request priced on its own tier.
4. `claude-first` behaviour is unchanged, except L5 (a refused key steps
   down to DeepSeek for the rest of the slot).
5. L5: under `ladder` or `claude-first` an authentication refusal marks the
   provider refused for the slot only (no month marker) and reruns the run
   on the next rung; one loop handles exhausted credits and refused keys in
   any order; the step-down skips every rung the run failed on and every
   provider refused earlier in the slot (the floor never climbs back); when
   nowhere is left the ERROR row (kind `auth`) is recorded on the profile
   that actually ran, its attempt billed once; fixed routings never move;
   a slot whose runs mostly failed alerts with the error kinds.
