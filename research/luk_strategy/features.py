"""Daily features "as of the close" of each row (SPEC.md §3, §4).

Causality contract (SPEC §2): every value at row t is a function of daily bars at rows <= t
only. The only operations used are backward-looking: ``rolling`` (window ends at the row),
``ewm(adjust=False)``, ``shift(n)`` with n > 0, ``cumsum``, and row-wise (cross-sectional)
``rank``. Nothing in this module moves data to "pre-open" time -- the single t-1 shift lives
in ``signals.build_watchlists``. The daily ``open`` field is never read here.

Storage: numeric frames are float32 to keep a ~2700 x 5000 panel affordable, except the two
price levels used as trigger/pivot prices (``high10``, ``high63``), which stay float64 so the
pivot equals an actual traded high exactly. Masks are bool.
"""

from __future__ import annotations

import logging

import numpy as np
import pandas as pd

from .config import BENCHMARKS, Config
from .models import DailyPanel, Features

log = logging.getLogger(__name__)

REGIME_INDEX = "QQQ"  # SPEC §4: risk_on_index is read from QQQ
RS_HORIZONS = (21, 63, 126)
BREADTH_NEAR_HIGH = 0.9  # leader for breadth: close >= 0.9 x high63 ...
BREADTH_MIN_RET63 = 0.30  # ... and ret63 >= 0.30
BREADTH_SMA = 20
# A run of more than this many consecutive missing sessions restarts hist_len: Alpaca keys
# history by today's ticker, so a reused ticker (e.g. FLY: Fly Leasing to 2021, Firefly IPO
# 2025) would otherwise count the previous issuer's bars toward min_history.
HIST_GAP_RESET_SESSIONS = 20

FEATURE_KEYS = (
    "sma10", "sma20", "sma50", "ema9", "ema10", "ema21", "adr20", "atr14", "adv20",
    "adv20_shares", "ret21", "ret63", "ret126", "high63", "high10", "range5",
    "bars_since_high10", "bars_since_high63", "hist_len", "universe", "ep_universe", "rs_score",
)  # fmt: skip


# --------------------------------------------------------------------------- primitives
def sma(df: pd.DataFrame, n: int) -> pd.DataFrame:
    """Trailing simple mean over rows t-n+1..t; NaN unless all n rows are present."""
    return df.rolling(n, min_periods=n).mean()


def ema(df: pd.DataFrame, n: int) -> pd.DataFrame:
    """Recursive EMA (adjust=False), defined after n observations."""
    return df.ewm(span=n, adjust=False, min_periods=n).mean()


def wilder_atr(
    high: pd.DataFrame, low: pd.DataFrame, close: pd.DataFrame, n: int = 14
) -> pd.DataFrame:
    """Wilder ATR: ewm(alpha=1/n) of true range; TR uses the prior row's close (shift(+1))."""
    prev_close = close.shift(1)
    hl = (high - low).to_numpy()
    hc = (high - prev_close).abs().to_numpy()
    lc = (low - prev_close).abs().to_numpy()
    # fmax ignores a NaN prev_close (first bar / after a data gap) -> TR = high - low.
    tr = np.fmax(hl, np.fmax(hc, lc))
    tr[np.isnan(hl)] = np.nan  # no bar today -> no TR
    tr_df = pd.DataFrame(tr, index=close.index, columns=close.columns)
    return tr_df.ewm(alpha=1.0 / n, adjust=False, min_periods=n).mean()


def bars_since_rolling_max(values: np.ndarray, rolling_max: np.ndarray, n: int) -> np.ndarray:
    """res[t] = smallest k in [0, n) with values[t-k] == rolling_max[t] (sessions since the
    window's max bar; 0 when row t itself is the max; ties -> the most recent occurrence).
    NaN where rolling_max is NaN. Reads rows t-n+1..t only."""
    T = values.shape[0]
    res = np.full(values.shape, np.nan, dtype=np.float32)
    # Iterate from the oldest lag to the newest so the smallest matching k wins.
    for k in range(min(n, T) - 1, -1, -1):
        eq = values[: T - k] == rolling_max[k:]  # values[t-k] vs max[t], aligned on t
        res[k:][eq] = k
    res[np.isnan(rolling_max)] = np.nan
    return res


def history_length(close: pd.DataFrame, gap: int = HIST_GAP_RESET_SESSIONS) -> pd.DataFrame:
    """Bars available up to and including row t since the last run of more than `gap`
    consecutive missing sessions (before the first bar the count is 0). Reads rows <= t."""
    valid = close.notna().to_numpy()
    out = np.zeros(valid.shape, dtype=np.float32)
    n_cols = valid.shape[1]
    count = np.zeros(n_cols)
    missing = np.zeros(n_cols)
    for t in range(valid.shape[0]):
        v = valid[t]
        count = np.where(v & (missing > gap), 0.0, count) + v
        missing = np.where(v, 0.0, missing + 1.0)
        out[t] = count
    return pd.DataFrame(out, index=close.index, columns=close.columns)


def _f32(df: pd.DataFrame) -> pd.DataFrame:
    return df.astype(np.float32)


def _frame(arr: np.ndarray, like: pd.DataFrame) -> pd.DataFrame:
    return pd.DataFrame(arr, index=like.index, columns=like.columns)


# --------------------------------------------------------------------------- main
def compute_features(panel: DailyPanel, cfg: Config) -> Features:
    close = panel.close.astype("float64")
    high = panel.high.astype("float64")
    low = panel.low.astype("float64")
    vol = panel.volume.astype("float64")
    fr: dict[str, pd.DataFrame] = {}

    for n in (10, 20, 50):
        fr[f"sma{n}"] = _f32(sma(close, n))
    for n in (9, 10, 21):
        fr[f"ema{n}"] = _f32(ema(close, n))

    adr20 = sma(high / low - 1.0, 20)
    adv20 = sma(close * vol, 20)
    fr["adr20"] = _f32(adr20)
    fr["atr14"] = _f32(wilder_atr(high, low, close, 14))
    fr["adv20"] = _f32(adv20)
    fr["adv20_shares"] = _f32(sma(vol, 20))

    rets = {n: close / close.shift(n) - 1.0 for n in RS_HORIZONS}
    for n, r in rets.items():
        fr[f"ret{n}"] = _f32(r)

    high_np = high.to_numpy()
    for n in (63, 10):
        hmax = high.rolling(n, min_periods=n).max()  # includes row t
        fr[f"high{n}"] = hmax  # float64: an exact traded high (used as a price level)
        fr[f"bars_since_high{n}"] = _frame(
            bars_since_rolling_max(high_np, hmax.to_numpy(), n), close
        )

    fr["range5"] = _f32(
        (high.rolling(5, min_periods=5).max() - low.rolling(5, min_periods=5).min()) / close
    )

    hist_len = history_length(close)  # bars since the last long gap, incl. row t
    fr["hist_len"] = hist_len

    # Point-in-time universes as of the close of row t (SPEC §3). Benchmarks never tradable.
    # Price floor on as-traded closes (split-adjusted history encodes future splits).
    px = panel.raw_close if panel.raw_close is not None else close
    base = px.ge(cfg.min_price) & hist_len.ge(cfg.min_history)
    universe = base & adv20.ge(cfg.min_adv) & adr20.ge(cfg.min_adr)
    ep_universe = base & adv20.ge(cfg.ep_min_adv)
    bench = close.columns.isin(BENCHMARKS)
    if bench.any():
        universe.loc[:, bench] = False
        ep_universe.loc[:, bench] = False
    fr["universe"] = universe.astype(bool)
    fr["ep_universe"] = ep_universe.astype(bool)

    # RS: per row, percentile rank (0-1] of each horizon's return among universe stocks;
    # rs_score = max of the three; NaN outside the universe.
    u = fr["universe"].to_numpy()
    best = np.full(close.shape, np.nan)
    for n in RS_HORIZONS:
        pr = rets[n].where(fr["universe"]).rank(axis=1, pct=True).to_numpy()
        best = np.fmax(best, pr)
    best[~u] = np.nan
    fr["rs_score"] = _frame(best.astype(np.float32), close)

    risk_on_index = _risk_on_index(close)

    leaders = (
        fr["universe"] & close.ge(BREADTH_NEAR_HIGH * fr["high63"]) & rets[63].ge(BREADTH_MIN_RET63)
    )
    leaders_count = leaders.sum(axis=1).astype("float64")
    breadth_sma = leaders_count.rolling(BREADTH_SMA, min_periods=BREADTH_SMA).mean()
    risk_on_breadth = (leaders_count >= breadth_sma).astype(bool)  # NaN SMA -> False

    missing = [k for k in FEATURE_KEYS if k not in fr]
    assert not missing, missing
    return Features(
        frames=fr,
        risk_on_index=risk_on_index,
        leaders_count=leaders_count,
        risk_on_breadth=risk_on_breadth,
    )


def _risk_on_index(close: pd.DataFrame) -> pd.Series:
    """QQQ close > SMA20 and SMA10 >= SMA20, as of the close of each row (float64 math)."""
    if REGIME_INDEX not in close.columns:
        log.warning("%s missing from panel: risk_on_index is False everywhere", REGIME_INDEX)
        return pd.Series(False, index=close.index, dtype=bool)
    q = close[REGIME_INDEX]
    s10 = q.rolling(10, min_periods=10).mean()
    s20 = q.rolling(20, min_periods=20).mean()
    return ((q > s20) & (s10 >= s20)).astype(bool)  # any NaN -> False


__all__ = [
    "FEATURE_KEYS",
    "HIST_GAP_RESET_SESSIONS",
    "bars_since_rolling_max",
    "compute_features",
    "ema",
    "history_length",
    "sma",
    "wilder_atr",
]
