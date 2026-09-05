# Codex-Led, Claude-Verified Hybrid Workflow

**Date:** 2026-08-10
**Status:** Approved design; implementation pending
**Scope:** Development and verification workflow only. Trading pipeline behavior remains governed by `specs/`.

## Objective

Codex is the primary builder for future repository changes. Claude Code is an
independent verifier for changes that benefit from model-assisted testing and
review. Deterministic tools remain the mandatory baseline for every completed
change; Claude is an additional risk-based gate, not a replacement for pytest,
Ruff, contract validation, or live integration tests.

The workflow must:

1. keep ordinary development fast and reproducible;
2. spend Claude subscription capacity on changes that require judgment;
3. prevent concurrent Codex and Claude edits to the same worktree;
4. leave a machine-readable verification record for required Claude reviews;
5. fail closed for trading-safety, contract, data-integrity, and orchestration changes.

## Roles and Ownership

### Codex — builder and integrator

Codex owns implementation, production-code edits, tests, specs, contract-first
changes, diagnosis, fixes, deterministic verification, and final integration.
Codex evaluates Claude findings before applying them; a finding is evidence to
investigate, not an instruction to accept blindly.

### Claude Code — independent verifier

Claude receives a bounded review packet after Codex has a coherent change. It
checks the governing specs and contracts, reviews the diff and deterministic
Layer 1 evidence, adds adversarial test ideas, and reports findings. Claude has
only Read capability; it does not run shell commands. The runner always checks out
the head revision into an isolated temporary worktree and starts Claude there;
the primary worktree is never exposed as Claude's writable working directory.
Any experiment stays in the temporary worktree, which is removed after the
report is captured. Claude returns only a report or patch for Codex to assess.

### User — authority for exceptions

Only the user can waive a required Claude gate. The waiver records the commit,
reason, unverified risk, and date in the verification report; unavailable
credentials or exhausted quota are not implicit waivers. A waiver report is
stamped `reviewer: user-waiver`, never presented as a Claude review or `pass`,
and can be created only after an explicit user instruction for that revision.

## Verification Layers

### Layer 1 — deterministic gate

Run for every completed change and before every commit intended for handoff:

```bash
.venv/bin/python -m pytest tests/ -q -m "not integration"
.venv/bin/python -m ruff check .
git diff --check
```

The integration marker is excluded explicitly because integration tests may
make paid or network calls whenever local credentials are present. CI continues
to run the deterministic suite without model credentials. Live integration is
a separate, explicit layer.

Any Layer 1 failure blocks handoff and merge. Tests must not be reclassified or
skipped merely to make the gate green.

### Layer 2 — Claude risk gate

Claude review runs after Layer 1 passes when a required trigger is present, or
when Codex or the user requests it. The command performs these preflights:

- `claude auth status` reports a logged-in account;
- the review base and head commits resolve;
- the worktree has no unreported tracked edits outside the review diff;
- the Layer 1 result belongs to the same head revision.

Claude gets the review packet defined below and returns the structured report.
A required Claude verdict of `fail`, an invalid report, a timeout, or missing
authentication blocks merge until Claude passes or the user records a waiver.
A `warn` verdict requires Codex to disposition every finding before completion.
When Layer 2 is explicitly optional and Claude authentication is unavailable,
only the Claude invocation is suppressed: tracked cleanliness, exact base/head
resolution, detached exact-head creation, and Layer 1 in that checkout must
still succeed before the command may continue with no report. Invalid refs and
Layer 1 failures remain blocking, and this path never invokes Claude or writes
a review report.

### Layer 3 — live integration

Live tests are always opt-in and named explicitly. They include real model,
market-data, CLI-subscription, or broker-paper boundaries. They never run as a
side effect of the default local gate. Paper broker tests retain all existing
fail-closed execution guards; no live-money account is in scope.

Layer 3 is required only when the acceptance criteria name the external
boundary, before enabling or changing a scheduled CLI backend, or when a fix
cannot be demonstrated offline. The report states the provider, command,
timestamp, and whether the test consumed subscription or API quota; it never
stores credentials.

## Claude Trigger Policy

### Mandatory triggers

Any one of these requires Layer 2:

- changes under `pipeline/contracts/` or a shared data-contract spec;
- order submission, cancellation, position, quote-freshness, market-state, or
  other trading-safety behavior;
- possible data loss, duplicate writes, incorrect overwrite, broken revision
  binding, or audit-chain loss;
- concurrency, locks, timeouts, retries, atomic writes, idempotency, scheduling,
  session dates, time zones, or DST behavior;
- a nondeterministic or recurring bug;
- a failure that can appear successful while bad data proceeds downstream;
- a new component or an end-to-end feature;
- the final merge/PR checkpoint for a material change.

### Complex-bug score

A bug also requires Layer 2 when at least two of these are true:

- its root cause crosses two or more components or processes;
- the fix changes three or more logic files;
- unit tests alone cannot establish the fix;
- fallback, retry, gating, or exit-code semantics change;
- the behavior depends on an external CLI, network, model, or third-party service;
- the root cause remains unclear after roughly 30 minutes of investigation;
- multiple plausible fixes carry architectural trade-offs;
- the fix can affect normal paths that did not exhibit the original symptom.

A simple bug must be deterministic, local to one clear module, outside every
mandatory-risk area, coverable by one focused regression test, and behaviorally
compatible with public interfaces and failure semantics.

### Optional triggers

Documentation-only changes, comments, formatting, mechanical fixture updates,
and incomplete work-in-progress commits normally stop after Layer 1. Codex may
escalate any change to Claude when uncertainty remains.

## Review Packet

Codex provides Claude with a bounded packet so the review is reproducible:

- detached-checkout identity, base SHA, and head SHA;
- changed-file list and diff;
- governing component specs and data contracts;
- requested behavior and acceptance criteria;
- Codex's risk classification and known limitations;
- exact Layer 1 commands and results;
- the original symptom and regression test for a bug fix;
- explicit permission boundaries, including no primary-worktree access and no
  external integration calls unless Layer 3 was requested. The Claude model
  call required by Layer 2 is not itself a Layer 3 test.

The packet excludes primary-worktree paths, `.env`, credentials, unrelated
untracked files, generated runtime data, and prior model reasoning.

## Claude Report Contract

Reports live outside the repository by default:

```text
~/.tradingagents/verification/<head-sha>.claude.json
~/.tradingagents/verification/<head-sha>.claude.md
```

The JSON report is authoritative for automation:

```json
{
  "schema_version": 1,
  "base_sha": "...",
  "head_sha": "...",
  "reviewed_at": "2026-08-10T12:00:00Z",
  "reviewer": "claude",
  "verdict": "pass",
  "tests_run": [
    {"command": "...", "status": "pass", "summary": "..."}
  ],
  "findings": [
    {
      "severity": "high",
      "file": "pipeline/example.py",
      "line": 42,
      "title": "...",
      "evidence": "...",
      "suggested_test": "..."
    }
  ],
  "limitations": [],
  "waiver": null
}
```

Allowed verdicts are `pass`, `warn`, and `fail`; severities are `critical`,
`high`, `medium`, and `low`; reviewer is `claude` or `user-waiver`. A user-waiver
report always has verdict `warn`, a non-null waiver object, and no fabricated
Claude tests or findings. Claude report verdicts are deterministic: any failed
test produces `fail`; otherwise any `not_run` test produces at least `warn`;
passing tests are neutral; critical/high findings produce `fail`; other
findings or limitations produce at least `warn`; only evidence with none of
those conditions produces `pass`. Every Claude report must store exactly the
verdict recomputed from its tests, findings, and limitations, so contradictory
persisted JSON is schema-invalid. The user-waiver shape is the sole explicit
exception: it stores `warn`, no tests or findings, and its waiver limitation.
A report is valid only when `head_sha` equals the revision being handed off.
Re-reviewing a changed revision creates a new report. The Markdown companion is
for humans and must not contradict the JSON verdict.

## End-to-End Development Flow

### 2026-09-05 efficiency revision (user-approved scope)

The four approved improvements are evidence handoff, navigable review context,
stage timing, and a concise finding-disposition handoff. They do not replace
Claude with GPT, loosen Read-only permissions, or change `compute_verdict`.

- Layer 1 remains mandatory in the detached exact-head checkout. The harness
  supplies the three successful normalized commands in persisted `tests_run`;
  their summaries explicitly identify harness execution. Only allowlisted pytest
  counts are extracted from captured output, never arbitrary stdout/stderr.
  Claude returns `tests_run: []`; an attempted model-authored test attestation is
  rejected rather than silently trusted, removed, or converted to pass.
  Normalized command attestations derive from the offline gate's step builder,
  with regression coverage for gate argument and ordering changes.
  Before running Layer 1, the isolated target's offline gate source must match
  the invoking harness's definition byte-for-byte. A historical `--head` with a
  different gate definition fails before subprocess/model execution; use that
  revision's matching harness rather than falsely attesting current commands.
- Claude may return `review_notes` for informational scope/permission statements
  and positive confirmations. These notes go to the per-run diagnostic archive,
  not the v1 report's `limitations`. `limitations` continues to mean material
  missing verification; every actual finding/limitation retains its old verdict
  effect. Existing v1 reports and user-waiver behavior remain unchanged. Historical
  reports are never normalized or rewritten to remove warnings.
  Notes are bounded to 32 entries of 4000 characters each and must encode as
  UTF-8; invalid notes fail schema validation before any report publication.
- The packet contains an exact-head file/heading/function navigation index.
  Explicit repeatable `--context-file` paths must be safe repository-relative,
  non-secret source/spec/test files present in that detached checkout; symlinks,
  hidden paths, traversal and missing files are rejected. Navigation is a reading
  aid, not a substitute for independent source inspection. Findings must provide
  structured file and positive line fields, or explain why location is unknown.
  The packet distinguishes approved tradeoffs from defects and docs-only scope
  from unimplemented requirements; it must not instruct the reviewer to pass.
- Each authenticated review run has a random run ID and a diagnostic JSON under
  `verification/runs/<head-sha>/<run-id>/`. It records exact identity, stage
  start/end and monotonic durations, packet byte count/hash, safe failure category,
  and informational notes. Stage transitions are emitted to stderr; stdout keeps
  its existing final-result contract. Raw subprocess output, secrets, primary
  paths and model reasoning are not diagnostic artifacts. An unfinished archive
  means interrupted/unknown, not a successful review. Timings measure the whole
  Claude CLI boundary, not unobserved provider reasoning time.
  Stderr progress is best-effort: a closed progress channel cannot discard an
  otherwise completed review. Required on-disk diagnostics remain fail-closed.
- Completed per-run archives retain the validated JSON/Markdown report before
  updating the existing `<head>.claude.*` convenience paths. Repeated reviews of
  the same head therefore retain prior attempts. Before replacement, any existing
  pair (including pre-upgrade evidence) is validated and preserved byte-for-byte
  under the new run's `previous/` directory; invalid or symlinked prior evidence
  blocks replacement. Diagnostic or archive write
  failure blocks success; failure recording must not mask the original error.
  Optional missing-auth behavior still runs Layer 1 without creating a report.
  Claude Read-deny rules include the selected report root, configured default
  and standard verification root as well as preexisting repository boundaries,
  so prior reports and benchmark findings are not review context. These explicit
  denials supplement the instruction to read only the isolated checkout; they
  are not a claim of a complete host-filesystem sandbox.
- The human report starts with a numbered findings index linking each item to its
  structured location. Full evidence remains below; Codex dispositions are not
  invented by the renderer. This supports targeted parallel checks and avoids
  reading both JSON and Markdown to discover the same findings.

The explicitly requested GPT comparison is a separate, bounded live experiment:
use existing authenticated CLI subscriptions, identical frozen source material,
no tools or external integration access, fixed parameters and reported limits.
Archive results under `verification/benchmarks/`, never as a Claude gate report.
Measure wall time and review quality separately; unsupported models, timeouts,
single-sample uncertainty and unequal provider internals remain visible.
If the Codex CLI cannot hide `apply_patch`, retain read-only sandboxing with all
other external tools disabled; only samples with zero observed tool invocations
qualify as packet-only. Record the tool-visibility difference, reject tool-using
samples, and never loosen the sandbox to complete a benchmark.

Example explicit reading navigation for a workflow review:

```bash
.venv/bin/python -m devtools.verification.claude_review run --base HEAD~1 --head HEAD \
  --acceptance "Verify the committed workflow change and its regression tests" \
  --risk "mandatory: verification interface and evidence integrity" \
  --original-symptom "Reviewer test attribution and timing were ambiguous" \
  --context-file devtools/verification/models.py \
  --context-file tests/test_claude_review.py
```

Stage progress appears on stderr during the run. After completion, use the normal
`check --head HEAD` command; inspect `runs/<head>/<run-id>/run.json` for timings and
safe reason codes, and the Markdown finding index for targeted disposition. A
diagnostic artifact is not an acceptance report and does not override `check`.

1. Codex reads the contract and component spec before changing a shared interface.
2. Feature work uses design and implementation planning; bug work uses systematic
   diagnosis. Behavior changes are developed with a failing regression test first.
3. Codex implements the smallest coherent change and runs targeted tests.
4. Codex runs Layer 1 on the complete change.
5. Codex evaluates the mandatory-trigger and complex-bug rules.
6. If Layer 2 is required, Codex invokes Claude with the review packet and waits
   for a valid report for the current head revision.
7. Codex verifies every Claude finding. Valid findings are fixed with regression
   coverage; invalid findings receive an evidence-based disposition.
8. Codex reruns Layer 1. Material fixes invalidate the prior Claude report and
   trigger one final Claude review when Layer 2 is mandatory.
9. Layer 3 runs only when explicitly required.
10. Codex hands off the change with deterministic results, Claude verdict or
    waiver, live-test status, and unresolved limitations.

## Common Workflow Mapping

- New feature or behavioral design: brainstorming → writing plan → test-driven
  development → verification → Claude risk gate when triggered.
- Bug: systematic debugging → regression test → fix → verification → Claude
  risk gate when the complex-bug policy triggers.
- Claude findings: receiving-code-review workflow → evidence check → fix or
  documented rejection → verification.
- Merge readiness: requesting-code-review workflow for the Claude packet,
  followed by verification-before-completion.

These workflows guide Codex's process; they do not grant extra permissions or
change the contracts in `specs/`.

## Automation Boundary

The existing GitHub Actions workflow remains the deterministic CI gate. It does
not receive local Claude subscription credentials. Claude verification runs from
the user's logged-in local environment at explicit risk checkpoints rather than
on every commit. This avoids copying OAuth credentials into hosted CI, consuming
quota on work-in-progress commits, and making ordinary CI nondeterministic.

The initial implementation provides explicit local commands and no Git hook.
Automatic pre-commit/pre-push invocation is deferred. A future hook may validate
that a required current-revision Claude report exists, but it must not invoke
Claude automatically unless the user separately approves that behavior.

Trading-session launchd scheduling is independent of this development workflow.
The scheduled `claude -p` brief evaluator is runtime pipeline behavior, not code
review, and its output remains governed by the brief evaluation contracts.

## Failure Handling

- Layer 1 failure: block immediately and report the failing command.
- Required Layer 2 unavailable or invalid: block merge; do not silently downgrade.
- Optional Layer 2 authentication unavailable: suppress only Layer 2 and
  continue without a report only after tracked-clean/ref preflights and Layer 1
  pass in a detached exact-head checkout; invalid refs and Layer 1 failures block.
- Claude `fail`: block until corrected and re-reviewed, or explicitly waived.
- Claude `warn`: Codex dispositions are mandatory; unresolved critical/high
  findings block completion.
- Explicit user waiver: satisfies the gate for the bound revision but is
  reported as `waived`, never `pass`; changing the revision invalidates it.
- Stale report SHA: treat as missing.
- Live integration unavailable: record `not_run` with the reason; whether it
  blocks depends on the component acceptance criteria.

## Initial Implementation Scope

The first implementation should add only:

1. a deterministic verification command that excludes integration tests;
2. a Claude review runner with auth, revision, timeout, and JSON-schema checks;
3. a small report schema/validator, explicit user-waiver command, and local
   report directory;
4. documented risk classification and handoff commands in `AGENTS.md`;
5. CI alignment so local and GitHub deterministic gates use the same command;
6. offline tests for report validation, stale-SHA rejection, temporary-worktree
   isolation, and failure semantics.

Risk classification is manual in the first implementation. Automatic risk
inference, hosted Claude review, Git hooks, dashboards, and unattended credential
management are explicitly deferred.

## Acceptance Criteria

1. One command runs the complete deterministic local gate without making live
   network/model calls, even when provider keys exist in the environment.
2. A required Claude review cannot pass with missing auth, malformed output, a
   timeout, a `fail` verdict, or a report for another head SHA.
3. A valid current-revision `pass` report satisfies Layer 2; `warn` and `fail`
   follow the failure rules above. An explicitly authorized current-revision
   waiver satisfies the gate while remaining visibly `waived`.
4. Mandatory triggers and the two-point complex-bug rule are documented in the
   agent guide; the first implementation does not claim to infer them automatically.
5. Claude cannot edit the primary worktree through the review runner.
6. Generated reports and credentials never enter Git; reports default to
   `~/.tradingagents/verification/`.
7. Existing pipeline tests and Ruff remain green, and GitHub CI uses the same
   deterministic test selection as the local gate.
