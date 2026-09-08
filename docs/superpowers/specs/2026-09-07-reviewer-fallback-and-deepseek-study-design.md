# Reviewer alternatives, recovery, and DeepSeek pilot

- **Date:** 2026-09-07
- **Owner:** Codex
- **Status:** Direction and $5 pilot approved in chat; written design awaiting user review. No production activation.
- **Baseline:** `8e7fb0cbd1326fb7e75fe7be9f4a0e951f937bf0`

The subsequent user-directed [universal Flash backup amendment](2026-09-07-flash-backup-and-reviewer-study-design.md)
takes precedence for backup selection. The pilot and its $5 budget remain one
campaign, not duplicated by the amendment.

## 1. Objective and authority

Reduce routine dependence on Claude while keeping Claude, Codex/GPT, and
DeepSeek recoverable as separate backends. Before the user cancels Claude,
prove a supported operating mode that does not invoke Claude, including when
a remaining provider fails. Preserve honest failure states and trading guards;
availability does not mean inventing a successful evaluation.

The user approved a research-only comparison: eight clean/mutated pairs,
DeepSeek Flash and Pro, two repetitions, at most 64 paid calls and $5 total.
GPT joins only after its transport isolation blocker is resolved. The user
also requested backups and resilience against future Claude cancellation.
This is not authority to cancel subscriptions, buy credits, spend beyond $5,
waive any exact-SHA review, run a trading slot, or activate new production defaults.

This document does not supersede `AGENTS.md`, runtime D19, or existing contract
specs. The work is staged: this pilot and recovery baseline first; a
provider-neutral development reviewer implementation second; independently
validated runtime evaluation/routing third. Each implementation must update
its governing contract before consumers and pass its required review gates.
Historical Builder fields and reports are never rewritten.

## 2. Verified starting point

- Collection uses Codex; runtime macro/ticker evaluation defaults to Claude
  (`pipeline/config.py`, `pipeline/evaluator.py`). A selectable Terra backend
  already exists, but its presence is not current integration-test evidence.
- Runtime evaluation requires independent web searches and at least five
  citation fetches. The DeepSeek API client does not provide that executor.
  A fixed-evidence pilot tests judgment, not autonomous retrieval or coverage.
- Main trading analysis uses DeepSeek Flash for fast roles and Pro for RM/PM;
  the manager ablation is not evidence that either model can replace a reviewer.
- Runtime eval caching currently keys only on the brief hash. Changing a
  backend can skip evaluation and reuse an old report. All pilot artifacts
  must be separate from production `*.eval.json` and caches.
- Development reports currently admit only `claude` and `user-waiver`, bind
  to exact base/head SHAs, and use `.claude.json` filenames. A different
  provider's result cannot be relabeled as a Claude report.
- The baseline's deterministic checks passed previously, but its required
  current-revision Claude report is missing after quota failures. A backup
  preserves this state; it does not certify the baseline as fully reviewed.
- The prior GPT transport can miss wrapper-tool attempts in stdout JSONL.
  The frozen manager-stress experiment remains separate and unmodified;
  its isolation failure cannot be waived by this document.

## 3. Options and intended role separation

Keeping Claude as the only accepted reviewer preserves current integration,
but cannot meet the cancellation goal. A wholesale GPT switch is simpler,
but puts collection and evaluation on the same provider and subscription
quota. Prefer a tested multi-provider arrangement with role-specific eligibility:

| Role | Candidate default / alternative | Activation requirement |
|---|---|---|
| Routine development review | GPT Terra; DeepSeek Pro independent alternative | Frozen-packet pilot, then exact-SHA isolated integration |
| Brief evaluation | DeepSeek Pro candidate; GPT alternative | Judgment pilot plus independent retrieval parity and runtime shadow acceptance |
| Lightweight checks | DeepSeek Flash candidate | Separate screening results; never promote to a safety gate from cost alone |
| High-risk/final development review | Claude under today's policy | An accepted non-Claude independent reviewer and explicit policy revision before Claude becomes optional |
| Experimental grading | Frozen answer keys, executable checks, evidence adjudication | No candidate judges its own outputs; no Claude calls in this pilot |

These are hypotheses, not a quality ranking or a configured fallback chain.
Retain all three adapters and non-secret presets, even if one is disabled.
The later cancellation profile must exclude Claude from the default path,
fallback path, startup checks that block other providers, and automatic probes.
Credentials and entitlements are not preserved by backing up code.

## 4. Approved pilot

### 4.1 Frozen paired inputs

Use four code-review pairs and four brief-evaluation pairs. Each contains a
clean control and a single documented fault mutation. Each model receives
both variants in separate fresh calls, twice: `8 × 2 × 2 × 2 = 64` calls.
There are eight independent scenario units, not 64 independent tasks.

| Pair | Governing evidence and planted fault |
|---|---|
| C1 | Explicit five-tier rating contract; mutated order-direction mapping |
| C2 | Explicit quote-age interval; mutated future-quote acceptance |
| C3 | Explicit idempotency contract; mutated duplicate-submission guard |
| C4 | Explicit revision binding; mutated acceptance of a different brief's eval |
| B1 | Frozen primary numeric disclosure; one misquoted value or unit |
| B2 | Frozen citation body; one claim contradicted by its cited source |
| B3 | Fixed as-of/window; one event incorrectly presented as current |
| B4 | Explicit dated-catalyst rule; inflated score without qualifying evidence |

Extract compact source/spec excerpts where available and label synthetic
variants unmistakably in the private manifest, not in the model-visible
packet. Code packets include sufficient surrounding logic to adjudicate the
fault; brief packets include a frozen, independently checked evidence bundle.
No live market or broker data is required. Review packet contents for secrets.

Before any model call, freeze packet bytes, prompts, output schemas, answer
keys, source revisions, pairing map, schedule seed, and all relevant hashes.
An independent read-only check must confirm each clean control, planted fault,
and evidence location without seeing candidate outputs. If a defensible pair
cannot be built, stop preparation rather than substitute after observing results.

The model sees the review task and full relevant evidence, not labels,
expected findings, counterpart output, prior review verdicts, or other models.
Use the same role-specific instruction text and schema across both candidates;
provider-specific transport settings are recorded, not hidden prompt changes.

### 4.2 Execution and accounting

- Official DeepSeek Chat Completions, explicit `deepseek-v4-flash` and
  `deepseek-v4-pro`, thinking enabled, reasoning effort `high`.
- No tools, tool executor, graph construction, SDK retries, repair prompts,
  web retrieval during generation, or cross-provider fallback inside a sample.
- JSON output with strict local schema validation. Refusal, invalid JSON,
  missing metadata, truncation, unexpected tool calls, or an incomplete
  response is invalid/unavailable, never a content pass.
- Maximum 8,192 output tokens per call, including the provider's billed
  reasoning allocation under its API semantics; 240-second total wall deadline;
  at most two in-flight requests total. Verify these bounds before inference.
- Pre-reserve the entire schedule at peak, all-cache-miss prices before launch.
  Use a verified tokenizer or a conservative UTF-8-byte upper estimate plus
  explicit framing allowance; refuse a schedule whose reservation exceeds $5.
  Reserve uncertain/failed requests at their maximum, including in-flight work;
  do not free their reservation to make extra calls. Stop on missing accounting.
- Record provider-requested model, response model, request ID, usage, latency,
  finish reason, validity, packet hash, and reservation/estimated cost per
  unique attempt. Response model is provider-reported, not cryptographic proof.
  Do not retain API keys, authorization headers, or private reasoning text.
- Fixed seeded mixed first-round order, reversed second-round order. Retain
  failures in the denominator. No selective reruns, sample replacement, or
  additional trials funded by unused reservation. Crashed attempts stay spent.

At design time, official peak prices per million tokens are Flash $0.44
cache-miss input / $1.32 output, Pro $1.32 / $3.96. Recheck prices before
launch and re-reserve if changed; $5 remains the ceiling. Estimates are not
invoices. The experiment uses the existing API balance, never a top-up.

No Claude invocation or Claude judging is part of this pilot. GPT receives
the same packets only in a separately recorded extension after preventive
tool isolation and attempt detection are demonstrated. Do not alter a model
catalog, extract OAuth tokens, suppress unknown CLI errors, or silently switch
to paid OpenAI API access to make that extension work.

### 4.3 Outcomes and promotion threshold

Pre-register per-case criteria and severity before generation. Report
evidence-grounded detection, serious-error misses, unsupported findings on
controls, strict output validity, unavailable calls, repetition disagreement,
latency distributions, and token-based cost. Separate code and brief results;
do not merge them into a single subjective quality mean.

Score using executable or explicit source-level answer keys. Blind candidate
labels during adjudication; record a short final-output quote and source
location for every substantive finding. Any uncertain judgment receives a
second independent adjudication; unresolved uncertainty remains visible and
blocks promotion. No candidate grades itself. Old Claude judgments are
context only, not ground truth or a matched live baseline.

For this screening stage, any missed planted serious fault or unsupported
serious accusation on a clean control disqualifies that candidate from a
replacement recommendation for that role. All 32 planned responses per
candidate must be valid and accounted for before a positive screening result.
Still complete the pre-reserved schedule unless a security, budget, credential,
or transport failure requires stopping; do not stop selectively for quality.
Timeouts and malformed responses are reliability failures, not evidence of
incorrect reasoning. Report incomplete coverage as inconclusive.

A clean screen permits only shadow evaluation. It does not prove equivalence,
real-world defect recall, retrieval quality, production reliability, trade
execution, or profitability. A production choice requires larger held-out
cases and the role-specific integration gates below.

## 5. Recovery and future routing requirements

These are acceptance constraints for subsequent implementation, not active
configuration changes in this revision.

### 5.1 Backups and rollback

1. Preserve the baseline in a named Git backup ref and a verified standalone
   bundle outside the repository. Record the SHA, bundle hash, exclusions,
   and a restore check into a separate directory. Never reset the working tree
   or restore over new ledger/order/outcome data.
2. Before changing deployment configuration, save an exact restricted local
   snapshot and non-secret manifest. Secret-bearing configuration stays out
   of Git, model packets, logs, and shared artifacts; directories use 0700,
   secret files 0600. No credential copying is necessary before configuration
   changes, and a backup is not proof that an expired subscription works.
3. Keep historical model presets, prompts, reports, eval revisions, manifests,
   and experiment results append-only or revisioned. Never overwrite old
   Claude evidence with GPT/DeepSeek results. New formats read old formats
   without silently granting new policy eligibility to old reports.
4. Configuration rollback restores only the selected versioned routing profile
   after a diff check. It does not delete new data or roll back protective
   trading fixes. The cancellation-safe profile must remain usable without
   reinstalling Claude or restoring an expired credential.

### 5.2 Failure handling and audit

Use separate provider adapters and capability eligibility per role. Track
local configuration, authenticated availability, and observed model-call
health separately; a non-billable local check does not prove remaining quota.
An explicitly disabled provider is never probed automatically.

Classify configuration/authentication failure, known quota/429 exhaustion,
timeout, process/transport error, content verdict, schema failure, and evidence
insufficiency separately. Circuit breakers follow the shared provider/account
quota rather than assuming another model alias has independent allowance.
Record a trustworthy reset time when available; otherwise do not invent one
or repeatedly probe. Use bounded, documented cooldowns and manual recovery.

At most one eligible alternate provider may be attempted for a classified
availability failure, using the same frozen input and the remaining overall
deadline/budget. Never fall back because a reviewer returned `fail`, `warn`,
insufficient evidence, or invalid schema in order to obtain a `pass`.
The existing same-provider schema retry, if retained in production, remains
distinct, bounded, and accounted for; the pilot itself has zero retries.

Persist the primary failure and alternate result as an attempt chain. A
successful alternate is visibly degraded-provider success, not an erased
failure. If no eligible provider can complete, record unavailable/missing;
send a deduplicated actionable notification, not repeated alerts per ticker.
Do not create a new notification channel or expose secrets in errors.

### 5.3 Evidence, cache, and trading boundaries

Future eligibility must bind input revision, provider/requested and reported
model, prompt/schema versions, policy version, evidence snapshot hash, and
review scope. Code review additionally binds base/head SHAs and deterministic
test evidence. Build a versioned cache/report contract before changing readers.
Keep attempts under distinct IDs; an old or different-provider pass is not a
cache hit for a newly requested evaluation identity.

Code review may use fixed packets for screening, but production acceptance
needs its exact-SHA isolated-reading boundary. Runtime brief evaluation must
preserve independently selected searches, cited-source fetches and coverage
checks; a packet generated only from the collector's own assertions is not
independent verification. Tool calling capability alone does not implement it.

Use fresh contexts and explicit independence metadata. GPT reviewing GPT
collection is a same-provider degradation, not independent cross-vendor
verification. DeepSeek reviewing DeepSeek-generated manager decisions has a
similar limitation. Any allowed same-provider profile must be explicit, not
a silent fallback; high-risk independent-review eligibility must be satisfied
by a different, validated provider under the later approved policy.

Preserve current analysis semantics accurately: a missing eval can allow
flagged analysis, including qualifying opportunity analysis; it is not a
content `fail`. Auto-execution remains blocked by default. The cancellation-
safe profile requires `execute_on_missing_eval=false` and must refuse
activation if a local override enables it. Macro/ticker content failures keep
their existing guards; no fallback clears them. CN stays research-only and
US remains subject to all existing execution checks. No broker calls belong
to this study or to its outage simulations.

## 6. Cancellation readiness gates

Do not report that Claude cancellation is safe until all of these are evidenced:

- The selected non-Claude backend passes held-out role tests; Flash and Pro
  are qualified independently, not inherited from each other's result.
- Runtime retrieval parity and an isolated integration run pass before any
  scheduled evaluator change. Shadow reports never replace production evals.
- Provider-neutral development report/policy migration is explicitly accepted;
  mandatory risky revisions obtain a valid independent exact-SHA report.
  Until then, the current Claude gate blocks those revisions unless the user
  explicitly waives that exact SHA. This design is not such a waiver.
- Offline fault injection covers Claude absent from PATH, logged out,
  quota-exhausted, hung and malformed; a disabled Claude profile makes zero
  Claude subprocess calls and no recurring Claude-dependent jobs remain.
- Test primary success, primary unavailable/alternate success, both unavailable,
  invalid output, negative content verdict, cold and mismatched caches,
  concurrent attempts, cooldowns, and crash recovery without duplicate writes.
- All-provider outage preserves report viewing and clearly marked research
  where existing inputs support it, while preventing automatic execution and
  false passes. A provider outage cannot guarantee fresh research availability.
- Restore the selected configuration backup in an isolated environment and
  replay deterministic tests; do not test recovery by deleting production data.
- After separately approved rollout, observe at least one complete qualifying
  automatic CN slot and one US slot with Claude disabled, actual non-Claude
  evaluations and no hidden Claude subprocess attempts. Slot status alone is
  insufficient because missing evaluation may still permit flagged analysis.

The final material implementation checkpoint still needs its required review.
No production cancellation claim follows from this design or the 64-call pilot.

## 7. Immediate deliverables and next gate

This revision delivers the written design and a local recovery baseline only.
After the user reviews this written design, create the pilot implementation
plan and execute the already approved $5 study. Freeze its artifacts under a
new private directory in `~/.tradingagents/verification/benchmarks/`; do not
edit the earlier manager ablation or blocked stress study. Results determine
the next implementation plan rather than silently changing production roles.

The normal full offline gate applies to repository changes. No Claude call,
manual slot, scheduler edit, Lark send, model generation, execution call,
credential migration, or production configuration change is part of this
design-only revision.

## References

- [Current evaluator contract](../../../specs/macro-brief-evaluator.md)
- [Analysis gating contract](../../../specs/analysis-runner.md)
- [Existing development review design](2026-08-10-codex-claude-hybrid-workflow-design.md)
- [DeepSeek pricing](https://api-docs.deepseek.com/quick_start/pricing/), checked 2026-09-07
- [DeepSeek thinking controls](https://api-docs.deepseek.com/guides/thinking_mode/), checked 2026-09-07
