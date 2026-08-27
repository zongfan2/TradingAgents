import json
import subprocess
import sys
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from devtools.verification import claude_review as review
from devtools.verification.models import (
    ClaudeJudgment,
    Finding,
    Reviewer,
    Waiver,
    build_report,
    build_waiver_report,
)


def completed(argv, code=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(argv, code, stdout=stdout, stderr=stderr)


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
    monkeypatch.setattr(
        review, "review_boundaries", lambda *args, **kwargs: (), raising=False
    )


@pytest.mark.unit
def test_auth_requires_logged_in_true():
    calls = []

    def runner(argv, **kwargs):
        calls.append((argv, kwargs))
        return completed(argv, stdout='{"loggedIn": true}')

    assert review.check_claude_auth(runner=runner) is None
    assert calls == [
        (
            ["claude", "auth", "status", "--json"],
            {"timeout": 15, "shell": False},
        )
    ]

    with pytest.raises(review.AuthUnavailable, match="not logged in"):
        review.check_claude_auth(
            runner=lambda argv, **kwargs: completed(
                argv, stdout='{"loggedIn": false}'
            )
        )


@pytest.mark.unit
@pytest.mark.parametrize(
    ("failure", "message"),
    [
        (FileNotFoundError(), "CLI not found"),
        (subprocess.TimeoutExpired(["claude"], 15), "timed out"),
    ],
)
def test_auth_translates_process_start_failures(failure, message):
    def runner(argv, **kwargs):
        raise failure

    with pytest.raises(review.AuthUnavailable, match=message):
        review.check_claude_auth(runner=runner)


@pytest.mark.unit
def test_auth_translates_general_os_errors_to_auth_unavailable():
    with pytest.raises(review.AuthUnavailable, match="auth status unavailable"):
        review.check_claude_auth(
            runner=lambda argv, **kwargs: (_ for _ in ()).throw(OSError("broken pipe"))
        )


@pytest.mark.unit
@pytest.mark.parametrize(
    ("result", "message"),
    [
        (completed([], code=2, stderr="credential helper exploded\nsecret detail"), "status failed"),
        (completed([], stdout="not-json"), "invalid JSON"),
        (completed([], stdout='{"loggedIn": 1}'), "not logged in"),
        (completed([], stdout="[]"), "not logged in"),
    ],
)
def test_auth_rejects_unusable_status_without_echoing_output(result, message):
    with pytest.raises(review.AuthUnavailable, match=message) as error:
        review.check_claude_auth(runner=lambda argv, **kwargs: result)
    assert "credential helper exploded" not in str(error.value)
    assert "secret detail" not in str(error.value)


@pytest.mark.unit
def test_auth_reports_logged_out_json_even_when_cli_exits_nonzero():
    with pytest.raises(review.AuthUnavailable, match="not logged in"):
        review.check_claude_auth(
            runner=lambda argv, **kwargs: completed(
                argv, code=1, stdout='{"loggedIn": false}'
            )
        )


@pytest.mark.unit
@pytest.mark.parametrize("length", [40, 64])
def test_resolve_commit_returns_full_lowercase_sha(tmp_path, length):
    sha = "A" * length
    calls = []

    def runner(argv, **kwargs):
        calls.append((argv, kwargs))
        return completed(argv, stdout=sha + "\n")

    assert review.resolve_commit(tmp_path, "HEAD", runner=runner) == sha.lower()
    assert calls == [
        (
            ["git", "rev-parse", "--verify", "HEAD^{commit}"],
            {"cwd": tmp_path, "timeout": 120, "shell": False},
        )
    ]


@pytest.mark.unit
@pytest.mark.parametrize("stdout", ["a" * 39, "g" * 40, "a" * 65, "a" * 40 + "\nextra"])
def test_resolve_commit_rejects_invalid_git_output(tmp_path, stdout):
    def runner(argv, **kwargs):
        return completed(argv, stdout=stdout)

    with pytest.raises(review.ReviewError, match="valid commit SHA"):
        review.resolve_commit(tmp_path, "HEAD", runner=runner)


@pytest.mark.unit
def test_resolve_commit_rejects_git_failure_concisely(tmp_path):
    def runner(argv, **kwargs):
        return completed(argv, code=128, stderr="huge secret output")

    with pytest.raises(review.ReviewError, match="could not resolve commit") as error:
        review.resolve_commit(tmp_path, "missing", runner=runner)
    assert "secret" not in str(error.value)


@pytest.mark.unit
def test_primary_preflight_rejects_tracked_edits_but_ignores_untracked(tmp_path):
    calls = []

    def dirty(argv, **kwargs):
        calls.append((argv, kwargs))
        return completed(argv, stdout=" M pipeline/x.py\n")

    with pytest.raises(review.ReviewError, match="tracked edits"):
        review.ensure_tracked_clean(tmp_path, runner=dirty)
    assert calls == [
        (
            ["git", "status", "--porcelain", "--untracked-files=no"],
            {"cwd": tmp_path, "timeout": 120, "shell": False},
        )
    ]

    def clean(argv, **kwargs):
        return completed(argv, stdout="")

    assert review.ensure_tracked_clean(tmp_path, runner=clean) is None


@pytest.mark.unit
def test_primary_preflight_rejects_status_failure(tmp_path):
    def failed(argv, **kwargs):
        return completed(argv, code=128)

    with pytest.raises(review.ReviewError, match="inspect tracked changes"):
        review.ensure_tracked_clean(tmp_path, runner=failed)


@pytest.mark.unit
def test_diff_helpers_use_exact_commit_range_and_return_text(tmp_path):
    base = "a" * 40
    head = "b" * 40
    calls = []

    def runner(argv, **kwargs):
        calls.append((argv, kwargs))
        stdout = "x.py\ntests/test_x.py\n" if "--name-only" in argv else "diff text"
        return completed(argv, stdout=stdout)

    assert review.git_diff(tmp_path, base, head, runner=runner) == "diff text"
    assert review.changed_files(tmp_path, base, head, runner=runner) == [
        "x.py",
        "tests/test_x.py",
    ]
    assert calls == [
        (
            ["git", "diff", "--no-ext-diff", f"{base}..{head}"],
            {"cwd": tmp_path, "timeout": 120, "shell": False},
        ),
        (
            ["git", "diff", "--no-ext-diff", "--name-only", f"{base}..{head}"],
            {"cwd": tmp_path, "timeout": 120, "shell": False},
        ),
    ]


@pytest.mark.unit
def test_diff_helper_failure_is_concise(tmp_path):
    def runner(argv, **kwargs):
        return completed(argv, code=1, stderr="secret and enormous diagnostics")

    with pytest.raises(review.ReviewError, match="diff inspection failed") as error:
        review.git_diff(tmp_path, "a" * 40, "b" * 40, runner=runner)
    assert "secret" not in str(error.value)


@pytest.mark.unit
def test_parse_judgment_accepts_plain_and_fenced_json():
    payload = {"tests_run": [], "findings": [], "limitations": []}
    assert review.parse_judgment(json.dumps(payload)).findings == []
    assert review.parse_judgment(
        f"```json\n{json.dumps(payload)}\n```"
    ).limitations == []


@pytest.mark.unit
@pytest.mark.parametrize(
    "output",
    [
        "looks good",
        'prefix {"tests_run": [], "findings": [], "limitations": []}',
        '```\n{"tests_run": [], "findings": [], "limitations": []}\n```',
        '```JSON\n{"tests_run": [], "findings": [], "limitations": []}\n```',
        '```json\n{"tests_run": [], "findings": [], "limitations": []}\n```\nmore',
        '```json\n{"tests_run": [], "findings": [], "limitations": []}\n```\n```json\n{}\n```',
        "[]",
    ],
)
def test_parse_judgment_rejects_prose_wrong_fences_and_non_objects(output):
    with pytest.raises(review.ReviewError, match="valid judgment JSON"):
        review.parse_judgment(output)


@pytest.mark.unit
def test_parse_judgment_rejects_unknown_fields_without_dumping_model_output():
    marker = "x" * 10_000
    output = json.dumps(
        {
            "tests_run": [],
            "findings": [],
            "limitations": [],
            "verdict": marker,
        }
    )
    with pytest.raises(review.ReviewError, match="schema") as error:
        review.parse_judgment(output)
    assert marker not in str(error.value)
    assert len(str(error.value)) < 200


@pytest.mark.unit
def test_packet_contains_required_context_policy_and_exact_schema(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "must-not-leak")
    (tmp_path / ".env").write_text("OTHER_SECRET=file-must-not-leak\n", encoding="utf-8")
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
    required = [
        "a" * 40,
        "b" * 40,
        "pipeline/x.py",
        "tests/test_x.py",
        "diff --git a/x b/x",
        "fallback returns non-zero",
        "complex bug: retry semantics changed",
        "Layer 1 deterministic gate completed.",
        "invalid output returned exit 0",
        "Codex",
        "Claude Code",
        "User",
        "governing component specs",
        "data contracts",
        "pytest",
        "ruff",
        "git diff",
        "Do not edit the primary worktree",
        "Do not make external integration calls",
        "Return JSON only",
        "Detached isolated checkout",
        json.dumps(review.ClaudeJudgment.model_json_schema(), indent=2, sort_keys=True),
    ]
    for expected in required:
        assert expected in packet
    assert str(tmp_path) not in packet
    assert "must-not-leak" not in packet
    assert "file-must-not-leak" not in packet


@pytest.mark.unit
def test_default_runner_uses_text_capture_without_a_shell(monkeypatch):
    received = {}

    def run(argv, **kwargs):
        received.update(argv=argv, kwargs=kwargs)
        return completed(argv)

    monkeypatch.setattr(subprocess, "run", run)
    review.default_runner(("git", "status"), cwd=Path("repo"), input="packet", timeout=7)
    assert received == {
        "argv": ["git", "status"],
        "kwargs": {
            "cwd": Path("repo"),
            "input": "packet",
            "capture_output": True,
            "text": True,
            "timeout": 7,
            "check": False,
            "shell": False,
        },
    }


@pytest.mark.unit
def test_detached_worktree_is_not_primary_and_is_removed(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(
        ["git", "init"], cwd=repo, check=True, capture_output=True, shell=False
    )
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=repo,
        check=True,
        shell=False,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=repo,
        check=True,
        shell=False,
    )
    (repo / "tracked.txt").write_text("one\n", encoding="utf-8")
    subprocess.run(
        ["git", "add", "tracked.txt"], cwd=repo, check=True, shell=False
    )
    subprocess.run(
        ["git", "commit", "-m", "base"],
        cwd=repo,
        check=True,
        capture_output=True,
        shell=False,
    )
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
        shell=False,
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
        shell=False,
    ).stdout
    assert str(isolated) not in listed


@pytest.mark.unit
def test_detached_worktree_uses_exact_head_and_shell_disabled(tmp_path):
    calls = []
    head = "a" * 40

    def runner(argv, **kwargs):
        calls.append((argv, kwargs))
        return completed(argv)

    with review.detached_worktree(tmp_path, head, runner=runner) as isolated:
        assert isolated.name == "worktree"
        assert not isolated.exists()

    target = calls[0][0][4]
    assert calls == [
        (
            ["git", "worktree", "add", "--detach", target, head],
            {"cwd": tmp_path, "timeout": 120, "shell": False},
        ),
        (
            ["git", "worktree", "remove", "--force", target],
            {"cwd": tmp_path, "timeout": 120, "shell": False},
        ),
        (
            ["git", "worktree", "prune"],
            {"cwd": tmp_path, "timeout": 120, "shell": False},
        ),
    ]


@pytest.mark.unit
def test_add_failure_still_removes_and_prunes(tmp_path):
    calls = []

    def runner(argv, **kwargs):
        calls.append(argv)
        return completed(argv, code=1 if len(calls) == 1 else 0)

    with (
        pytest.raises(review.ReviewError, match="create detached"),
        review.detached_worktree(tmp_path, "a" * 40, runner=runner),
    ):
        pytest.fail("a failed add must never yield")

    assert [argv[2] for argv in calls] == ["add", "remove", "prune"]


@pytest.mark.unit
def test_cleanup_warnings_do_not_mask_review_exception(tmp_path):
    calls = 0

    def runner(argv, **kwargs):
        nonlocal calls
        calls += 1
        return completed(argv, code=0 if calls == 1 else 1)

    with (
        pytest.warns(RuntimeWarning) as warnings_seen,
        pytest.raises(ValueError, match="review failed"),
        review.detached_worktree(tmp_path, "a" * 40, runner=runner),
    ):
        raise ValueError("review failed")

    assert len(warnings_seen) == 2
    assert calls == 3


@pytest.mark.unit
def test_cleanup_runner_exception_does_not_mask_review_exception(tmp_path):
    calls = 0

    def runner(argv, **kwargs):
        nonlocal calls
        calls += 1
        if calls > 1:
            raise RuntimeError("cleanup runner failed")
        return completed(argv)

    with (
        pytest.warns(RuntimeWarning) as warnings_seen,
        pytest.raises(ValueError, match="original review failure"),
        review.detached_worktree(tmp_path, "a" * 40, runner=runner),
    ):
        raise ValueError("original review failure")

    assert len(warnings_seen) == 2


@pytest.mark.unit
def test_run_review_stamps_identity_and_writes_both_reports(tmp_path, monkeypatch):
    judgment = '{"tests_run": [], "findings": [], "limitations": []}'
    prepare_review_preflights(monkeypatch, tmp_path)

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
def test_required_missing_auth_raises_immediately(tmp_path, monkeypatch):
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


@pytest.mark.unit
def test_optional_missing_auth_runs_exact_head_layer1_without_claude_or_report(
    tmp_path, monkeypatch
):
    events = []

    def unavailable(**kwargs):
        events.append("auth-unavailable")
        raise review.AuthUnavailable("not logged in")

    def clean(*args, **kwargs):
        events.append("tracked-clean")

    def resolve(repo, ref, **kwargs):
        events.append(f"resolve:{ref}")
        return {"base": "a" * 40, "HEAD": "b" * 40}[ref]

    @contextmanager
    def exact_worktree(repo, head_sha, **kwargs):
        events.append(f"worktree:{head_sha}")
        isolated = tmp_path / "isolated"
        isolated.mkdir()
        yield isolated

    def layer1(worktree, python_executable, **kwargs):
        events.append(f"layer1:{worktree.name}")
        return "gate passed"

    def forbidden_claude(argv, **kwargs):
        pytest.fail("Claude must not run when optional authentication is unavailable")

    monkeypatch.setattr(review, "check_claude_auth", unavailable)
    monkeypatch.setattr(review, "ensure_tracked_clean", clean)
    monkeypatch.setattr(review, "resolve_commit", resolve)
    monkeypatch.setattr(review, "git_diff", lambda *args, **kwargs: "diff")
    monkeypatch.setattr(review, "changed_files", lambda *args, **kwargs: ["x.py"])
    monkeypatch.setattr(review, "detached_worktree", exact_worktree)
    monkeypatch.setattr(review, "run_layer1", layer1)
    monkeypatch.setattr(review, "review_boundaries", lambda *args, **kwargs: ())
    report_dir = tmp_path / "reports"

    assert (
        review.run_review(
            repo=tmp_path,
            base_ref="base",
            acceptance="test the gate",
            risk="optional review",
            original_symptom="none",
            report_dir=report_dir,
            required=False,
            runner=forbidden_claude,
        )
        is None
    )
    assert events == [
        "auth-unavailable",
        "tracked-clean",
        "resolve:base",
        "resolve:HEAD",
        f"worktree:{'b' * 40}",
        "layer1:isolated",
    ]
    assert not report_dir.exists()


@pytest.mark.unit
def test_optional_missing_auth_propagates_invalid_ref(tmp_path, monkeypatch):
    monkeypatch.setattr(
        review,
        "check_claude_auth",
        lambda **kwargs: (_ for _ in ()).throw(
            review.AuthUnavailable("not logged in")
        ),
    )
    monkeypatch.setattr(review, "ensure_tracked_clean", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        review,
        "resolve_commit",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            review.ReviewError("could not resolve commit")
        ),
    )
    with pytest.raises(review.ReviewError, match="resolve commit"):
        review.run_review(
            repo=tmp_path,
            base_ref="missing",
            acceptance="test the gate",
            risk="optional review",
            original_symptom="none",
            required=False,
        )


@pytest.mark.unit
def test_optional_missing_auth_propagates_layer1_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(
        review,
        "check_claude_auth",
        lambda **kwargs: (_ for _ in ()).throw(
            review.AuthUnavailable("not logged in")
        ),
    )
    monkeypatch.setattr(review, "ensure_tracked_clean", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        review,
        "resolve_commit",
        lambda repo, ref, **kwargs: {"base": "a" * 40, "HEAD": "b" * 40}[ref],
    )
    monkeypatch.setattr(review, "git_diff", lambda *args, **kwargs: "diff")
    monkeypatch.setattr(review, "changed_files", lambda *args, **kwargs: ["x.py"])
    monkeypatch.setattr(
        review,
        "detached_worktree",
        lambda *args, **kwargs: fake_worktree(tmp_path / "isolated"),
    )
    monkeypatch.setattr(
        review,
        "run_layer1",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            review.ReviewError("Layer 1 verification exited 1")
        ),
    )
    monkeypatch.setattr(review, "review_boundaries", lambda *args, **kwargs: ())
    with pytest.raises(review.ReviewError, match="Layer 1 verification exited 1"):
        review.run_review(
            repo=tmp_path,
            base_ref="base",
            acceptance="test the gate",
            risk="optional review",
            original_symptom="none",
            required=False,
        )


@pytest.mark.unit
def test_optional_review_propagates_non_auth_review_error(tmp_path, monkeypatch):
    monkeypatch.setattr(
        review,
        "check_claude_auth",
        lambda **kwargs: (_ for _ in ()).throw(
            review.ReviewError("unexpected auth inspection error")
        ),
    )
    with pytest.raises(review.ReviewError, match="unexpected auth inspection"):
        review.run_review(
            repo=tmp_path,
            base_ref="base",
            acceptance="test the gate",
            risk="optional review",
            original_symptom="none",
            required=False,
        )


@pytest.mark.unit
def test_layer1_runs_against_isolated_head_before_claude(tmp_path):
    seen = {}

    def runner(argv, **kwargs):
        seen["argv"] = argv
        seen["cwd"] = kwargs["cwd"]
        return completed(argv, stdout="gate passed")

    summary = review.run_layer1(tmp_path, Path("/venv/python"), runner)
    assert seen["argv"] == [
        "/venv/python",
        "-m",
        "devtools.verification.offline",
        "--only",
        "all",
        "--repo",
        str(tmp_path),
        "--python",
        "/venv/python",
    ]
    assert seen["cwd"] == tmp_path
    assert summary == "gate passed"


@pytest.mark.unit
def test_layer1_failure_has_bounded_sanitized_step_summary(tmp_path):
    with pytest.raises(review.ReviewError, match=r"exited 7 \(tests\)") as error:
        review.run_layer1(
            tmp_path,
            Path("/venv/python"),
            runner=lambda argv, **kwargs: completed(
                argv,
                code=7,
                stdout="==> tests: /private/secret/python -m pytest\nsecret traceback",
            ),
        )
    assert "private/secret" not in str(error.value)
    assert "traceback" not in str(error.value)


@pytest.mark.unit
def test_review_defaults_layer1_to_invoking_python_without_local_venv(tmp_path, monkeypatch):
    repo = tmp_path / "linked-worktree"
    repo.mkdir()
    report_dir = tmp_path / "reports"
    prepare_review_preflights(monkeypatch, tmp_path)
    seen = {}

    def layer1(worktree, python_executable, **kwargs):
        seen["python"] = python_executable
        return "gate passed"

    monkeypatch.setattr(review, "run_layer1", layer1)
    review.run_review(
        repo=repo,
        base_ref="base",
        acceptance="works",
        risk="mandatory",
        original_symptom="none",
        report_dir=report_dir,
        runner=lambda argv, **kwargs: completed(
            argv, stdout='{"tests_run": [], "findings": [], "limitations": []}'
        ),
    )
    assert seen["python"] == Path(sys.executable).absolute()


@pytest.mark.unit
@pytest.mark.parametrize(
    ("severity", "expected"),
    [(None, 0), ("medium", 3), ("high", 4)],
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
    monkeypatch.setattr(review, "review_boundaries", lambda *args, **kwargs: ())
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
    assert review.exit_code_for_report(outcome.report) == 0


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
def test_check_rejects_stored_verdict_that_contradicts_test_evidence(tmp_path):
    head = "b" * 40
    payload = model_report(head_sha=head).model_dump(mode="json")
    payload["verdict"] = "pass"
    payload["tests_run"] = [
        {
            "command": "pytest -q",
            "status": "fail",
            "summary": "one regression failed",
        }
    ]
    path = tmp_path / f"{head}.claude.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(review.ReviewError, match="schema validation"):
        review.load_current_report(path, expected_head=head)


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


@pytest.mark.unit
@pytest.mark.parametrize(
    "argv",
    [
        ["run", "--base", "base"],
        [
            "waive",
            "--base",
            "base",
            "--reason",
            "reason",
            "--unverified-risk",
            "risk",
        ],
    ],
)
def test_cli_reserves_argparse_exit_two_for_missing_required_fields(argv):
    with pytest.raises(SystemExit) as error:
        review.main(argv)
    assert error.value.code == 2


@pytest.mark.unit
def test_run_cli_computes_review_and_returns_report_verdict(tmp_path, monkeypatch, capsys):
    seen = {}
    report = model_report(findings=[model_finding("medium")])
    outcome = review.ReviewOutcome(
        report,
        tmp_path / f"{report.head_sha}.claude.json",
        tmp_path / f"{report.head_sha}.claude.md",
    )

    def fake_run_review(**kwargs):
        seen.update(kwargs)
        return outcome

    monkeypatch.setattr(review, "run_review", fake_run_review)
    result = review.main(
        [
            "run",
            "--base",
            "base",
            "--head",
            "topic",
            "--acceptance",
            "works",
            "--risk",
            "mandatory",
            "--original-symptom",
            "none",
            "--report-dir",
            str(tmp_path),
            "--timeout",
            "17",
        ]
    )
    assert result == 3
    assert seen["base_ref"] == "base"
    assert seen["head_ref"] == "topic"
    assert seen["acceptance"] == "works"
    assert seen["risk"] == "mandatory"
    assert seen["original_symptom"] == "none"
    assert seen["report_dir"] == tmp_path
    assert seen["timeout"] == 17
    assert seen["required"] is True
    assert capsys.readouterr().out.strip() == f"warn {outcome.json_path}"


@pytest.mark.unit
def test_run_cli_optional_unavailable_exits_zero(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(review, "run_review", lambda **kwargs: None)
    result = review.main(
        [
            "run",
            "--base",
            "base",
            "--acceptance",
            "works",
            "--risk",
            "optional",
            "--original-symptom",
            "none",
            "--optional",
            "--report-dir",
            str(tmp_path),
        ]
    )
    assert result == 0
    assert capsys.readouterr().out.strip() == "optional Claude review unavailable"


@pytest.mark.unit
def test_check_cli_resolves_exact_head_and_loads_sha_named_report(
    tmp_path, monkeypatch, capsys
):
    head = "b" * 40
    expected_path = tmp_path / f"{head}.claude.json"
    seen = {}
    monkeypatch.setattr(
        review,
        "resolve_commit",
        lambda repo, ref, **kwargs: seen.setdefault("resolved", (repo, ref)) and head,
    )

    def fake_load(path, *, expected_head):
        seen["loaded"] = (path, expected_head)
        return model_report(head_sha=head)

    monkeypatch.setattr(review, "load_current_report", fake_load)
    result = review.main(
        ["check", "--head", "topic", "--report-dir", str(tmp_path)]
    )
    assert result == 0
    assert seen["resolved"][1] == "topic"
    assert seen["loaded"] == (expected_path, head)
    assert capsys.readouterr().out.strip() == f"pass {expected_path}"


@pytest.mark.unit
def test_check_cli_prints_waived_and_exits_zero(tmp_path, monkeypatch, capsys):
    waiver_report = build_waiver_report(
        base_sha="a" * 40,
        head_sha="b" * 40,
        waiver=Waiver(
            approved_by="user",
            approved_at=datetime(2026, 8, 10, tzinfo=timezone.utc),
            reason="accepted",
            unverified_risk="risk",
        ),
    )
    path = tmp_path / f"{'b' * 40}.claude.json"
    monkeypatch.setattr(review, "resolve_commit", lambda *args, **kwargs: "b" * 40)
    monkeypatch.setattr(review, "load_current_report", lambda *args, **kwargs: waiver_report)
    assert review.main(["check", "--report", str(path)]) == 0
    assert capsys.readouterr().out.strip() == f"waived {path}"


@pytest.mark.unit
def test_waive_cli_passes_literal_approval_and_prints_waived(
    tmp_path, monkeypatch, capsys
):
    seen = {}
    report = build_waiver_report(
        base_sha="a" * 40,
        head_sha="b" * 40,
        waiver=Waiver(
            approved_by="user",
            approved_at=datetime(2026, 8, 10, tzinfo=timezone.utc),
            reason="accepted",
            unverified_risk="scheduler",
        ),
    )
    outcome = review.ReviewOutcome(
        report,
        tmp_path / f"{report.head_sha}.claude.json",
        tmp_path / f"{report.head_sha}.claude.md",
    )

    def fake_waiver(**kwargs):
        seen.update(kwargs)
        return outcome

    monkeypatch.setattr(review, "write_user_waiver", fake_waiver)
    result = review.main(
        [
            "waive",
            "--base",
            "base",
            "--head",
            "topic",
            "--reason",
            "accepted",
            "--unverified-risk",
            "scheduler",
            "--user-approved",
            "--report-dir",
            str(tmp_path),
        ]
    )
    assert result == 0
    assert seen["user_approved"] is True
    assert seen["reason"] == "accepted"
    assert seen["unverified_risk"] == "scheduler"
    assert capsys.readouterr().out.strip() == f"waived {outcome.json_path}"


@pytest.mark.unit
def test_cli_operational_error_is_one_line_without_raw_output(monkeypatch, capsys):
    monkeypatch.setattr(
        review,
        "run_review",
        lambda **kwargs: (_ for _ in ()).throw(review.ReviewError("review timed out")),
    )
    result = review.main(
        [
            "run",
            "--base",
            "base",
            "--acceptance",
            "works",
            "--risk",
            "mandatory",
            "--original-symptom",
            "none",
        ]
    )
    captured = capsys.readouterr()
    assert result == 1
    assert captured.out == ""
    assert captured.err == "error: review timed out\n"


@pytest.mark.unit
def test_run_review_uses_exact_safe_argv_packet_stdin_and_timeout(
    tmp_path, monkeypatch
):
    prepare_review_preflights(monkeypatch, tmp_path)
    monkeypatch.setattr(
        review, "run_layer1", lambda *args, **kwargs: "exact isolated gate result"
    )
    python = Path("/approved/venv/python")
    seen = {}

    def runner(argv, **kwargs):
        seen.update(argv=argv, kwargs=kwargs)
        return completed(
            argv,
            stdout='{"tests_run": [], "findings": [], "limitations": []}',
        )

    review.run_review(
        repo=tmp_path,
        base_ref="base",
        acceptance="works",
        risk="mandatory",
        original_symptom="none",
        report_dir=tmp_path / "reports",
        python_executable=python,
        timeout=37,
        runner=runner,
    )
    assert seen["argv"][:4] == ["claude", "-p", "--output-format", "text"]
    assert seen["argv"][seen["argv"].index("--tools") + 1] == "Read,Grep,Glob"
    assert seen["argv"][seen["argv"].index("--allowedTools") + 1] == "Read,Grep,Glob"
    assert seen["argv"][seen["argv"].index("--disallowedTools") + 1] == (
        "Bash,Write,Edit,NotebookEdit"
    )
    assert seen["kwargs"]["cwd"] == tmp_path / "isolated"
    assert seen["kwargs"]["timeout"] == 37
    assert seen["kwargs"]["shell"] is False
    assert "Layer 1 deterministic gate completed." in seen["kwargs"]["input"]


@pytest.mark.unit
def test_claude_argv_is_read_only_and_denies_preexisting_boundaries():
    primary = Path("/private/parent/../primary")
    common = Path("/private/git-common")
    argv = review._claude_argv((primary, common))
    assert argv[argv.index("--tools") + 1] == "Read"
    assert argv[argv.index("--allowedTools") + 1] == "Read"
    assert argv[argv.index("--disallowedTools") + 1] == "Bash,Grep,Glob,Write,Edit,NotebookEdit"
    assert "--safe-mode" in argv
    assert "--no-session-persistence" in argv
    assert argv[argv.index("--permission-mode") + 1] == "dontAsk"
    assert argv[argv.index("--setting-sources") + 1] == ""
    settings = json.loads(argv[argv.index("--settings") + 1])
    assert settings["permissions"]["deny"] == [
        "Read(//private/primary/**)",
        "Read(//private/git-common/**)",
    ]
    assert all("Grep(" not in rule and "Glob(" not in rule for rule in settings["permissions"]["deny"])


@pytest.mark.unit
def test_review_settings_deny_preexisting_paths_but_not_isolated_checkout(
    tmp_path, monkeypatch
):
    repo = tmp_path / "primary"
    common = tmp_path / "git-common"
    isolated = tmp_path / "isolated"
    report_dir = tmp_path / "reports"
    prepare_review_preflights(monkeypatch, tmp_path)
    monkeypatch.setattr(review, "review_boundaries", lambda *args, **kwargs: (repo, common))
    seen = {}

    def runner(argv, **kwargs):
        seen.update(argv=argv, kwargs=kwargs)
        return completed(argv, stdout='{"tests_run": [], "findings": [], "limitations": []}')

    review.run_review(
        repo=repo,
        base_ref="base",
        acceptance="works",
        risk="mandatory",
        original_symptom="none",
        report_dir=report_dir,
        runner=runner,
    )
    assert seen["kwargs"]["cwd"] == isolated
    denied = json.loads(seen["argv"][seen["argv"].index("--settings") + 1])["permissions"][
        "deny"
    ]
    for path in (repo, common):
        assert f"Read(/{path}/**)" in denied
    assert f"Read(/{isolated}/**)" not in denied


@pytest.mark.unit
def test_review_packet_does_not_disclose_primary_repository_path():
    primary = Path("/private/primary")
    packet = review.build_review_packet(
        repo=primary,
        base_sha="a" * 40,
        head_sha="b" * 40,
        diff="diff",
        changed_files=["x.py"],
        acceptance="works",
        risk="mandatory",
        layer1_result="passed",
        original_symptom="none",
    )
    assert str(primary) not in packet
    assert "Detached isolated checkout" in packet
    assert "only Read" in packet
    assert "Do not run shell commands" in packet


@pytest.mark.unit
def test_review_packet_sanitizes_primary_path_from_layer1_evidence():
    primary = Path("/private/primary")
    packet = review.build_review_packet(
        repo=primary,
        base_sha="a" * 40,
        head_sha="b" * 40,
        diff="diff",
        changed_files=["x.py"],
        acceptance="works",
        risk="mandatory",
        layer1_result=f"==> tests: {primary}/.venv/bin/python -m pytest\n1559 passed",
        original_symptom="none",
    )
    assert str(primary) not in packet
    assert "Layer 1 completed: tests." in packet


@pytest.mark.unit
def test_review_packet_redacts_primary_path_from_caller_supplied_context():
    primary = Path("/private/primary")
    packet = review.build_review_packet(
        repo=primary,
        base_sha="a" * 40,
        head_sha="b" * 40,
        diff=f"diff --git {primary}/x.py",
        changed_files=[str(primary / "x.py")],
        acceptance=f"verify {primary}",
        risk=f"risk at {primary}",
        layer1_result="passed",
        original_symptom=f"failure in {primary}",
    )
    assert str(primary) not in packet
    assert "<primary checkout>" in packet


@pytest.mark.unit
def test_review_boundaries_include_every_worktree_and_git_common_dir(tmp_path):
    primary = tmp_path / "primary"
    linked = tmp_path / "linked"
    common = tmp_path / "git-common"

    def runner(argv, **kwargs):
        if argv == ["git", "worktree", "list", "--porcelain"]:
            return completed(argv, stdout=f"worktree {primary}\n\nworktree {linked}\n")
        assert argv == ["git", "rev-parse", "--git-common-dir"]
        return completed(argv, stdout=str(common) + "\n")

    assert review.review_boundaries(primary, runner=runner) == (primary, linked, common)


@pytest.mark.unit
def test_report_dir_rejects_aliases_and_all_repository_boundaries(tmp_path):
    repo = tmp_path / "repo"
    primary = tmp_path / "primary"
    linked = tmp_path / "linked"
    common = tmp_path / "git-common"
    safe = tmp_path / "verification"
    alias = tmp_path / "verification-alias"
    alias.symlink_to(safe, target_is_directory=True)

    def runner(argv, **kwargs):
        if argv == ["git", "worktree", "list", "--porcelain"]:
            return completed(argv, stdout=f"worktree {primary}\n\nworktree {linked}\n")
        assert argv == ["git", "rev-parse", "--git-common-dir"]
        return completed(argv, stdout=str(common) + "\n")

    assert review.normalize_report_dir(repo, safe.absolute(), runner=runner) == safe.absolute()
    for invalid in (
        Path("relative"),
        alias.absolute(),
        primary / "reports",
        linked / "reports",
        common / "reports",
    ):
        with pytest.raises(review.ReviewError, match="report directory"):
            review.normalize_report_dir(repo, invalid, runner=runner)


@pytest.mark.unit
def test_run_and_waive_reject_relative_report_directories(tmp_path, monkeypatch):
    prepare_review_preflights(monkeypatch, tmp_path)
    with pytest.raises(review.ReviewError, match="report directory"):
        review.run_review(
            repo=tmp_path,
            base_ref="base",
            acceptance="works",
            risk="mandatory",
            original_symptom="none",
            report_dir=Path("relative"),
            runner=lambda argv, **kwargs: completed(
                argv, stdout='{"tests_run": [], "findings": [], "limitations": []}'
            ),
        )
    with pytest.raises(review.ReviewError, match="report directory"):
        review.write_user_waiver(
            repo=tmp_path,
            base_ref="base",
            reason="accepted",
            unverified_risk="risk",
            user_approved=True,
            report_dir=Path("relative"),
        )


@pytest.mark.unit
def test_check_cli_rejects_relative_report_directory(monkeypatch, capsys):
    monkeypatch.setattr(review, "resolve_commit", lambda *args, **kwargs: "b" * 40)
    result = review.main(["check", "--report-dir", "relative"])
    assert result == review.EXIT_ERROR
    assert capsys.readouterr().err == (
        "error: report directory must be absolute and outside repository boundaries\n"
    )


@pytest.mark.unit
def test_persist_report_restores_preexisting_pair_when_markdown_replace_fails(
    tmp_path, monkeypatch
):
    report_dir = tmp_path / "reports"
    old = model_report(head_sha="b" * 40)
    json_path = report_dir / f"{old.head_sha}.claude.json"
    markdown_path = report_dir / f"{old.head_sha}.claude.md"
    report_dir.mkdir()
    old_json = old.model_dump_json() + "\n"
    old_markdown = "# previously valid evidence\n"
    json_path.write_text(old_json, encoding="utf-8")
    markdown_path.write_text(old_markdown, encoding="utf-8")
    replacements = []
    real_replace = review.os.replace

    def fail_markdown_replace(source, destination):
        replacements.append(destination)
        if destination == markdown_path and replacements.count(markdown_path) == 1:
            raise OSError("disk full")
        return real_replace(source, destination)

    monkeypatch.setattr(review.os, "replace", fail_markdown_replace)
    with pytest.raises(review.ReviewError, match="write report"):
        review._persist_report(model_report(findings=[model_finding("medium")]), report_dir)
    assert json_path.read_text(encoding="utf-8") == old_json
    assert markdown_path.read_text(encoding="utf-8") == old_markdown


@pytest.mark.unit
def test_layer1_failure_prevents_claude_invocation(tmp_path, monkeypatch):
    prepare_review_preflights(monkeypatch, tmp_path)
    monkeypatch.setattr(
        review,
        "run_layer1",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            review.ReviewError("Layer 1 verification exited 1")
        ),
    )

    def forbidden_runner(argv, **kwargs):
        pytest.fail("Claude must not run after Layer 1 fails")

    with pytest.raises(review.ReviewError, match="Layer 1 verification exited 1"):
        review.run_review(
            repo=tmp_path,
            base_ref="base",
            acceptance="works",
            risk="mandatory",
            original_symptom="none",
            runner=forbidden_runner,
        )


@pytest.mark.unit
def test_default_report_dir_reads_only_named_override(monkeypatch, tmp_path):
    configured = tmp_path / "configured"
    monkeypatch.setenv("TRADINGAGENTS_VERIFICATION_DIR", str(configured))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    assert review.default_report_dir() == configured
    monkeypatch.delenv("TRADINGAGENTS_VERIFICATION_DIR")
    assert review.default_report_dir() == tmp_path / "home/.tradingagents/verification"


@pytest.mark.unit
def test_harness_normalizes_injected_clock_to_utc(tmp_path, monkeypatch):
    prepare_review_preflights(monkeypatch, tmp_path)
    supplied = datetime(
        2026, 8, 10, 12, 0, tzinfo=timezone(timedelta(hours=5))
    )
    outcome = review.run_review(
        repo=tmp_path,
        base_ref="base",
        acceptance="works",
        risk="mandatory",
        original_symptom="none",
        report_dir=tmp_path / "reports",
        runner=lambda argv, **kwargs: completed(
            argv,
            stdout='{"tests_run": [], "findings": [], "limitations": []}',
        ),
        clock=lambda: supplied,
    )
    assert outcome.report.reviewed_at == datetime(
        2026, 8, 10, 7, 0, tzinfo=timezone.utc
    )
    assert outcome.report.reviewed_at.utcoffset() == timedelta(0)


@pytest.mark.unit
def test_second_artifact_replacement_failure_leaves_no_new_report_pair(tmp_path, monkeypatch):
    prepare_review_preflights(monkeypatch, tmp_path)
    report_dir = tmp_path / "reports"
    markdown_path = report_dir / f"{'b' * 40}.claude.md"
    real_replace = review.os.replace
    markdown_attempts = 0

    def fail_markdown_replace(source, destination):
        nonlocal markdown_attempts
        if destination == markdown_path:
            markdown_attempts += 1
            if markdown_attempts == 1:
                raise OSError("disk full")
        return real_replace(source, destination)

    monkeypatch.setattr(review.os, "replace", fail_markdown_replace)
    with pytest.raises(review.ReviewError, match="write report"):
        review.run_review(
            repo=tmp_path,
            base_ref="base",
            acceptance="works",
            risk="mandatory",
            original_symptom="none",
            report_dir=report_dir,
            runner=lambda argv, **kwargs: completed(
                argv,
                stdout='{"tests_run": [], "findings": [], "limitations": []}',
            ),
        )
    assert not list(report_dir.glob("*"))
