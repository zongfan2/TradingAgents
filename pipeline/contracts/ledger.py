"""Trade plan + ledger contract models (specs/trade-plan-ledger-contract.md v1).

``validate_trade_plan`` is the deterministic validator the analysis runner
applies to LLM-proposed plans: it returns the complete list of violations
(empty list = valid) rather than raising, because an invalid plan is a
recorded outcome (``plan_valid: false``), not a crash.
"""

from __future__ import annotations

import math
import re
from datetime import date
from typing import Literal

from pydantic import Field, field_validator, model_validator

from pipeline.contracts.base import ContractModel, UtcInstant
from pipeline.contracts.briefs import Session

# ---------------------------------------------------------------------------
# Identifier formats
# ---------------------------------------------------------------------------

#: Pipeline symbol inside an id: alphanumeric segments joined by ``.``/``-``
#: (``NVDA``, ``0700.HK``, ``BRK-B``) — a lone separator never qualifies.
_ID_TICKER = r"[A-Z0-9]+(?:[.-][A-Z0-9]+)*"
#: ``<date>-<session>-<ticker>-<arm>-<n>`` — n is 1-based, minted under flock.
RUN_ID_RE = re.compile(rf"^\d{{4}}-\d{{2}}-\d{{2}}-(cn|us)-{_ID_TICKER}-(brief|feeds)-[1-9]\d*$")
#: ``<date>-<session>-<ticker>-a<k>`` — pairing attempt number, 1-based.
PAIR_ID_RE = re.compile(rf"^\d{{4}}-\d{{2}}-\d{{2}}-(cn|us)-{_ID_TICKER}-a[1-9]\d*$")


def _id_date_valid(value: str) -> bool:
    """The leading ``YYYY-MM-DD`` must be a real calendar date (the shape
    regexes alone would accept e.g. ``2026-99-99``)."""
    try:
        date.fromisoformat(value[:10])
    except ValueError:
        return False
    return True


def is_valid_run_id(value: str) -> bool:
    """Full run-id check: :data:`RUN_ID_RE` shape plus a real calendar date."""
    return bool(RUN_ID_RE.match(value)) and _id_date_valid(value)


def is_valid_pair_id(value: str) -> bool:
    """Full pair-id check: :data:`PAIR_ID_RE` shape plus a real calendar date."""
    return bool(PAIR_ID_RE.match(value)) and _id_date_valid(value)

Action = Literal["BUY", "SELL", "HOLD"]
Decision = Literal["BUY", "SELL", "HOLD", "ERROR"]
Arm = Literal["brief", "feeds"]
Trigger = Literal["core", "catalyst", "manual"]
GateVerdict = Literal["pass", "warn", "fail", "missing"]
OrderRowKind = Literal["intent", "submitted", "skip", "cancel", "refresh"]
OrderKind = Literal["bracket", "close"]

# ---------------------------------------------------------------------------
# TradePlan
# ---------------------------------------------------------------------------


class Sizing(ContractModel):
    risk_pct: float


class TradePlan(ContractModel):
    """LLM-proposed plan embedded in a decision record.

    The model checks *shape* only; every behavioral rule (level ordering,
    distances, ranges, numeric hygiene) lives in :func:`validate_trade_plan`
    so violations surface as a reportable list instead of a parse failure.
    """

    action: Action
    conviction: float
    add_intent: bool = False
    add_rationale: str | None = None
    entry_zone: list[float] | None = None
    stop: float | None = None
    targets: list[float] | None = None
    horizon_days: int | None = None
    invalidation: str | None = None
    sizing: Sizing | None = None
    source_levels: str | None = None


def _finite(value: float | int | None) -> bool:
    return value is not None and math.isfinite(value)


def _finite_price(value: float | int | None) -> bool:
    return _finite(value) and value > 0


def validate_trade_plan(plan: TradePlan, last_close: float, atr14: float) -> list[str]:
    """Deterministic contract checks; returns the complete violation list.

    Contract rules: numeric hygiene for all actions (finite positive prices,
    ordered entry zone, strictly ascending unique targets, conviction in
    [0, 1], NaN/Inf anywhere ⇒ invalid, ``add_rationale`` required non-empty
    when ``add_intent``); BUY level checks against ``last_close``/``ATR14``;
    SELL is schema + hygiene only (broker-state checks are the execution
    adapter's, not ours); HOLD is trivially valid beyond hygiene.
    """
    violations: list[str] = []

    # -- numeric hygiene (all actions) --------------------------------------
    if not _finite(plan.conviction) or not 0.0 <= plan.conviction <= 1.0:
        violations.append(f"conviction {plan.conviction} not a finite number in [0, 1]")
    if plan.entry_zone is not None:
        if len(plan.entry_zone) != 2:
            violations.append(f"entry_zone must have exactly 2 prices, got {len(plan.entry_zone)}")
        if any(not _finite_price(price) for price in plan.entry_zone):
            violations.append(f"entry_zone {plan.entry_zone} contains a non-finite/non-positive price")
        elif len(plan.entry_zone) == 2 and plan.entry_zone[0] > plan.entry_zone[1]:
            violations.append(f"entry_zone {plan.entry_zone} not ordered (low ≤ high)")
    if plan.stop is not None and not _finite_price(plan.stop):
        violations.append(f"stop {plan.stop} is not a finite positive price")
    if plan.targets is not None:
        if not 1 <= len(plan.targets) <= 2:
            violations.append(f"targets must have 1-2 entries, got {len(plan.targets)}")
        if any(not _finite_price(price) for price in plan.targets):
            violations.append(f"targets {plan.targets} contain a non-finite/non-positive price")
        elif any(a >= b for a, b in zip(plan.targets, plan.targets[1:], strict=False)):
            violations.append(f"targets {plan.targets} not strictly ascending (no duplicates)")
    if plan.sizing is not None and not _finite(plan.sizing.risk_pct):
        violations.append(f"sizing.risk_pct {plan.sizing.risk_pct} is not finite")
    if plan.add_intent and not (plan.add_rationale or "").strip():
        violations.append("add_intent requires a non-empty add_rationale (the NEW information)")

    if plan.action != "BUY":
        # SELL: schema + numeric hygiene only. HOLD: trivially valid.
        return violations

    # -- BUY level checks ----------------------------------------------------
    for field_name in ("entry_zone", "stop", "targets", "horizon_days", "sizing"):
        if getattr(plan, field_name) is None:
            violations.append(f"BUY plan missing required field '{field_name}'")

    zone_ok = (
        plan.entry_zone is not None
        and len(plan.entry_zone) == 2
        and all(_finite_price(price) for price in plan.entry_zone)
        and plan.entry_zone[0] <= plan.entry_zone[1]
    )
    targets_ok = (
        plan.targets is not None
        and len(plan.targets) >= 1
        and all(_finite_price(price) for price in plan.targets)
    )

    if not _finite_price(last_close) or not _finite_price(atr14):
        violations.append(
            f"reference data unusable (last_close={last_close}, atr14={atr14}) — "
            "cannot validate BUY levels"
        )
        return violations

    if zone_ok:
        entry_mid = (plan.entry_zone[0] + plan.entry_zone[1]) / 2.0
        if _finite_price(plan.stop) and not plan.stop < plan.entry_zone[0]:
            violations.append(f"stop {plan.stop} not below entry_zone[0] {plan.entry_zone[0]}")
        if targets_ok and not plan.entry_zone[1] < min(plan.targets):
            violations.append(
                f"entry_zone[1] {plan.entry_zone[1]} not below min(targets) {min(plan.targets)}"
            )
        if abs(entry_mid - last_close) > 0.10 * last_close:
            violations.append(
                f"entry mid {entry_mid:g} not within ±10% of last close {last_close:g}"
            )
        if _finite_price(plan.stop) and plan.stop < plan.entry_zone[0]:
            stop_distance = entry_mid - plan.stop
            if stop_distance < 0.5 * atr14:
                violations.append(
                    f"stop distance {stop_distance:g} below 0.5×ATR14 ({0.5 * atr14:g})"
                )
            if stop_distance > 0.15 * entry_mid:
                violations.append(
                    f"stop distance {stop_distance:g} above 15% of entry mid ({0.15 * entry_mid:g})"
                )
    if (
        plan.sizing is not None
        and _finite(plan.sizing.risk_pct)
        and not 0.1 <= plan.sizing.risk_pct <= 2.0
    ):
        violations.append(f"sizing.risk_pct {plan.sizing.risk_pct} outside [0.1, 2.0]")
    if plan.horizon_days is not None and not 1 <= plan.horizon_days <= 30:
        violations.append(f"horizon_days {plan.horizon_days} outside [1, 30]")

    return violations


# ---------------------------------------------------------------------------
# decisions.jsonl
# ---------------------------------------------------------------------------


class InputRef(ContractModel):
    """Audit reference to the exact brief/pool revision a run consumed."""

    path: str
    generated_at: UtcInstant
    sha256: str


class DecisionInputs(ContractModel):
    macro_brief: InputRef | None = None
    ticker_brief: InputRef | None = None
    pool: InputRef | None = None
    config_digest: str


class DecisionRecord(ContractModel):
    run_id: str
    pair_id: str | None = None
    date: date
    session: Session
    ticker: str
    arm: Arm
    preset: str
    trigger: Trigger
    catalyst_score: float | None = None
    macro_eval_verdict: GateVerdict
    ticker_eval_verdict: GateVerdict
    inputs: DecisionInputs
    decided_at: UtcInstant
    decision: Decision
    plan: TradePlan | None = None
    plan_valid: bool

    @field_validator("run_id")
    @classmethod
    def _run_id_format(cls, value: str) -> str:
        if not is_valid_run_id(value):
            raise ValueError(f"run_id {value!r} not in <date>-<session>-<ticker>-<arm>-<n> form")
        return value

    @field_validator("pair_id")
    @classmethod
    def _pair_id_format(cls, value: str | None) -> str | None:
        if value is not None and not is_valid_pair_id(value):
            raise ValueError(f"pair_id {value!r} not in <date>-<session>-<ticker>-a<k> form")
        return value

    @model_validator(mode="after")
    def _ids_match_fields(self) -> DecisionRecord:
        """The contract derives both ids from the row's own fields — an
        inconsistent row would silently corrupt A/B aggregation joins."""
        errors: list[str] = []
        run_prefix = f"{self.date.isoformat()}-{self.session}-{self.ticker}-{self.arm}-"
        if not (self.run_id.startswith(run_prefix) and self.run_id[len(run_prefix):].isdigit()):
            errors.append(
                f"run_id {self.run_id!r} does not match this row's fields "
                f"(expected '{run_prefix}<n>')"
            )
        if self.pair_id is not None:
            pair_prefix = f"{self.date.isoformat()}-{self.session}-{self.ticker}-a"
            if not (
                self.pair_id.startswith(pair_prefix)
                and self.pair_id[len(pair_prefix):].isdigit()
            ):
                errors.append(
                    f"pair_id {self.pair_id!r} does not match this row's fields "
                    f"(expected '{pair_prefix}<k>')"
                )
        if errors:
            raise ValueError("; ".join(errors))
        return self


# ---------------------------------------------------------------------------
# orders.jsonl (execution adapter only)
# ---------------------------------------------------------------------------


class OrderRecord(ContractModel):
    run_id: str
    written_at: UtcInstant
    session: Session
    ticker: str
    kind: OrderRowKind
    dry_run: bool
    reason: str | None = None
    client_order_id: str
    order_kind: OrderKind | None = None
    tif: str | None = None
    qty: float | None = None
    limit_price: float | None = None
    stop_price: float | None = None
    target_price: float | None = None
    broker_status: str | None = None
    filled_avg_price: float | None = None
    filled_qty: float | None = None

    @field_validator("run_id")
    @classmethod
    def _run_id_format(cls, value: str) -> str:
        if not is_valid_run_id(value):
            raise ValueError(f"run_id {value!r} not in <date>-<session>-<ticker>-<arm>-<n> form")
        return value

    @model_validator(mode="after")
    def _kind_rules(self) -> OrderRecord:
        errors: list[str] = []
        if self.kind == "skip" and not (self.reason or "").strip():
            errors.append("kind='skip' rows require a non-empty reason")
        # Contract: ``order_kind`` is ``bracket | close`` — null for skip/refresh.
        if self.kind in ("skip", "refresh") and self.order_kind is not None:
            errors.append(
                f"kind={self.kind!r} rows must carry order_kind=null, "
                f"not {self.order_kind!r}"
            )
        if errors:
            raise ValueError("; ".join(errors))
        return self


# ---------------------------------------------------------------------------
# outcomes.jsonl (settle step only)
# ---------------------------------------------------------------------------


class HorizonReturns(ContractModel):
    d1: float | None = None
    d5: float | None = None
    d20: float | None = None


class PlanReplay(ContractModel):
    entry_hit: bool
    stop_hit_first: bool
    target_hit_first: bool
    ambiguous: bool
    realized_rr: float | None = None


class PaperOutcome(ContractModel):
    realized_pnl: float
    closed: bool


class OutcomeRecord(ContractModel):
    run_id: str
    settled_at: UtcInstant
    returns: HorizonReturns
    benchmark_returns: HorizonReturns
    plan_replay: PlanReplay | None = None
    paper: PaperOutcome | None = None

    @field_validator("run_id")
    @classmethod
    def _run_id_format(cls, value: str) -> str:
        if not is_valid_run_id(value):
            raise ValueError(f"run_id {value!r} not in <date>-<session>-<ticker>-<arm>-<n> form")
        return value


# ---------------------------------------------------------------------------
# positions.json (execution adapter refresh — atomic snapshot)
# ---------------------------------------------------------------------------


class OpenOrderLeg(ContractModel):
    client_order_id: str
    leg: str
    price: float


class PositionEntry(ContractModel):
    ticker: str
    qty: float
    avg_entry: float
    last_fill_at: UtcInstant
    market_value: float
    unrealized_pl: float
    tranches: int
    open_orders: list[OpenOrderLeg] = Field(default_factory=list)


class PositionsSnapshot(ContractModel):
    as_of: UtcInstant
    equity: float
    positions: list[PositionEntry] = Field(default_factory=list)
