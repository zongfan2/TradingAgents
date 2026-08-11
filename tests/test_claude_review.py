import json
import subprocess
from pathlib import Path

import pytest

from devtools.verification import claude_review as review


def completed(argv, code=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(argv, code, stdout=stdout, stderr=stderr)


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
        str(tmp_path),
        "a" * 40,
        "b" * 40,
        "pipeline/x.py",
        "tests/test_x.py",
        "diff --git a/x b/x",
        "fallback returns non-zero",
        "complex bug: retry semantics changed",
        "1459 passed; ruff clean",
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
        json.dumps(review.ClaudeJudgment.model_json_schema(), indent=2, sort_keys=True),
    ]
    for expected in required:
        assert expected in packet
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
