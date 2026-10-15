"""Portfolio simulation (SPEC.md §6 regime, §7 sizing & portfolio, §8 exits).

Day loop for every session t (row ``i`` of ``panel.dates``) in
[cfg.start, cfg.end or last panel date], in this exact order:

1. OPEN     Positions held from before t: ``open_t <= stop`` -> exit at open_t.
            Capital freed here IS available to step 2 (open_t is known before
            any entry can trigger).
2. ENTRIES  ``watchlists[t]`` -> setup filter -> regime filter -> triggers ->
            same-symbol de-dup -> sort by (ts, setup priority, -rank) ->
            already_held / cap_open / cap_new / sizing on E_{t-1}.
3. ENTRY-DAY EXITS for positions opened in step 2 (``intraday.entry_day_exit``).
            Proceeds are credited only after step 2 has finished, so they are
            never reused on day t (SPEC §7, conservative).
4. CLOSE    Positions held from before t that survived step 1: missing-bar
            bookkeeping / ``data_end`` (after ``data_gap_sessions`` consecutive
            missing bars; never from knowing where the data ends); then daily
            low vs stop; then partial+breakeven (once, at the first session with
            a bar from ``partial_day`` on), trail (from ``partial_day`` on unless
            ``trail_after_partial`` is False) and time stop (luk) or target/time
            (contract). Positions opened today only refresh their mark.
            On the last session every remaining position exits at the close
            (``end_of_test``).
5. MARK     equity_t = cash + sum(qty * close_t) (last valid close if NaN).

Look-ahead audit (SPEC §2):
- Step 2 reads only: Candidate fields (built pre-open by signals), intraday
  bars through ``find_trigger`` (causal by its contract), E_{t-1} (``e_prev``),
  prior-close marks (``pos.mark`` is not refreshed until step 4) and cash.
  In daily entry mode ``find_trigger_daily`` receives day-t OHLC, which is that
  ablation's documented stop-order approximation (SPEC §5).
- high_t / low_t / close_t and feature row t are read only in steps 3-5, i.e.
  for exits decided at/after the entry and for marks. In intraday mode step 3
  passes only the intraday bars (``day_bar=None``).
- ``data_end`` exits fire in step 4 (the session's missing bar is only known by
  the close), so their proceeds are not reused on day t either. They count
  consecutive missing sessions only; the symbol's last bar in the full panel is
  never consulted (that would be a future read).

All exit prices handled internally are RAW; ``_exit`` applies exit slippage
``raw * (1 - slip_bps/1e4)`` exactly once. ``Trigger.fill`` already includes
entry slippage.
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime

import numpy as np
import pandas as pd

from .config import SETUP_PRIORITY, Config
from .intraday import (
    contract_target,
    entry_day_exit,
    find_trigger,
    find_trigger_daily,
    touched_down,
    touched_up,
)
from .models import BacktestResult, Candidate, DailyPanel, Features, SkipEvent, Trigger

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
EQUITY_COLUMNS = ["equity", "cash", "gross", "n_positions"]
SKIP_COLUMNS = ["day", "symbol", "setup", "reason"]

ENTRY_MODES = ("intraday", "daily")
EXIT_MODES = ("luk", "contract")
REGIME_MODES = ("index", "breadth", "both", "none")

# Exit reasons written by the engine (entry-day "stop"/"target" come from intraday).
STOP, TARGET, PARTIAL, TRAIL, TIME = "stop", "target", "partial", "trail", "time"
DATA_END, END_OF_TEST = "data_end", "end_of_test"
# Skip reasons added by the engine (trigger skips come from intraday).
SKIP_SETUP_DISABLED = "setup_disabled"
SKIP_REGIME = "regime"
SKIP_NO_DATA = "no_data"
SKIP_DUP_SETUP = "dup_setup"
SKIP_ALREADY_HELD = "already_held"
SKIP_CAP_OPEN = "cap_open"
SKIP_CAP_NEW = "cap_new"
SKIP_QTY_ZERO = "qty_zero"
SKIP_CAP_EXPOSURE = "cap_exposure"

_EPS = 1e-9  # share-count float tolerance (e.g. 0.3/0.1 -> 3, not 2)

IntradayStoreFn = Callable[[date], "dict[str, pd.DataFrame] | None"]


# --------------------------------------------------------------------------- helpers
def _floor_shares(x: float) -> int:
    """floor(x) with a tiny tolerance; non-finite or negative -> 0."""
    if not math.isfinite(x) or x <= 0:
        return 0
    return math.floor(x + _EPS)


def _share_unit(cand: Candidate) -> float:
    """Split-adjusted shares per as-traded share at t-1, from the candidate's
    point-in-time tick (one as-traded cent in adjusted units). Whole-share rounding is
    done in as-traded shares so later splits cannot change historical sizing."""
    tick = getattr(cand, "tick", None)
    try:
        f = float(tick) / 0.01
    except (TypeError, ValueError):
        return 1.0
    return 1.0 / f if math.isfinite(f) and f > 0 else 1.0


def _flag(x: object) -> bool:
    """Regime flag -> bool; None/NaN/NA count as False (risk-off), never True."""
    try:
        if pd.isna(x):
            return False
    except (TypeError, ValueError):  # non-scalar: let bool() decide / raise
        pass
    return bool(x)


def _normalize_watchlists(
    watchlists: dict[date, list[Candidate]],
) -> dict[date, list[Candidate]]:
    """Key by plain datetime.date. pd.Timestamp/datetime keys would otherwise
    silently miss every lookup (Timestamp != date), i.e. a run with no trades."""
    out: dict[date, list[Candidate]] = {}
    for k, cands in watchlists.items():
        d = k.date() if isinstance(k, datetime) else k
        out.setdefault(d, []).extend(cands)
    return out


def _risk_on(cand: Candidate, mode: str) -> bool:
    """Regime for day t as stored on the candidate (flags read at t-1 by signals)."""
    if mode == "index":
        return _flag(cand.risk_on_index)
    if mode == "breadth":
        return _flag(cand.risk_on_breadth)
    if mode == "both":
        return _flag(cand.risk_on_index) and _flag(cand.risk_on_breadth)
    return True  # "none"


def _rank_desc(cand: Candidate) -> float:
    r = float(cand.rank_key) if cand.rank_key is not None else math.nan
    return -r if math.isfinite(r) else math.inf


def _priority(setup: str) -> int:
    return SETUP_PRIORITY.get(setup, len(SETUP_PRIORITY))


@dataclass
class _Position:
    trade_id: int
    symbol: str
    col: int
    setup: str
    entry_i: int  # panel row of the entry session (sessions held = i - entry_i)
    entry_ts: pd.Timestamp
    entry_fill: float  # incl. entry slippage
    initial_stop: float
    stop: float  # current stop (raised to breakeven after the partial)
    qty_initial: float  # split-adjusted shares (= as-traded shares * unit)
    qty: float  # remaining split-adjusted shares
    unit: float  # split-adjusted shares per as-traded share at entry (1.0 if unknown)
    target: float  # contract take-profit; +inf in luk mode
    mark: float  # last valid close seen (entry fill until the first close)
    missing: int = 0  # consecutive sessions without a daily bar
    partial_checked: bool = False  # partial/breakeven rule evaluated (exactly once)
    proceeds: float = 0.0  # sum of exit qty * exit fill (after slippage)
    legs: list[dict] = field(default_factory=list)


@dataclass
class _Pending:
    """A fired trigger waiting for the sequential portfolio checks of step 2."""

    cand: Candidate
    trig: Trigger
    mult: float  # regime risk multiplier (1.0, or risk_off_ep_mult for EP risk-off)


def _aligned(df: pd.DataFrame, index: pd.Index, columns: pd.Index) -> np.ndarray:
    if not (df.index.equals(index) and df.columns.equals(columns)):
        df = df.reindex(index=index, columns=columns)
    return df.to_numpy(dtype=np.float64)


# --------------------------------------------------------------------------- simulator
class _Simulator:
    def __init__(
        self,
        panel: DailyPanel,
        feats: Features,
        watchlists: dict[date, list[Candidate]],
        cfg: Config,
        intraday_store: IntradayStoreFn | None,
    ):
        self.cfg = cfg
        self.watchlists = _normalize_watchlists(watchlists)
        self.store = intraday_store
        self.intraday = cfg.entry_mode == "intraday"
        self.luk = cfg.exit_mode == "luk"
        self.slip_out = 1.0 - cfg.slip_bps / 1e4

        self.dates = pd.DatetimeIndex(panel.dates)
        self.days = [ts.date() for ts in self.dates]
        cols = panel.close.columns
        self.col_of = {s: j for j, s in enumerate(cols)}
        self.O = _aligned(panel.open, self.dates, cols)
        self.H = _aligned(panel.high, self.dates, cols)
        self.L = _aligned(panel.low, self.dates, cols)
        self.C = _aligned(panel.close, self.dates, cols)
        self.trail: np.ndarray | None = None
        if self.luk:
            if cfg.trail_ma not in feats.frames:
                raise KeyError(f"trail_ma {cfg.trail_ma!r} not in features")
            self.trail = _aligned(feats[cfg.trail_ma], self.dates, cols)
        self.trail_from = cfg.partial_day if cfg.trail_after_partial else 1

        self.cash = float(cfg.initial_equity)
        self.book: dict[str, _Position] = {}  # symbol -> open position (insertion order)
        self.next_id = 1
        self.trades: list[dict] = []
        self.skips: list[tuple] = []
        self.equity_rows: list[tuple] = []

    # ------------------------------------------------------------------ bookkeeping
    def _skip(self, i: int, cand: Candidate, reason: str) -> None:
        self.skips.append((self.dates[i], cand.symbol, cand.setup, reason))

    def _day_bar(self, i: int, j: int) -> dict:
        return {
            "open": float(self.O[i, j]),
            "high": float(self.H[i, j]),
            "low": float(self.L[i, j]),
            "close": float(self.C[i, j]),
        }

    def _exit(self, pos: _Position, i: int, qty: float, raw: float, reason: str) -> None:
        """Sell `qty` split-adjusted shares at RAW price `raw` on session i (exit slippage
        applied here). Selling the full remainder passes pos.qty, which closes exactly."""
        qty = min(float(qty), pos.qty)
        if qty <= 0:
            return
        px = float(raw) * self.slip_out
        self.cash += qty * px
        pos.qty -= qty
        pos.proceeds += qty * px
        pos.legs.append(
            {"date": self.days[i].isoformat(), "qty": qty, "price": px, "reason": reason}
        )
        if pos.qty <= pos.unit * _EPS:  # float residue below a billionth of a share
            pos.qty = 0.0
            del self.book[pos.symbol]
            self._record_trade(pos, i, reason)

    def _record_trade(self, pos: _Position, i: int, reason: str) -> None:
        cost = pos.qty_initial * pos.entry_fill
        pnl = pos.proceeds - cost
        risk = pos.qty_initial * (pos.entry_fill - pos.initial_stop)
        self.trades.append(
            {
                "trade_id": pos.trade_id,
                "symbol": pos.symbol,
                "setup": pos.setup,
                "entry_date": self.dates[pos.entry_i],
                "entry_ts": pos.entry_ts,
                "entry_fill": pos.entry_fill,
                "initial_stop": pos.initial_stop,
                "qty": pos.qty_initial,
                "risk_dollars": risk,
                "exit_date": self.dates[i],
                "exit_price_avg": pos.proceeds / pos.qty_initial,
                "exit_reason": reason,
                "pnl": pnl,
                "r_multiple": pnl / risk if risk > 0 else math.nan,
                "return_pct": pnl / cost,
                "hold_sessions": i - pos.entry_i,
                "legs": json.dumps(pos.legs),
            }
        )

    # ------------------------------------------------------------------ step 1
    def _open_exits(self, i: int) -> None:
        """Gap through the stop at the open of t. Every position in the book here was
        entered before t. Reads open_t only; a missing bar (NaN open) skips the check."""
        for pos in list(self.book.values()):
            o = self.O[i, pos.col]
            if touched_down(o, pos.stop):  # NaN -> False
                self._exit(pos, i, pos.qty, o, STOP)

    # ------------------------------------------------------------------ step 2
    def _regime_mult(self, cand: Candidate) -> float | None:
        """Risk multiplier for this candidate, or None if the regime blocks it."""
        if _risk_on(cand, self.cfg.regime_mode):
            return 1.0
        if cand.setup == "EP" and self.cfg.ep_in_risk_off:
            return float(self.cfg.risk_off_ep_mult)
        return None

    def _size(
        self, trig: Trigger, mult: float, e_prev: float, gross: float
    ) -> tuple[float, str]:
        """SPEC §7 sizing on prior-close equity E_{t-1}. Returns (split-adjusted qty,
        skip_reason). Whole shares are counted in as-traded units (qty / unit)."""
        cfg = self.cfg
        unit = _share_unit(trig.candidate)
        fill, stop = float(trig.fill), float(trig.stop)
        per_share = (fill - stop) * unit  # risk per as-traded share
        if not (math.isfinite(per_share) and per_share > 0 and fill > 0):
            return 0.0, SKIP_QTY_ZERO
        risk_dollars = cfg.risk_per_trade * e_prev * mult
        shares = _floor_shares(risk_dollars / per_share)
        if shares < 1:
            return 0.0, SKIP_QTY_ZERO
        position_cap = cfg.max_position_pct * e_prev
        gross_cap = cfg.max_gross * e_prev - gross  # room left under the gross cap
        for cap_dollars in (position_cap, gross_cap, self.cash):
            shares = min(shares, _floor_shares(cap_dollars / (fill * unit)))
        if shares < 1:
            return 0.0, SKIP_CAP_EXPOSURE
        return shares * unit, ""

    def _entries(
        self, i: int, e_prev: float
    ) -> tuple[list[tuple[_Position, Trigger]], dict[str, pd.DataFrame]]:
        cfg = self.cfg
        day = self.days[i]
        eligible: list[tuple[Candidate, float]] = []
        for cand in self.watchlists.get(day) or []:
            if cand.setup not in cfg.setups:
                self._skip(i, cand, SKIP_SETUP_DISABLED)
                continue
            mult = self._regime_mult(cand)
            if mult is None:
                self._skip(i, cand, SKIP_REGIME)
                continue
            if cand.symbol not in self.col_of:
                self._skip(i, cand, SKIP_NO_DATA)
                continue
            eligible.append((cand, mult))
        if not eligible:
            return [], {}

        bars_map: dict[str, pd.DataFrame] = {}
        if self.intraday:
            assert self.store is not None
            bars_map = self.store(day) or {}

        fired: list[_Pending] = []
        for cand, mult in eligible:
            if self.intraday:
                res = find_trigger(cand, bars_map.get(cand.symbol), cfg)
            else:
                j = self.col_of[cand.symbol]
                if np.isnan(self.C[i, j]):
                    self._skip(i, cand, SKIP_NO_DATA)
                    continue
                res = find_trigger_daily(cand, self._day_bar(i, j), cfg)
            if isinstance(res, SkipEvent):
                self._skip(i, cand, res.reason)
                continue
            fired.append(_Pending(cand, res, mult))

        # Same symbol in several setups: earliest trigger wins; ties EP > BO > PB.
        best: dict[str, _Pending] = {}
        for p in fired:
            cur = best.get(p.cand.symbol)
            key = (p.trig.ts, _priority(p.cand.setup))
            if cur is None:
                best[p.cand.symbol] = p
            elif key < (cur.trig.ts, _priority(cur.cand.setup)):
                self._skip(i, cur.cand, SKIP_DUP_SETUP)
                best[p.cand.symbol] = p
            else:
                self._skip(i, p.cand, SKIP_DUP_SETUP)

        ordered = sorted(
            best.values(),
            key=lambda p: (p.trig.ts, _priority(p.cand.setup), _rank_desc(p.cand), p.cand.symbol),
        )
        # Gross before any day-t entry: surviving positions at their prior-close mark
        # (positions exited at today's open are already out of the book).
        gross = sum(p.qty * p.mark for p in self.book.values())
        opened: list[tuple[_Position, Trigger]] = []
        for p in ordered:
            cand, trig = p.cand, p.trig
            if cand.symbol in self.book:
                self._skip(i, cand, SKIP_ALREADY_HELD)
                continue
            if len(self.book) >= cfg.max_open_positions:
                self._skip(i, cand, SKIP_CAP_OPEN)
                continue
            if len(opened) >= cfg.max_new_per_day:
                self._skip(i, cand, SKIP_CAP_NEW)
                continue
            qty, reason = self._size(trig, p.mult, e_prev, gross)
            if qty <= 0:
                self._skip(i, cand, reason)
                continue
            fill = float(trig.fill)
            pos = _Position(
                trade_id=self.next_id,
                symbol=cand.symbol,
                col=self.col_of[cand.symbol],
                setup=cand.setup,
                entry_i=i,
                entry_ts=trig.ts,
                entry_fill=fill,
                initial_stop=float(trig.stop),
                stop=float(trig.stop),
                qty_initial=qty,
                qty=qty,
                unit=_share_unit(cand),
                target=float(contract_target(trig, cfg)) if not self.luk else math.inf,
                mark=fill,
            )
            self.next_id += 1
            self.book[pos.symbol] = pos
            self.cash -= qty * fill
            gross += qty * fill
            opened.append((pos, trig))
        return opened, bars_map

    # ------------------------------------------------------------------ step 3
    def _entry_day_exits(
        self, i: int, opened: list[tuple[_Position, Trigger]], bars_map: dict[str, pd.DataFrame]
    ) -> None:
        for pos, trig in opened:
            if self.intraday:
                res = entry_day_exit(trig, bars_map.get(pos.symbol), None, self.cfg)
            else:
                res = entry_day_exit(trig, None, self._day_bar(i, pos.col), self.cfg)
            if res is not None:
                _ts, raw, reason = res
                self._exit(pos, i, pos.qty, raw, reason)

    # ------------------------------------------------------------------ step 4
    def _close_rules(self, i: int) -> None:
        cfg = self.cfg
        for pos in list(self.book.values()):
            j = pos.col
            c = self.C[i, j]
            if pos.entry_i == i:  # opened today: entry-day exits were step 3
                if not np.isnan(c):
                    pos.mark = float(c)
                continue
            if np.isnan(c):  # missing bar: no checks, keep the last mark
                pos.missing += 1
                if pos.missing >= cfg.data_gap_sessions:
                    self._exit(pos, i, pos.qty, pos.mark, DATA_END)
                continue
            pos.missing = 0
            c = float(c)
            pos.mark = c
            # Stop first (open > stop here, step 1 handled gaps through the stop).
            if touched_down(self.L[i, j], pos.stop):
                self._exit(pos, i, pos.qty, pos.stop, STOP)
                continue
            held = i - pos.entry_i  # panel sessions since entry (entry day = 0)
            if self.luk:
                self._luk_close(pos, i, c, held)
            else:
                self._contract_close(pos, i, c, held)

    def _luk_close(self, pos: _Position, i: int, c: float, held: int) -> None:
        cfg = self.cfg
        # Partial/breakeven: evaluated once, at the first session with a bar from the
        # partial_day-th session on (a missing bar on that exact session must not skip it).
        if not pos.partial_checked and held >= cfg.partial_day:
            pos.partial_checked = True
            if c > pos.entry_fill:
                shares = round(pos.qty_initial / pos.unit)  # as-traded shares held
                n = max(1, _floor_shares(shares * cfg.partial_frac)) * pos.unit
                if pos.qty > n * (1 + _EPS):
                    self._exit(pos, i, n, c, PARTIAL)
                pos.stop = max(pos.stop, pos.entry_fill)  # breakeven on the remainder
        assert self.trail is not None
        if held >= self.trail_from and c < self.trail[i, pos.col]:  # NaN MA -> no signal
            self._exit(pos, i, pos.qty, c, TRAIL)
        elif held >= cfg.max_hold:
            self._exit(pos, i, pos.qty, c, TIME)

    def _contract_close(self, pos: _Position, i: int, c: float, held: int) -> None:
        o, h = self.O[i, pos.col], self.H[i, pos.col]
        if touched_up(o, pos.target):
            self._exit(pos, i, pos.qty, o, TARGET)
        elif touched_up(h, pos.target):
            self._exit(pos, i, pos.qty, pos.target, TARGET)
        elif held >= self.cfg.contract_horizon:
            self._exit(pos, i, pos.qty, c, TIME)

    def _liquidate(self, i: int) -> None:
        """Last session: close everything at the close (last valid close if NaN)."""
        for pos in list(self.book.values()):
            self._exit(pos, i, pos.qty, pos.mark, END_OF_TEST)

    # ------------------------------------------------------------------ step 5
    def _mark(self, i: int) -> float:
        gross = sum(p.qty * p.mark for p in self.book.values())
        equity = self.cash + gross
        self.equity_rows.append((self.dates[i], equity, self.cash, gross, len(self.book)))
        return equity

    # ------------------------------------------------------------------ main loop
    def session_rows(self) -> np.ndarray:
        cfg = self.cfg
        if len(self.dates) == 0:
            return np.array([], dtype=np.int64)
        end = pd.Timestamp(cfg.end) if cfg.end is not None else self.dates[-1]
        mask = (self.dates >= pd.Timestamp(cfg.start)) & (self.dates <= end)
        return np.flatnonzero(mask)

    def run(self) -> BacktestResult:
        rows = self.session_rows()
        e_prev = float(self.cfg.initial_equity)  # E_{t-1}; initial equity on day one
        last = int(rows[-1]) if len(rows) else -1
        for i in rows.tolist():
            self._open_exits(i)
            opened, bars_map = self._entries(i, e_prev)
            self._entry_day_exits(i, opened, bars_map)
            self._close_rules(i)
            if i == last:
                self._liquidate(i)
            e_prev = self._mark(i)
        return self._result()

    def _result(self) -> BacktestResult:
        trades = pd.DataFrame(self.trades, columns=TRADE_COLUMNS)
        if len(trades):
            trades = trades.sort_values("trade_id", kind="stable").reset_index(drop=True)
        equity = pd.DataFrame(
            [r[1:] for r in self.equity_rows],
            index=pd.DatetimeIndex([r[0] for r in self.equity_rows], name="date"),
            columns=EQUITY_COLUMNS,
        )
        equity["n_positions"] = equity["n_positions"].astype("int64")
        skips = pd.DataFrame(self.skips, columns=SKIP_COLUMNS)
        return BacktestResult(trades=trades, equity=equity, skips=skips, config=self.cfg.to_dict())


# --------------------------------------------------------------------------- public API
def run_backtest(
    panel: DailyPanel,
    feats: Features,
    watchlists: dict[date, list[Candidate]],
    cfg: Config,
    intraday_store: IntradayStoreFn | None = None,
) -> BacktestResult:
    """Simulate the portfolio over [cfg.start, cfg.end or last panel date]."""
    if cfg.entry_mode not in ENTRY_MODES:
        raise ValueError(f"entry_mode must be one of {ENTRY_MODES}, got {cfg.entry_mode!r}")
    if cfg.exit_mode not in EXIT_MODES:
        raise ValueError(f"exit_mode must be one of {EXIT_MODES}, got {cfg.exit_mode!r}")
    if cfg.regime_mode not in REGIME_MODES:
        raise ValueError(f"regime_mode must be one of {REGIME_MODES}, got {cfg.regime_mode!r}")
    if cfg.entry_mode == "intraday" and intraday_store is None:
        raise ValueError("entry_mode='intraday' requires intraday_store")
    return _Simulator(panel, feats, watchlists, cfg, intraday_store).run()


__all__ = ["EQUITY_COLUMNS", "SKIP_COLUMNS", "TRADE_COLUMNS", "run_backtest"]
