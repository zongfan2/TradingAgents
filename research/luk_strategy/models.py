"""Shared data contracts between modules (see SPEC.md)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

import pandas as pd


@dataclass
class DailyPanel:
    """Wide daily OHLCV. Index: tz-naive session dates (DatetimeIndex, sorted,
    unique). Columns: symbols (identical across fields). Missing bar = NaN."""

    open: pd.DataFrame
    high: pd.DataFrame
    low: pd.DataFrame
    close: pd.DataFrame
    volume: pd.DataFrame
    # Unadjusted (as-traded) closes aligned to `close`; backs the point-in-time
    # price floor. None when the raw cache is absent (falls back to adjusted).
    raw_close: pd.DataFrame | None = None
    # Unadjusted opens aligned to `open`; NaN where the raw cache has no row (never
    # back-filled). Used only to check the EP gap across a change of Alpaca's
    # adjustment factor (spin-offs booked as splits). None when the raw cache is absent.
    raw_open: pd.DataFrame | None = None

    @property
    def dates(self) -> pd.DatetimeIndex:
        return self.close.index  # type: ignore[return-value]

    @property
    def symbols(self) -> list[str]:
        return list(self.close.columns)


@dataclass
class Features:
    """Wide feature frames aligned to DailyPanel index/columns. Every value in
    row t uses data <= t only (as of the close of t). Regime series are indexed
    by date."""

    frames: dict[str, pd.DataFrame]
    risk_on_index: pd.Series  # bool
    leaders_count: pd.Series  # float
    risk_on_breadth: pd.Series  # bool

    def __getitem__(self, name: str) -> pd.DataFrame:
        return self.frames[name]


@dataclass
class Candidate:
    """A watchlist entry for session `day`, built from pre-open (<= t-1) data,
    plus open_t for EP. `level` is the setup's pre-open trigger level: BO pivot,
    PB prior-day high; None for EP (ORH is intraday). `tick` is one as-traded cent
    expressed in the split-adjusted price units of the bars (0.01 x close_{t-1} /
    raw_close_{t-1}), so trigger/stop offsets do not depend on later splits."""

    day: date
    symbol: str
    setup: str  # "EP" | "BO" | "PB"
    level: float | None
    prev_close: float
    prev_high: float
    prev_low: float
    open_today: float  # daily open of `day` (at-open info)
    adr: float  # ADR20 at t-1 (fraction)
    atr: float  # ATR14 at t-1 (price units)
    adv_shares: float  # ADV20 shares at t-1
    rank_key: float  # rs_score (BO/PB) or gap (EP); higher = better
    risk_on_index: bool
    risk_on_breadth: bool
    meta: dict = field(default_factory=dict)
    tick: float = 0.01


@dataclass
class Trigger:
    """An entry that fired. `ts` is the trigger bar start (tz-aware ET) in
    intraday mode, or the session open (09:30 ET) in daily mode."""

    candidate: Candidate
    ts: pd.Timestamp
    fill: float  # after slippage
    stop: float  # initial stop after floor
    bar_pos: int  # index of trigger bar within that day's bars (-1 for daily)

    @property
    def stop_pct(self) -> float:
        return (self.fill - self.stop) / self.fill


@dataclass
class SkipEvent:
    day: date
    symbol: str
    setup: str
    reason: str  # e.g. no_data, no_trigger, too_wide, chase, ep_volume, cap_*, regime


@dataclass
class BacktestResult:
    trades: pd.DataFrame
    equity: pd.DataFrame  # index date: equity, cash, gross, n_positions
    skips: pd.DataFrame
    config: dict
