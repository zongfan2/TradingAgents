"""Pre-open watchlists: Leader, BO / EP / PB conditions, ranking and caps (SPEC.md §5).

Time discipline (SPEC §2). Two layers:

1. ``leader_mask`` / ``bo_mask`` / ``pb_mask`` / ``ep_eligible_mask`` are "as of the close"
   frames: row j uses daily data at rows <= j only (features are already causal; any extra
   lookback here is a ``shift(n > 0)`` or a trailing ``rolling``). They never read ``open``.
2. ``build_watchlists`` is the ONLY place that maps "as of close" to "pre-open": for session
   row i it reads every frame at row ``p = i - 1``. The only day-t datum it reads is
   ``open[i]`` (at-open info, plus the unadjusted ``raw_open[i]``) -- for the EP gap and as
   the informational ``open_today``. Day-t high/low/close/volume are never read.

Regime is never applied here: the flags at t-1 are stored on each Candidate and the engine
decides. All three setups are always built (``cfg.setups`` is applied by the engine), and each
setup's cap is applied by its own rank before anything else.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date

import numpy as np
import pandas as pd

from .config import SETUP_PRIORITY, Config
from .models import Candidate, DailyPanel, Features

# --- fixed rule constants from SPEC §5 (a priori, not in Config) ---
BO_NEAR_HIGH63 = 0.85  # close >= 0.85 x high63
BO_MIN_BARS_SINCE_PIVOT = 2  # pivot (high10 bar) set at t-3 or earlier
BO_MAX_RANGE5_ADR = 2.0  # range5 <= 2.0 x ADR20
BO_MAX_BELOW_PIVOT_ADR = 1.5  # close >= pivot x (1 - 1.5 x ADR20)
PB_FRESH_HIGH_SESSIONS = 15  # high63 bar within the last 15 sessions (bars_since <= 14)
PB_EMA9_TOUCH = 1.01  # low <= EMA9 x 1.01
PB_EMA21_FLOOR = 0.97  # close >= EMA21 x 0.97
PB_EMA21_RISING_LAG = 5  # EMA21 > EMA21 five sessions earlier
PB_PRIOR_CLOSE_LOOKBACK = 5  # close < max(close over the 5 sessions before)
# Data guard (not a strategy rule): Alpaca's SPLIT adjustment also books some spin-offs as
# "splits", so an adjusted gap can be +13% while the stock really opened -25% (XRX
# 2017-01-03). When the raw/adjusted factor changes between close_{t-1} and open_t by more
# than this fraction, the EP gap must also pass on unadjusted prices.
EP_FACTOR_CHANGE_TOL = 0.01
CENT = 0.01  # one as-traded cent


def _np(df: pd.DataFrame) -> np.ndarray:
    return df.to_numpy()


def _like(arr: np.ndarray, like: pd.DataFrame) -> pd.DataFrame:
    return pd.DataFrame(arr, index=like.index, columns=like.columns)


# --------------------------------------------------------------------------- as-of-close masks
def leader_mask(feats: Features, cfg: Config) -> pd.DataFrame:
    """Leader as of the close of row j: in universe, rs_score >= leader_rs and
    max(ret21, ret63, ret126) >= leader_min_ret."""
    best_ret = np.fmax(np.fmax(_np(feats["ret21"]), _np(feats["ret63"])), _np(feats["ret126"]))
    m = (
        _np(feats["universe"])
        & (_np(feats["rs_score"]) >= cfg.leader_rs)
        & (best_ret >= cfg.leader_min_ret)
    )
    return _like(m, feats["universe"])


def bo_mask(panel: DailyPanel, feats: Features, cfg: Config) -> pd.DataFrame:
    """Consolidation-breakout setup as of the close of row j (pivot = high10 at j)."""
    c = _np(panel.close)
    s10, s20, s50 = _np(feats["sma10"]), _np(feats["sma20"]), _np(feats["sma50"])
    adr = _np(feats["adr20"])
    pivot = _np(feats["high10"])
    m = (
        _np(leader_mask(feats, cfg))
        & (c >= s20)
        & (s10 >= s20)
        & (s20 >= s50)
        & (c >= BO_NEAR_HIGH63 * _np(feats["high63"]))
        & (_np(feats["bars_since_high10"]) >= BO_MIN_BARS_SINCE_PIVOT)
        & (_np(feats["range5"]) <= BO_MAX_RANGE5_ADR * adr)
        & (c >= pivot * (1.0 - BO_MAX_BELOW_PIVOT_ADR * adr))
    )
    return _like(m, panel.close)


def pb_mask(panel: DailyPanel, feats: Features, cfg: Config) -> pd.DataFrame:
    """Pullback-to-rising-EMAs setup as of the close of row j."""
    c = _np(panel.close)
    ema21 = feats["ema21"]
    ema21_earlier = _np(ema21.shift(PB_EMA21_RISING_LAG))  # row j-5 (lookback, not pre-open)
    # max close over rows j-5..j-1 (the 5 sessions before j)
    prior_max = _np(
        panel.close.shift(1)
        .rolling(PB_PRIOR_CLOSE_LOOKBACK, min_periods=PB_PRIOR_CLOSE_LOOKBACK)
        .max()
    )
    s20, s50 = _np(feats["sma20"]), _np(feats["sma50"])
    m = (
        _np(leader_mask(feats, cfg))
        & (s20 > s50)
        & (c > s50)
        & (_np(ema21) > ema21_earlier)
        & (_np(feats["bars_since_high63"]) < PB_FRESH_HIGH_SESSIONS)
        & (c < prior_max)
        & (_np(panel.low) <= _np(feats["ema9"]) * PB_EMA9_TOUCH)
        & (c >= _np(ema21) * PB_EMA21_FLOOR)
        & (_np(panel.volume) < _np(feats["adv20_shares"]))
    )
    return _like(m, panel.close)


def ep_eligible_mask(feats: Features, cfg: Config) -> pd.DataFrame:
    """EP pre-gap eligibility as of the close of row j: EP universe (+ optional neglect)."""
    m = _np(feats["ep_universe"]).copy()
    if cfg.ep_require_neglect:
        m &= _np(feats["ret63"]) <= cfg.ep_neglect_max_ret63
    return _like(m, feats["ep_universe"])


# --------------------------------------------------------------------------- watchlists
def _opt(x: float) -> float | None:
    x = float(x)
    return None if math.isnan(x) else x


def _top(idx: np.ndarray, keys: np.ndarray, syms: np.ndarray, cap: int) -> np.ndarray:
    """Column indices sorted by key desc (ties: symbol asc), truncated to cap."""
    if idx.size == 0 or cap <= 0:
        return idx[:0]
    order = np.lexsort((syms[idx], -keys.astype("float64")))
    return idx[order[:cap]]


@dataclass(frozen=True)
class _DayCtx:
    day: date
    p: int  # row of t-1 (pre-open information)
    i: int  # row of t (only `open` is read from it)
    gap_row: np.ndarray  # open_t / close_{t-1} - 1 for every symbol
    roi: bool  # risk_on_index at t-1
    rob: bool  # risk_on_breadth at t-1


def _candidate(
    arr: dict[str, np.ndarray],
    ctx: _DayCtx,
    k: int,
    setup: str,
    level: float | None,
    rank_key: float,
) -> Candidate:
    p, i = ctx.p, ctx.i
    tick = float(arr["tick"][p, k])
    return Candidate(
        day=ctx.day,
        symbol=str(arr["symbol"][k]),
        setup=setup,
        level=level,
        prev_close=float(arr["close"][p, k]),
        prev_high=float(arr["high"][p, k]),
        prev_low=float(arr["low"][p, k]),
        open_today=float(arr["open"][i, k]),
        adr=float(arr["adr20"][p, k]),
        atr=float(arr["atr14"][p, k]),
        adv_shares=float(arr["adv20_shares"][p, k]),
        rank_key=float(rank_key),
        risk_on_index=ctx.roi,
        risk_on_breadth=ctx.rob,
        meta={
            "ret63": _opt(arr["ret63"][p, k]),
            "gap": _opt(ctx.gap_row[k]),
            "pivot": None if level is None else float(level),
            "rs_score": _opt(arr["rs_score"][p, k]),
        },
        tick=tick if math.isfinite(tick) and tick > 0 else CENT,
    )


def build_watchlists(
    panel: DailyPanel, feats: Features, cfg: Config
) -> dict[date, list[Candidate]]:
    """Candidates for every session in [cfg.start, cfg.end or last panel date].

    Each day's list holds EP (gap desc), then BO and PB (rs_score desc), each capped.
    """
    dates = panel.dates
    out: dict[date, list[Candidate]] = {}
    if len(dates) == 0:
        return out
    end = cfg.end or dates[-1].date()
    syms = np.asarray(panel.symbols, dtype=object)

    # As-of-close frames (row j uses data <= j).
    bo = _np(bo_mask(panel, feats, cfg))
    pb = _np(pb_mask(panel, feats, cfg))
    ep_ok = _np(ep_eligible_mask(feats, cfg))
    arr = {
        "symbol": syms,
        "close": _np(panel.close),
        "high": _np(panel.high),
        "low": _np(panel.low),
        "open": _np(panel.open),  # day-t open: the only same-day field read here
        "adr20": _np(feats["adr20"]),
        "atr14": _np(feats["atr14"]),
        "adv20_shares": _np(feats["adv20_shares"]),
        "rs_score": _np(feats["rs_score"]),
        "ret63": _np(feats["ret63"]),
    }
    close_np, open_np, rs = arr["close"], arr["open"], arr["rs_score"]
    # Point-in-time tick: one as-traded cent in split-adjusted units, from row t-1 (read at p).
    raw_close = None if panel.raw_close is None else _np(panel.raw_close.reindex_like(panel.close))
    with np.errstate(divide="ignore", invalid="ignore"):
        ratio = close_np / raw_close if raw_close is not None else np.ones_like(close_np)
    arr["tick"] = np.where(np.isfinite(ratio) & (ratio > 0), CENT * ratio, CENT)
    raw_open = None if panel.raw_open is None else _np(panel.raw_open.reindex_like(panel.open))
    pivot = _np(feats["high10"])
    roi = feats.risk_on_index.reindex(dates, fill_value=False).to_numpy(dtype=bool)
    rob = feats.risk_on_breadth.reindex(dates, fill_value=False).to_numpy(dtype=bool)

    for i, ts in enumerate(dates):
        day = ts.date()
        if day < cfg.start or day > end:
            continue
        if i == 0:  # no pre-open history
            out[day] = []
            continue
        p = i - 1  # <<< THE t-1 SHIFT: every pre-open read below uses row p >>>

        with np.errstate(divide="ignore", invalid="ignore"):
            gap_row = open_np[i] / close_np[p] - 1.0  # at-open info (open_t vs close_{t-1})
            gap_ok = gap_row >= cfg.ep_min_gap
            if raw_open is not None and raw_close is not None:
                # Factor check needs real raw rows on both days (raw_open is never back-filled,
                # so a finite raw_open[p] also vouches for raw_close[p]).
                f_open = raw_open[i] / open_np[i]
                f_close = raw_close[p] / close_np[p]
                both = np.isfinite(raw_open[i]) & np.isfinite(raw_open[p])
                moved = both & (np.abs(f_open / f_close - 1.0) > EP_FACTOR_CHANGE_TOL)
                raw_gap = raw_open[i] / raw_close[p] - 1.0
                gap_ok &= ~moved | (raw_gap >= cfg.ep_min_gap)
        ctx = _DayCtx(day=day, p=p, i=i, gap_row=gap_row, roi=bool(roi[p]), rob=bool(rob[p]))
        day_list: list[Candidate] = []

        # EP: eligible at t-1 and gap_t >= ep_min_gap; rank by gap desc.
        ep_idx = np.flatnonzero(ep_ok[p] & gap_ok)
        for k in _top(ep_idx, gap_row[ep_idx], syms, cfg.ep_max_candidates):
            day_list.append(_candidate(arr, ctx, k, "EP", None, gap_row[k]))

        # BO: setup at t-1; level = pivot (high10 at t-1); rank by rs_score desc.
        bo_idx = np.flatnonzero(bo[p])
        for k in _top(bo_idx, rs[p, bo_idx], syms, cfg.bo_max_candidates):
            day_list.append(_candidate(arr, ctx, k, "BO", float(pivot[p, k]), rs[p, k]))

        # PB: setup at t-1; level = high_{t-1}; rank by rs_score desc.
        pb_idx = np.flatnonzero(pb[p])
        for k in _top(pb_idx, rs[p, pb_idx], syms, cfg.pb_max_candidates):
            day_list.append(_candidate(arr, ctx, k, "PB", float(arr["high"][p, k]), rs[p, k]))

        out[day] = day_list
    return out


def intraday_manifest(watchlists: dict[date, list[Candidate]]) -> dict[date, list[str]]:
    """Unique candidate symbols per day (days with no candidates are omitted)."""
    return {
        day: sorted({c.symbol for c in cands}) for day, cands in sorted(watchlists.items()) if cands
    }


def watchlists_to_frame(watchlists: dict[date, list[Candidate]]) -> pd.DataFrame:
    """Flat table of all candidates (for inspection / CSV), sorted by day, setup priority, rank."""
    rows = []
    for day, cands in watchlists.items():
        for c in cands:
            rows.append(
                {
                    "day": day,
                    "symbol": c.symbol,
                    "setup": c.setup,
                    "level": c.level,
                    "rank_key": c.rank_key,
                    "prev_close": c.prev_close,
                    "prev_high": c.prev_high,
                    "prev_low": c.prev_low,
                    "open_today": c.open_today,
                    "adr": c.adr,
                    "atr": c.atr,
                    "adv_shares": c.adv_shares,
                    "tick": c.tick,
                    "risk_on_index": c.risk_on_index,
                    "risk_on_breadth": c.risk_on_breadth,
                    **{f"meta_{k}": v for k, v in c.meta.items()},
                }
            )
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df["_prio"] = df["setup"].map(SETUP_PRIORITY)
    df = df.sort_values(["day", "_prio", "rank_key"], ascending=[True, True, False], kind="stable")
    return df.drop(columns="_prio").reset_index(drop=True)


__all__ = [
    "bo_mask",
    "build_watchlists",
    "ep_eligible_mask",
    "intraday_manifest",
    "leader_mask",
    "pb_mask",
    "watchlists_to_frame",
]
