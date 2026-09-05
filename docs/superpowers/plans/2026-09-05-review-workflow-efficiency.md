# Review Workflow Efficiency Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans or superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reduce avoidable review handoff work and measure a bounded GPT/Claude comparison without weakening the existing Claude gate.

**Architecture:** Keep persisted report v1 and verdict computation intact. Add harness-owned test attestations, optional informational review notes, exact-checkout navigation, and per-run timing/archive artifacts around the existing runner. Run the model benchmark separately from the production gate.

**Tech Stack:** Python 3.14, Pydantic, pytest, existing Claude/Codex CLIs.

**Spec:** `docs/superpowers/specs/2026-08-10-codex-claude-hybrid-workflow-design.md`, 2026-09-05 efficiency revision.

## Global Constraints

- Claude receives only Read capability; no shell or primary-checkout access.
- No model calls in offline tests, no trading pipeline or broker calls.
- Keep v1 reports and `compute_verdict` compatible; no automatic waiver.
- Runtime reports stay outside the repository; do not persist raw output or credentials.
- Preserve existing tracked/untracked work and do not push or merge.

## Task 1: Evidence and navigation

Files: `devtools/verification/models.py`, `devtools/verification/claude_review.py`, new `devtools/verification/review_context.py`, `tests/test_review_context.py`, `tests/test_claude_review.py`.

Interfaces: `harness_test_evidence(output: str) -> list[TestRun]`; `navigation_index(root: Path, paths: Sequence[str], *, explicit: Sequence[str] = ()) -> str`. `ClaudeJudgment.review_notes` defaults to an empty string list; legacy report fields stay unchanged.

- [ ] Add a runner test asserting three persisted harness-owned passing commands, and a negative test rejecting model-supplied `tests_run`.
- [ ] Run targeted pytest and observe missing-behavior failures.
- [ ] Extract only numeric pytest summaries, write explicit harness provenance, and pass these records into `build_report`. Never remove existing real limitations.
- [ ] Add navigation tests with a real temporary Python/spec file, missing explicit path, traversal, hidden path and symlink; require correct heading/function line references and zero outside reads.
- [ ] Implement the index and `--context-file` plumbing; add packet instructions for source locations, notes and true validation gaps.
- [ ] Run all verification-module tests; retain prior report compatibility tests.

## Task 2: Timing, archive and concise report

Files: new `devtools/verification/review_runtime.py`, `devtools/verification/claude_review.py`, `tests/test_review_runtime.py`, `tests/test_claude_review.py`.

Interfaces: a per-run recorder provides stage contexts, safe metadata fields, `finish(status)` and a unique archive directory. The runner owns the recorder; subprocess execution and verdict authority remain unchanged.

- [ ] Add tests for success/failure stage durations using injected clocks, secret-free failures, same-head unique runs, symlink rejection, and diagnostic persistence failure.
- [ ] Observe failures, then implement atomic diagnostic JSON writes and stderr phase updates.
- [ ] Wrap detached checkout/Layer 1, packet, Claude, validation and persistence stages. Archive validated report pairs before updating the existing convenience paths.
- [ ] Add runner tests proving failed/invalid responses leave no successful current report and keep categorized run diagnostics; optional missing-auth still writes nothing.
- [ ] Add numbered Markdown finding index tests without losing full evidence or fabricating dispositions.
- [ ] Run targeted tests and Ruff.

## Task 3: Bounded comparison and final verification

Files: benchmark artifacts only under the configured external verification directory; operational notes in the existing workflow documentation.

- [ ] Freeze the full trade-brief design at `a4f48490f408134f9a7fb73547ff38b599484efe` plus declared source excerpts and hash the common packet.
- [ ] Run at most one initial sample each for `gpt-5.4-mini`, `gpt-5.6-luna`, `gpt-5.6-sol` at medium reasoning, plus one Claude control, sequentially with a 600-second per-call ceiling. Record unsupported models honestly, do not use API-key fallbacks.
- [ ] Validate output shape and compare actual findings with source evidence; record sampling and context limits rather than declaring a universal winner.
- [ ] Run `.venv/bin/python -m devtools.verification.offline --only all`.
- [ ] Commit only the coherent workflow changes, run the independent Claude review against that exact commit, and check its bound report. Resolve actual findings; no unauthorized waiver.
- [ ] Hand off implementation evidence and benchmark results separately; do not change the required reviewer based on the experiment.
