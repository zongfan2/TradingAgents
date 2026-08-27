# Codex-Claude Hybrid Workflow Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a deterministic offline verification command and a local, revision-bound Claude review gate that lets Codex lead development while Claude independently verifies risky changes from an isolated worktree.

**Architecture:** A small `devtools.verification` package owns two independent command-line entry points: `offline` composes pytest, Ruff, and diff checks; `claude_review` preflights local Claude authentication, checks out the reviewed commit in a temporary detached worktree, invokes read-only Claude, validates its structured judgment, and writes SHA-bound reports outside the repository. Claude has only Read, Grep, and Glob capabilities; it does not run shell commands. GitHub Actions runs the same deterministic command set but never receives local Claude credentials.

**Tech Stack:** Python 3.10+, Pydantic, `subprocess`, `tempfile`, Git worktrees, pytest, Ruff, GitHub Actions, Claude Code CLI.

## Global Constraints

- Trading pipeline behavior and file contracts remain governed by `specs/`; this feature changes development workflow only.
- Codex is the builder/integrator; Claude is a verifier and must not edit the primary worktree.
- Default deterministic verification must make no live model, market-data, or broker calls even when credentials exist.
- Integration tests stay opt-in via the existing `integration` pytest marker.
- Generated reports default to `~/.tradingagents/verification/` and never enter Git.
- Missing auth, invalid output, timeouts, stale SHA, or `fail` block a required Claude gate; no silent downgrade is allowed.
- No Git hooks, hosted Claude credentials, automated risk inference, dashboards, or unattended credential management in the first implementation.
- Preserve unrelated `.DS_Store`, `.beads/`, and any other user-owned untracked or modified files.

---

## File Structure

- Create `devtools/__init__.py`: marks repository development tooling as a package.
- Create `devtools/verification/__init__.py`: lightweight package marker; it deliberately imports no Pydantic models so the Ruff-only CI job needs only Ruff.
- Create `devtools/verification/models.py`: strict Claude judgment/report models, SHA validation, and deterministic verdict computation.
- Create `devtools/verification/offline.py`: deterministic verification step builder, runner, and CLI.
- Create `devtools/verification/claude_review.py`: auth/git preflights, review packet, temporary worktree, Claude invocation, report persistence, report checking, and CLI.
- Create `tests/test_verification_models.py`: report contract and verdict tests.
- Create `tests/test_offline_verification.py`: deterministic command composition and failure behavior.
- Create `tests/test_claude_review.py`: auth, revision, isolation, parsing, persistence, and exit-semantics tests.
- Create `tests/test_hybrid_workflow_docs.py`: guards CI and agent-guide workflow policy.
- Modify `pyproject.toml`: include `devtools*` in package discovery.
- Modify `.github/workflows/ci.yml`: call the deterministic verifier with explicit test/lint selections.
- Modify `AGENTS.md`: replace historical component ownership with the approved Codex-led, Claude-verified workflow and commands.

### Task 1: Strict Claude report contract

**Files:**
- Create: `devtools/__init__.py`
- Create: `devtools/verification/__init__.py`
- Create: `devtools/verification/models.py`
- Create: `tests/test_verification_models.py`
- Modify: `pyproject.toml:53-54`

**Interfaces:**
- Produces from `devtools.verification.models`: `Severity`, `ReviewVerdict`, `Reviewer`, `TestStatus`, `TestRun`, `Finding`, `ClaudeJudgment`, `Waiver`, `ReviewReport`.
- Produces: `compute_verdict(tests_run: Sequence[TestRun], findings: Sequence[Finding], limitations: Sequence[str]) -> ReviewVerdict`.
- Produces: `build_report(judgment: ClaudeJudgment, *, base_sha: str, head_sha: str, reviewed_at: datetime) -> ReviewReport`.
- Produces: `build_waiver_report(*, base_sha: str, head_sha: str, waiver: Waiver) -> ReviewReport`.
- Later tasks rely on strict `extra="forbid"`, lower-case enum values, and 40–64 character lowercase hexadecimal commit SHAs.

- [ ] **Step 1: Write failing model tests**

Create `tests/test_verification_models.py` with these cases:

```python
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from devtools.verification.models import (
    ClaudeJudgment,
    Finding,
    ReviewReport,
    ReviewVerdict,
    TestRun,
    Waiver,
    build_report,
    build_waiver_report,
    compute_verdict,
)

BASE = "a" * 40
HEAD = "b" * 40


def finding(severity: str) -> Finding:
    return Finding(
        severity=severity,
        file="pipeline/example.py",
        line=42,
        title="unsafe fallback",
        evidence="the error path returns success",
        suggested_test="assert a non-zero exit",
    )


def model_test_run(status: str) -> TestRun:
    return TestRun(command="pytest -q", status=status, summary="controlled result")


@pytest.mark.unit
@pytest.mark.parametrize(
    ("tests_run", "findings", "limitations", "expected"),
    [
        ([], [], [], ReviewVerdict.PASS),
        ([model_test_run("pass")], [], [], ReviewVerdict.PASS),
        ([model_test_run("fail")], [], [], ReviewVerdict.FAIL),
        ([model_test_run("not_run")], [], [], ReviewVerdict.WARN),
        ([], [finding("low")], [], ReviewVerdict.WARN),
        ([], [finding("medium")], [], ReviewVerdict.WARN),
        ([], [finding("high")], [], ReviewVerdict.FAIL),
        ([], [finding("critical")], [], ReviewVerdict.FAIL),
        ([], [], ["live boundary not exercised"], ReviewVerdict.WARN),
    ],
)
def test_compute_verdict(tests_run, findings, limitations, expected):
    assert compute_verdict(tests_run, findings, limitations) is expected


@pytest.mark.unit
def test_build_report_stamps_identity_and_recomputes_verdict():
    judgment = ClaudeJudgment(tests_run=[], findings=[finding("high")], limitations=[])
    report = build_report(
        judgment,
        base_sha=BASE,
        head_sha=HEAD,
        reviewed_at=datetime(2026, 8, 10, tzinfo=timezone.utc),
    )
    assert report.schema_version == 1
    assert report.reviewer == "claude"
    assert report.verdict is ReviewVerdict.FAIL
    assert report.base_sha == BASE
    assert report.head_sha == HEAD


@pytest.mark.unit
def test_report_rejects_bad_sha_and_unknown_fields():
    judgment = ClaudeJudgment(tests_run=[], findings=[], limitations=[])
    with pytest.raises(ValidationError):
        build_report(
            judgment,
            base_sha="not-a-sha",
            head_sha=HEAD,
            reviewed_at=datetime.now(timezone.utc),
        )
    with pytest.raises(ValidationError):
        ClaudeJudgment(tests_run=[], findings=[], limitations=[], invented=True)


@pytest.mark.unit
def test_finding_line_must_be_positive_when_present():
    with pytest.raises(ValidationError):
        Finding(
            severity="low",
            file="x.py",
            line=0,
            title="bad line",
            evidence="line must be positive",
            suggested_test="construct the model",
        )


@pytest.mark.unit
def test_user_waiver_is_visible_and_never_a_pass():
    report = build_waiver_report(
        base_sha=BASE,
        head_sha=HEAD,
        waiver=Waiver(
            approved_by="user",
            approved_at="2026-08-10T12:00:00Z",
            reason="Claude quota unavailable",
            unverified_risk="scheduler behavior was not independently reviewed",
        ),
    )
    assert report.reviewer.value == "user-waiver"
    assert report.verdict is ReviewVerdict.WARN
    assert report.waiver is not None
    assert report.tests_run == [] and report.findings == []


@pytest.mark.unit
def test_claude_report_cannot_carry_a_waiver():
    with pytest.raises(ValidationError, match="waiver"):
        ReviewReport(
            schema_version=1,
            base_sha=BASE,
            head_sha=HEAD,
            reviewed_at="2026-08-10T12:00:00Z",
            reviewer="claude",
            verdict="warn",
            tests_run=[],
            findings=[],
            limitations=[],
            waiver={
                "approved_by": "user",
                "approved_at": "2026-08-10T12:00:00Z",
                "reason": "no",
                "unverified_risk": "unknown",
            },
        )
```

- [ ] **Step 2: Run the tests and verify they fail**

Run:

```bash
.venv/bin/python -m pytest tests/test_verification_models.py -q
```

Expected: collection fails with `ModuleNotFoundError: No module named 'devtools'`.

- [ ] **Step 3: Implement the strict models**

Add empty package initializers and implement `models.py` with this public shape:

```python
from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

SHA_PATTERN = r"^[0-9a-f]{40,64}$"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Severity(str, Enum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class ReviewVerdict(str, Enum):
    PASS = "pass"
    WARN = "warn"
    FAIL = "fail"


class Reviewer(str, Enum):
    CLAUDE = "claude"
    USER_WAIVER = "user-waiver"


class TestStatus(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    NOT_RUN = "not_run"


class TestRun(StrictModel):
    command: str = Field(min_length=1)
    status: TestStatus
    summary: str = Field(min_length=1)


class Finding(StrictModel):
    severity: Severity
    file: str | None = None
    line: int | None = Field(default=None, ge=1)
    title: str = Field(min_length=1)
    evidence: str = Field(min_length=1)
    suggested_test: str = Field(min_length=1)


class ClaudeJudgment(StrictModel):
    tests_run: list[TestRun]
    findings: list[Finding]
    limitations: list[str]


class Waiver(StrictModel):
    approved_by: Literal["user"]
    approved_at: datetime
    reason: str = Field(min_length=1)
    unverified_risk: str = Field(min_length=1)


class ReviewReport(StrictModel):
    schema_version: Literal[1] = 1
    base_sha: str = Field(pattern=SHA_PATTERN)
    head_sha: str = Field(pattern=SHA_PATTERN)
    reviewed_at: datetime
    reviewer: Reviewer = Reviewer.CLAUDE
    verdict: ReviewVerdict
    tests_run: list[TestRun]
    findings: list[Finding]
    limitations: list[str]
    waiver: Waiver | None = None

    @model_validator(mode="after")
    def validate_reviewer_shape(self):
        if self.reviewer is Reviewer.CLAUDE:
            if self.waiver is not None:
                raise ValueError("claude report cannot carry a waiver")
            expected = compute_verdict(
                self.tests_run, self.findings, self.limitations
            )
            if self.verdict is not expected:
                raise ValueError("claude report verdict contradicts review evidence")
        if self.reviewer is Reviewer.USER_WAIVER:
            if self.waiver is None or self.verdict is not ReviewVerdict.WARN:
                raise ValueError("user-waiver report requires waiver and warn verdict")
            if self.tests_run or self.findings:
                raise ValueError("user-waiver report cannot fabricate tests or findings")
        return self


def compute_verdict(
    tests_run: Sequence[TestRun],
    findings: Sequence[Finding],
    limitations: Sequence[str],
) -> ReviewVerdict:
    statuses = {item.status for item in tests_run}
    severities = {item.severity for item in findings}
    if TestStatus.FAIL in statuses or severities & {
        Severity.CRITICAL,
        Severity.HIGH,
    }:
        return ReviewVerdict.FAIL
    if TestStatus.NOT_RUN in statuses or severities or limitations:
        return ReviewVerdict.WARN
    return ReviewVerdict.PASS


def build_report(
    judgment: ClaudeJudgment,
    *,
    base_sha: str,
    head_sha: str,
    reviewed_at: datetime,
) -> ReviewReport:
    return ReviewReport(
        base_sha=base_sha,
        head_sha=head_sha,
        reviewed_at=reviewed_at,
        verdict=compute_verdict(
            judgment.tests_run, judgment.findings, judgment.limitations
        ),
        tests_run=judgment.tests_run,
        findings=judgment.findings,
        limitations=judgment.limitations,
    )


def build_waiver_report(*, base_sha: str, head_sha: str, waiver: Waiver) -> ReviewReport:
    return ReviewReport(
        base_sha=base_sha,
        head_sha=head_sha,
        reviewed_at=waiver.approved_at,
        reviewer=Reviewer.USER_WAIVER,
        verdict=ReviewVerdict.WARN,
        tests_run=[],
        findings=[],
        limitations=[f"Claude review waived: {waiver.unverified_risk}"],
        waiver=waiver,
    )
```

Keep `devtools/verification/__init__.py` empty except for its module docstring. Add `"devtools*"` to `[tool.setuptools.packages.find].include` so the commands also work from an installed checkout.

- [ ] **Step 4: Run model tests and Ruff**

Run:

```bash
.venv/bin/python -m pytest tests/test_verification_models.py -q
.venv/bin/python -m ruff check devtools tests/test_verification_models.py pyproject.toml
```

Expected: all tests pass and Ruff reports `All checks passed!`.

- [ ] **Step 5: Commit Task 1**

```bash
git add devtools/__init__.py devtools/verification/__init__.py devtools/verification/models.py tests/test_verification_models.py pyproject.toml
git commit -m "feat(devtools): add Claude review report contract"
```

### Task 2: Deterministic offline verification command

**Files:**
- Create: `devtools/verification/offline.py`
- Create: `tests/test_offline_verification.py`

**Interfaces:**
- Consumes: repository Python environment and existing pytest markers.
- Produces: `GateStep(name: str, argv: tuple[str, ...])`.
- Produces: `build_steps(repo_root: Path, python_executable: Path, only: str = "all") -> tuple[GateStep, ...]`.
- Produces: `run_gate(..., runner: Runner = subprocess.run) -> int` and `main(argv: Sequence[str] | None = None) -> int`.
- Supported `--only` values: `all`, `tests`, `lint`, `diff`; default is `all`.

- [ ] **Step 1: Write failing command-composition tests**

Create `tests/test_offline_verification.py`:

```python
import subprocess
from pathlib import Path

import pytest

from devtools.verification import offline


@pytest.mark.unit
def test_all_steps_are_offline_and_ordered(tmp_path):
    steps = offline.build_steps(tmp_path, Path("/venv/python"))
    assert [step.name for step in steps] == ["tests", "lint", "diff"]
    assert steps[0].argv == (
        "/venv/python", "-m", "pytest", "tests/", "-q", "-m", "not integration"
    )
    assert steps[1].argv == ("/venv/python", "-m", "ruff", "check", ".")
    assert steps[2].argv == ("git", "diff", "--check")


@pytest.mark.unit
@pytest.mark.parametrize("only", ["tests", "lint", "diff"])
def test_only_selects_one_step(tmp_path, only):
    assert [s.name for s in offline.build_steps(tmp_path, Path("python"), only)] == [only]


@pytest.mark.unit
def test_gate_stops_at_first_failure(tmp_path):
    calls = []

    def runner(argv, **kwargs):
        calls.append(tuple(argv))
        code = 7 if "pytest" in argv else 0
        return subprocess.CompletedProcess(argv, code)

    assert offline.run_gate(tmp_path, Path("python"), runner=runner) == 7
    assert len(calls) == 1


@pytest.mark.unit
def test_unknown_selection_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="unknown verification selection"):
        offline.build_steps(tmp_path, Path("python"), "network")
```

- [ ] **Step 2: Run the tests and verify they fail**

Run:

```bash
.venv/bin/python -m pytest tests/test_offline_verification.py -q
```

Expected: import fails because `devtools.verification.offline` does not exist.

- [ ] **Step 3: Implement the gate runner and CLI**

Implement these invariants in `offline.py`:

```python
@dataclass(frozen=True)
class GateStep:
    name: str
    argv: tuple[str, ...]


def build_steps(repo_root: Path, python_executable: Path, only: str = "all") -> tuple[GateStep, ...]:
    steps = (
        GateStep("tests", (str(python_executable), "-m", "pytest", "tests/", "-q", "-m", "not integration")),
        GateStep("lint", (str(python_executable), "-m", "ruff", "check", ".")),
        GateStep("diff", ("git", "diff", "--check")),
    )
    if only == "all":
        return steps
    selected = tuple(step for step in steps if step.name == only)
    if not selected:
        raise ValueError(f"unknown verification selection: {only}")
    return selected
```

`run_gate` must invoke each command with `cwd=repo_root`, never `shell=True`, print `==> <name>: <argv>` before execution, stop on the first non-zero return code, and return zero only after every selected step succeeds.

The CLI resolves the repository from `Path(__file__).resolve().parents[2]`, accepts an internal/public `--repo` override used by isolated review worktrees, uses `--python` when supplied, otherwise the invoking `sys.executable`, and fails with one-line stderr if the repository or Python executable is absent. `python -m devtools.verification.offline --only all` is the canonical local command.

- [ ] **Step 4: Run focused tests and the real deterministic gate**

Run:

```bash
.venv/bin/python -m pytest tests/test_offline_verification.py -q
.venv/bin/python -m devtools.verification.offline --only all
```

Expected: focused tests pass; the real gate reports 1459 or more non-integration tests passing, Ruff clean, and diff check clean. The DeepSeek live integration test must be deselected even when `DEEPSEEK_API_KEY` exists.

- [ ] **Step 5: Commit Task 2**

```bash
git add devtools/verification/offline.py tests/test_offline_verification.py
git commit -m "feat(devtools): add deterministic offline verification gate"
```

### Task 3: Claude review preflights, packet, and worktree isolation

**Files:**
- Create: `devtools/verification/claude_review.py`
- Create: `tests/test_claude_review.py`

**Interfaces:**
- Consumes: Task 1 `ClaudeJudgment`, `ReviewReport`, and `build_report`.
- Produces: `ReviewError`, `AuthUnavailable`, `resolve_commit`, `ensure_tracked_clean`, `check_claude_auth`, `parse_judgment`, `build_review_packet`, and `detached_worktree`.
- All subprocesses receive argv lists with `shell=False`; secrets and `.env` content are never included in the packet.

- [ ] **Step 1: Write failing preflight and parser tests**

Start `tests/test_claude_review.py` with injected-command tests:

```python
import json
import subprocess
from pathlib import Path

import pytest

from devtools.verification import claude_review as review


def completed(argv, code=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(argv, code, stdout=stdout, stderr=stderr)


@pytest.mark.unit
def test_auth_requires_logged_in_true():
    assert review.check_claude_auth(
        runner=lambda argv, **kwargs: completed(argv, stdout='{"loggedIn": true}')
    ) is None
    with pytest.raises(review.AuthUnavailable, match="not logged in"):
        review.check_claude_auth(
            runner=lambda argv, **kwargs: completed(argv, stdout='{"loggedIn": false}')
        )


@pytest.mark.unit
def test_resolve_commit_returns_full_lowercase_sha(tmp_path):
    sha = "A" * 40
    runner = lambda argv, **kwargs: completed(argv, stdout=sha + "\n")
    assert review.resolve_commit(tmp_path, "HEAD", runner=runner) == sha.lower()


@pytest.mark.unit
def test_primary_preflight_rejects_tracked_edits_but_ignores_untracked(tmp_path):
    dirty = lambda argv, **kwargs: completed(argv, stdout=" M pipeline/x.py\n")
    with pytest.raises(review.ReviewError, match="tracked edits"):
        review.ensure_tracked_clean(tmp_path, runner=dirty)
    clean = lambda argv, **kwargs: completed(argv, stdout="")
    assert review.ensure_tracked_clean(tmp_path, runner=clean) is None


@pytest.mark.unit
def test_parse_judgment_accepts_plain_and_fenced_json():
    payload = {"tests_run": [], "findings": [], "limitations": []}
    assert review.parse_judgment(json.dumps(payload)).findings == []
    assert review.parse_judgment(f"```json\n{json.dumps(payload)}\n```").limitations == []


@pytest.mark.unit
def test_parse_judgment_rejects_prose_or_unknown_fields():
    with pytest.raises(review.ReviewError, match="valid judgment JSON"):
        review.parse_judgment("looks good")
    with pytest.raises(review.ReviewError, match="schema"):
        review.parse_judgment(
            '{"tests_run": [], "findings": [], "limitations": [], "verdict": "pass"}'
        )


@pytest.mark.unit
def test_packet_contains_refs_policy_and_results_but_not_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "must-not-leak")
    packet = review.build_review_packet(
        repo=tmp_path,
        base_sha="a" * 40,
        head_sha="b" * 40,
        diff="diff --git a/x b/x",
        changed_files=["pipeline/x.py", "tests/test_x.py"],
        acceptance="fallback returns non-zero",
        risk="complex bug: retry semantics changed",
        layer1_result="1459 passed; ruff clean",
        original_symptom="invalid output returned exit 0",
    )
    assert "a" * 40 in packet and "b" * 40 in packet
    assert "must-not-leak" not in packet
    assert "Do not edit the primary worktree" in packet
    assert "Return JSON only" in packet
```

- [ ] **Step 2: Run the tests and verify they fail**

Run:

```bash
.venv/bin/python -m pytest tests/test_claude_review.py -q
```

Expected: import fails because `claude_review.py` does not exist.

- [ ] **Step 3: Implement subprocess and parsing helpers**

Implement:

```python
class ReviewError(RuntimeError):
    pass


class AuthUnavailable(ReviewError):
    pass


Runner = Callable[..., subprocess.CompletedProcess[str]]


def default_runner(argv, *, cwd=None, input=None, timeout=120):
    return subprocess.run(
        list(argv),
        cwd=cwd,
        input=input,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
```

`check_claude_auth` executes `claude auth status --json` with a 15-second timeout, distinguishes missing CLI, timeout, non-zero status, invalid JSON, and `loggedIn != true`, and raises `AuthUnavailable` with a one-line reason.

`resolve_commit` executes `git rev-parse --verify <ref>^{commit}`, requires a 40–64 character hexadecimal result, and normalizes it to lowercase. `ensure_tracked_clean` runs `git status --porcelain --untracked-files=no` and rejects any output, deliberately ignoring user-owned untracked files. Add private `git_text(repo, argv, runner)` for `git diff --no-ext-diff <base>..<head>` and `git diff --name-only <base>..<head>`.

`parse_judgment` strips one optional Markdown JSON fence, parses exactly one JSON object, validates `ClaudeJudgment`, and translates JSON/Pydantic errors to `ReviewError` without dumping the whole model output.

`build_review_packet` renders the approved roles, base/head, changed files, diff, acceptance criteria, risk classification, Layer 1 result, original symptom, read-only permission boundaries, no-primary-access rule, no-external-integration rule, and the exact `ClaudeJudgment` JSON schema. It must not read `.env` or serialize `os.environ`.

- [ ] **Step 4: Add a real temporary-worktree isolation test**

Append a test that creates a temporary Git repository with two commits and uses the real Git binary:

```python
@pytest.mark.unit
def test_detached_worktree_is_not_primary_and_is_removed(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    (repo / "tracked.txt").write_text("one\n", encoding="utf-8")
    subprocess.run(["git", "add", "tracked.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "base"], cwd=repo, check=True, capture_output=True)
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()

    with review.detached_worktree(repo, head) as isolated:
        assert isolated != repo
        assert (isolated / "tracked.txt").read_text(encoding="utf-8") == "one\n"
        (isolated / "claude-scratch.txt").write_text("temporary", encoding="utf-8")

    assert not isolated.exists()
    assert not (repo / "claude-scratch.txt").exists()
    listed = subprocess.run(
        ["git", "worktree", "list", "--porcelain"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert str(isolated) not in listed
```

- [ ] **Step 5: Implement `detached_worktree` and run focused tests**

Use `tempfile.TemporaryDirectory(prefix="tradingagents-claude-review-")`, create a non-existent `<temp>/worktree` target, run `git worktree add --detach <target> <head_sha>`, yield the target, and always run `git worktree remove --force <target>` plus `git worktree prune` in `finally`. Cleanup failures must be surfaced as warnings without hiding an earlier review failure.

Run:

```bash
.venv/bin/python -m pytest tests/test_claude_review.py -q
.venv/bin/python -m ruff check devtools/verification/claude_review.py tests/test_claude_review.py
```

Expected: all focused tests pass and Ruff is clean.

- [ ] **Step 6: Commit Task 3**

```bash
git add devtools/verification/claude_review.py tests/test_claude_review.py
git commit -m "feat(devtools): add isolated Claude review preflights"
```

### Task 4: Claude invocation, report persistence, and CLI semantics

**Files:**
- Modify: `devtools/verification/claude_review.py`
- Modify: `tests/test_claude_review.py`

**Interfaces:**
- Consumes: Task 3 helpers and Task 1 report models.
- Produces: `ReviewOutcome(report: ReviewReport, json_path: Path, markdown_path: Path)`.
- Produces: `run_layer1(worktree: Path, python_executable: Path, runner: Runner) -> str`.
- Produces: `run_review(...) -> ReviewOutcome | None`, `write_user_waiver(...) -> ReviewOutcome`, `load_current_report(...) -> ReviewReport`, `render_markdown(report) -> str`, and `main(argv=None) -> int`.
- CLI subcommands: `run --base <ref> [--head <ref>] [--optional]`, `check [--head <ref>]`, and `waive --base <ref> --reason <text> --unverified-risk <text> --user-approved`.
- Exit codes: `0=pass, explicit user waiver, or explicitly optional unavailable`, `1=operational/validation error`, `3=warn`, `4=fail`.
- `required=False` suppresses only `AuthUnavailable` from Layer 2. Before it may
  return `None`, `run_review` still requires tracked cleanliness, exact base/head
  resolution, a detached exact-head worktree, and successful Layer 1 there. It
  never invokes Claude or writes a report on this path. Invalid refs, Layer 1
  failures, and every non-auth `ReviewError` propagate.

- [ ] **Step 1: Write failing orchestration tests**

Append these helpers and tests using injected auth/git/Claude runners and an injected worktree context:

```python
from contextlib import contextmanager
from datetime import datetime, timezone

from devtools.verification.models import (
    ClaudeJudgment,
    Finding,
    Reviewer,
    build_report,
)


@contextmanager
def fake_worktree(path):
    path.mkdir(parents=True, exist_ok=True)
    yield path


def model_finding(severity):
    return Finding(
        severity=severity,
        file="pipeline/x.py",
        line=10,
        title="finding",
        evidence="evidence",
        suggested_test="test it",
    )


def model_report(*, findings=(), head_sha="b" * 40):
    return build_report(
        ClaudeJudgment(tests_run=[], findings=list(findings), limitations=[]),
        base_sha="a" * 40,
        head_sha=head_sha,
        reviewed_at=datetime(2026, 8, 10, tzinfo=timezone.utc),
    )


def write_report(directory, *, head_sha):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{head_sha}.claude.json"
    path.write_text(model_report(head_sha=head_sha).model_dump_json(), encoding="utf-8")
    return path


def prepare_review_preflights(monkeypatch, tmp_path):
    monkeypatch.setattr(review, "check_claude_auth", lambda **kwargs: None)
    monkeypatch.setattr(review, "ensure_tracked_clean", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        review,
        "resolve_commit",
        lambda repo, ref, **kwargs: {"base": "a" * 40, "HEAD": "b" * 40}[ref],
    )
    monkeypatch.setattr(review, "git_diff", lambda *args, **kwargs: "diff --git a/x b/x")
    monkeypatch.setattr(review, "changed_files", lambda *args, **kwargs: ["x"])
    monkeypatch.setattr(
        review,
        "detached_worktree",
        lambda *args, **kwargs: fake_worktree(tmp_path / "isolated"),
    )
    monkeypatch.setattr(review, "run_layer1", lambda *args, **kwargs: "gate passed")


@pytest.mark.unit
def test_run_review_stamps_identity_and_writes_both_reports(tmp_path, monkeypatch):
    judgment = '{"tests_run": [], "findings": [], "limitations": []}'
    monkeypatch.setattr(review, "check_claude_auth", lambda **kwargs: None)
    monkeypatch.setattr(review, "ensure_tracked_clean", lambda *args, **kwargs: None)
    monkeypatch.setattr(review, "resolve_commit", lambda repo, ref, **kwargs: {
        "base": "a" * 40, "HEAD": "b" * 40
    }[ref])
    monkeypatch.setattr(review, "git_diff", lambda *args, **kwargs: "diff --git a/x b/x")
    monkeypatch.setattr(review, "changed_files", lambda *args, **kwargs: ["x"])
    monkeypatch.setattr(
        review,
        "detached_worktree",
        lambda *args, **kwargs: fake_worktree(tmp_path / "isolated"),
    )
    monkeypatch.setattr(review, "run_layer1", lambda *args, **kwargs: "1459 passed; ruff clean")

    seen = {}
    def claude_runner(argv, **kwargs):
        seen["argv"] = argv
        seen["cwd"] = kwargs["cwd"]
        return completed(argv, stdout=judgment)

    outcome = review.run_review(
        repo=tmp_path,
        base_ref="base",
        head_ref="HEAD",
        report_dir=tmp_path / "reports",
        acceptance="works",
        risk="mandatory: contract",
        original_symptom="none",
        runner=claude_runner,
    )
    assert outcome.report.verdict.value == "pass"
    assert outcome.report.head_sha == "b" * 40
    assert outcome.json_path.exists() and outcome.markdown_path.exists()
    assert seen["cwd"] == tmp_path / "isolated"
    assert "Write" in seen["argv"][seen["argv"].index("--disallowedTools") + 1]


@pytest.mark.unit
def test_required_missing_auth_raises_but_optional_runs_layer1_then_returns_none(
    tmp_path, monkeypatch
):
    def unavailable(**kwargs):
        raise review.AuthUnavailable("not logged in")
    monkeypatch.setattr(review, "check_claude_auth", unavailable)
    with pytest.raises(review.AuthUnavailable):
        review.run_review(
            repo=tmp_path,
            base_ref="HEAD~1",
            acceptance="test the gate",
            risk="mandatory: new component",
            original_symptom="none",
            required=True,
        )
    monkeypatch.setattr(review, "ensure_tracked_clean", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        review,
        "resolve_commit",
        lambda repo, ref, **kwargs: {"HEAD~1": "a" * 40, "HEAD": "b" * 40}[ref],
    )
    monkeypatch.setattr(
        review,
        "detached_worktree",
        lambda *args, **kwargs: fake_worktree(tmp_path / "isolated"),
    )
    layer1_calls = []
    monkeypatch.setattr(
        review,
        "run_layer1",
        lambda *args, **kwargs: layer1_calls.append(args) or "gate passed",
    )
    assert review.run_review(
        repo=tmp_path,
        base_ref="HEAD~1",
        report_dir=tmp_path / "reports",
        acceptance="test the gate",
        risk="optional review",
        original_symptom="none",
        required=False,
    ) is None
    assert layer1_calls and layer1_calls[0][0] == tmp_path / "isolated"
    assert not list((tmp_path / "reports").glob("*"))


@pytest.mark.unit
def test_layer1_runs_against_isolated_head_before_claude(tmp_path):
    seen = {}
    def runner(argv, **kwargs):
        seen["argv"] = argv
        seen["cwd"] = kwargs["cwd"]
        return completed(argv, stdout="gate passed")
    summary = review.run_layer1(tmp_path, Path("/venv/python"), runner=runner)
    assert seen["argv"] == [
        "/venv/python", "-m", "devtools.verification.offline",
        "--only", "all", "--repo", str(tmp_path), "--python", "/venv/python",
    ]
    assert seen["cwd"] == tmp_path
    assert summary == "gate passed"


@pytest.mark.unit
@pytest.mark.parametrize(
    ("severity", "expected"),
    [(None, review.EXIT_PASS), ("medium", review.EXIT_WARN), ("high", review.EXIT_FAIL)],
)
def test_exit_code_follows_computed_verdict(severity, expected):
    findings = [] if severity is None else [model_finding(severity)]
    report = model_report(findings=findings)
    assert review.exit_code_for_report(report) == expected


@pytest.mark.unit
def test_explicit_user_waiver_is_bound_and_exits_zero(tmp_path, monkeypatch):
    monkeypatch.setattr(
        review,
        "resolve_commit",
        lambda repo, ref, **kwargs: {"base": "a" * 40, "HEAD": "b" * 40}[ref],
    )
    outcome = review.write_user_waiver(
        repo=tmp_path,
        base_ref="base",
        head_ref="HEAD",
        report_dir=tmp_path / "reports",
        reason="user accepted the unverified change",
        unverified_risk="Claude did not test scheduler behavior",
        user_approved=True,
        clock=lambda: datetime(2026, 8, 10, tzinfo=timezone.utc),
    )
    assert outcome.report.reviewer is Reviewer.USER_WAIVER
    assert outcome.report.verdict.value == "warn"
    assert outcome.report.waiver.reason == "user accepted the unverified change"
    assert review.exit_code_for_report(outcome.report) == review.EXIT_PASS


@pytest.mark.unit
def test_waiver_without_explicit_user_approval_is_rejected(tmp_path):
    with pytest.raises(review.ReviewError, match="explicit user approval"):
        review.write_user_waiver(
            repo=tmp_path,
            base_ref="base",
            reason="not enough",
            unverified_risk="unknown",
            user_approved=False,
        )


@pytest.mark.unit
def test_check_rejects_stale_report(tmp_path):
    path = write_report(tmp_path, head_sha="a" * 40)
    with pytest.raises(review.ReviewError, match="stale"):
        review.load_current_report(path, expected_head="b" * 40)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("side_effect", "message"),
    [
        (subprocess.TimeoutExpired(["claude"], 10), "timed out"),
        (None, "exited 9"),
    ],
)
def test_claude_failures_are_concise(tmp_path, monkeypatch, side_effect, message):
    prepare_review_preflights(monkeypatch, tmp_path)
    def failing(argv, **kwargs):
        if side_effect:
            raise side_effect
        return completed(argv, code=9, stderr="quota exceeded\nsecret detail")
    with pytest.raises(review.ReviewError, match=message):
        review.run_review(
            repo=tmp_path,
            base_ref="base",
            acceptance="test failure mapping",
            risk="mandatory: new component",
            original_symptom="none",
            runner=failing,
        )


@pytest.mark.unit
def test_invalid_claude_json_writes_no_partial_report(tmp_path, monkeypatch):
    prepare_review_preflights(monkeypatch, tmp_path)
    with pytest.raises(review.ReviewError, match="valid judgment JSON"):
        review.run_review(
            repo=tmp_path,
            base_ref="base",
            report_dir=tmp_path / "reports",
            acceptance="reject invalid output",
            risk="mandatory: new component",
            original_symptom="none",
            runner=lambda argv, **kwargs: completed(argv, stdout="not json"),
        )
    assert not list((tmp_path / "reports").glob("*"))


@pytest.mark.unit
def test_markdown_is_derived_from_report():
    report = model_report(findings=[model_finding("medium")])
    rendered = review.render_markdown(report)
    assert "Verdict: warn" in rendered
    assert "pipeline/x.py:10" in rendered
    assert "finding" in rendered


@pytest.mark.unit
def test_markdown_renders_zero_findings_and_limitations():
    report = build_report(
        ClaudeJudgment(
            tests_run=[], findings=[], limitations=["live boundary not exercised"]
        ),
        base_sha="a" * 40,
        head_sha="b" * 40,
        reviewed_at=datetime(2026, 8, 10, tzinfo=timezone.utc),
    )
    rendered = review.render_markdown(report)
    assert "No findings." in rendered
    assert "live boundary not exercised" in rendered


@pytest.mark.unit
def test_atomic_write_failure_leaves_no_final_or_temp_file(tmp_path, monkeypatch):
    target = tmp_path / "report.json"
    def disk_full(source, destination):
        raise OSError("disk full")
    monkeypatch.setattr(review.os, "replace", disk_full)
    with pytest.raises(review.ReviewError, match="write report"):
        review.atomic_write_text(target, "{}\n")
    assert not target.exists()
    assert not list(tmp_path.glob(f".{target.name}.*"))
```

The tests above require `atomic_write_text(path: Path, content: str) -> Path` to clean up its temporary file and translate write/replace failures to `ReviewError("write report ...")`.

- [ ] **Step 2: Run orchestration tests and verify they fail**

Run:

```bash
.venv/bin/python -m pytest tests/test_claude_review.py -q
```

Expected: failures name missing `run_review`, exit constants, and report helpers.

- [ ] **Step 3: Implement Claude invocation and report generation**

The Claude argv must be a list and include:

```python
[
    "claude", "-p", "--output-format", "text",
    "--safe-mode", "--no-session-persistence",
    "--permission-mode", "dontAsk", "--setting-sources", "",
    "--tools", "Read,Grep,Glob", "--allowedTools", "Read,Grep,Glob",
    "--disallowedTools", "Bash,Write,Edit,NotebookEdit",
    "--settings", '{"permissions":{"deny":["Read(<pre-existing-worktree>/**)"]}}',
]
```

Pass the packet on stdin, set `cwd` to the isolated worktree, use the CLI timeout (default 1200 seconds), and convert missing binary, timeout, non-zero exit, invalid judgment, and schema failure to concise `ReviewError` messages. The deny settings enumerate every pre-existing worktree and the Git-common path before the isolated checkout is added, so only the isolated source tree remains readable.

Immediately after creating the isolated worktree and before invoking Claude, `run_review` calls `run_layer1` with the exact head checkout. A non-zero Layer 1 result raises `ReviewError` and prevents the Claude model call. The captured Layer 1 stdout/stderr summary is inserted into the review packet, proving the deterministic result belongs to the reviewed revision.

The harness—not Claude—stamps base/head SHA, reviewer, timestamp, and verdict. Use an injectable UTC clock. Write `<head>.claude.json` and `<head>.claude.md` atomically using a local temp-file-plus-`os.replace` helper. JSON is authoritative and uses `report.model_dump(mode="json")`; Markdown is generated deterministically from that report.

`write_user_waiver` must reject `user_approved=False` before resolving refs or writing files. With explicit approval it builds `Waiver(approved_by="user", ...)`, calls Task 1's `build_waiver_report`, writes the same SHA-bound JSON/Markdown paths, and never calls Claude or claims `pass`. `exit_code_for_report` returns zero for `reviewer=user-waiver` while console output says `waived`.

Default report directory resolution is:

```python
Path(os.environ.get("TRADINGAGENTS_VERIFICATION_DIR", "~/.tradingagents/verification")).expanduser()
```

- [ ] **Step 4: Implement `run` and `check` subcommands**

`run` requires `--base`, `--acceptance`, `--risk`, and `--original-symptom`; it defaults only `--head` to `HEAD` and also accepts `--report-dir`, `--timeout`, and `--optional`. Layer 1 results are never accepted from the caller; the runner computes them inside the isolated head worktree.

`check` resolves the requested head, loads `<resolved-head>.claude.json` unless `--report` is supplied, validates schema and exact head binding, prints the verdict/report path, and returns the verdict exit code. A valid `user-waiver` report prints `waived` and exits zero without converting its stored `warn` verdict to `pass`.

`waive` requires `--base`, defaults `--head` to `HEAD`, requires non-empty `--reason`, `--unverified-risk`, and the literal `--user-approved` acknowledgement. `AGENTS.md` must state that an agent may invoke it only after the user explicitly authorizes a waiver for that revision.

Both subcommands reserve argparse's exit 2. They print one-line operational errors to stderr and never print credentials or the full raw Claude response.

- [ ] **Step 5: Run all Claude-review tests and exercise CLI help**

Run:

```bash
.venv/bin/python -m pytest tests/test_claude_review.py tests/test_verification_models.py -q
.venv/bin/python -m devtools.verification.claude_review --help
.venv/bin/python -m devtools.verification.claude_review run --help
.venv/bin/python -m devtools.verification.claude_review check --help
.venv/bin/python -m devtools.verification.claude_review waive --help
.venv/bin/python -m ruff check devtools/verification tests/test_claude_review.py tests/test_verification_models.py
```

Expected: tests and Ruff pass; every help command exits zero. Do not run a real Claude review yet because that is a subscription-consuming Layer 2 smoke test.

- [ ] **Step 6: Commit Task 4**

```bash
git add devtools/verification/claude_review.py tests/test_claude_review.py
git commit -m "feat(devtools): add revision-bound Claude review gate"
```

### Task 5: CI alignment and repository governance

**Files:**
- Modify: `.github/workflows/ci.yml:29-30,59-61`
- Modify: `AGENTS.md:22-27,47-52`
- Create: `tests/test_hybrid_workflow_docs.py`

**Interfaces:**
- Consumes: Task 2 `python -m devtools.verification.offline --only ...` commands.
- Produces: stable CI policy and agent-facing trigger/command documentation.

- [ ] **Step 1: Write failing policy tests**

Create `tests/test_hybrid_workflow_docs.py`:

```python
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.unit
def test_ci_uses_offline_verifier_for_tests_and_lint():
    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert "python -m devtools.verification.offline --only tests" in workflow
    assert "python -m devtools.verification.offline --only lint" in workflow


@pytest.mark.unit
def test_agent_guide_declares_hybrid_roles_and_required_gate():
    guide = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    assert "Codex — primary builder" in guide
    assert "Claude Code — independent verifier" in guide
    assert "python -m devtools.verification.offline --only all" in guide
    assert "two or more" in guide
    assert "pipeline/contracts/" in guide
    assert "~/.tradingagents/verification/" in guide
```

- [ ] **Step 2: Run policy tests and verify they fail**

Run:

```bash
.venv/bin/python -m pytest tests/test_hybrid_workflow_docs.py -q
```

Expected: both tests fail because CI and `AGENTS.md` still use the old workflow text.

- [ ] **Step 3: Align GitHub Actions**

In the matrix test job, replace `pytest -q` with:

```yaml
      - name: Run deterministic test suite
        run: python -m devtools.verification.offline --only tests
```

In the lint job, replace `ruff check .` with:

```yaml
      - name: Lint the repository
        run: python -m devtools.verification.offline --only lint
```

Keep the clean-install smoke job unchanged. CI must not configure or invoke Claude.

- [ ] **Step 4: Replace historical ownership guidance in `AGENTS.md`**

Update Build & Test so the canonical first command is:

```bash
.venv/bin/python -m devtools.verification.offline --only all
```

Keep the explicit pytest/Ruff commands as troubleshooting commands. Replace “Division of labor (current)” with “Codex-Claude hybrid workflow,” covering:

- `Codex — primary builder`: owns production edits, tests, specs, and integration.
- `Claude Code — independent verifier`: tests/reviews coherent risky changes from an isolated worktree and returns reports, not primary-worktree edits.
- deterministic gate required for every completed change;
- all mandatory triggers from the approved design;
- complex bug = any mandatory trigger or at least two scored factors;
- `run` and `check` command examples;
- reports in `~/.tradingagents/verification/` bound to the reviewed head SHA;
- the `waive` command is allowed only after explicit user authorization for the
  bound revision and remains visibly `waived`, never `pass`;
- runtime D19 remains Codex collection / Claude evaluation and is separate from development review.

Do not rewrite historical `Builder` fields in component specs; they record who delivered the existing implementation, while `AGENTS.md` governs future work.

- [ ] **Step 5: Run policy tests and inspect the workflow diff**

Run:

```bash
.venv/bin/python -m pytest tests/test_hybrid_workflow_docs.py -q
git diff --check
git diff -- .github/workflows/ci.yml AGENTS.md
```

Expected: policy tests pass, diff check is clean, and the workflow diff contains no secrets or Claude invocation.

- [ ] **Step 6: Commit Task 5**

```bash
git add .github/workflows/ci.yml AGENTS.md tests/test_hybrid_workflow_docs.py
git commit -m "docs(workflow): adopt Codex-led Claude verification"
```

### Task 6: Full verification and safe smoke tests

**Files:**
- Modify only if verification reveals a defect: files introduced in Tasks 1–5 and their corresponding tests.

**Interfaces:**
- Consumes every earlier task.
- Produces a verified implementation ready for the first user-authorized live Claude review.

- [ ] **Step 1: Run the canonical deterministic gate**

Run:

```bash
.venv/bin/python -m devtools.verification.offline --only all
```

Expected: all non-integration tests pass, Ruff is clean, and `git diff --check` passes. The DeepSeek live test is reported as deselected rather than attempted.

- [ ] **Step 2: Prove missing Claude auth fails closed without spending quota**

Run:

```bash
.venv/bin/python -m devtools.verification.claude_review run --base HEAD~1 --head HEAD --acceptance "workflow implementation matches the approved design" --risk "mandatory: new end-to-end development component" --original-symptom "none"
```

Expected on the current machine until Claude login is configured: exit 1 and a concise `not logged in` error; no report file and no model call. If the machine is logged in by execution time, skip this manual command and rely on the injected missing-auth test so no unapproved subscription call occurs.

- [ ] **Step 3: Verify package installation and CLI imports**

Run:

```bash
.venv/bin/python -c "from devtools.verification.models import ReviewReport; import devtools.verification.offline; import devtools.verification.claude_review; print('verification imports OK')"
.venv/bin/python -m pytest tests/test_verification_models.py tests/test_offline_verification.py tests/test_claude_review.py tests/test_hybrid_workflow_docs.py -q
```

Expected: import message prints and all workflow tests pass.

- [ ] **Step 4: Inspect final scope and generated-data safety**

Run:

```bash
git status --short
git diff --stat HEAD~5..HEAD
```

Expected: only planned repository files are tracked or modified; `.DS_Store` and `.beads/` remain untouched. Reports cannot appear because their resolved directory is outside the repository.

- [ ] **Step 5: Request Claude Layer 2 review only after user authorizes quota use**

This implementation is itself a mandatory trigger because it is a new end-to-end development component. Once Claude CLI authentication is available and the user authorizes a subscription-consuming review, run:

```bash
.venv/bin/python -m devtools.verification.claude_review run --base 044feec --head HEAD --acceptance "all seven acceptance criteria in the approved hybrid-workflow design" --risk "mandatory: new end-to-end development component" --original-symptom "per-commit Claude review was too costly and nondeterministic"
```

Expected: a valid current-SHA report is written under `~/.tradingagents/verification/`. `pass` exits 0; `warn` exits 3 and requires finding disposition; `fail` exits 4 and blocks completion.

- [ ] **Step 6: Commit any verification-only corrections**

If Steps 1–4 required corrections, commit only those corrections and their regression tests:

```bash
git add devtools tests .github/workflows/ci.yml AGENTS.md pyproject.toml
git commit -m "fix(devtools): close hybrid verification gaps"
```

If no corrections were required, do not create an empty commit.
