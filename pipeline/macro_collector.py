"""Macro brief collector — session-scoped deep-search briefs.

Spec: ``specs/macro-brief-collector.md`` (R1–R9); data shape:
``specs/macro-brief-data-contract.md`` (v2). One invocation renders the
deep-search prompt for a session slot (R1), runs a subscription-backed CLI
backend (R2), validates the result in full collector mode with a single
errors-appended retry (R3), and atomically writes
``<brief_dir>/YYYY-MM-DD.<session>.md`` (R4) — archiving any displaced
revision first, per the contract's archive-on-overwrite rule. An existing
same-session same-date brief short-circuits to exit 0 without invoking the
backend unless ``--force`` (R5). Any failure is a non-zero exit with a
one-line reason on stderr and never leaves a partial file (R6). Optional S3
sync is best-effort (R7), every run appends one line to ``collector.log``
(R8), and no scheduling lives here (R9 — the orchestrator owns the slots).

CLI::

    python -m pipeline.macro_collector --session cn|us [--date YYYY-MM-DD]
                                       [--backend claude|codex] [--force]

The backend subprocess boundary is injectable (``runner`` / ``s3_copy``
callables) so tests run fully offline.
"""

from __future__ import annotations

import argparse
import functools
import logging
import os
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

from pipeline.common import (
    SESSIONS,
    append_log_line,
    archive_existing,
    atomic_write,
    session_date,
    to_utc_iso,
)
from pipeline.config import load_config
from pipeline.contracts.base import ContractError
from pipeline.contracts.briefs import (
    MacroBriefMeta,
    parse_macro_brief,
    split_frontmatter,
    validate_macro_brief_path,
)
from pipeline.prompts import render_macro_prompt

logger = logging.getLogger("pipeline.macro_collector")

# ---------------------------------------------------------------------------
# Configuration surface
# ---------------------------------------------------------------------------

MACRO_BRIEF_DIR_ENV = "TRADINGAGENTS_MACRO_BRIEF_DIR"
S3_URI_ENV = "MACRO_BRIEF_S3_URI"
DEFAULT_MACRO_BRIEF_DIR = Path.home() / ".tradingagents" / "macro_briefs"
COLLECTOR_LOG_NAME = "collector.log"

#: R1/R2: generator identity written into the brief per invoked backend.
GENERATORS = {
    "claude": "claude-deep-search",
    "codex": "codex-deep-search",
}

#: Default backend commands; the rendered prompt is piped on stdin.
#: ``--skip-git-repo-check``: codex exec refuses to run outside a trusted git
#: worktree, and orchestrated components inherit an arbitrary cwd (launchd
#: starts them in the user's home) — without it the codex path dies before
#: searching (evaluator R2 precedent, D19 makes codex the collection default).
#: Deliberately no ``--model`` (unlike the evaluator's pinned gpt-5.6-terra):
#: collection deep searches ride the codex CLI's user-configured default
#: model, and R1 stamps the CLI-level identity ``codex-deep-search``.
BACKEND_COMMANDS = {
    "claude": ("claude", "-p", "--allowedTools", "WebSearch,WebFetch"),
    "codex": ("codex", "exec", "-c", "tools.web_search=true", "--skip-git-repo-check", "-"),
}

#: Share of the component budget reserved for everything that is not a
#: backend call: prompt render, validation, archive + atomic write, S3 sync.
HEADROOM_SECONDS = 120.0

S3_TIMEOUT_SECONDS = 300.0


def backend_timeout_seconds() -> float:
    """Per-attempt deep-search timeout for :func:`default_runner`.

    The first attempt receives the component budget minus non-backend
    headroom. A validation retry shares the resulting absolute deadline and
    therefore receives only its remaining time.
    """
    budget = float(load_config().component_timeouts.get("macro_collector", 1800))
    return max(1.0, budget - HEADROOM_SECONDS)

#: Injectable subprocess boundaries (tests fake these).
Runner = Callable[[str, str], str]  # (backend, prompt) -> brief text
S3Copy = Callable[[Path, str], None]  # (local file, MACRO_BRIEF_S3_URI)


class CollectorError(RuntimeError):
    """A collector failure whose message is the one-line stderr reason (R6)."""


@dataclass
class CollectResult:
    path: Path
    outcome: str  # "written" | "skipped"
    sources_count: int | None
    attempts: int


def resolve_brief_dir(override: str | Path | None = None) -> Path:
    """Target directory (R4): explicit override, else the contract's env var,
    else ``~/.tradingagents/macro_briefs``."""
    if override is not None:
        return Path(override).expanduser()
    env = os.environ.get(MACRO_BRIEF_DIR_ENV, "").strip()
    if env:
        return Path(env).expanduser()
    return DEFAULT_MACRO_BRIEF_DIR


# ---------------------------------------------------------------------------
# Backend invocation (R2) — the default runner shells out; tests inject fakes
# ---------------------------------------------------------------------------


def _first_line(text: str) -> str:
    for line in text.splitlines():
        if line.strip():
            return line.strip()
    return ""


def default_runner(
    backend: str, prompt: str, *, deadline: float | None = None
) -> str:
    """Run the deep-search CLI for ``backend`` with the prompt on stdin."""
    command = list(BACKEND_COMMANDS[backend])
    timeout = backend_timeout_seconds()
    if deadline is not None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise CollectorError(f"backend '{backend}' deadline exhausted before launch")
        timeout = min(timeout, remaining)
    try:
        proc = subprocess.run(
            command,
            input=prompt,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise CollectorError(f"backend CLI '{command[0]}' not found on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise CollectorError(f"backend '{backend}' timed out after {int(timeout)}s") from exc
    if proc.returncode != 0:
        reason = _first_line(proc.stderr or proc.stdout) or "no output"
        raise CollectorError(f"backend '{backend}' exited {proc.returncode}: {reason}")
    return proc.stdout


# ---------------------------------------------------------------------------
# Validation + single retry (R3)
# ---------------------------------------------------------------------------

RETRY_INSTRUCTIONS = (
    "# Previous attempt rejected by the structural validator\n"
    "\n"
    "Your previous brief failed validation with the errors listed below.\n"
    "Produce the complete corrected brief in the exact required format,\n"
    "fixing every error:\n"
)


def build_retry_prompt(prompt: str, errors: list[str]) -> str:
    """R3 retry: the original prompt with the validator's full error list appended."""
    bullets = "\n".join(f"- {error}" for error in errors)
    return f"{prompt}\n\n{RETRY_INSTRUCTIONS}\n{bullets}\n"


def _validate(text: str, target: Path, generator: str) -> MacroBriefMeta:
    """Full collector-mode validation (R3): structural requirements, quality
    gates, generator match, and the filename cross-check — all errors merged
    into one :class:`ContractError` so the retry prompt sees the complete list.
    """
    errors: list[str] = []
    meta: MacroBriefMeta | None = None
    try:
        meta, _body = parse_macro_brief(text, expected_generator=generator)
    except ContractError as exc:
        errors.extend(exc.errors)
    if meta is not None:
        try:
            validate_macro_brief_path(target, meta)
        except ContractError as exc:
            errors.extend(exc.errors)
    if errors or meta is None:
        raise ContractError(errors)
    return meta


def _generate_validated(
    runner: Runner, backend: str, prompt: str, target: Path, generator: str
) -> tuple[str, MacroBriefMeta, int]:
    text = runner(backend, prompt)
    try:
        return text, _validate(text, target, generator), 1
    except ContractError as first:
        logger.warning(
            "attempt 1 for %s failed validation (%d errors); retrying once",
            target.name,
            len(first.errors),
        )
        text = runner(backend, build_retry_prompt(prompt, first.errors))
        try:
            return text, _validate(text, target, generator), 2
        except ContractError as second:
            raise CollectorError(
                "validation failed after retry: " + "; ".join(second.errors)
            ) from second


# ---------------------------------------------------------------------------
# Archive-before-replace (R4 + contract) and optional S3 sync (R7)
# ---------------------------------------------------------------------------


def _displaced_generated_at(path: Path) -> str:
    """``generated_at`` of the revision about to be displaced, for the stable
    archive name; falls back to the file's mtime when unreadable."""
    try:
        data, _body, _errors = split_frontmatter(path.read_text(encoding="utf-8"))
    except OSError:
        data = None
    if isinstance(data, dict):
        value = data.get("generated_at")
        if isinstance(value, datetime):
            if value.tzinfo is None:
                value = value.replace(tzinfo=timezone.utc)
            return to_utc_iso(value)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return to_utc_iso(datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc))


def default_s3_copy(path: Path, uri: str) -> None:
    """Copy the brief to ``$MACRO_BRIEF_S3_URI`` via the aws CLI."""
    dest = f"{uri.rstrip('/')}/{path.name}"
    proc = subprocess.run(
        ["aws", "s3", "cp", str(path), dest],
        capture_output=True,
        text=True,
        timeout=S3_TIMEOUT_SECONDS,
    )
    if proc.returncode != 0:
        detail = _first_line(proc.stderr or proc.stdout) or f"exit {proc.returncode}"
        raise RuntimeError(f"aws s3 cp failed: {detail}")


def _maybe_s3_sync(target: Path, s3_copy: S3Copy | None) -> None:
    uri = os.environ.get(S3_URI_ENV, "").strip()
    if not uri:
        return
    copier = s3_copy or default_s3_copy
    try:
        copier(target, uri)
    except Exception as exc:  # R7: sync failure warns; local is the source of truth
        logger.warning("S3 sync of %s to %s failed: %s", target.name, uri, exc)
    else:
        logger.info("synced %s to %s", target.name, uri)


# ---------------------------------------------------------------------------
# Collection flow
# ---------------------------------------------------------------------------


def collect(
    session: str,
    as_of: date | str | None = None,
    *,
    backend: str | None = None,
    force: bool = False,
    runner: Runner | None = None,
    brief_dir: str | Path | None = None,
    s3_copy: S3Copy | None = None,
) -> CollectResult:
    """Collect one session-scoped macro brief; raises on any failure (R6).

    ``backend`` ``None`` resolves from the shared config's ``collect_backend``
    (env ``TRADINGAGENTS_COLLECT_BACKEND``, default ``codex`` per D19); an
    explicit value always wins. The brief is written only after full
    validation passes, via :func:`pipeline.common.atomic_write` — killing the
    process at any point never leaves a partial file (AC2).
    """
    if session not in SESSIONS:
        raise CollectorError(f"unknown session '{session}' — expected one of {sorted(SESSIONS)}")
    if backend is None:
        backend = load_config().collect_backend
    if backend not in GENERATORS:
        raise CollectorError(f"unknown backend '{backend}' — expected one of {sorted(GENERATORS)}")
    if as_of is None:
        as_of = session_date(session)  # R1: today in the *session* timezone
    elif isinstance(as_of, str):
        as_of = date.fromisoformat(as_of)

    generator = GENERATORS[backend]
    directory = resolve_brief_dir(brief_dir)
    target = directory / f"{as_of.isoformat()}.{session}.md"
    log_path = directory / COLLECTOR_LOG_NAME

    if target.exists() and not force:
        # R5: idempotent — same session + date already collected.
        logger.info("%s already exists — skipping (use --force to re-collect)", target)
        append_log_line(log_path, as_of.isoformat(), session, backend, "-", "skipped")
        return CollectResult(path=target, outcome="skipped", sources_count=None, attempts=0)

    try:
        run = runner
        if run is None:
            deadline = time.monotonic() + backend_timeout_seconds()
            run = functools.partial(default_runner, deadline=deadline)
        prompt = render_macro_prompt(as_of, session, generator)
        text, meta, attempts = _generate_validated(
            run, backend, prompt, target, generator
        )
    except Exception:
        append_log_line(log_path, as_of.isoformat(), session, backend, "-", "failed")
        raise

    if target.exists():  # only reachable with --force
        archived = archive_existing(target, _displaced_generated_at(target))
        if archived is not None:
            logger.info("archived previous revision to %s", archived)
    atomic_write(target, text)
    logger.info("wrote %s (%d sources, attempt %d)", target, meta.sources_count, attempts)
    _maybe_s3_sync(target, s3_copy)
    append_log_line(log_path, as_of.isoformat(), session, backend, meta.sources_count, "written")
    return CollectResult(
        path=target, outcome="written", sources_count=meta.sources_count, attempts=attempts
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"invalid date {value!r} — expected YYYY-MM-DD") from None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m pipeline.macro_collector",
        description="Collect one session-scoped deep-search macro brief "
        "(specs/macro-brief-collector.md).",
    )
    parser.add_argument(
        "--session", required=True, choices=SESSIONS, help="session slot: cn | us"
    )
    parser.add_argument(
        "--date",
        type=_parse_date,
        default=None,
        metavar="YYYY-MM-DD",
        help="as-of date in the session's local calendar "
        "(default: today in the session timezone; use for backfill)",
    )
    parser.add_argument(
        "--backend",
        choices=tuple(GENERATORS),
        default=None,
        help="deep-search backend (default: config collect_backend — codex per "
        "D19, env TRADINGAGENTS_COLLECT_BACKEND)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="re-collect even if this session's brief already exists for the date",
    )
    return parser


def _one_line(exc: BaseException) -> str:
    return " ".join(str(exc).split()) or exc.__class__.__name__


def main(argv: list[str] | None = None, runner: Runner | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    try:
        result = collect(
            args.session,
            as_of=args.date,
            backend=args.backend,
            force=args.force,
            runner=runner,
        )
    except KeyboardInterrupt:
        print("macro-collector: interrupted", file=sys.stderr)
        return 130
    except Exception as exc:  # R6: any failure => non-zero exit + one-line stderr reason
        logger.debug("collector failure detail", exc_info=True)
        print(f"macro-collector: {_one_line(exc)}", file=sys.stderr)
        return 1
    print(f"{result.outcome}: {result.path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
