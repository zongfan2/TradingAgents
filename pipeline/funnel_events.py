"""Decision-funnel event log (specs/decision-funnel.md R1).

Each pipeline component records, per ticker, why it passed a ticker on or
excluded it, as one JSON line in
``<state_dir>/funnel/<session>/<date>.events.jsonl``. The log is purely
additive: no existing contract file changes, and emitting is best-effort — a
write failure logs a warning and never alters the component's behavior,
outputs or exit code.

Every :class:`FunnelEvents` instance stamps its events with one ``run``
id, so a rerun of the same component and slot appends a new run.
:func:`current_outcomes` is the supersession rule the snapshot builder uses:
per ``(stage, ticker)``, the latest run that *decided* the ticker (a
``passed``/``excluded`` event) wins, and within that run the strongest
outcome (``passed`` > ``excluded`` > ``info``) is the stage outcome — so both
arms of a pair, a SELL's cancels next to its close, or a feeds-arm
non-candidate next to the brief arm's order all keep the right result. A
run with only ``info`` events (``already-done``, ``superseded``, the
adapter's rerun ``dedupe``) never replaces a decision; other runs' events
are kept as notes. The ``execute`` stage is the exception: a LIVE order a
run placed stays at the broker, so the latest run with a live
``submitted`` decides there whatever later runs emit; dry-run submissions
place nothing and follow the general rule.
"""

from __future__ import annotations

import functools
import json
import logging
import uuid
from collections.abc import Callable
from datetime import date, datetime, timezone
from pathlib import Path

from pipeline.common import locked_append, to_utc_iso
from pipeline.contracts.funnel import SCHEMA_VERSION, FunnelEvent

logger = logging.getLogger(__name__)

FUNNEL_DIRNAME = "funnel"
EVENTS_SUFFIX = ".events.jsonl"

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
            # Strict writer validation (specs/README.md rule 5) on the line as
            # it will be read back.
            FunnelEvent.model_validate(json.loads(line))
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


_STRENGTH = {"info": 0, "excluded": 1, "passed": 2}


def _is_live_submission(event: dict) -> bool:
    details = event.get("details") or {}
    return (
        event.get("outcome") == "passed"
        and event.get("code") == "submitted"
        and details.get("dry_run") is False
    )


def current_outcomes(events: list[dict]) -> dict[tuple[str, str | None], dict]:
    """Reduce a slot's events (file order) to one outcome per
    ``(stage, ticker)`` — the documented supersession rule (module docstring).

    Each value holds the chosen ``outcome``/``code``/``reason``, the deciding
    ``run``, that run's ``events`` for the key (both arms of a pair, …) and
    ``notes``: the other runs' events (superseded decisions, info-only
    reruns)."""
    grouped: dict[tuple[str, str | None], list[dict]] = {}
    for event in events:
        grouped.setdefault((event.get("stage"), event.get("ticker")), []).append(event)
    result: dict[tuple[str, str | None], dict] = {}
    for key, group in grouped.items():
        deciding = [e.get("run") for e in group if e.get("outcome") in ("passed", "excluded")]
        run = deciding[-1] if deciding else group[-1].get("run")
        if key[0] == "execute":
            # A LIVE order stays at the broker whatever a later run emits, so
            # the latest run that placed one decides; dry runs place nothing.
            placed = [e.get("run") for e in group if _is_live_submission(e)]
            if placed:
                run = placed[-1]
        own = [e for e in group if e.get("run") == run]
        best = max(own, key=lambda e: _STRENGTH.get(e.get("outcome"), -1))
        result[key] = {
            "outcome": best.get("outcome"),
            "code": best.get("code"),
            "reason": best.get("reason"),
            "run": run,
            "events": own,
            "notes": [e for e in group if e.get("run") != run],
        }
    return result
