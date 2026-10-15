"""Entry triggers and entry-day exits (SPEC.md §5, §8.1, contract target §8).

Pure functions, no I/O. All prices returned here are RAW except `Trigger.fill`,
which already includes entry slippage; the engine applies exit slippage.

Look-ahead discipline (SPEC §2), as enforced in this module:

* Pre-open inputs come only from the `Candidate` (built from data <= t-1 by
  `signals.build_watchlists`) plus `cand.open_today` (at-open info, used only
  for the PB gap test).
* Intraday mode: the opening-range bar is the 09:30 bar (fallback: earliest
  bar if it starts before 09:45). Evaluation starts at the bar after it. The
  trigger is the FIRST bar k (in time order) whose high reaches the level and
  whose start is before the cutoff; the fill reads only bar k's open. The stop
  reads only prices known to precede the fill: lows of bars 0..k-1 and bar k's
  open (its first trade). Bar k's low may print after the fill, so it never
  widens the stop or decides the width filter. Nothing after bar k's open
  influences an entry beyond the trigger test itself. The only read of bars
  after the cutoff is to label a non-entry as `after_cutoff` instead of
  `no_trigger` (diagnostic only).
* Prices are split-adjusted, so the "+/- 0.01" offsets use `Candidate.tick`
  (one as-traded cent in adjusted units at t-1), never a fixed adjusted 0.01.
* Data guard: the first intraday bar's open is compared with the daily open_t
  (at-open info); a >15% disagreement is skipped as `price_mismatch`.
* Daily mode is the SPEC's stop-order approximation: it necessarily reads
  high_t/low_t of the entry day (allowed only in daily mode, SPEC §2).
* `entry_day_exit` in intraday mode checks the trigger bar's low against the
  stop (the low may follow the fill: certain when the fill is the bar's open,
  the conservative assumption otherwise), then scans the later bars.
* Daily mode never places a BO/PB stop above open_t (a price that traded
  before the buy-stop filled), so `low_t <= stop` is never a certainty created
  by pre-fill trading.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date
from functools import lru_cache
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from .config import Config
from .models import Candidate, SkipEvent, Trigger

ET = ZoneInfo("America/New_York")
TICK = 0.01  # one cent; fallback when a candidate carries no point-in-time tick
EPS = 1e-9  # float tolerance for width comparisons
PRICE_EPS = 1e-9  # relative tolerance so a print exactly at a level/stop counts as a touch


def touched_up(px, level):
    """px reached `level` from below (float-safe; works on arrays; NaN -> False)."""
    return px >= level * (1.0 - PRICE_EPS)


def touched_down(px, stop):
    """px reached `stop` from above (float-safe; works on arrays; NaN -> False)."""
    return px <= stop * (1.0 + PRICE_EPS)

SESSION_OPEN_MIN = 9 * 60 + 30
SESSION_CLOSE_MIN = 16 * 60
OR_FALLBACK_LIMIT_MIN = 9 * 60 + 45  # earliest bar must start before 09:45 to act as OR
# Data-integrity guard (not a strategy rule): the first intraday bar's open must agree with
# the daily open_t within this fraction. A larger gap means the intraday and daily caches
# carry different split adjustments (e.g. a split between the two fetches); trading on
# mixed price scales would book bogus P&L once daily bars take over the exits.
OPEN_MISMATCH_TOL = 0.15

# Skip reasons (exact strings shared with engine/metrics).
NO_DATA = "no_data"
NO_TRIGGER = "no_trigger"
TOO_WIDE = "too_wide"
CHASE = "chase"
EP_VOLUME = "ep_volume"
PB_GAP_ABOVE = "pb_gap_above"
AFTER_CUTOFF = "after_cutoff"
PRICE_MISMATCH = "price_mismatch"

SETUPS = ("EP", "BO", "PB")


# --------------------------------------------------------------------------- helpers
@lru_cache(maxsize=64)
def _hhmm_to_min(s: str) -> int:
    h, m = s.strip().split(":")
    return int(h) * 60 + int(m)


def _cutoff_min(setup: str, cfg: Config) -> int:
    raw = {"BO": cfg.bo_cutoff, "EP": cfg.ep_cutoff, "PB": cfg.pb_cutoff}[setup]
    return _hhmm_to_min(raw)


def _finite(*xs: object) -> bool:
    try:
        return all(x is not None and math.isfinite(float(x)) for x in xs)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return False


def _tick(cand: Candidate) -> float:
    """One as-traded cent in the candidate's split-adjusted price units (SPEC §5)."""
    t = getattr(cand, "tick", TICK)
    return float(t) if _finite(t) and float(t) > 0 else TICK  # type: ignore[arg-type]


def _pivot(cand: Candidate) -> float | None:
    lvl = cand.level if cand.level is not None else cand.meta.get("pivot")
    return float(lvl) if _finite(lvl) else None


def _scale_mismatch(first_open: float, open_today: object) -> bool:
    """True if the first intraday open disagrees with the daily open_t by more than
    OPEN_MISMATCH_TOL (at-open info only). Unknown open_t -> no verdict (False)."""
    if not _finite(open_today) or float(open_today) <= 0:  # type: ignore[arg-type]
        return False
    return abs(first_open / float(open_today) - 1.0) > OPEN_MISMATCH_TOL  # type: ignore[arg-type]


def _session_bounds(day: date) -> tuple[pd.Timestamp, pd.Timestamp]:
    d = pd.Timestamp(year=day.year, month=day.month, day=day.day)
    start = (d + pd.Timedelta(minutes=SESSION_OPEN_MIN)).tz_localize(ET)
    end = (d + pd.Timedelta(minutes=SESSION_CLOSE_MIN)).tz_localize(ET)
    return start, end


def _session_open_ts(day: date) -> pd.Timestamp:
    return _session_bounds(day)[0]


def _slip_in(raw: float, cfg: Config) -> float:
    return raw * (1.0 + cfg.slip_bps / 1e4)


def _chase_limit(pivot: float, cand: Candidate, cfg: Config) -> float:
    return pivot * (1.0 + cfg.bo_max_extension * float(cand.adr))


def _missing_fields(cand: Candidate, cfg: Config, daily: bool) -> bool:
    """True if a field the setup's rules need is missing/non-finite (-> no_data)."""
    s = cand.setup
    if s not in SETUPS:
        raise ValueError(f"unknown setup {s!r} for {cand.symbol} on {cand.day}")
    positive: list[object] = []
    if s == "BO":
        need: list[object] = [_pivot(cand), cand.adr, cand.prev_low if daily else 0.0]
    elif s == "PB":
        need = [cand.prev_high, cand.adr, cand.prev_low if daily else cand.open_today]
    else:  # EP
        positive = [cand.prev_close] if daily else [cand.adv_shares]
        need = [cand.adr, *positive] if daily else list(positive)
    if cfg.exit_mode == "contract":
        need.append(cand.atr)
    ok = _finite(*need) and all(float(x) > 0 for x in positive)  # type: ignore[arg-type]
    return not ok


def _finalize_stop(
    cand: Candidate, cfg: Config, fill: float, stop: float, width_tol: float = 0.0
) -> float | None:
    """min_stop_pct floor -> width filter -> contract floor (SPEC §5, §8).
    Returns the final stop, or None if the trade is too wide."""
    if (fill - stop) / fill < cfg.min_stop_pct:
        stop = fill * (1.0 - cfg.min_stop_pct)
    if cand.setup == "EP":
        max_w = cfg.ep_max_stop_pct
    else:
        max_w = min(cfg.max_stop_adr_mult * float(cand.adr), cfg.max_stop_pct)
    if (fill - stop) / fill > max_w + width_tol + EPS:
        return None
    if cfg.exit_mode == "contract":
        stop = min(stop, fill - cfg.contract_atr_floor * float(cand.atr))
    return stop


def contract_target(trig: Trigger, cfg: Config) -> float:
    """Fixed take-profit for exit_mode == "contract": fill + R_mult x (fill - stop)."""
    return trig.fill + cfg.contract_target_r * (trig.fill - trig.stop)


# --------------------------------------------------------------------------- bars
@dataclass(frozen=True)
class _DayBars:
    """One symbol's regular-session bars for one day, sorted by start time."""

    ts: pd.DatetimeIndex  # bar start, tz America/New_York
    minute: np.ndarray  # bar start, minutes since midnight ET
    open: np.ndarray
    high: np.ndarray
    low: np.ndarray
    volume: np.ndarray
    pos: np.ndarray  # positional index of each bar in the caller's DataFrame


def _prepare(bars: pd.DataFrame | None, day: date, symbol: str) -> _DayBars | None:
    """Restrict to `symbol`, the regular session of `day`, and finite OHLC;
    sort by bar start. Returns None if nothing usable remains."""
    if bars is None or len(bars) == 0:
        return None
    df = bars
    pos = np.arange(len(df))
    if "symbol" in df.columns:
        m = df["symbol"].to_numpy() == symbol
        if not m.all():
            df, pos = df[m], pos[m]
    ts = pd.DatetimeIndex(df["ts"])
    ts = ts.tz_localize(ET) if ts.tz is None else ts.tz_convert(ET)
    o = df["open"].to_numpy(dtype="float64")
    h = df["high"].to_numpy(dtype="float64")
    lo = df["low"].to_numpy(dtype="float64")
    v = (
        df["volume"].to_numpy(dtype="float64")
        if "volume" in df.columns
        else np.zeros(len(df), dtype="float64")
    )
    start, end = _session_bounds(day)
    keep = (
        np.asarray(ts >= start)
        & np.asarray(ts < end)
        & np.isfinite(o)
        & np.isfinite(h)
        & np.isfinite(lo)
    )
    if not keep.any():
        return None
    ts, o, h, lo, v, pos = ts[keep], o[keep], h[keep], lo[keep], v[keep], pos[keep]
    order = np.argsort(ts.asi8, kind="stable")
    ts, o, h, lo, v, pos = ts[order], o[order], h[order], lo[order], v[order], pos[order]
    minute = np.asarray(ts.hour * 60 + ts.minute, dtype=np.int64)
    v = np.where(np.isfinite(v), v, 0.0)
    return _DayBars(ts=ts, minute=minute, open=o, high=h, low=lo, volume=v, pos=pos)


# --------------------------------------------------------------------------- intraday entry
def find_trigger(cand: Candidate, bars: pd.DataFrame | None, cfg: Config) -> Trigger | SkipEvent:
    """Intraday-mode entry for one candidate on `cand.day` (SPEC §5)."""

    def skip(reason: str) -> SkipEvent:
        return SkipEvent(day=cand.day, symbol=cand.symbol, setup=cand.setup, reason=reason)

    setup = cand.setup
    if _missing_fields(cand, cfg, daily=False):
        return skip(NO_DATA)
    # PB gap test uses at-open info only (independent of intraday data).
    if setup == "PB" and not float(cand.open_today) < float(cand.prev_high):
        return skip(PB_GAP_ABOVE)

    b = _prepare(bars, cand.day, cand.symbol)
    if b is None:
        return skip(NO_DATA)
    # Opening-range bar: index 0 (the 09:30 bar, or the earliest bar if < 09:45).
    if b.minute[0] >= OR_FALLBACK_LIMIT_MIN:
        return skip(NO_DATA)
    if _scale_mismatch(float(b.open[0]), cand.open_today):
        return skip(PRICE_MISMATCH)
    orh = float(b.high[0])

    if setup == "EP" and b.volume[0] < cfg.ep_min_first_bar_vol_frac * float(cand.adv_shares):
        return skip(EP_VOLUME)

    tick = _tick(cand)
    if setup == "BO":
        pivot = _pivot(cand)
        assert pivot is not None  # guaranteed by _missing_fields
        level = max(pivot, orh) + tick
    elif setup == "EP":
        pivot = None
        level = orh + tick
    else:  # PB
        pivot = None
        level = float(cand.prev_high) + tick

    cutoff = _cutoff_min(setup, cfg)
    reached = touched_up(b.high, level)
    reached[0] = False  # the opening-range bar never triggers
    in_time = b.minute < cutoff  # compare bar START to cutoff
    hits = np.flatnonzero(reached & in_time)
    if hits.size == 0:
        # Diagnostic label only; no entry is made either way.
        return skip(AFTER_CUTOFF if bool((reached & ~in_time).any()) else NO_TRIGGER)

    k = int(hits[0])  # first qualifying bar in time order (k >= 1); nothing after k is used
    raw_fill = max(level, float(b.open[k]))
    if setup == "BO" and raw_fill > _chase_limit(pivot, cand, cfg):  # type: ignore[arg-type]
        return skip(CHASE)
    fill = _slip_in(raw_fill, cfg)
    # Low of day known at the fill: bars 09:30 .. k-1 plus bar k's open (its first trade,
    # at or before the fill). Bar k's low may come after the fill, so it is excluded here
    # (including it would let post-fill prices widen the stop and pass the width filter);
    # entry_day_exit checks bar k's low against this stop instead.
    lod = min(float(b.low[:k].min()), float(b.open[k]))
    stop = _finalize_stop(cand, cfg, fill, lod - tick)
    if stop is None:
        return skip(TOO_WIDE)
    return Trigger(candidate=cand, ts=b.ts[k], fill=fill, stop=stop, bar_pos=int(b.pos[k]))


# --------------------------------------------------------------------------- daily entry
def find_trigger_daily(cand: Candidate, day_bar: dict, cfg: Config) -> Trigger | SkipEvent:
    """Daily-mode entry (SPEC §5 "Daily entry mode"): a stop-buy approximated
    with day-t OHLC. No cutoffs, no EP volume check. The BO chase guard is
    applied (it only needs at-open info, SPEC §2).

    BO/PB stop: min(max(low_{t-1}, fill*(1-min(ADR, max_stop_pct))), open_t) - tick.
    The open_t cap keeps the stop below a price that certainly traded before the
    buy-stop at L > open_t filled; otherwise `low_t <= open_t <= stop` would book
    a sure entry-day stop from pre-fill trading. Width filter for BO/PB tolerates
    the extra tick, so without the tolerance every trade where the ADR band binds
    would be `too_wide`."""

    def skip(reason: str) -> SkipEvent:
        return SkipEvent(day=cand.day, symbol=cand.symbol, setup=cand.setup, reason=reason)

    setup = cand.setup
    if _missing_fields(cand, cfg, daily=True):
        return skip(NO_DATA)
    if not day_bar:
        return skip(NO_DATA)
    o, h = day_bar.get("open"), day_bar.get("high")
    if not _finite(o, h):
        return skip(NO_DATA)
    o, h = float(o), float(h)  # type: ignore[arg-type]
    adr = float(cand.adr)
    tick = _tick(cand)
    pivot: float | None = None

    if setup == "EP":
        if not (o / float(cand.prev_close) - 1.0 >= cfg.ep_min_gap):
            return skip(NO_TRIGGER)
        fill = _slip_in(o, cfg)
        stop0 = fill * (1.0 - min(adr, cfg.ep_max_stop_pct))
        width_tol = 0.0
    else:
        if setup == "PB":
            if not o < float(cand.prev_high):
                return skip(PB_GAP_ABOVE)
            level = float(cand.prev_high) + tick
        else:  # BO
            pivot = _pivot(cand)
            assert pivot is not None
            level = pivot + tick
        if not touched_up(h, level):
            return skip(NO_TRIGGER)
        raw_fill = max(level, o)
        if setup == "BO" and raw_fill > _chase_limit(pivot, cand, cfg):  # type: ignore[arg-type]
            return skip(CHASE)
        fill = _slip_in(raw_fill, cfg)
        band = max(float(cand.prev_low), fill * (1.0 - min(adr, cfg.max_stop_pct)))
        stop0 = min(band, o) - tick  # never above open_t, which traded before the fill
        width_tol = tick / fill

    stop = _finalize_stop(cand, cfg, fill, stop0, width_tol=width_tol)
    if stop is None:
        return skip(TOO_WIDE)
    return Trigger(candidate=cand, ts=_session_open_ts(cand.day), fill=fill, stop=stop, bar_pos=-1)


# --------------------------------------------------------------------------- entry-day exit
def entry_day_exit(
    trig: Trigger,
    bars: pd.DataFrame | None,
    day_bar: dict | None,
    cfg: Config,
) -> tuple[pd.Timestamp | None, float, str] | None:
    """Exit on the entry day, if any: (ts, RAW exit price, "stop" | "target").

    Intraday mode: the trigger bar first, stop only: its low <= stop -> stopped at
    the stop. The stop was built without that low, which may print after the
    fill (certain when the fill is the bar's open; otherwise the order inside the
    bar is unknown and the stop-out is the conservative assumption). Then the
    later bars; per bar, stop first (low <= stop -> min(stop, open), so a gap
    through the stop fills at the open), then, in contract mode, target (open >=
    target -> open; high >= target -> target). Stop and target in the same bar ->
    stop. The trigger bar's high is never used for the target (it may precede
    the fill). If intraday bars are unavailable, falls back to the daily rule.

    Daily mode: low_t <= stop -> exit at stop (sequence unknown; conservative),
    ts None. The contract target is not checked on the entry day."""
    cand = trig.candidate
    stop = float(trig.stop)
    if cfg.entry_mode != "daily" and trig.bar_pos >= 0:
        b = _prepare(bars, cand.day, cand.symbol)
        if b is not None:
            target = contract_target(trig, cfg) if cfg.exit_mode == "contract" else math.inf
            for j in np.flatnonzero(np.asarray(b.ts >= trig.ts)):
                o, h, lo = float(b.open[j]), float(b.high[j]), float(b.low[j])
                if touched_down(lo, stop):
                    return b.ts[j], min(stop, o), "stop"
                if b.ts[j] == trig.ts:  # trigger bar: stop check only
                    continue
                if touched_up(o, target):
                    return b.ts[j], o, "target"
                if touched_up(h, target):
                    return b.ts[j], target, "target"
            return None
    if not day_bar:
        return None
    low = day_bar.get("low")
    if not _finite(low):
        return None
    if touched_down(float(low), stop):  # type: ignore[arg-type]
        return None, stop, "stop"
    return None


__all__ = [
    "AFTER_CUTOFF",
    "PRICE_EPS",
    "touched_down",
    "touched_up",
    "CHASE",
    "EP_VOLUME",
    "NO_DATA",
    "NO_TRIGGER",
    "PB_GAP_ABOVE",
    "PRICE_MISMATCH",
    "TICK",
    "TOO_WIDE",
    "contract_target",
    "entry_day_exit",
    "find_trigger",
    "find_trigger_daily",
]
