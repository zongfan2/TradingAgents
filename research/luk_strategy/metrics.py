"""Performance metrics, reports and ablation tables (SPEC.md §9, §10).

Conventions (also written into every summary under ``notes``):

- Strategy wealth = equity / cfg.initial_equity, with an implicit 1.0 base point just
  before the first equity row, so day-1 P&L and a day-1 drawdown are counted.
- Benchmarks = buy & hold of ``panel.close[SPY|QQQ]``, bought at the close of the panel
  session immediately before the first equity row when the panel has it (else at the
  first equity row's close), marked on exactly the equity-curve dates.
- CAGR is calendar-day based: years = (last date - base date).days / 365.25, where the
  base date is that prior session (else the first equity date).
- ``max_dd`` is a negative fraction (-0.25 = -25%); MAR = CAGR / |max_dd|.
  ``max_dd_duration_sessions`` = longest run of consecutive sessions below a prior peak.
- Daily-return statistics: 252 sessions/year, rf = 0, sample std (ddof=1); Sortino uses
  downside deviation vs 0 over all sessions.
- Trade statistics use trades with a finite ``pnl``; win = pnl > 0, loss = pnl <= 0.
  ``return_pct`` is a fraction (0.05 = +5%). by_year trade counts use the entry year.

Everything here is a pure function of (result, panel, cfg); nothing reads market data
beyond ``panel.close`` (benchmarks; where each symbol's data ends, for the coverage
disclosure), so no look-ahead can enter at this layer.
"""

from __future__ import annotations

import json
import math
from datetime import date, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from .config import BENCHMARKS, SETUP_PRIORITY, Config
from .models import BacktestResult, DailyPanel

TRADING_DAYS = 252
_EPS = 1e-12
# Coverage disclosure (SPEC §10): a symbol "ends early" when its last bar is more than this
# many sessions before the panel end (delisted, acquired or renamed away).
EARLY_END_SESSIONS = 10
# A test-period year with fewer early endings than this fraction of the busiest year is
# reported as effectively survivor-only (its delistings are missing from the universe).
SURVIVOR_ONLY_FRAC = 0.10
# Engine skips decided before any trigger test (not part of the no_data denominator).
_PRE_TRIGGER_SKIPS = frozenset({"setup_disabled", "regime"})

TRADE_COLUMNS = [
    "trade_id",
    "symbol",
    "setup",
    "entry_date",
    "entry_ts",
    "entry_fill",
    "initial_stop",
    "qty",
    "risk_dollars",
    "exit_date",
    "exit_price_avg",
    "exit_reason",
    "pnl",
    "r_multiple",
    "return_pct",
    "hold_sessions",
    "legs",
]
_TRADE_NUMERIC = (
    "entry_fill",
    "initial_stop",
    "qty",
    "risk_dollars",
    "exit_price_avg",
    "pnl",
    "r_multiple",
    "return_pct",
    "hold_sessions",
)
EQUITY_COLUMNS = ["equity", "cash", "gross", "n_positions"]
SKIP_COLUMNS = ["day", "symbol", "setup", "reason"]
ABLATION_COLUMNS = [
    "variant",
    "total_return",
    "cagr",
    "max_dd",
    "sharpe",
    "mar",
    "trades",
    "win_rate",
    "expectancy_r",
    "profit_factor",
    "avg_exposure",
    "top10_share",
]

# SPEC.md §10 -- must appear in every report.
LIMITATIONS = [
    "Survivorship: the symbol list is today's Alpaca asset list filtered by current exchange, "
    "so many delisted names are missing entirely (e.g. SIVB, FRC, BBBY, TWTR); pre-delisting "
    "history is uneven by year, so years with almost no early-ending names are effectively "
    "survivor-only (see Data coverage), and delisted/renamed tickers more often lack 5-min "
    "bars (intraday no_data).",
    "No news/catalyst or pre-market data for episodic pivots (EPs).",
    "No sector/theme judgment.",
    "5-min bar granularity and unknown intra-bar ordering.",
    "SIP split-adjusted prices without dividends (strategy and benchmarks alike); Alpaca's "
    "adjustment also folds in some spin-offs and has a few misdated factors (fake moves in "
    "adjusted prices). EP gaps are re-checked on raw prices; other uses are not corrected.",
    "No borrow / margin / taxes modeled.",
    "Second-hand description of a discretionary trader; rules are an approximation.",
    "Parameters set a priori (not fitted); by-year results are the only robustness view in v1.",
]

NOTES = [
    "Strategy wealth = equity / initial_equity with an implicit 1.0 base before the first "
    "session; day-1 P&L and drawdown count.",
    "Benchmarks are price-only buy & hold from the close of the session before the first "
    "equity row (else the first row's close), marked on the equity dates.",
    "CAGR is calendar-day based (days / 365.25); max_dd is negative; MAR = CAGR / |max_dd|.",
    "Sharpe/Sortino/vol use daily returns, 252 sessions/year, rf = 0.",
    "Trade stats: win = pnl > 0, loss = pnl <= 0; return_pct is a fraction; by_year trades "
    "are counted by entry year.",
    "top10_share = positive P&L of the top 10% trades (by pnl, ceil, >= 1 trade) / total "
    "positive P&L; top10_share_of_net = their pnl / net pnl (> 1 means the rest lose money).",
    "The universe price floor (close >= min_price) uses unadjusted as-traded closes when the "
    "daily_raw cache exists (else split-adjusted, which leaks future splits); dollar-volume and "
    "ADR filters are split-invariant; all trading prices are split-adjusted.",
    "Intraday entries are skipped as price_mismatch when the first 5-min open disagrees with "
    "the daily open by > 15% (intraday and daily caches with different split adjustments).",
    "Intraday initial stop = low of day known at the fill (bars before the trigger bar and "
    "its open) - 1 tick; a trigger-bar low at or below it is booked as an entry-day stop "
    "(it may follow the fill). Ticks are one as-traded cent in split-adjusted units.",
    "entry_day_stops counts trades stopped out on their entry session, per setup.",
]

_CURVE_FLOAT_KEYS = ("total_return", "cagr", "max_dd", "ann_vol", "sharpe", "sortino", "mar")


# --------------------------------------------------------------------------- JSON safety
def to_jsonable(obj: Any) -> Any:
    """Recursively convert numpy/pandas/datetime objects to JSON-safe builtins.

    Non-finite floats (NaN, +/-inf), NaT and NA become ``None``.
    """
    if obj is None or isinstance(obj, str):
        return obj
    if obj is pd.NaT or obj is pd.NA:
        return None
    if isinstance(obj, bool | np.bool_):
        return bool(obj)
    if isinstance(obj, int | np.integer):
        return int(obj)
    if isinstance(obj, float | np.floating):
        f = float(obj)
        return f if math.isfinite(f) else None
    if isinstance(obj, dict):
        return {_json_key(k): to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, pd.Series):
        return [to_jsonable(v) for v in obj.tolist()]
    if isinstance(obj, list | tuple | set | frozenset | np.ndarray):
        return [to_jsonable(v) for v in (obj.tolist() if isinstance(obj, np.ndarray) else obj)]
    if isinstance(obj, np.datetime64):
        return to_jsonable(pd.Timestamp(obj))
    if isinstance(obj, pd.Timestamp):
        if obj.tzinfo is None and obj == obj.normalize():
            return obj.date().isoformat()
        return obj.isoformat()
    if isinstance(obj, datetime | date):
        return obj.isoformat()
    if isinstance(obj, Path):
        return str(obj)
    try:
        if pd.isna(obj):
            return None
    except (TypeError, ValueError):
        pass
    return str(obj)


def _json_key(k: Any) -> str:
    if isinstance(k, str):
        return k
    v = to_jsonable(k)
    return "null" if v is None else str(v)


# --------------------------------------------------------------------------- input normalization
def _norm_index(idx: Any) -> pd.DatetimeIndex:
    di = pd.DatetimeIndex(pd.to_datetime(idx))
    if di.tz is not None:
        di = di.tz_localize(None)
    return di.normalize()


def _prep_equity(equity: pd.DataFrame | None) -> pd.DataFrame:
    """Equity frame with a sorted, unique, tz-naive, normalized DatetimeIndex named 'date'."""
    if equity is None or len(equity) == 0:
        return pd.DataFrame(
            {c: pd.Series(dtype=float) for c in EQUITY_COLUMNS},
            index=pd.DatetimeIndex([], name="date"),
        )
    eq = equity.copy()
    if "date" in eq.columns and not isinstance(eq.index, pd.DatetimeIndex):
        eq = eq.set_index("date")  # e.g. reloaded from equity.csv without index_col
    eq.index = _norm_index(eq.index)
    eq.index.name = "date"
    eq = eq[~eq.index.duplicated(keep="last")].sort_index()
    for c in EQUITY_COLUMNS:
        if c in eq.columns:
            eq[c] = pd.to_numeric(eq[c], errors="coerce").astype(float)
    return eq


def _prep_trades(trades: pd.DataFrame | None) -> pd.DataFrame:
    if trades is None or len(trades) == 0:
        return pd.DataFrame({c: pd.Series(dtype=object) for c in TRADE_COLUMNS})
    t = trades.copy().reset_index(drop=True)
    for c in _TRADE_NUMERIC:
        if c in t.columns:
            t[c] = pd.to_numeric(t[c], errors="coerce").astype(float)
    if "return_pct" not in t.columns and {"pnl", "qty", "entry_fill"} <= set(t.columns):
        with np.errstate(divide="ignore", invalid="ignore"):
            t["return_pct"] = t["pnl"] / (t["qty"] * t["entry_fill"])
    for c in ("entry_date", "exit_date"):
        if c in t.columns:
            t[c] = pd.to_datetime(t[c], errors="coerce")
    return t


def _closed(t: pd.DataFrame) -> pd.DataFrame:
    if "pnl" not in t.columns or t.empty:
        return t.iloc[0:0]
    pnl = pd.to_numeric(t["pnl"], errors="coerce")
    return t[np.isfinite(pnl.to_numpy(dtype=float))]


def _col(df: pd.DataFrame, name: str) -> pd.Series:
    """Float column, or all-NaN if missing."""
    if name in df.columns:
        return pd.to_numeric(df[name], errors="coerce").astype(float)
    return pd.Series(np.nan, index=df.index, dtype=float)


def _f(x: Any) -> float:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return math.nan
    return v


# --------------------------------------------------------------------------- curve metrics
def _cagr(total: float, years: float) -> float:
    if not (math.isfinite(total) and math.isfinite(years)) or years <= 0:
        return math.nan
    growth = 1.0 + total
    if growth <= 0:
        return -1.0
    try:
        return math.exp(math.log(growth) / years) - 1.0
    except OverflowError:
        return math.nan


def _years(start: pd.Timestamp, end: pd.Timestamp) -> float:
    return (end - start).days / 365.25


def curve_metrics(wealth: pd.Series, base_date: pd.Timestamp | None) -> dict[str, Any]:
    """Metrics for a normalized wealth curve (value at each session close) whose implicit
    starting value 1.0 sits at ``base_date`` (or at the first row when None)."""
    out: dict[str, Any] = dict.fromkeys(_CURVE_FLOAT_KEYS, math.nan)
    out.update(
        max_dd_duration_sessions=None,
        max_dd_peak=None,
        max_dd_trough=None,
        max_dd_recovery=None,
        years=math.nan,
        n_sessions=len(wealth),
    )
    if len(wealth) == 0:
        return out
    w = pd.to_numeric(wealth, errors="coerce").astype(float).ffill().fillna(1.0)
    dates = pd.DatetimeIndex(w.index)
    start = base_date if base_date is not None else dates[0]
    vals = w.to_numpy(dtype=float)
    path = np.concatenate([[1.0], vals])  # path[i] for i >= 1 is the close of dates[i-1]

    def date_at(i: int) -> pd.Timestamp:
        return start if i == 0 else dates[i - 1]

    total = float(vals[-1] - 1.0)
    years = _years(start, dates[-1])
    out.update(total_return=total, years=years, cagr=_cagr(total, years))

    # Drawdown on the path including the base point.
    peak = np.maximum.accumulate(path)
    dd = path / peak - 1.0  # peak >= 1 > 0 always
    trough_i = int(np.argmin(dd))
    max_dd = float(dd[trough_i])
    underwater = dd[1:] < -_EPS
    longest = run = 0
    for flag in underwater:
        run = run + 1 if flag else 0
        longest = max(longest, run)
    out["max_dd_duration_sessions"] = int(longest)
    if max_dd < -_EPS:
        peak_val = peak[trough_i]
        # Drawdown starts at the last session at the running peak before the trough.
        peak_i = int(np.nonzero(path[: trough_i + 1] >= peak_val * (1 - _EPS))[0][-1])
        rec = np.nonzero(path[trough_i + 1 :] >= peak_val * (1 - _EPS))[0]
        out.update(
            max_dd=max_dd,
            max_dd_peak=date_at(peak_i),
            max_dd_trough=date_at(trough_i),
            max_dd_recovery=date_at(trough_i + 1 + int(rec[0])) if len(rec) else None,
        )
    else:
        out["max_dd"] = 0.0

    with np.errstate(divide="ignore", invalid="ignore"):
        rets = path[1:] / path[:-1] - 1.0
    rets = rets[np.isfinite(rets)]
    if len(rets) >= 2:
        mean = float(rets.mean())
        std = float(rets.std(ddof=1))
        out["ann_vol"] = std * math.sqrt(TRADING_DAYS)
        if std > _EPS:
            out["sharpe"] = mean / std * math.sqrt(TRADING_DAYS)
        downside = float(np.sqrt(np.mean(np.minimum(rets, 0.0) ** 2)))
        if downside > _EPS:
            out["sortino"] = mean / downside * math.sqrt(TRADING_DAYS)
    cagr = out["cagr"]
    if out["max_dd"] < -_EPS and math.isfinite(cagr):
        out["mar"] = cagr / abs(out["max_dd"])
    return out


def _prior_session(panel_dates: pd.DatetimeIndex, first: pd.Timestamp) -> pd.Timestamp | None:
    prior = panel_dates[panel_dates < first]
    return prior[-1] if len(prior) else None


def benchmark_wealth(
    panel: DailyPanel, symbol: str, idx: pd.DatetimeIndex, base_date: pd.Timestamp | None
) -> pd.Series | None:
    """Buy & hold wealth of ``symbol`` on ``idx`` (1.0 at base_date's close)."""
    if len(idx) == 0 or symbol not in panel.close.columns:
        return None
    s = pd.to_numeric(panel.close[symbol], errors="coerce").astype(float)
    s.index = _norm_index(s.index)
    s = s[~s.index.duplicated(keep="last")].sort_index().dropna()
    s = s[s > 0]
    if s.empty:
        return None
    aligned = s.reindex(s.index.union(idx)).ffill().reindex(idx)
    base = math.nan
    if base_date is not None:
        prior = s[s.index <= base_date]
        if len(prior):
            base = float(prior.iloc[-1])
    if not math.isfinite(base):
        valid = aligned.dropna()
        if valid.empty:
            return None
        base = float(valid.iloc[0])
    # Leading sessions before the benchmark has any data are treated as flat (cash).
    return aligned.fillna(base) / base


# --------------------------------------------------------------------------- trade metrics
def _trade_row(t: pd.DataFrame, i: Any) -> dict[str, Any]:
    r = t.loc[i]
    return {
        "symbol": r.get("symbol"),
        "setup": r.get("setup"),
        "entry_date": r.get("entry_date"),
        "exit_date": r.get("exit_date"),
        "exit_reason": r.get("exit_reason"),
        "return_pct": _f(r.get("return_pct")),
        "r_multiple": _f(r.get("r_multiple")),
        "pnl": _f(r.get("pnl")),
    }


def trade_stats(trades: pd.DataFrame | None, years: float) -> dict[str, Any]:
    t = _prep_trades(trades)
    closed = _closed(t)
    n = len(closed)
    out: dict[str, Any] = {
        "trades": n,
        "n_open": len(t) - n,
        "n_wins": 0,
        "n_losses": 0,
        "total_pnl": 0.0,
        "win_rate": math.nan,
        "avg_win_pct": math.nan,
        "avg_loss_pct": math.nan,
        "avg_win_r": math.nan,
        "avg_loss_r": math.nan,
        "expectancy_r": math.nan,
        "profit_factor": math.nan,
        "trades_per_year": math.nan,
        "avg_hold_sessions": math.nan,
        "best_trade": None,
        "worst_trade": None,
        "best_worst_by": None,
        "top10_share": math.nan,
        "top10_share_of_net": math.nan,
    }
    if n == 0:
        if math.isfinite(years) and years > 0:
            out["trades_per_year"] = 0.0
        return out
    pnl = _col(closed, "pnl")
    r = _col(closed, "r_multiple")
    ret = _col(closed, "return_pct")
    win = pnl > 0
    loss = ~win
    gross_win = float(pnl[pnl > 0].sum())
    gross_loss = float(-pnl[pnl < 0].sum())
    net = float(pnl.sum())
    out.update(
        n_wins=int(win.sum()),
        n_losses=int(loss.sum()),
        total_pnl=net,
        win_rate=float(win.mean()),
        avg_win_pct=float(ret[win].mean()),
        avg_loss_pct=float(ret[loss].mean()),
        avg_win_r=float(r[win].mean()),
        avg_loss_r=float(r[loss].mean()),
        expectancy_r=float(r.mean()),
        profit_factor=gross_win / gross_loss if gross_loss > 0 else math.nan,
        avg_hold_sessions=float(_col(closed, "hold_sessions").mean()),
    )
    if math.isfinite(years) and years > 0:
        out["trades_per_year"] = n / years

    for key_name, key in (("r_multiple", r), ("return_pct", ret), ("pnl", pnl)):
        if key.notna().any():
            out["best_trade"] = _trade_row(closed, key.idxmax())
            out["worst_trade"] = _trade_row(closed, key.idxmin())
            out["best_worst_by"] = key_name
            break

    # Tail dependence: share of positive P&L from the top 10% of trades by pnl.
    k = max(1, (n + 9) // 10)
    top = pnl.sort_values(ascending=False, kind="mergesort").iloc[:k]
    if gross_win > 0:
        out["top10_share"] = float(top.clip(lower=0).sum()) / gross_win
    if net > 0:
        out["top10_share_of_net"] = float(top.sum()) / net
    return out


def entry_day_stops(closed: pd.DataFrame) -> dict[str, dict[str, Any]]:
    """Per setup: trades stopped out on their entry session (hold_sessions == 0 and
    exit_reason == "stop"): count, share of that setup's trades, P&L, mean R."""
    need = {"setup", "exit_reason", "hold_sessions"}
    if closed.empty or not need <= set(closed.columns):
        return {}
    hit = (_col(closed, "hold_sessions") == 0) & (closed["exit_reason"].astype(str) == "stop")
    keys = closed["setup"].astype(object).where(closed["setup"].notna(), "unknown").astype(str)
    out: dict[str, dict[str, Any]] = {}
    for key in sorted(set(keys), key=lambda k: (SETUP_PRIORITY.get(k, 99), k)):
        g = closed[keys == key]
        e = g[hit[keys == key]]
        out[key] = {
            "count": len(e),
            "share": len(e) / len(g) if len(g) else math.nan,
            "total_pnl": float(_col(e, "pnl").sum()),
            "expectancy_r": float(_col(e, "r_multiple").mean()) if len(e) else math.nan,
        }
    return out


def data_end_profile(panel: DailyPanel) -> tuple[set[str], dict[str, int]]:
    """Tradable panel symbols whose data ends more than EARLY_END_SESSIONS sessions before
    the panel end, and their count by year of the last bar (coverage disclosure only)."""
    syms = [s for s in panel.symbols if s not in BENCHMARKS]
    if not syms or len(panel.dates) == 0:
        return set(), {}
    valid = panel.close[syms].notna().to_numpy()
    n = valid.shape[0]
    last = n - 1 - np.argmax(valid[::-1], axis=0)
    early = valid.any(axis=0) & (last < n - 1 - EARLY_END_SESSIONS)
    years = _norm_index(panel.dates)[last[early]].year
    counts = pd.Series(np.asarray(years), dtype="int64").value_counts().sort_index()
    ended = {s for s, e in zip(syms, early, strict=True) if e}
    return ended, {str(int(y)): int(c) for y, c in counts.items()}


def survivor_only_years(by_year: dict[str, int], years: list[int]) -> list[int]:
    """Test-period years whose early-ending count is < SURVIVOR_ONLY_FRAC x the busiest year."""
    peak = max(by_year.values(), default=0)
    return [y for y in years if by_year.get(str(y), 0) < SURVIVOR_ONLY_FRAC * peak or peak == 0]


def no_data_by_status(
    trades: pd.DataFrame | None, skips: pd.DataFrame | None, ended: set[str]
) -> dict[str, dict[str, Any]]:
    """no_data rate of evaluated candidates (every trigger test ends in exactly one trade or
    one skip), split by whether the symbol's data ends early ("ended_early") or not ("live")."""
    syms: list[str] = []
    nd: list[bool] = []
    if skips is not None and len(skips) and {"symbol", "reason"} <= set(skips.columns):
        sk = skips[~skips["reason"].astype(str).isin(_PRE_TRIGGER_SKIPS)]
        syms += sk["symbol"].astype(str).tolist()
        nd += (sk["reason"].astype(str) == "no_data").tolist()
    if trades is not None and len(trades) and "symbol" in trades.columns:
        syms += trades["symbol"].astype(str).tolist()
        nd += [False] * len(trades)
    out: dict[str, dict[str, Any]] = {}
    for status in ("live", "ended_early"):
        flags = [f for s, f in zip(syms, nd, strict=True) if (s in ended) == (status != "live")]
        n, m = len(flags), int(sum(flags))
        out[status] = {"evaluated": n, "no_data": m, "no_data_rate": m / n if n else math.nan}
    return out


def group_stats(closed: pd.DataFrame, col: str, order: dict[str, int] | None = None) -> dict:
    """count / win_rate / expectancy_r / total_pnl per value of ``col``."""
    if closed.empty or col not in closed.columns:
        return {}
    keys = closed[col].astype(object).where(closed[col].notna(), "unknown").astype(str)
    rows: list[tuple[str, dict[str, Any]]] = []
    for key, g in closed.groupby(keys, sort=True):
        pnl = _col(g, "pnl")
        rows.append(
            (
                str(key),
                {
                    "count": len(g),
                    "win_rate": float((pnl > 0).mean()),
                    "expectancy_r": float(_col(g, "r_multiple").mean()),
                    "total_pnl": float(pnl.sum()),
                },
            )
        )
    if order is not None:
        rows.sort(key=lambda kv: (order.get(kv[0], len(order)), kv[0]))
    else:
        rows.sort(key=lambda kv: (-kv[1]["count"], kv[0]))
    return dict(rows)


def _by_year(
    strat_w: pd.Series, bench_w: dict[str, pd.Series | None], closed: pd.DataFrame
) -> dict[str, dict[str, Any]]:
    if strat_w.empty:
        return {}
    years_idx = pd.DatetimeIndex(strat_w.index).year
    entry_years = (
        pd.to_datetime(closed["entry_date"], errors="coerce").dt.year
        if "entry_date" in closed.columns and not closed.empty
        else pd.Series(dtype=float)
    )
    prev_s = 1.0
    prev_b = dict.fromkeys(bench_w, 1.0)
    out: dict[str, dict[str, Any]] = {}
    for y in sorted(set(years_idx)):
        mask = np.asarray(years_idx == y)
        ws = strat_w.to_numpy(dtype=float)[mask]
        path = np.concatenate([[prev_s], ws])
        dd = path / np.maximum.accumulate(path) - 1.0 if prev_s > 0 else np.array([math.nan])
        row: dict[str, Any] = {
            "strategy": ws[-1] / prev_s - 1.0 if prev_s > 0 else math.nan,
            "max_dd": float(min(np.nanmin(dd), 0.0)) if np.isfinite(dd).any() else math.nan,
        }
        prev_s = float(ws[-1])
        for sym, bw in bench_w.items():
            if bw is None:
                row[sym] = math.nan
                continue
            bv = bw.to_numpy(dtype=float)[mask]
            row[sym] = bv[-1] / prev_b[sym] - 1.0
            prev_b[sym] = float(bv[-1])
        if len(entry_years):
            in_year = np.asarray(entry_years == y)
            pnl = _col(closed, "pnl")[in_year]
            row["trades"] = len(pnl)
            row["win_rate"] = float((pnl > 0).mean()) if len(pnl) else math.nan
        else:
            row["trades"] = 0
            row["win_rate"] = math.nan
        out[str(int(y))] = row
    return out


def _skip_counts(skips: pd.DataFrame | None) -> tuple[dict[str, int], dict[str, dict[str, int]]]:
    if skips is None or skips.empty or "reason" not in skips.columns:
        return {}, {}
    reasons = skips["reason"].astype(object).where(skips["reason"].notna(), "unknown").astype(str)
    vc = reasons.value_counts()
    by_reason = {
        str(k): int(v) for k, v in sorted(vc.items(), key=lambda kv: (-int(kv[1]), str(kv[0])))
    }
    by_setup: dict[str, dict[str, int]] = {}
    if "setup" in skips.columns:
        setups = skips["setup"].astype(object).where(skips["setup"].notna(), "unknown").astype(str)
        ct = pd.crosstab(setups, reasons)
        ordered = sorted(ct.index, key=lambda s: (SETUP_PRIORITY.get(s, 99), s))
        for s in ordered:
            row = ct.loc[s]
            row = row[row > 0].sort_values(ascending=False, kind="mergesort")
            by_setup[str(s)] = {str(k): int(v) for k, v in row.items()}
    return by_reason, by_setup


# --------------------------------------------------------------------------- summarize
def summarize(result: BacktestResult, panel: DailyPanel, cfg: Config) -> dict:
    """JSON-serializable summary of one backtest (SPEC §9). NaN/inf -> None."""
    eq = _prep_equity(result.equity)
    trades = _prep_trades(result.trades)
    closed = _closed(trades)
    idx = pd.DatetimeIndex(eq.index)
    panel_dates = _norm_index(panel.dates)
    base_date = _prior_session(panel_dates, idx[0]) if len(idx) else None
    init = float(cfg.initial_equity)

    if "equity" in eq.columns and len(eq):
        strat_w = (eq["equity"] / init).ffill().fillna(1.0)
    else:
        strat_w = pd.Series(dtype=float, index=idx)
    curve = curve_metrics(strat_w, base_date)
    years = curve["years"]
    tstats = trade_stats(trades, years)

    avg_exposure = avg_npos = math.nan
    if len(eq) and {"gross", "equity"} <= set(eq.columns):
        e = eq["equity"].where(eq["equity"] > 0)
        avg_exposure = float((eq["gross"] / e).mean())
    if len(eq) and "n_positions" in eq.columns:
        avg_npos = float(eq["n_positions"].mean())

    overall = {
        **curve,
        **tstats,
        "initial_equity": init,
        "final_equity": float(eq["equity"].iloc[-1]) if len(eq) and "equity" in eq else math.nan,
        "avg_exposure": avg_exposure,
        "avg_n_positions": avg_npos,
    }

    bench_w = {sym: benchmark_wealth(panel, sym, idx, base_date) for sym in BENCHMARKS}
    benchmarks = {
        sym: (curve_metrics(w, base_date) if w is not None else None) for sym, w in bench_w.items()
    }
    skips_by_reason, skips_by_setup = _skip_counts(result.skips)

    notes = list(NOTES)
    if cfg.entry_mode == "daily":
        notes.append(
            "Daily entry mode: BO/PB stops are capped below open_t (a pre-fill price); the "
            "entry-day stop is assumed hit when low_t <= stop although low_t may precede the "
            "fill (deliberately pessimistic, see entry_day_stops); no cutoffs or EP "
            "first-bar volume check."
        )
    if cfg.exit_mode == "contract":
        notes.append(
            f"Contract exits: {cfg.contract_target_r}R target, {cfg.contract_horizon}-session "
            "horizon, stop floored at "
            f"{cfg.contract_atr_floor}x ATR14; no partials or trail."
        )
    cfg_dict = to_jsonable(cfg.to_dict())
    if result.config and to_jsonable(result.config) != cfg_dict:
        notes.append("result.config differs from the cfg passed to summarize; cfg is reported.")

    tradable = [s for s in panel.symbols if s not in BENCHMARKS]
    ended, ended_by_year = data_end_profile(panel)
    period_years = sorted({int(y) for y in idx.year}) if len(idx) else []
    summary = {
        "period": {
            "start": idx[0] if len(idx) else None,
            "end": idx[-1] if len(idx) else None,
            "base_date": base_date,
            "n_sessions": len(idx),
            "years": years,
        },
        "overall": overall,
        "by_year": _by_year(strat_w, bench_w, closed),
        "by_setup": group_stats(closed, "setup", SETUP_PRIORITY),
        "by_exit_reason": group_stats(closed, "exit_reason"),
        "entry_day_stops": entry_day_stops(closed),
        "skips_by_reason": skips_by_reason,
        "skips_by_setup": skips_by_setup,
        "benchmarks": benchmarks,
        "coverage": {
            "panel_start": panel_dates[0] if len(panel_dates) else None,
            "panel_end": panel_dates[-1] if len(panel_dates) else None,
            "n_panel_symbols": len(tradable),
            "benchmarks_available": [s for s, w in bench_w.items() if w is not None],
            "early_end_sessions": EARLY_END_SESSIONS,
            "symbols_ending_early_by_year": ended_by_year,
            "survivor_only_years": survivor_only_years(ended_by_year, period_years),
            "no_data_by_data_status": no_data_by_status(result.trades, result.skips, ended),
        },
        "limitations": list(LIMITATIONS),
        "notes": notes,
        "config": cfg_dict,
    }
    return to_jsonable(summary)


# --------------------------------------------------------------------------- formatting
def _num(x: Any) -> float | None:
    if x is None or isinstance(x, bool):
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def fmt_pct(x: Any, digits: int = 2, signed: bool = False) -> str:
    v = _num(x)
    if v is None:
        return "n/a"
    return f"{v * 100:+.{digits}f}%" if signed else f"{v * 100:.{digits}f}%"


def fmt_num(x: Any, digits: int = 2, signed: bool = False) -> str:
    v = _num(x)
    if v is None:
        return "n/a"
    return f"{v:+.{digits}f}" if signed else f"{v:.{digits}f}"


def fmt_int(x: Any) -> str:
    v = _num(x)
    return "n/a" if v is None else f"{int(round(v)):,d}"


def fmt_money(x: Any) -> str:
    v = _num(x)
    if v is None:
        return "n/a"
    return f"-${abs(v):,.0f}" if v < 0 else f"${v:,.0f}"


def _cell(x: Any) -> str:
    s = "" if x is None else str(x)
    return s.replace("|", "\\|").replace("\n", " ")


def md_table(headers: list[str], rows: list[list[Any]]) -> str:
    """Plain GitHub-flavored markdown table; first column left-aligned, rest right."""
    lines = [
        "| " + " | ".join(_cell(h) for h in headers) + " |",
        "|" + "|".join([":---"] + ["---:"] * (len(headers) - 1)) + "|",
    ]
    lines += ["| " + " | ".join(_cell(c) for c in row) + " |" for row in rows]
    return "\n".join(lines)


def _trade_line(label: str, tr: dict | None) -> str:
    if not tr:
        return f"- {label}: n/a"
    return (
        f"- {label}: {tr.get('symbol')} ({tr.get('setup')}), entry {tr.get('entry_date') or 'n/a'}"
        f" -> exit {tr.get('exit_date') or 'n/a'} [{tr.get('exit_reason') or 'n/a'}]: "
        f"{fmt_pct(tr.get('return_pct'), 2, signed=True)}, "
        f"{fmt_num(tr.get('r_multiple'), 2, signed=True)}R, P&L {fmt_money(tr.get('pnl'))}"
    )


def render_markdown(summary: dict, name: str) -> str:
    s = to_jsonable(summary)
    o = s.get("overall") or {}
    per = s.get("period") or {}
    cfg = s.get("config") or {}
    bm = s.get("benchmarks") or {}
    out: list[str] = [f"# Luk 动量波段回测 / Luk momentum swing backtest — {name}", ""]

    setups = cfg.get("setups")
    setups_s = ",".join(setups) if isinstance(setups, list) else setups
    out += [
        f"- Period 区间: {per.get('start') or 'n/a'} → {per.get('end') or 'n/a'} "
        f"({fmt_int(per.get('n_sessions'))} sessions, {fmt_num(per.get('years'), 2)} years; "
        f"benchmark base close {per.get('base_date') or 'first session'})",
        f"- Mode 模式: entry={cfg.get('entry_mode')}, exit={cfg.get('exit_mode')}, "
        f"setups={setups_s}, regime={cfg.get('regime_mode')}, "
        f"risk/trade={fmt_pct(cfg.get('risk_per_trade'), 2)}, slip={cfg.get('slip_bps')} bps",
        f"- Equity 资金: {fmt_money(o.get('initial_equity'))} → {fmt_money(o.get('final_equity'))}",
        "",
    ]

    # Overview vs benchmarks.
    cols = [("Strategy 策略", o)] + [(sym, bm.get(sym) or {}) for sym in BENCHMARKS]
    metric_rows = [
        ("Total return 总收益", "total_return", lambda v: fmt_pct(v, 2, signed=True)),
        ("CAGR 年化收益", "cagr", lambda v: fmt_pct(v, 2, signed=True)),
        ("Max drawdown 最大回撤", "max_dd", lambda v: fmt_pct(v, 2)),
        ("Max DD duration 最长水下期 (sessions)", "max_dd_duration_sessions", fmt_int),
        ("Ann. volatility 年化波动", "ann_vol", lambda v: fmt_pct(v, 2)),
        ("Sharpe (rf=0)", "sharpe", lambda v: fmt_num(v, 2)),
        ("Sortino", "sortino", lambda v: fmt_num(v, 2)),
        ("MAR (CAGR/|MaxDD|)", "mar", lambda v: fmt_num(v, 2)),
    ]
    out += ["## 总览 / Overview", ""]
    out.append(
        md_table(
            ["Metric 指标"] + [c[0] for c in cols],
            [[label] + [f(d.get(key)) for _, d in cols] for label, key, f in metric_rows],
        )
    )
    dd_peak, dd_trough = o.get("max_dd_peak"), o.get("max_dd_trough")
    if dd_peak or dd_trough:
        out += [
            "",
            f"Strategy max drawdown: peak {dd_peak} → trough {dd_trough}, "
            f"recovered {o.get('max_dd_recovery') or 'not yet'}.",
        ]
    out.append("")

    # Trade statistics.
    trade_rows = [
        ("Trades 交易数", fmt_int(o.get("trades"))),
        ("Open at end 期末未平", fmt_int(o.get("n_open"))),
        ("Win rate 胜率", fmt_pct(o.get("win_rate"), 1)),
        ("Avg win % 平均盈利", fmt_pct(o.get("avg_win_pct"), 2, signed=True)),
        ("Avg loss % 平均亏损", fmt_pct(o.get("avg_loss_pct"), 2, signed=True)),
        ("Avg win R", fmt_num(o.get("avg_win_r"), 2, signed=True)),
        ("Avg loss R", fmt_num(o.get("avg_loss_r"), 2, signed=True)),
        ("Expectancy R 期望 (mean R)", fmt_num(o.get("expectancy_r"), 3, signed=True)),
        ("Profit factor 盈亏比", fmt_num(o.get("profit_factor"), 2)),
        ("Total P&L 总盈亏", fmt_money(o.get("total_pnl"))),
        ("Trades / year 年均交易", fmt_num(o.get("trades_per_year"), 1)),
        ("Avg hold (sessions) 平均持有", fmt_num(o.get("avg_hold_sessions"), 1)),
        ("Avg gross exposure 平均仓位", fmt_pct(o.get("avg_exposure"), 1)),
        ("Avg open positions 平均持仓数", fmt_num(o.get("avg_n_positions"), 1)),
        ("Top 10% trades share of positive P&L 头部依赖", fmt_pct(o.get("top10_share"), 1)),
        ("Top 10% trades share of net P&L", fmt_pct(o.get("top10_share_of_net"), 1)),
    ]
    out += ["## 交易统计 / Trade statistics", ""]
    out.append(md_table(["Metric 指标", "Value 数值"], [list(r) for r in trade_rows]))
    by = o.get("best_worst_by") or "r_multiple"
    out += [
        "",
        _trade_line(f"Best trade 最佳 (by {by})", o.get("best_trade")),
        _trade_line(f"Worst trade 最差 (by {by})", o.get("worst_trade")),
        "",
    ]

    # By year.
    by_year = s.get("by_year") or {}
    out += ["## 分年度 / By year", ""]
    if by_year:
        out.append(
            md_table(
                ["Year 年", "Strategy 策略", *BENCHMARKS, "MaxDD 回撤", "Trades 交易", "Win rate 胜率"],
                [
                    [
                        y,
                        fmt_pct(r.get("strategy"), 2, signed=True),
                        *[fmt_pct(r.get(sym), 2, signed=True) for sym in BENCHMARKS],
                        fmt_pct(r.get("max_dd"), 2),
                        fmt_int(r.get("trades")),
                        fmt_pct(r.get("win_rate"), 1),
                    ]
                    for y, r in by_year.items()
                ],
            )
        )
    else:
        out.append("_No equity data._")
    out.append("")

    # By setup / exit reason.
    for title, key in (
        ("## 按形态 / By setup", "by_setup"),
        ("## 按离场原因 / By exit reason", "by_exit_reason"),
    ):
        grp = s.get(key) or {}
        out += [title, ""]
        if grp:
            out.append(
                md_table(
                    ["Group", "Trades 交易", "Win rate 胜率", "Expectancy R", "Total P&L 盈亏"],
                    [
                        [
                            g,
                            fmt_int(r.get("count")),
                            fmt_pct(r.get("win_rate"), 1),
                            fmt_num(r.get("expectancy_r"), 3, signed=True),
                            fmt_money(r.get("total_pnl")),
                        ]
                        for g, r in grp.items()
                    ],
                )
            )
        else:
            out.append("_No closed trades._")
        out.append("")

    # Skips.
    skips = s.get("skips_by_reason") or {}
    out += ["## 跳过原因 / Skipped triggers by reason", ""]
    if skips:
        out.append(md_table(["Reason 原因", "Count 次数"], [[k, fmt_int(v)] for k, v in skips.items()]))
        sbs = s.get("skips_by_setup") or {}
        if sbs:
            parts = [
                f"{st}: " + ", ".join(f"{k}={v}" for k, v in rs.items()) for st, rs in sbs.items()
            ]
            out += ["", "By setup 按形态: " + "; ".join(parts)]
    else:
        out.append("_No skips recorded._")
    out.append("")

    eds = s.get("entry_day_stops") or {}
    out += ["## 入场当日止损 / Entry-day stops", ""]
    if eds:
        out.append(
            md_table(
                ["Setup 形态", "Count 次数", "Share of setup trades 占比", "Expectancy R",
                 "Total P&L 盈亏"],
                [
                    [
                        g,
                        fmt_int(r.get("count")),
                        fmt_pct(r.get("share"), 1),
                        fmt_num(r.get("expectancy_r"), 3, signed=True),
                        fmt_money(r.get("total_pnl")),
                    ]
                    for g, r in eds.items()
                ],
            )
        )
    else:
        out.append("_No closed trades._")
    out.append("")

    cov = s.get("coverage") or {}
    out += ["## 数据覆盖 / Data coverage", ""]
    eby = cov.get("symbols_ending_early_by_year") or {}
    out.append(
        f"Panel symbols whose data ends more than {cov.get('early_end_sessions', 'n/a')} "
        "sessions before the panel end, by year of the last bar (delistings that the "
        "universe still contains):"
    )
    out.append("")
    if eby:
        out.append(md_table(["Year 年", "Ended early 提前结束"], [[y, fmt_int(c)] for y, c in eby.items()]))
    else:
        out.append("_None._")
    sury = cov.get("survivor_only_years") or []
    out += [
        "",
        "Effectively survivor-only test years (fewer than "
        f"{SURVIVOR_ONLY_FRAC:.0%} of the busiest year's early endings): "
        + (", ".join(str(y) for y in sury) if sury else "none"),
        "",
    ]
    ndb = cov.get("no_data_by_data_status") or {}
    if ndb:
        out.append(
            md_table(
                ["Data status 数据状态", "Evaluated 评估", "no_data", "Rate 比例"],
                [
                    [k, fmt_int(r.get("evaluated")), fmt_int(r.get("no_data")),
                     fmt_pct(r.get("no_data_rate"), 2)]
                    for k, r in ndb.items()
                ],
            )
        )
        out.append("")

    out += ["## 说明 / Notes", ""]
    out += [f"- {n}" for n in (s.get("notes") or [])]
    out.append("")
    out += ["## 已知局限 / Known limitations (SPEC §10)", ""]
    out += [f"- {lim}" for lim in (s.get("limitations") or LIMITATIONS)]
    out.append("")
    out += ["## 配置 / Config snapshot", "", "```json"]
    out.append(json.dumps(cfg, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False))
    out += ["```", ""]
    return "\n".join(out)


# --------------------------------------------------------------------------- outputs
def _frame_or_empty(df: pd.DataFrame | None, columns: list[str]) -> pd.DataFrame:
    if df is None:
        return pd.DataFrame(columns=columns)
    if len(df.columns) == 0:
        return pd.DataFrame(columns=columns)
    return df


def write_results(result: BacktestResult, summary: dict, out_dir: Path) -> None:
    """Write trades.csv, equity.csv, skips.csv, summary.json and report.md into out_dir."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    _frame_or_empty(result.trades, TRADE_COLUMNS).to_csv(out_dir / "trades.csv", index=False)
    equity = result.equity if result.equity is not None else pd.DataFrame(columns=EQUITY_COLUMNS)
    equity.to_csv(out_dir / "equity.csv", index=True, index_label="date")
    _frame_or_empty(result.skips, SKIP_COLUMNS).to_csv(out_dir / "skips.csv", index=False)
    clean = to_jsonable(summary)
    (out_dir / "summary.json").write_text(
        json.dumps(clean, indent=2, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8"
    )
    (out_dir / "report.md").write_text(render_markdown(clean, out_dir.name), encoding="utf-8")


# --------------------------------------------------------------------------- ablations
def ablation_table(rows: list[tuple[str, dict]]) -> pd.DataFrame:
    """One row per variant from summaries (or bare ``overall`` dicts)."""
    recs = []
    for name, summ in rows:
        o = summ.get("overall", summ) if isinstance(summ, dict) else {}
        rec: dict[str, Any] = {"variant": name}
        for c in ABLATION_COLUMNS[1:]:
            rec[c] = o.get(c)
        recs.append(rec)
    df = pd.DataFrame(recs, columns=ABLATION_COLUMNS)
    for c in ABLATION_COLUMNS[1:]:
        df[c] = pd.to_numeric(df[c], errors="coerce").astype(float)
    df["variant"] = df["variant"].astype(object)
    return df


_ABLATION_FORMAT: dict[str, tuple[str, Any]] = {
    "variant": ("Variant 变体", str),
    "total_return": ("Total 总收益", lambda v: fmt_pct(v, 1, signed=True)),
    "cagr": ("CAGR", lambda v: fmt_pct(v, 2, signed=True)),
    "max_dd": ("MaxDD", lambda v: fmt_pct(v, 1)),
    "sharpe": ("Sharpe", lambda v: fmt_num(v, 2)),
    "mar": ("MAR", lambda v: fmt_num(v, 2)),
    "trades": ("Trades", fmt_int),
    "win_rate": ("Win rate", lambda v: fmt_pct(v, 1)),
    "expectancy_r": ("Exp. R", lambda v: fmt_num(v, 3, signed=True)),
    "profit_factor": ("PF", lambda v: fmt_num(v, 2)),
    "avg_exposure": ("Exposure", lambda v: fmt_pct(v, 1)),
    "top10_share": ("Top10% share", lambda v: fmt_pct(v, 1)),
}


def ablation_markdown(df: pd.DataFrame) -> str:
    cols = [c for c in ABLATION_COLUMNS if c in df.columns]
    headers = [_ABLATION_FORMAT[c][0] for c in cols]
    rows = [[_ABLATION_FORMAT[c][1](rec[c]) for c in cols] for rec in df.to_dict("records")]
    return md_table(headers, rows) + "\n"
