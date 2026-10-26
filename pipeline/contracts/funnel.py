"""Decision-funnel event contract (specs/decision-funnel.md R1).

One line of ``<state_dir>/funnel/<session>/<date>.events.jsonl``. The
emitter (:mod:`pipeline.funnel_events`) validates every event against
:class:`FunnelEvent` before appending it; readers use ``parse_lenient``.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Literal

from pydantic import Field, model_validator

from pipeline.contracts.base import ContractModel, UtcInstant
from pipeline.contracts.briefs import Session

SCHEMA_VERSION = 1
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


class FunnelEvent(ContractModel):
    schema_: Literal[1] = Field(alias="schema")
    written_at: UtcInstant
    session: Session
    date: date
    component: str = Field(min_length=1)
    run: str = Field(min_length=1)
    stage: Literal["nominate", "pool", "collect", "evaluate", "trigger", "decide", "execute"]
    ticker: str | None
    outcome: Literal["passed", "excluded", "info"]
    code: str = Field(min_length=1)
    reason: str
    details: dict[str, Any]

    @model_validator(mode="after")
    def _code_known_for_stage(self) -> FunnelEvent:
        allowed = CODES[self.stage]
        if allowed is not None and self.code not in allowed:
            raise ValueError(f"unknown {self.stage} code {self.code!r}")
        return self
