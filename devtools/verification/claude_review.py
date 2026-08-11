"""Safe boundaries for revision-bound Claude code review."""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
import warnings
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Literal

from pydantic import ValidationError

from devtools.verification.models import ClaudeJudgment


class ReviewError(RuntimeError):
    """A review preflight or validation failed."""


class AuthUnavailable(ReviewError):
    """The local Claude CLI cannot supply an authenticated review."""


Runner = Callable[..., subprocess.CompletedProcess[Any]]

_SHA_RE = re.compile(r"[0-9a-fA-F]{40,64}")
_JSON_FENCE_RE = re.compile(r"```json[ \t]*\r?\n(?P<body>.*?)\r?\n```", re.DOTALL)


def default_runner(
    argv: Sequence[str],
    *,
    cwd: Path | None = None,
    input: str | None = None,
    timeout: int | float = 120,
    shell: Literal[False] = False,
) -> subprocess.CompletedProcess[str]:
    """Run one captured text command without invoking a shell."""
    if shell is not False:
        raise ValueError("review subprocesses cannot use a shell")
    return subprocess.run(
        list(argv),
        cwd=cwd,
        input=input,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        shell=shell,
    )


def check_claude_auth(*, runner: Runner = default_runner) -> None:
    """Require a logged-in Claude CLI account."""
    argv = ["claude", "auth", "status", "--json"]
    try:
        result = runner(argv, timeout=15, shell=False)
    except FileNotFoundError as exc:
        raise AuthUnavailable("Claude CLI not found") from exc
    except subprocess.TimeoutExpired as exc:
        raise AuthUnavailable("Claude auth status timed out") from exc
    if result.returncode:
        raise AuthUnavailable("Claude auth status failed")
    try:
        status = json.loads(result.stdout)
    except (json.JSONDecodeError, TypeError) as exc:
        raise AuthUnavailable("Claude auth status returned invalid JSON") from exc
    if not isinstance(status, dict) or status.get("loggedIn") is not True:
        raise AuthUnavailable("Claude CLI is not logged in")


def resolve_commit(repo: Path, ref: str, *, runner: Runner = default_runner) -> str:
    """Resolve a Git ref to a normalized full commit object ID."""
    argv = ["git", "rev-parse", "--verify", f"{ref}^{{commit}}"]
    try:
        result = runner(argv, cwd=repo, timeout=120, shell=False)
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        raise ReviewError(f"could not resolve commit {ref!r}") from exc
    if result.returncode:
        raise ReviewError(f"could not resolve commit {ref!r}")
    sha = result.stdout.strip()
    if _SHA_RE.fullmatch(sha) is None:
        raise ReviewError(f"Git did not return a valid commit SHA for {ref!r}")
    return sha.lower()


def ensure_tracked_clean(repo: Path, *, runner: Runner = default_runner) -> None:
    """Reject tracked changes while deliberately ignoring untracked files."""
    argv = ["git", "status", "--porcelain", "--untracked-files=no"]
    try:
        result = runner(argv, cwd=repo, timeout=120, shell=False)
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        raise ReviewError("could not inspect tracked changes") from exc
    if result.returncode:
        raise ReviewError("could not inspect tracked changes")
    if result.stdout:
        raise ReviewError("primary worktree has tracked edits")


def git_text(repo: Path, argv: Sequence[str], runner: Runner) -> str:
    """Run a read-only Git inspection command and return captured text."""
    command = ["git", *argv]
    try:
        result = runner(command, cwd=repo, timeout=120, shell=False)
    except (OSError, subprocess.SubprocessError) as exc:
        raise ReviewError("Git diff inspection failed") from exc
    if result.returncode:
        raise ReviewError("Git diff inspection failed")
    return result.stdout


def git_diff(
    repo: Path, base_sha: str, head_sha: str, *, runner: Runner = default_runner
) -> str:
    """Return the bounded diff between two resolved commits."""
    return git_text(
        repo,
        ["diff", "--no-ext-diff", f"{base_sha}..{head_sha}"],
        runner,
    )


def changed_files(
    repo: Path, base_sha: str, head_sha: str, *, runner: Runner = default_runner
) -> list[str]:
    """Return changed paths between two resolved commits."""
    output = git_text(
        repo,
        ["diff", "--name-only", f"{base_sha}..{head_sha}"],
        runner,
    )
    return output.splitlines()


def parse_judgment(output: str) -> ClaudeJudgment:
    """Parse a plain or singly JSON-fenced strict Claude judgment."""
    stripped = output.strip()
    if stripped.startswith("```"):
        fence = _JSON_FENCE_RE.fullmatch(stripped)
        if fence is None:
            raise ReviewError("Claude did not return valid judgment JSON")
        stripped = fence.group("body")
    try:
        payload = json.loads(stripped)
    except (json.JSONDecodeError, TypeError) as exc:
        raise ReviewError("Claude did not return valid judgment JSON") from exc
    if not isinstance(payload, dict):
        raise ReviewError("Claude did not return valid judgment JSON object")
    try:
        return ClaudeJudgment.model_validate(payload)
    except ValidationError as exc:
        raise ReviewError("Claude judgment failed schema validation") from exc


def build_review_packet(
    *,
    repo: Path,
    base_sha: str,
    head_sha: str,
    diff: str,
    changed_files: Sequence[str],
    acceptance: str,
    risk: str,
    layer1_result: str,
    original_symptom: str,
) -> str:
    """Build a bounded, secret-free independent-review prompt."""
    schema = json.dumps(ClaudeJudgment.model_json_schema(), indent=2, sort_keys=True)
    paths = "\n".join(f"- {path}" for path in changed_files) or "- (none)"
    return f"""# Independent Claude Code review packet

## Roles and authority
- Codex is the builder and integrator. It owns fixes and evaluates findings.
- Claude Code is the independent verifier. Review the committed change only.
- User is the sole authority for waivers and external-integration authorization.

## Immutable review identity
- Repository: {repo}
- Base commit: {base_sha}
- Head commit: {head_sha}

## Governing context
Read AGENTS.md plus applicable governing component specs and data contracts in the
repository. Check the requested behavior, committed regression tests, and diff
against those sources of truth.

## Changed files
{paths}

## Requested behavior and acceptance criteria
{acceptance}

## Risk classification and known limitations
{risk}

## Layer 1 deterministic commands and result
The exact head checkout was checked with pytest, ruff, and git diff --check.
{layer1_result}

## Original symptom and regression-test context
{original_symptom}

## Review diff
```diff
{diff}
```

## Permission boundaries
You may read repository files and run targeted pytest, ruff, git diff, and git
status commands inside the detached temporary review worktree.
Do not edit the primary worktree. Do not edit committed source files; experiments
must remain disposable in the temporary worktree.
Do not make external integration calls, network calls, provider calls, or consume
additional model/API quota. Layer 3 has not been requested.

## Required response
Return JSON only: exactly one object matching the schema below. Do not add prose,
Markdown fences, identity fields, a verdict, or unknown fields. The harness derives
the verdict and stamps immutable review identity.

```json
{schema}
```
"""


def _warn_cleanup_failure(action: str) -> None:
    warnings.warn(
        f"temporary review worktree {action} failed",
        RuntimeWarning,
        stacklevel=3,
    )


@contextmanager
def detached_worktree(
    repo: Path, head_sha: str, *, runner: Runner = default_runner
) -> Iterator[Path]:
    """Yield an exact detached commit checkout and dispose of it afterward."""
    with tempfile.TemporaryDirectory(prefix="tradingagents-claude-review-") as root:
        target = Path(root) / "worktree"
        argv = ["git", "worktree", "add", "--detach", str(target), head_sha]
        try:
            try:
                result = runner(argv, cwd=repo, timeout=120, shell=False)
            except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
                raise ReviewError("could not create detached review worktree") from exc
            if result.returncode:
                raise ReviewError("could not create detached review worktree")
            yield target
        finally:
            cleanup_commands = (
                ("removal", ["git", "worktree", "remove", "--force", str(target)]),
                ("prune", ["git", "worktree", "prune"]),
            )
            for action, cleanup_argv in cleanup_commands:
                try:
                    cleanup = runner(
                        cleanup_argv,
                        cwd=repo,
                        timeout=120,
                        shell=False,
                    )
                except Exception:
                    _warn_cleanup_failure(action)
                else:
                    if cleanup.returncode:
                        _warn_cleanup_failure(action)
