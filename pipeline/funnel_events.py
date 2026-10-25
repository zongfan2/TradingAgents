"""Decision-funnel event log (specs/decision-funnel.md R1).

Each pipeline component records, per ticker, why it passed a ticker on or
excluded it, as one JSON line in
``<state_dir>/funnel/<session>/<date>.events.jsonl``. The log is purely
additive: no existing contract file changes, and emitting is best-effort — a
write failure logs a warning and never alters the component's behavior,
outputs or exit code.

Every :class:`FunnelEvents` instance stamps its events with one ``run``
id, so a rerun of the same component and slot appends a new run whose events
supersede the earlier one's (the snapshot builder keeps the last run per
component).
"""

from __future__ import annotations

import functools
import json
import logging
import uuid
from collections.abc import Callable
from datetime import date, datetime, timezone
from pathlib import Path

from pipeline.common import SESSIONS, locked_append, to_utc_iso

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1
FUNNEL_DIRNAME = "funnel"
EVENTS_SUFFIX = ".events.jsonl"

OUTCOMES = ("passed", "excluded", "info")

#: Stable codes per stage (spec R1 table). ``execute`` also accepts every
#: adapter skip reason, so it is open-ended there (the code is the reason's
#: prefix before any ``:`` detail).
CODES: dict[str, frozenset[str] | None] = {
    "nominate": frozenset(
        {"nominated", "wrong-session", "duplicate", "nomination-failed"}
    ),
    "pool": frozenset(
        {
            "core",
            "opportunity-enter",
            "opportunity-stay",
            "watch",
            "carried-forward",
            "dropped-low-score",
            "dropped-gate-fail",
            "cap-truncated",
            "watch-cap",
            "removed-low-score-streak",
            "removed-gate-fail-streak",
            "now-core",
        }
    ),
    "collect": frozenset(
        {"written", "exists", "failed", "not-collected-watch", "opportunity-dropped-stale-pool"}
    ),
    "evaluate": frozenset(
        {"pass", "warn", "fail", "eval-error", "skipped-below-threshold", "skipped-absent"}
    ),
    "trigger": frozenset(
        {
            "core",
            "core-pair",
            "catalyst",
            "no-score",
            "eval-fail",
            "below-threshold",
            "budget-dropped",
            "already-done",
            "macro-fail-feeds-only",
        }
    ),
    "decide": frozenset({"BUY", "SELL", "HOLD", "ERROR"}),
    "execute": None,
}

Clock = Callable[[], datetime]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def funnel_dir(state_dir: str | Path, session: str) -> Path:
    return Path(state_dir) / FUNNEL_DIRNAME / session


def events_path(state_dir: str | Path, session: str, slot_date: date | str) -> Path:
    day = slot_date.isoformat() if isinstance(slot_date, date) else str(slot_date)
    return funnel_dir(state_dir, session) / f"{day}{EVENTS_SUFFIX}"


def reason_code(reason: str) -> str:
    """An adapter skip reason's stable code: the text before any ``:``
    detail (``submit-error: HTTP 422 …`` ⇒ ``submit-error``)."""
    return reason.split(":", 1)[0].strip()


class FunnelEvents:
    """Best-effort emitter for one component invocation and slot."""

    def __init__(
        self,
        state_dir: str | Path,
        session: str,
        slot_date: date | str,
        component: str,
        *,
        clock: Clock = _utc_now,
        run: str | None = None,
    ) -> None:
        self.session = session
        self.date = slot_date.isoformat() if isinstance(slot_date, date) else str(slot_date)
        self.component = component
        self.path = events_path(state_dir, session, self.date)
        self._clock = clock
        self.run = run or f"{to_utc_iso(clock())}-{uuid.uuid4().hex[:8]}"
        #: Events this instance wrote (tests and summaries read it).
        self.emitted: list[dict] = []

    def emit(
        self,
        stage: str,
        ticker: str | None,
        outcome: str,
        code: str,
        reason: str = "",
        **details: object,
    ) -> None:
        try:
            if self.session not in SESSIONS:
                raise ValueError(f"unknown session {self.session!r}")
            if stage not in CODES:
                raise ValueError(f"unknown funnel stage {stage!r}")
            if outcome not in OUTCOMES:
                raise ValueError(f"unknown funnel outcome {outcome!r}")
            allowed = CODES[stage]
            if allowed is not None and code not in allowed:
                raise ValueError(f"unknown {stage} code {code!r}")
            event = {
                "schema": SCHEMA_VERSION,
                "written_at": to_utc_iso(self._clock()),
                "session": self.session,
                "date": self.date,
                "component": self.component,
                "run": self.run,
                "stage": stage,
                "ticker": ticker,
                "outcome": outcome,
                "code": code,
                "reason": reason,
                "details": details,
            }
            line = json.dumps(event, ensure_ascii=False, sort_keys=True, default=str)
            locked_append(self.path, line)
        except Exception as exc:  # best-effort: never change the component
            logger.warning(
                "funnel event not recorded (%s %s %s): %s",
                stage,
                ticker,
                code,
                " ".join(str(exc).split()) or type(exc).__name__,
            )
            return
        self.emitted.append(event)


def best_effort(func: Callable) -> Callable:
    """Wrap a component's funnel helper so that nothing it does — argument
    binding or formatting included — can escape into the component."""

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except Exception as exc:
            logger.warning(
                "funnel events skipped in %s: %s",
                func.__name__,
                " ".join(str(exc).split()) or type(exc).__name__,
            )
            return None

    return wrapper


def read_events(state_dir: str | Path, session: str, slot_date: date | str) -> list[dict]:
    """Every parseable event line for one slot (junk lines are skipped)."""
    path = events_path(state_dir, session, slot_date)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    events: list[dict] = []
    for line in text.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events
