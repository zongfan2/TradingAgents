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


# ---------------------------------------------------------------------------
# Snapshot (specs/decision-funnel.md R3): <state_dir>/funnel/<session>/<date>.json
# ---------------------------------------------------------------------------

SNAPSHOT_SCHEMA_VERSION = 1

Stage = Literal["nominate", "pool", "collect", "evaluate", "trigger", "decide", "execute"]
Outcome = Literal["passed", "excluded", "info"]
Provenance = Literal["recorded", "reconstructed", "unknown"]
Final = Literal["order-live", "order-dry", "excluded", "pending", "unknown"]


class StageEvent(ContractModel):
    outcome: Outcome
    code: str | None
    reason: str | None
    details: dict[str, Any]


class StageEntry(ContractModel):
    outcome: Outcome
    code: str | None
    reason: str | None
    provenance: Provenance
    details: dict[str, Any]
    events: list[StageEvent]
    #: Recorded stages only: how many other runs' events were superseded.
    notes: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _unknown_iff_no_code(self) -> StageEntry:
        # Acceptance 3: a stage the files cannot explain carries no code;
        # recorded and reconstructed stages always do.
        if (self.provenance == "unknown") != (self.code is None):
            raise ValueError(f"provenance {self.provenance!r} with code {self.code!r}")
        return self


class StopInfo(ContractModel):
    stage: Stage
    code: str | None
    reason: str | None
    provenance: Provenance


class TickerEntry(ContractModel):
    ticker: str = Field(min_length=1)
    layer: Literal["core", "opportunity", "watch", "removed", "nominee"] | None
    furthest_stage: Stage | None
    final: Final
    stop: StopInfo | None
    provenance: list[Provenance]
    stages: dict[Stage, StageEntry]


class PoolContext(ContractModel):
    as_of_date: date | None
    carried_forward: bool | None
    error: str | None
    core_fallback: bool
    core_error: str | None


class Thresholds(ContractModel):
    trigger: float
    entry: float
    exit: float
    opportunity_cap: int = Field(ge=0)
    watch_cap: int = Field(ge=0)
    source: dict[
        Literal["trigger", "entry", "exit"], Literal["config-snapshot", "events", "default"]
    ]


class SnapshotContext(ContractModel):
    macro_verdict: Literal["pass", "warn", "fail", "missing"]
    pool: PoolContext
    nomination_failed: str | None
    thresholds: Thresholds
    execute_ran: bool
    #: Only ambiguous evidence (a manual run's manual-trigger skip): the
    #: execute state is unknown — no ticker is pending.
    execute_uncertain: bool
    events: int = Field(ge=0)
    #: input label -> sha256 hex
    inputs: dict[str, str]


class SnapshotCounts(ContractModel):
    nominate: int = Field(ge=0)
    pool: int = Field(ge=0)
    collect: int = Field(ge=0)
    evaluate: int = Field(ge=0)
    trigger: int = Field(ge=0)
    decide: int = Field(ge=0)
    execute: int = Field(ge=0)
    final: dict[Final, int]


class FunnelSnapshot(ContractModel):
    """One slot's funnel snapshot. ``pipeline.funnel.write_snapshot``
    validates strictly before writing; readers use ``parse_lenient``."""

    schema_: Literal[1] = Field(alias="schema")
    session: Session
    date: date
    context: SnapshotContext
    counts: SnapshotCounts
    tickers: list[TickerEntry]

    @model_validator(mode="after")
    def _tickers_sorted_unique(self) -> FunnelSnapshot:
        names = [entry.ticker for entry in self.tickers]
        if names != sorted(set(names)):
            raise ValueError("tickers must be unique and sorted by ticker")
        if sum(self.counts.final.values()) != len(self.tickers):
            raise ValueError("counts.final does not add up to the ticker count")
        return self
