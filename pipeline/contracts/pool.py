"""Tiered stock pool contract models (specs/pool-data-contract.md v1).

The pool builder validates strictly before writing; consumers use
:func:`read_pool`, which implements the contract's reading rule (newest file
dated on or before the analysis date, with a staleness classification).
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Literal

from pydantic import Field, ValidationError, field_validator, model_validator

from pipeline.common import session_for_ticker
from pipeline.contracts.base import (
    ContractError,
    ContractModel,
    UtcInstant,
    validation_error_messages,
)
from pipeline.contracts.briefs import CatalystType, Session

logger = logging.getLogger("pipeline.contracts.pool")

#: Layer caps (contract hard requirement 4). ``core`` is soft — warn only.
CORE_CAP = 10
OPPORTUNITY_CAP = 5
WATCH_CAP = 10

#: Reading rule (contract, normative): gap beyond this ⇒ the pool is absent.
POOL_MAX_STALENESS_DAYS = 3

Gate = Literal["pass", "watch", "fail"]

_POOL_NAME_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})\.json$")


class BollBands(ContractModel):
    close: float
    mid: float
    upper: float
    lower: float


class TechnicalBlock(ContractModel):
    gate: Gate
    boll_daily: BollBands | None = None
    boll_weekly: BollBands | None = None
    #: Gate v1.1 liquidity snapshot (optional — pre-v1.1 pool files stay
    #: valid). ``avg_dollar_volume_20d`` is the 20-day mean of close × volume
    #: in the listing currency; only positive values are recorded (an
    #: uncomputable or zero-volume tape stays ``None`` — never fabricated).
    avg_dollar_volume_20d: float | None = Field(default=None, gt=0.0)
    #: 5-day / 20-day average-volume ratio (volume confirmation); 0.0 is a
    #: legal observation (a dead-quiet week), negatives are not.
    volume_ratio_5d_20d: float | None = Field(default=None, ge=0.0)


class CoreEntry(ContractModel):
    """Mirror of a ``core.<session>.yaml`` row; ``score``/``technical`` are
    informative annotations the builder MAY add (renderable, never gating)."""

    ticker: str = Field(min_length=1)
    note: str | None = None
    score: float | None = Field(default=None, ge=0.0, le=10.0)
    technical: TechnicalBlock | None = None


class WatchEntry(ContractModel):
    ticker: str = Field(min_length=1)
    score: float = Field(ge=0.0, le=10.0)
    catalyst_type: CatalystType
    rationale: str = Field(min_length=1)
    citations: list[str] = Field(min_length=1)
    technical: TechnicalBlock

    @field_validator("citations")
    @classmethod
    def _citations_are_urls(cls, value: list[str]) -> list[str]:
        for url in value:
            if not url.startswith(("http://", "https://")):
                raise ValueError(f"citation {url!r} is not a URL")
        return value


class OpportunityEntry(WatchEntry):
    """Watch entry plus the hysteresis state that lives in the pool file."""

    entered_on: date
    low_score_streak: int = Field(ge=0)
    gate_fail_streak: int = Field(ge=0)


class RemovedEntry(ContractModel):
    ticker: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    last_score: float | None = None


class PoolFile(ContractModel):
    """``pools/<session>/YYYY-MM-DD.json``."""

    as_of_date: date
    session: Session
    generated_at: UtcInstant
    generator: str = Field(min_length=1)
    carried_forward: bool = False
    core: list[CoreEntry] = Field(default_factory=list)
    opportunity: list[OpportunityEntry] = Field(default_factory=list)
    watch: list[WatchEntry] = Field(default_factory=list)
    removed: list[RemovedEntry] = Field(default_factory=list)

    @model_validator(mode="after")
    def _contract_rules(self) -> PoolFile:
        errors: list[str] = []
        # Caps (hard requirement 4): opportunity/watch are hard; core is soft.
        if len(self.opportunity) > OPPORTUNITY_CAP:
            errors.append(f"opportunity has {len(self.opportunity)} entries (cap {OPPORTUNITY_CAP})")
        if len(self.watch) > WATCH_CAP:
            errors.append(f"watch has {len(self.watch)} entries (cap {WATCH_CAP})")
        if len(self.core) > CORE_CAP:
            logger.warning(
                "core layer has %d entries (soft cap %d) — core is user-controlled, not failing",
                len(self.core),
                CORE_CAP,
            )
        # No duplicates within or across the membership layers (hard req. 5).
        layers = {"core": self.core, "opportunity": self.opportunity, "watch": self.watch}
        seen: dict[str, str] = {}
        for layer_name, entries in layers.items():
            tickers = [entry.ticker for entry in entries]
            for ticker in sorted({t for t in tickers if tickers.count(t) > 1}):
                errors.append(f"duplicate ticker '{ticker}' within layer '{layer_name}'")
            for ticker in tickers:
                if ticker in seen and seen[ticker] != layer_name:
                    errors.append(
                        f"ticker '{ticker}' appears in both '{seen[ticker]}' and '{layer_name}'"
                    )
                seen.setdefault(ticker, layer_name)
        # Symbols must belong to the session's market (hard requirement 5).
        for layer_name, entries in {**layers, "removed": self.removed}.items():
            for entry in entries:
                derived = session_for_ticker(entry.ticker)
                if derived != self.session:
                    errors.append(
                        f"{layer_name} ticker '{entry.ticker}' belongs to session "
                        f"'{derived}', not '{self.session}'"
                    )
        if errors:
            raise ValueError("; ".join(errors))
        return self


Staleness = Literal["fresh", "warn", "absent"]


@dataclass(frozen=True)
class PoolReadResult:
    """Outcome of the contract's reading rule.

    ``staleness == "absent"`` means consumers must treat the opportunity layer
    as empty and fall back to ``core.<session>.yaml`` for core coverage; the
    parsed file (when one exists at all) is still returned so the ledger can
    record what was actually on disk.
    """

    pool: PoolFile | None
    path: Path | None
    as_of_date: date | None
    gap_days: int | None
    staleness: Staleness


def read_pool(
    pool_dir: str | Path,
    session: Session,
    on_date: date,
    pool_max_staleness_days: int = POOL_MAX_STALENESS_DAYS,
) -> PoolReadResult:
    """Read the newest pool file with date ≤ ``on_date`` for the session.

    Classification: gap 0 ⇒ ``fresh``; 1 ≤ gap ≤ ``pool_max_staleness_days``
    ⇒ ``warn`` (proceed with a prominent staleness warning); gap beyond that,
    or no file at all ⇒ ``absent``. Files are parsed leniently (readers warn
    on unknown fields); a file that fails validation raises
    :class:`ContractError`.
    """
    session_dir = Path(pool_dir) / session
    candidates: list[tuple[date, Path]] = []
    if session_dir.is_dir():
        for entry in session_dir.iterdir():
            match = _POOL_NAME_RE.match(entry.name)
            if match and entry.is_file():
                try:
                    file_date = date.fromisoformat(match.group(1))
                except ValueError:
                    # Pool-shaped name with an impossible calendar date (e.g.
                    # 2026-99-99.json): skip it like any other non-pool file
                    # instead of taking every consumer of the directory down.
                    logger.warning(
                        "%s: ignoring file — name matches the pool pattern but is "
                        "not a real calendar date",
                        entry,
                    )
                    continue
                if file_date <= on_date:
                    candidates.append((file_date, entry))
    if not candidates:
        return PoolReadResult(None, None, None, None, "absent")

    file_date, path = max(candidates)
    gap_days = (on_date - file_date).days
    if gap_days == 0:
        staleness: Staleness = "fresh"
    elif gap_days <= pool_max_staleness_days:
        staleness = "warn"
    else:
        staleness = "absent"

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        pool = PoolFile.parse_lenient(payload)
    except json.JSONDecodeError as exc:
        raise ContractError([f"{path.name}: not valid JSON: {exc}"]) from exc
    except ValidationError as exc:
        raise ContractError(validation_error_messages(exc, prefix=f"{path.name}: ")) from exc
    return PoolReadResult(pool, path, file_date, gap_days, staleness)
