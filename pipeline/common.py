"""Dependency-light utilities shared by every pipeline component.

File-writing rules (atomic write, archive-before-replace, locked appends) come
from the data contracts in ``specs/``; session/timezone helpers implement the
design rule that slot dates always resolve in the *session* timezone, never the
host timezone (design doc D2 — the host may be America/Chicago).
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import os
import re
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

# ---------------------------------------------------------------------------
# Atomic file operations
# ---------------------------------------------------------------------------


def atomic_write(path: str | Path, content: str) -> Path:
    """Write ``content`` to ``path`` atomically (temp file in the same
    directory + ``os.replace``). Killing the process mid-write can never leave
    a partial file at ``path``.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise
    return path


def archive_existing(path: str | Path, generated_at: str | datetime) -> Path | None:
    """Move an existing file to ``<dir>/archive/<name-stem>.<generated_at><suffix>``.

    This is the contracts' archive-on-overwrite rule (e.g. a brief
    ``2026-08-03.us.md`` whose previous revision carried
    ``generated_at: 2026-08-03T12:35:00Z`` archives to
    ``archive/2026-08-03.us.2026-08-03T12:35:00Z.md``). ``generated_at`` is the
    frontmatter/JSON timestamp of the revision being displaced, so the archived
    name is stable and immutable — ledger hashes always resolve to preserved
    content. Archived revisions are never overwritten: ``generated_at`` is
    self-reported by the generator, so two displaced revisions can collide on
    the same name — the later one gets a ``.2``/``.3``/... disambiguator
    (``archive/2026-08-03.us.2026-08-03T12:35:00Z.2.md``) instead of silently
    destroying the earlier archive. Returns the archived path, or ``None``
    when ``path`` does not exist (nothing to archive).
    """
    path = Path(path)
    if not path.exists():
        return None
    if isinstance(generated_at, datetime):
        generated_at = to_utc_iso(generated_at)
    archive_dir = path.parent / "archive"
    archive_dir.mkdir(parents=True, exist_ok=True)
    target = archive_dir / f"{path.stem}.{generated_at}{path.suffix}"
    counter = 1
    while True:
        try:
            # Hardlink refuses to clobber an existing archived revision
            # (os.replace would), and a crash between link and unlink leaves
            # both copies — never zero.
            os.link(path, target)
            break
        except FileExistsError:
            counter += 1
            target = archive_dir / f"{path.stem}.{generated_at}.{counter}{path.suffix}"
        except FileNotFoundError:
            return None  # raced away since the exists() check — nothing to archive
    path.unlink(missing_ok=True)
    return target


def to_utc_iso(moment: datetime) -> str:
    """Render an aware datetime as the contracts' ISO 8601 UTC form (``...Z``)."""
    if moment.tzinfo is None:
        raise ValueError("naive datetime — contracts require timezone-aware UTC instants")
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Hashing
# ---------------------------------------------------------------------------


def sha256_text(text: str) -> str:
    """Hex sha256 of a string (UTF-8) — the ledger's brief/pool revision hash."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_file(path: str | Path) -> str:
    """Hex sha256 of a file's bytes."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


# ---------------------------------------------------------------------------
# Log / ledger appends
# ---------------------------------------------------------------------------

LOG_FIELD_SEPARATOR = " | "


def append_log_line(path: str | Path, *fields: object) -> None:
    """Append one ``field | field | ...`` line to a component log
    (collector R8 / evaluator R6 style). Creates parent directories.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = LOG_FIELD_SEPARATOR.join(str(field) for field in fields)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def locked_append(path: str | Path, line: str) -> None:
    """Append one line under an exclusive ``flock`` with ``O_APPEND``.

    The ledger contract requires jsonl writers to append atomically and the
    analysis runner to count-and-append run ids under an exclusive lock
    (single host), so concurrent slot and manual runs cannot interleave
    partial lines or mint duplicate ids.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not line.endswith("\n"):
        line += "\n"
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            os.write(fd, line.encode("utf-8"))
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


# ---------------------------------------------------------------------------
# Sessions (design doc D2: IANA names only; dates resolve in the session tz)
# ---------------------------------------------------------------------------

SESSION_TZ = {
    "cn": "Asia/Shanghai",
    "us": "America/New_York",
}

SESSIONS = tuple(SESSION_TZ)


def _session_zone(session: str) -> ZoneInfo:
    try:
        return ZoneInfo(SESSION_TZ[session])
    except KeyError:
        raise ValueError(
            f"unknown session {session!r} — expected one of {sorted(SESSION_TZ)}"
        ) from None


def session_now(session: str) -> datetime:
    """Current aware datetime in the session's timezone."""
    return datetime.now(tz=_session_zone(session))


def session_date(session: str, at: datetime | None = None) -> date:
    """The session-local calendar date at instant ``at`` (default: now).

    ``at`` must be timezone-aware; the host timezone never participates —
    e.g. 19:30 America/Chicago is already the *next* day for the ``cn``
    session (08:30 Asia/Shanghai).
    """
    zone = _session_zone(session)
    if at is None:
        at = datetime.now(timezone.utc)
    if at.tzinfo is None:
        raise ValueError("session_date requires an aware datetime (or None for now)")
    return at.astimezone(zone).date()


_CN_SUFFIXES = (".SS", ".SZ", ".HK")


def session_for_ticker(ticker: str) -> str:
    """Session a pipeline symbol belongs to: ``.SS``/``.SZ``/``.HK`` suffix
    means ``cn``, everything else ``us`` (ticker-brief contract's session
    uniqueness invariant).
    """
    return "cn" if ticker.upper().endswith(_CN_SUFFIXES) else "us"


# ---------------------------------------------------------------------------
# Prompt templating
# ---------------------------------------------------------------------------


class TemplateError(ValueError):
    """A rendered template still contains ``{{...}}`` placeholders."""


_PLACEHOLDER_RE = re.compile(r"\{\{\s*([A-Za-z0-9_]+)\s*\}\}")


def render_template(text: str, mapping: dict[str, object]) -> str:
    """Substitute ``{{KEY}}`` placeholders and refuse partial renders.

    Any ``{{`` left in the output raises :class:`TemplateError` — a collector
    must never send a prompt with an unfilled ``{{SESSION}}`` to the backend.
    """
    out = text
    for key, value in mapping.items():
        out = out.replace("{{" + key + "}}", str(value))
    if "{{" in out:
        unresolved = sorted(set(_PLACEHOLDER_RE.findall(out)))
        detail = ", ".join(unresolved) if unresolved else "malformed '{{' sequence"
        raise TemplateError(f"unresolved template placeholders: {detail}")
    return out
