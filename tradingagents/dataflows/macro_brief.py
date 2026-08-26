"""Local macro-brief vendor: reads the daily deep-search brief from disk.

Briefs are produced offline by the collector (specs/macro-brief-collector.md)
and consumed here read-only. File layout and format are defined in
specs/macro-brief-data-contract.md (v2, session-scoped); this module never
writes to the brief directory.
"""

import logging
import os
import re
from datetime import datetime

from .config import get_config
from .errors import VendorNotConfiguredError

logger = logging.getLogger(__name__)

# Legacy v1 files: one session-agnostic brief per day.
_BRIEF_NAME = re.compile(r"^(\d{4}-\d{2}-\d{2})\.md$")
# v2 files: session-scoped, two per day (specs/macro-brief-data-contract.md).
_SESSION_BRIEF_NAME = re.compile(r"^(\d{4}-\d{2}-\d{2})\.(cn|us)\.md$")

_SESSIONS = ("cn", "us")

# A brief older than this many calendar days relative to the analysis date gets
# a WARNING header (weekends make a 2-3 day gap normal; beyond that it's stale).
STALE_AFTER_DAYS = 3

# Candidate rank on a date tie: requested session beats the other session,
# which beats a legacy session-agnostic file (contract selection rule).
_RANK_SESSION_MATCH = 2
_RANK_OTHER = 1
_RANK_LEGACY = 0


def _normalize_session(session: str | None) -> str | None:
    """Lowercase/strip a session value; unknown sessions raise, None passes."""
    if session is None:
        return None
    session = str(session).strip().lower()
    if session not in _SESSIONS:
        raise ValueError(
            f"Unknown session {session!r}; expected one of {'/'.join(_SESSIONS)}"
        )
    return session


def _select_brief(curr_date: str, session: str | None):
    """Selection core shared by the reader and the path resolver.

    Returns ``(brief_dir, curr_dt, brief_dt, rank, name, file_session)`` for
    the winning candidate; raises ``VendorNotConfiguredError`` when nothing
    qualifies. ``session`` must already be normalized.
    """
    brief_dir = get_config()["macro_brief_dir"]
    curr_dt = datetime.strptime(curr_date, "%Y-%m-%d")

    # Candidate tuples ordered so ``max`` implements the contract's selection
    # rule directly: date first, then rank, then name (deterministic tiebreak).
    candidates: list[tuple[datetime, int, str, str | None]] = []
    if os.path.isdir(brief_dir):
        for name in os.listdir(brief_dir):
            session_match = _SESSION_BRIEF_NAME.match(name)
            legacy_match = None if session_match else _BRIEF_NAME.match(name)
            if session_match:
                date_str, file_session = session_match.groups()
            elif legacy_match:
                date_str, file_session = legacy_match.group(1), None
            else:
                continue
            try:
                brief_dt = datetime.strptime(date_str, "%Y-%m-%d")
            except ValueError:
                continue
            if brief_dt > curr_dt:
                continue
            if file_session is None:
                rank = _RANK_LEGACY
            elif session is not None and file_session == session:
                rank = _RANK_SESSION_MATCH
            else:
                rank = _RANK_OTHER
            candidates.append((brief_dt, rank, name, file_session))

    if not candidates:
        wanted = f" for the {session} session" if session else ""
        raise VendorNotConfiguredError(
            f"No macro brief found in {brief_dir} dated on or before {curr_date}"
            f"{wanted}. Generate one with the collector "
            "(specs/macro-brief-collector.md) or the bootstrap one-liner "
            "documented there."
        )

    return (brief_dir, curr_dt, *max(candidates))


def get_macro_brief_local(curr_date: str, session: str | None = None) -> str:
    """Return the best macro brief for ``curr_date``, optionally session-scoped.

    Candidates are all v2 session briefs (``YYYY-MM-DD.<session>.md``) and
    legacy v1 briefs (``YYYY-MM-DD.md``) dated on or before ``curr_date``,
    ranked by date first (newest wins); on a date tie the requested session
    beats the other session, which beats legacy. Serving a cross-session or
    legacy brief when a session was requested adds a NOTE header; a gap over
    ``STALE_AFTER_DAYS`` adds a WARNING header (unchanged from v1).

    ``session=None`` keeps the v1-compatible view: date-first with legacy
    files fully acceptable and no fallback note (there is no requested
    session to fall back from); on a date tie session-scoped files win over
    legacy, with the filename as a final deterministic tiebreak.

    Raises ``VendorNotConfiguredError`` when no qualifying brief exists — the
    ``macro_brief`` category is optional, so a missing brief degrades to the
    standard DATA_UNAVAILABLE sentinel instead of aborting the run. Callers
    that need loud failure (e.g. the A/B harness pre-flight) call this
    directly.
    """
    session = _normalize_session(session)
    brief_dir, curr_dt, brief_dt, rank, name, file_session = _select_brief(
        curr_date, session
    )
    path = os.path.join(brief_dir, name)
    with open(path, encoding="utf-8") as f:
        content = f.read()

    session_label = f" ({file_session} session)" if file_session else ""
    header = (
        f"[Macro brief as of {brief_dt:%Y-%m-%d}{session_label}, "
        f"read for analysis date {curr_date}]\n\n"
    )
    if session is not None and rank < _RANK_SESSION_MATCH:
        served = (
            f"the {file_session}-session brief"
            if file_session
            else "a legacy session-agnostic brief"
        )
        header = (
            f"[NOTE: requested the {session}-session macro brief; serving "
            f"{served}, the newest available on or before {curr_date}]\n" + header
        )
        logger.warning(
            "Serving %s macro brief %s for requested session %s (analysis date %s)",
            file_session or "legacy", name, session, curr_date,
        )
    age_days = (curr_dt - brief_dt).days
    if age_days > STALE_AFTER_DAYS:
        header = (
            f"[WARNING: this macro brief is {age_days} days older than the analysis "
            "date; treat time-sensitive claims with caution]\n" + header
        )
        logger.warning("Serving stale macro brief %s for analysis date %s", name, curr_date)

    return header + content


def resolve_macro_brief_path(curr_date: str, session: str | None = None) -> str:
    """Filesystem path of the exact brief ``get_macro_brief_local`` would serve.

    Shares the reader's selection core, so callers that need the served file
    itself — runner gating and the report header looking up the eval verdict
    via ``brief_evals.get_eval_verdict`` — bind to the same revision the
    reader serves instead of re-deriving selection (revision binding,
    specs/macro-brief-data-contract.md). Raises ``VendorNotConfiguredError``
    when no qualifying brief exists.
    """
    session = _normalize_session(session)
    brief_dir, _curr_dt, _brief_dt, _rank, name, _file_session = _select_brief(
        curr_date, session
    )
    return os.path.join(brief_dir, name)
