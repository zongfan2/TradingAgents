"""Safe boundaries for revision-bound Claude code review."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import warnings
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from pydantic import ValidationError

from devtools.verification.models import (
    ClaudeJudgment,
    Reviewer,
    ReviewReport,
    ReviewVerdict,
    Waiver,
    build_report,
    build_waiver_report,
)


class ReviewError(RuntimeError):
    """A review preflight or validation failed."""


class AuthUnavailable(ReviewError):
    """The local Claude CLI cannot supply an authenticated review."""


Runner = Callable[..., subprocess.CompletedProcess[Any]]
Clock = Callable[[], datetime]

EXIT_PASS = 0
EXIT_ERROR = 1
EXIT_WARN = 3
EXIT_FAIL = 4
DEFAULT_TIMEOUT = 1200

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
    except OSError as exc:
        raise AuthUnavailable("Claude auth status unavailable") from exc
    if result.returncode:
        try:
            failed_status = json.loads(result.stdout)
        except (json.JSONDecodeError, TypeError) as exc:
            raise AuthUnavailable("Claude auth status failed") from exc
        if isinstance(failed_status, dict) and failed_status.get("loggedIn") is False:
            raise AuthUnavailable("Claude CLI is not logged in")
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
        ["diff", "--no-ext-diff", "--name-only", f"{base_sha}..{head_sha}"],
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
    primary_paths = sorted({str(repo), str(repo.resolve())}, key=len, reverse=True)

    def redact_primary_path(value: str) -> str:
        for primary_path in primary_paths:
            value = value.replace(primary_path, "<primary checkout>")
        return value

    schema = json.dumps(ClaudeJudgment.model_json_schema(), indent=2, sort_keys=True)
    paths = "\n".join(f"- {redact_primary_path(path)}" for path in changed_files) or "- (none)"
    completed_steps: list[str] = []
    for line in layer1_result.splitlines():
        if line.startswith("==> "):
            step = line.removeprefix("==> ").partition(":")[0]
            if step in {"tests", "lint", "diff"} and step not in completed_steps:
                completed_steps.append(step)
    layer1_summary = (
        f"Layer 1 completed: {', '.join(completed_steps)}."
        if completed_steps
        else "Layer 1 deterministic gate completed."
    )
    return f"""# Independent Claude Code review packet

## Roles and authority
- Codex is the builder and integrator. It owns fixes and evaluates findings.
- Claude Code is the independent verifier. Review the committed change only.
- User is the sole authority for waivers and external-integration authorization.

## Immutable review identity
- Repository: Detached isolated checkout
- Base commit: {base_sha}
- Head commit: {head_sha}

## Governing context
Read AGENTS.md plus applicable governing component specs and data contracts in the
repository. Check the requested behavior, committed regression tests, and diff
against those sources of truth.

## Changed files
{paths}

## Requested behavior and acceptance criteria
{redact_primary_path(acceptance)}

## Risk classification and known limitations
{redact_primary_path(risk)}

## Layer 1 deterministic commands and result
The exact head checkout was checked with pytest, ruff, and git diff --check.
{layer1_summary}

## Original symptom and regression-test context
{redact_primary_path(original_symptom)}

## Review diff
```diff
{redact_primary_path(diff)}
```

## Permission boundaries
You may use only Read inside the detached temporary review worktree. Do not run shell commands.
Do not edit the primary worktree or inspect any other checkout, Git metadata, or repository-local secret.
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


@dataclass(frozen=True)
class ReviewOutcome:
    """A validated report and its two persisted representations."""

    report: ReviewReport
    json_path: Path
    markdown_path: Path


def default_report_dir() -> Path:
    """Return the sole configured location for verification reports."""
    return Path(
        os.environ.get(
            "TRADINGAGENTS_VERIFICATION_DIR", "~/.tradingagents/verification"
        )
    ).expanduser()


def review_boundaries(repo: Path, *, runner: Runner = default_runner) -> tuple[Path, ...]:
    """Return every existing checkout and shared Git directory for ``repo``."""
    try:
        worktrees = runner(
            ["git", "worktree", "list", "--porcelain"],
            cwd=repo,
            timeout=120,
            shell=False,
        )
        common = runner(
            ["git", "rev-parse", "--git-common-dir"],
            cwd=repo,
            timeout=120,
            shell=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ReviewError("could not inspect repository boundaries") from exc
    if worktrees.returncode or common.returncode:
        raise ReviewError("could not inspect repository boundaries")

    paths: list[Path] = []
    for line in worktrees.stdout.splitlines():
        if line.startswith("worktree "):
            paths.append(Path(line.removeprefix("worktree ")).resolve())
    common_dir = Path(common.stdout.strip())
    if not common_dir.is_absolute():
        common_dir = repo / common_dir
    paths.append(common_dir.resolve())
    return tuple(dict.fromkeys(paths))


def normalize_report_dir(
    repo: Path,
    report_dir: Path,
    *,
    runner: Runner = default_runner,
    boundaries: Sequence[Path] | None = None,
) -> Path:
    """Reject repository-backed, relative, and symlinked report locations."""
    if not report_dir.is_absolute():
        raise ReviewError("report directory must be absolute and outside repository boundaries")
    try:
        resolved = report_dir.resolve(strict=False)
    except OSError as exc:
        raise ReviewError("could not resolve report directory") from exc
    if resolved != report_dir:
        raise ReviewError("report directory must not use symlink aliases")
    protected = boundaries if boundaries is not None else review_boundaries(repo, runner=runner)
    for boundary in protected:
        try:
            resolved.relative_to(boundary)
        except ValueError:
            continue
        raise ReviewError("report directory must be outside repository boundaries")
    return resolved


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _clock_utc(clock: Clock) -> datetime:
    timestamp = clock()
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ReviewError("review clock must return a timezone-aware timestamp")
    return timestamp.astimezone(timezone.utc)


def _required_text(value: str, name: str) -> str:
    if not value.strip():
        raise ReviewError(f"{name} must not be empty")
    return value


def run_layer1(
    worktree: Path, python_executable: Path, runner: Runner = default_runner
) -> str:
    """Run the deterministic gate in the exact isolated head checkout."""
    argv = [
        str(python_executable),
        "-m",
        "devtools.verification.offline",
        "--only",
        "all",
        "--repo",
        str(worktree),
        "--python",
        str(python_executable),
    ]
    try:
        result = runner(
            argv,
            cwd=worktree,
            timeout=DEFAULT_TIMEOUT,
            shell=False,
        )
    except FileNotFoundError as exc:
        raise ReviewError("Layer 1 Python executable not found") from exc
    except subprocess.TimeoutExpired as exc:
        raise ReviewError("Layer 1 verification timed out") from exc
    except OSError as exc:
        raise ReviewError("Layer 1 verification could not start") from exc
    if result.returncode:
        step = "unknown step"
        for output in (result.stdout, result.stderr):
            if not isinstance(output, str):
                continue
            for line in output.splitlines():
                if line.startswith("==> "):
                    candidate = line.removeprefix("==> ").partition(":")[0]
                    if candidate in {"tests", "lint", "diff"}:
                        step = candidate
                        break
            if step != "unknown step":
                break
        raise ReviewError(f"Layer 1 verification exited {result.returncode} ({step})")
    return "\n".join(
        text.strip()
        for text in (result.stdout, result.stderr)
        if isinstance(text, str) and text.strip()
    )


def _claude_argv(boundaries: Sequence[Path]) -> list[str]:
    """Build a non-interactive, read-only Claude command for one isolated checkout."""
    allowed = "Read"
    denied = [f"Read(/{path.resolve()}/**)" for path in boundaries]
    settings = json.dumps({"permissions": {"deny": denied}}, sort_keys=True)
    return [
        "claude",
        "-p",
        "--output-format",
        "text",
        "--safe-mode",
        "--no-session-persistence",
        "--strict-mcp-config",
        "--mcp-config",
        '{"mcpServers":{}}',
        "--permission-mode",
        "dontAsk",
        "--setting-sources",
        "",
        "--tools",
        allowed,
        "--allowedTools",
        allowed,
        "--disallowedTools",
        "Bash,Grep,Glob,Write,Edit,NotebookEdit",
        "--settings",
        settings,
    ]


def _stage_text(path: Path, content: str) -> Path:
    """Write and fsync a report artifact without replacing its destination."""
    temporary: Path | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            delete=False,
        ) as stream:
            temporary = Path(stream.name)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        return temporary
    except OSError as exc:
        if temporary is not None:
            with suppress(OSError):
                temporary.unlink(missing_ok=True)
        raise ReviewError(f"could not write report {path.name}") from exc


def atomic_write_text(path: Path, content: str) -> Path:
    """Atomically replace one text artifact and remove failed temporary files."""
    temporary: Path | None = None
    try:
        temporary = _stage_text(path, content)
        os.replace(temporary, path)
        temporary = None
    except OSError as exc:
        raise ReviewError(f"could not write report {path.name}") from exc
    finally:
        if temporary is not None:
            with suppress(OSError):
                temporary.unlink(missing_ok=True)
    return path


def render_markdown(report: ReviewReport) -> str:
    """Derive a deterministic human-readable rendering from authoritative JSON."""
    lines = [
        "# Claude review report",
        "",
        f"Verdict: {report.verdict.value}",
        f"Reviewer: {report.reviewer.value}",
        f"Base: {report.base_sha}",
        f"Head: {report.head_sha}",
        f"Reviewed at: {report.reviewed_at.isoformat()}",
        "",
        "## Tests run",
        "",
    ]
    if report.tests_run:
        for test in report.tests_run:
            lines.append(
                f"- `{test.command}` — {test.status.value}: {test.summary}"
            )
    else:
        lines.append("No tests reported.")
    lines.extend(["", "## Findings", ""])
    if report.findings:
        for finding in report.findings:
            location = finding.file or "unspecified location"
            if finding.line is not None:
                location = f"{location}:{finding.line}"
            lines.extend(
                [
                    f"- [{finding.severity.value}] {location} — {finding.title}",
                    f"  Evidence: {finding.evidence}",
                    f"  Suggested test: {finding.suggested_test}",
                ]
            )
    else:
        lines.append("No findings.")
    lines.extend(["", "## Limitations", ""])
    if report.limitations:
        lines.extend(f"- {limitation}" for limitation in report.limitations)
    else:
        lines.append("No limitations.")
    if report.waiver is not None:
        lines.extend(
            [
                "",
                "## User waiver",
                "",
                f"- Approved by: {report.waiver.approved_by}",
                f"- Approved at: {report.waiver.approved_at.isoformat()}",
                f"- Reason: {report.waiver.reason}",
                f"- Unverified risk: {report.waiver.unverified_risk}",
            ]
        )
    return "\n".join(lines) + "\n"


def _persist_report(report: ReviewReport, report_dir: Path) -> ReviewOutcome:
    json_path = report_dir / f"{report.head_sha}.claude.json"
    markdown_path = report_dir / f"{report.head_sha}.claude.md"
    json_text = json.dumps(
        report.model_dump(mode="json"), indent=2, sort_keys=True
    ) + "\n"
    markdown_text = render_markdown(report)
    staged: list[Path | None] = [None, None]
    backups: list[Path | None] = [None, None]
    paths = (json_path, markdown_path)
    replaced = [False, False]
    try:
        staged[0] = _stage_text(json_path, json_text)
        staged[1] = _stage_text(markdown_path, markdown_text)
        for index, path in enumerate(paths):
            if path.exists():
                backups[index] = _stage_text(path, path.read_text(encoding="utf-8"))
        for index, path in enumerate(paths):
            os.replace(staged[index], path)
            staged[index] = None
            replaced[index] = True
    except OSError as exc:
        for index, path in enumerate(paths):
            backup = backups[index]
            try:
                if backup is not None:
                    os.replace(backup, path)
                    backups[index] = None
                elif replaced[index]:
                    path.unlink(missing_ok=True)
            except OSError:
                pass
        raise ReviewError("could not write report pair") from exc
    finally:
        for temporary in (*staged, *backups):
            if temporary is not None:
                with suppress(OSError):
                    temporary.unlink(missing_ok=True)
    return ReviewOutcome(report, json_path, markdown_path)


def load_current_report(path: Path, *, expected_head: str) -> ReviewReport:
    """Load authoritative JSON and require an exact resolved-head binding."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        report = ReviewReport.model_validate(payload)
    except OSError as exc:
        raise ReviewError(f"could not read report {path}") from exc
    except (json.JSONDecodeError, ValidationError, TypeError) as exc:
        raise ReviewError("report failed schema validation") from exc
    if report.head_sha != expected_head:
        raise ReviewError("stale report does not match requested head")
    return report


def exit_code_for_report(report: ReviewReport) -> int:
    """Map the stored verdict to the CLI contract, honoring explicit waivers."""
    if report.reviewer is Reviewer.USER_WAIVER:
        return EXIT_PASS
    return {
        ReviewVerdict.PASS: EXIT_PASS,
        ReviewVerdict.WARN: EXIT_WARN,
        ReviewVerdict.FAIL: EXIT_FAIL,
    }[report.verdict]


def run_review(
    *,
    repo: Path,
    base_ref: str,
    acceptance: str,
    risk: str,
    original_symptom: str,
    head_ref: str = "HEAD",
    report_dir: Path | None = None,
    python_executable: Path | None = None,
    timeout: int | float = DEFAULT_TIMEOUT,
    required: bool = True,
    runner: Runner = default_runner,
    clock: Clock = _utc_now,
) -> ReviewOutcome | None:
    """Run an independent review against an immutable isolated revision."""
    _required_text(acceptance, "acceptance")
    _required_text(risk, "risk")
    _required_text(original_symptom, "original symptom")
    auth_available = True
    try:
        check_claude_auth(runner=runner)
    except AuthUnavailable:
        if required:
            raise
        auth_available = False

    ensure_tracked_clean(repo, runner=runner)
    base_sha = resolve_commit(repo, base_ref, runner=runner)
    head_sha = resolve_commit(repo, head_ref, runner=runner)
    python = python_executable or Path(sys.executable).absolute()
    boundaries = review_boundaries(repo, runner=runner)
    persisted_report_dir = normalize_report_dir(
        repo,
        report_dir or default_report_dir(),
        runner=runner,
        boundaries=boundaries,
    )

    if auth_available:
        diff = git_diff(repo, base_sha, head_sha, runner=runner)
        paths = changed_files(repo, base_sha, head_sha, runner=runner)

    with detached_worktree(repo, head_sha, runner=runner) as isolated:
        layer1_result = run_layer1(isolated, python, runner=runner)
        if not auth_available:
            return None
        packet = build_review_packet(
            repo=repo,
            base_sha=base_sha,
            head_sha=head_sha,
            diff=diff,
            changed_files=paths,
            acceptance=acceptance,
            risk=risk,
            layer1_result=layer1_result,
            original_symptom=original_symptom,
        )
        argv = _claude_argv(boundaries)
        try:
            result = runner(
                argv,
                cwd=isolated,
                input=packet,
                timeout=timeout,
                shell=False,
            )
        except FileNotFoundError as exc:
            raise ReviewError("Claude CLI not found") from exc
        except subprocess.TimeoutExpired as exc:
            raise ReviewError("Claude review timed out") from exc
        except OSError as exc:
            raise ReviewError("Claude review could not start") from exc
        if result.returncode:
            raise ReviewError(f"Claude review exited {result.returncode}")
        judgment = parse_judgment(result.stdout)

    report = build_report(
        judgment,
        base_sha=base_sha,
        head_sha=head_sha,
        reviewed_at=_clock_utc(clock),
    )
    return _persist_report(report, persisted_report_dir)


def write_user_waiver(
    *,
    repo: Path,
    base_ref: str,
    reason: str,
    unverified_risk: str,
    user_approved: bool,
    head_ref: str = "HEAD",
    report_dir: Path | None = None,
    runner: Runner = default_runner,
    clock: Clock = _utc_now,
) -> ReviewOutcome:
    """Persist a user-authorized, revision-bound warning without model claims."""
    if user_approved is not True:
        raise ReviewError("waiver requires explicit user approval")
    _required_text(reason, "waiver reason")
    _required_text(unverified_risk, "unverified risk")
    persisted_report_dir = normalize_report_dir(
        repo, report_dir or default_report_dir(), runner=runner
    )
    base_sha = resolve_commit(repo, base_ref, runner=runner)
    head_sha = resolve_commit(repo, head_ref, runner=runner)
    approved_at = _clock_utc(clock)
    report = build_waiver_report(
        base_sha=base_sha,
        head_sha=head_sha,
        waiver=Waiver(
            approved_by="user",
            approved_at=approved_at,
            reason=reason,
            unverified_risk=unverified_risk,
        ),
    )
    return _persist_report(report, persisted_report_dir)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser(
        "run", help="run Layer 1 and an isolated Claude review"
    )
    run_parser.add_argument("--base", required=True)
    run_parser.add_argument("--head", default="HEAD")
    run_parser.add_argument("--acceptance", required=True)
    run_parser.add_argument("--risk", required=True)
    run_parser.add_argument("--original-symptom", required=True)
    run_parser.add_argument("--report-dir", type=Path)
    run_parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    run_parser.add_argument("--optional", action="store_true")

    check_parser = subparsers.add_parser(
        "check", help="validate the report bound to an exact head"
    )
    check_parser.add_argument("--head", default="HEAD")
    check_parser.add_argument("--report", type=Path)
    check_parser.add_argument("--report-dir", type=Path)

    waive_parser = subparsers.add_parser(
        "waive", help="record an explicitly user-approved waiver"
    )
    waive_parser.add_argument("--base", required=True)
    waive_parser.add_argument("--head", default="HEAD")
    waive_parser.add_argument("--reason", required=True)
    waive_parser.add_argument("--unverified-risk", required=True)
    waive_parser.add_argument("--user-approved", action="store_true", required=True)
    waive_parser.add_argument("--report-dir", type=Path)
    return parser


def _print_outcome(outcome: ReviewOutcome) -> int:
    label = (
        "waived"
        if outcome.report.reviewer is Reviewer.USER_WAIVER
        else outcome.report.verdict.value
    )
    print(f"{label} {outcome.json_path}")
    return exit_code_for_report(outcome.report)


def main(argv: Sequence[str] | None = None) -> int:
    """Dispatch revision-bound review commands with stable exit semantics."""
    args = _parser().parse_args(argv)
    repo = Path(__file__).resolve().parents[2]
    try:
        if args.command == "run":
            outcome = run_review(
                repo=repo,
                base_ref=args.base,
                head_ref=args.head,
                report_dir=args.report_dir,
                acceptance=args.acceptance,
                risk=args.risk,
                original_symptom=args.original_symptom,
                timeout=args.timeout,
                required=not args.optional,
            )
            if outcome is None:
                print("optional Claude review unavailable")
                return EXIT_PASS
            return _print_outcome(outcome)

        if args.command == "check":
            head_sha = resolve_commit(repo, args.head)
            if args.report is not None:
                report_dir = normalize_report_dir(repo, args.report.parent)
                path = report_dir / args.report.name
            else:
                report_dir = normalize_report_dir(repo, args.report_dir or default_report_dir())
                path = report_dir / f"{head_sha}.claude.json"
            report = load_current_report(path, expected_head=head_sha)
            return _print_outcome(
                ReviewOutcome(report, path, path.with_suffix(".md"))
            )

        outcome = write_user_waiver(
            repo=repo,
            base_ref=args.base,
            head_ref=args.head,
            report_dir=args.report_dir,
            reason=args.reason,
            unverified_risk=args.unverified_risk,
            user_approved=args.user_approved,
        )
        return _print_outcome(outcome)
    except ReviewError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    raise SystemExit(main())
