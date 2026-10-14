"""Synthetic end-to-end run: panel -> features -> watchlists -> 5-min bars -> engine -> metrics.

Fully offline and deterministic. The synthetic market has trending leaders that cycle through
run-ups, tight consolidations (BO) and shallow low-volume pullbacks (PB), a few names with
large opening gaps on heavy volume (EP), random-walk fillers, a symbol with a data gap and one
whose data ends mid-test (data_end), plus SPY/QQQ (QQQ has one correction so the index regime
switches off for a while). Intraday bars are generated only for watchlist symbol-days, the
way `fetch-intraday` works, and are consistent with the daily bar of that day (open == daily
open, close == daily close, bar highs/lows inside the daily range and touching both ends).

The config loosens only the leader RS threshold (a 40-name universe has ~2 names per 5%
bucket) so all three setups occur; every other rule is the default.
"""

from __future__ import annotations

import json
import math
from datetime import date

import numpy as np
import pandas as pd
import pytest

from research.luk_strategy import engine, features, intraday, metrics, signals
from research.luk_strategy.config import BENCHMARKS, Config
from research.luk_strategy.models import Candidate, DailyPanel, Trigger

ET = "America/New_York"
N_DAYS = 400
START_ROW = 200  # first backtest session (>= min_history 150 + ret126 warmup)
N_LEADERS, N_EP, N_FILLERS = 10, 6, 22
BARS_PER_DAY = 78  # 09:30 .. 15:55 5-min bar starts
TICK = intraday.TICK


# --------------------------------------------------------------------------- daily panel
def _ohlc_from_closes(
    rng: np.random.Generator,
    close: np.ndarray,
    gaps: np.ndarray,
    rng_pct: np.ndarray,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """open = prev close x exp(gap); high/low extend beyond max/min(open, close)."""
    prev = np.concatenate([[close[0] / math.exp(gaps[0])], close[:-1]])
    open_ = prev * np.exp(gaps)
    top, bot = np.maximum(open_, close), np.minimum(open_, close)
    up = rng.uniform(0.25, 0.75, len(close))
    high = top * (1.0 + rng_pct * up)
    low = bot * (1.0 - rng_pct * (1.0 - up))
    return np.round(open_, 2), np.round(high, 2), np.round(low, 2)


def _leader(rng: np.random.Generator, n: int, phase: int) -> dict[str, np.ndarray]:
    """Run-up (14d) -> tight base (7d) -> shallow pullback (4d), repeated; high base volume."""
    cycle = [("run", 14), ("base", 7), ("pull", 4)]
    kinds: list[str] = []
    while len(kinds) < n + phase:
        for k, m in cycle:
            kinds += [k] * m
    kinds = kinds[phase : phase + n]
    mu = {"run": 0.012, "base": 0.0005, "pull": -0.009}
    sd = {"run": 0.014, "base": 0.004, "pull": 0.006}
    rp = {"run": 0.055, "base": 0.03, "pull": 0.035}
    vm = {"run": 1.4, "base": 0.7, "pull": 0.55}
    r = np.array([rng.normal(mu[k], sd[k]) for k in kinds])
    close = 20.0 * np.exp(np.cumsum(r))
    gaps = rng.normal(0.0, 0.003, n)
    rng_pct = np.array([rp[k] for k in kinds]) * rng.uniform(0.8, 1.2, n)
    o, h, lo = _ohlc_from_closes(rng, close, gaps, rng_pct)
    vol = 1.2e6 * np.array([vm[k] for k in kinds]) * rng.uniform(0.8, 1.2, n)
    return {"open": o, "high": h, "low": lo, "close": np.round(close, 2), "volume": vol}


def _ep_name(rng: np.random.Generator, n: int, gap_days: list[int]) -> dict[str, np.ndarray]:
    """Quiet drifter that gaps up 12-25% on 4x volume on `gap_days`, then drifts up."""
    r = rng.normal(0.0003, 0.02, n)
    gaps = rng.normal(0.0, 0.004, n)
    vol = 8e5 * rng.uniform(0.8, 1.2, n)
    for g in gap_days:
        gaps[g] = rng.uniform(0.12, 0.25)
        r[g] = gaps[g] + rng.normal(0.01, 0.01)  # close above the gap open on average
        r[g + 1 : g + 8] += 0.006
        vol[g] *= 4.0
        vol[g + 1 : g + 3] *= 2.0
    close = 30.0 * np.exp(np.cumsum(r))
    # close of the gap day = open x exp(intraday move): rebuild open from the prior close
    rng_pct = rng.uniform(0.035, 0.06, n)
    o, h, lo = _ohlc_from_closes(rng, close, gaps, rng_pct)
    return {"open": o, "high": h, "low": lo, "close": np.round(close, 2), "volume": vol}


def _filler(rng: np.random.Generator, n: int) -> dict[str, np.ndarray]:
    r = rng.normal(rng.normal(0.0, 0.0008), 0.022, n)
    close = rng.uniform(15, 60) * np.exp(np.cumsum(r))
    gaps = rng.normal(0.0, 0.004, n)
    o, h, lo = _ohlc_from_closes(rng, close, gaps, rng.uniform(0.03, 0.06, n))
    vol = 1.0e6 * rng.uniform(0.7, 1.3, n)
    return {"open": o, "high": h, "low": lo, "close": np.round(close, 2), "volume": vol}


def _index(rng: np.random.Generator, n: int, mu: float, sd: float, start: float, dip: bool):
    r = rng.normal(mu, sd, n)
    if dip:  # a correction inside the test window -> risk_on_index False for a while
        r[260:285] -= 0.006
    close = start * np.exp(np.cumsum(r))
    o, h, lo = _ohlc_from_closes(rng, close, rng.normal(0, 0.002, n), np.full(n, 0.008))
    vol = np.full(n, 5e7)
    return {"open": o, "high": h, "low": lo, "close": np.round(close, 2), "volume": vol}


def make_panel(seed: int = 7) -> DailyPanel:
    rng = np.random.default_rng(seed)
    n = N_DAYS
    dates = pd.bdate_range("2021-01-04", periods=n)
    series: dict[str, dict[str, np.ndarray]] = {}
    for k in range(N_LEADERS):
        series[f"LD{k:02d}"] = _leader(rng, n, phase=int(rng.integers(0, 25)))
    for k in range(N_EP):
        days = sorted(rng.choice(np.arange(START_ROW + 5, n - 15), size=2, replace=False))
        series[f"EP{k:02d}"] = _ep_name(rng, n, [int(d) for d in days])
    for k in range(N_FILLERS):
        series[f"FL{k:02d}"] = _filler(rng, n)
    series["SPY"] = _index(rng, n, 0.0004, 0.009, 400.0, dip=False)
    series["QQQ"] = _index(rng, n, 0.0006, 0.012, 300.0, dip=True)

    # A leader with a 3-session data hole and one whose data ends inside the test window.
    gap_sym, end_sym = "LD01", "LD02"
    for fld in ("open", "high", "low", "close", "volume"):
        series[gap_sym][fld] = series[gap_sym][fld].astype(float)
        series[gap_sym][fld][300:303] = np.nan
        series[end_sym][fld] = series[end_sym][fld].astype(float)
        series[end_sym][fld][340:] = np.nan

    syms = sorted(series)
    frames = {
        fld: pd.DataFrame({s: series[s][fld] for s in syms}, index=dates).astype("float64")
        for fld in ("open", "high", "low", "close", "volume")
    }
    return DailyPanel(**frames)


def e2e_config(panel: DailyPanel, **kw) -> Config:
    base = Config(start=panel.dates[START_ROW].date(), leader_rs=0.80)
    return base.with_overrides(**kw)


# --------------------------------------------------------------------------- 5-min bars
def make_bars(
    day: date, symbol: str, o: float, h: float, lo: float, c: float, v: float, seed: int
) -> pd.DataFrame:
    """78 regular-session bars consistent with the daily bar: first open == o, last close
    == c, every bar inside [lo, h], and the bar extremes touch h and lo exactly."""
    rng = np.random.default_rng(seed)
    n = BARS_PER_DAY
    steps = rng.normal(0.0, 1.0, n)
    walk = np.concatenate([[0.0], np.cumsum(steps)])
    bridge = walk - np.linspace(0.0, 1.0, n + 1) * walk[-1]
    amp = max(np.abs(bridge).max(), 1e-9)
    width = (math.log(h) - math.log(lo)) * 0.6
    drift = np.linspace(math.log(o), math.log(c), n + 1)
    path = np.exp(drift + bridge / amp * width / 2.0)
    path = np.clip(path, lo, h)
    path[0], path[-1] = o, c
    opens, closes = path[:-1].copy(), path[1:].copy()
    wiggle = rng.uniform(0.0, 0.002, (2, n))
    highs = np.minimum(np.maximum(opens, closes) * (1.0 + wiggle[0]), h)
    lows = np.maximum(np.minimum(opens, closes) * (1.0 - wiggle[1]), lo)
    highs[int(rng.integers(1, n))] = h  # the day's high is set after the opening bar...
    lows[int(rng.integers(0, n))] = lo  # ...the low can be anywhere
    vols = np.full(n, v * 0.85 / (n - 1))
    vols[0] = v * 0.15
    start = pd.Timestamp(f"{day.isoformat()} 09:30", tz=ET)
    ts = start + pd.to_timedelta(np.arange(n) * 5, unit="min")
    return pd.DataFrame(
        {
            "symbol": symbol,
            "ts": ts,
            "open": opens,
            "high": highs,
            "low": lows,
            "close": closes,
            "volume": vols,
        }
    )


class FakeIntradayStore:
    """callable(day) -> {symbol: bars} for that day's watchlist symbols only (like the real
    IntradayStore over a cache filled from intraday_manifest). Bars derive from `source`
    (the full panel), never from whatever panel the engine is given."""

    def __init__(self, source: DailyPanel, manifest: dict[date, list[str]]):
        self.src = source
        self.manifest = manifest
        self.row = {ts.date(): i for i, ts in enumerate(source.dates)}
        self.col = {s: j for j, s in enumerate(source.symbols)}
        self.cache: dict[date, dict[str, pd.DataFrame]] = {}
        self.calls: list[date] = []

    def __call__(self, day: date) -> dict[str, pd.DataFrame] | None:
        self.calls.append(day)
        if day in self.cache:
            return self.cache[day]
        syms = self.manifest.get(day)
        if not syms:
            return None
        i = self.row[day]
        out = {}
        for s in syms:
            j = self.col[s]
            o, h, lo, c, v = (
                float(getattr(self.src, f).iat[i, j])
                for f in ("open", "high", "low", "close", "volume")
            )
            if not all(math.isfinite(x) for x in (o, h, lo, c, v)):
                continue  # no daily bar -> nothing in the intraday cache either
            out[s] = make_bars(day, s, o, h, lo, c, v, seed=i * 1000 + j)
        self.cache[day] = out
        return out


# --------------------------------------------------------------------------- fixtures
@pytest.fixture(autouse=True)
def _sandbox(tmp_path, monkeypatch):
    monkeypatch.setenv("LUK_DATA_DIR", str(tmp_path / "luk"))


@pytest.fixture(scope="module")
def world():
    panel = make_panel()
    cfg = e2e_config(panel)
    feats = features.compute_features(panel, cfg)
    wl = signals.build_watchlists(panel, feats, cfg)
    store = FakeIntradayStore(panel, signals.intraday_manifest(wl))
    return panel, cfg, feats, wl, store


MODES = [
    ("intraday", "luk"),
    ("intraday", "contract"),
    ("daily", "luk"),
    ("daily", "contract"),
]


@pytest.fixture(scope="module")
def runs(world):
    panel, cfg, feats, wl, store = world
    out = {}
    for entry_mode, exit_mode in MODES:
        c = cfg.with_overrides(entry_mode=entry_mode, exit_mode=exit_mode)
        res = engine.run_backtest(
            panel, feats, wl, c, intraday_store=store if entry_mode == "intraday" else None
        )
        out[(entry_mode, exit_mode)] = (c, res)
    return out


def _cand_index(wl: dict[date, list[Candidate]]) -> dict[tuple, Candidate]:
    return {(d, c.symbol, c.setup): c for d, cands in wl.items() for c in cands}


# --------------------------------------------------------------------------- tests
def test_synthetic_world_is_consistent(world):
    panel, cfg, feats, wl, store = world
    o, h, lo, c = panel.open, panel.high, panel.low, panel.close
    ok = c.notna().to_numpy()
    on, hn, ln, cn = (x.to_numpy()[ok] for x in (o, h, lo, c))
    assert (hn >= np.maximum(on, cn) - 1e-9).all()
    assert (ln <= np.minimum(on, cn) + 1e-9).all()
    counts = {
        s: sum(1 for cs in wl.values() for x in cs if x.setup == s) for s in ("EP", "BO", "PB")
    }
    assert all(v > 0 for v in counts.values()), counts
    assert not any(x.symbol in BENCHMARKS for cs in wl.values() for x in cs)
    assert (
        feats.risk_on_index.iloc[START_ROW:].any()
        and not feats.risk_on_index.iloc[START_ROW:].all()
    )  # the QQQ dip produces risk-off days inside the test window
    # Bars agree with the daily bar of their day.
    day = next(d for d, cs in sorted(wl.items()) if cs)
    bars_map = store(day)
    i = panel.dates.get_loc(pd.Timestamp(day))
    for s, b in bars_map.items():
        j = panel.symbols.index(s)
        assert b["open"].iat[0] == pytest.approx(o.iat[i, j])
        assert b["close"].iat[-1] == pytest.approx(c.iat[i, j])
        assert b["high"].max() == pytest.approx(h.iat[i, j])
        assert b["low"].min() == pytest.approx(lo.iat[i, j])
        assert (b["high"] >= b[["open", "close"]].max(axis=1) - 1e-9).all()
        assert (b["low"] <= b[["open", "close"]].min(axis=1) + 1e-9).all()


@pytest.mark.parametrize("mode", MODES, ids=lambda m: f"{m[0]}-{m[1]}")
def test_e2e_run_invariants(world, runs, mode, tmp_path):
    panel, _, _, wl, _ = world
    cfg, res = runs[mode]
    trades, equity = res.trades, res.equity
    assert len(trades) > 0, res.skips["reason"].value_counts().to_dict()

    # Equity identity: everything is liquidated on the last session.
    final = float(equity["equity"].iloc[-1])
    expected = cfg.initial_equity + float(trades["pnl"].sum())
    assert final == pytest.approx(expected, rel=1e-6)
    assert float(equity["cash"].iloc[-1]) == pytest.approx(final, rel=1e-9)
    assert int(equity["n_positions"].iloc[-1]) == 0
    assert equity.index[0] == panel.dates[START_ROW]
    assert equity.index[-1] == panel.dates[-1]

    cands = _cand_index(wl)
    for t in trades.itertuples(index=False):
        key = (t.entry_date.date(), t.symbol, t.setup)
        # Every entry was a pre-open candidate for that symbol/setup on that day.
        assert key in cands, key
        cand = cands[key]
        if t.setup in ("BO", "PB"):
            # Stop-buy above the pre-open level: fill (incl. slippage) >= level + 1 tick.
            assert cand.level is not None
            assert t.entry_fill >= cand.level + TICK - 1e-9, (key, t.entry_fill, cand.level)
        # Exits never precede entries; legs are dated within [entry, exit].
        assert t.exit_date >= t.entry_date
        legs = json.loads(t.legs)
        assert sum(leg["qty"] for leg in legs) == t.qty
        for leg in legs:
            assert t.entry_date.date() <= date.fromisoformat(leg["date"]) <= t.exit_date.date()
        assert t.initial_stop < t.entry_fill
        assert t.qty >= 1 and t.risk_dollars > 0
        if mode[0] == "intraday":
            assert t.entry_ts.tz is not None and t.entry_ts.date() == t.entry_date.date()
            assert t.entry_ts.hour * 60 + t.entry_ts.minute >= 9 * 60 + 35  # never the OR bar

    # Portfolio caps hold every day.
    assert int(equity["n_positions"].max()) <= cfg.max_open_positions
    per_day = trades.groupby("entry_date").size()
    assert int(per_day.max()) <= cfg.max_new_per_day
    assert (equity["cash"] >= -1e-6).all()
    # One position per symbol at a time.
    for _, g in trades.sort_values(["symbol", "entry_date"]).groupby("symbol"):
        ent, ex = g["entry_date"].to_numpy(), g["exit_date"].to_numpy()
        assert (ent[1:] >= ex[:-1]).all()

    summary = metrics.summarize(res, panel, cfg)
    json.dumps(summary, allow_nan=False)
    assert summary["overall"]["trades"] == len(trades)
    assert summary["overall"]["final_equity"] == pytest.approx(final)
    out = tmp_path / f"{mode[0]}_{mode[1]}"
    metrics.write_results(res, summary, out)
    for name in ("trades.csv", "equity.csv", "skips.csv", "summary.json", "report.md"):
        assert (out / name).stat().st_size > 0
    reloaded = json.loads((out / "summary.json").read_text())
    assert reloaded["overall"]["trades"] == len(trades)
    assert len(pd.read_csv(out / "trades.csv")) == len(trades)


def test_e2e_exercises_all_setups_and_exit_paths(runs):
    setups = set()
    reasons = set()
    skip_reasons = set()
    for (_entry, _exit), (_cfg, res) in runs.items():
        setups |= set(res.trades["setup"])
        reasons |= set(res.trades["exit_reason"])
        skip_reasons |= set(res.skips["reason"])
    assert setups == {"EP", "BO", "PB"}, setups
    assert {"stop", "end_of_test"} <= reasons, reasons
    assert reasons & {"trail", "time"}, reasons
    assert "target" in reasons, reasons
    assert "regime" in skip_reasons, skip_reasons
    _, intraday_luk = runs[("intraday", "luk")]
    legs = [leg for s in intraday_luk.trades["legs"] for leg in json.loads(s)]
    assert any(leg["reason"] == "partial" for leg in legs)


def test_e2e_no_lookahead_under_truncation(world, runs):
    """Truncating the daily panel at session T (features, watchlists, engine all rebuilt)
    must not change anything decided before T: equity before T, every trade closed before
    T, every entry on or before T, and every skip on or before T."""
    panel, _, _, _, store = world
    cfg, full = runs[("intraday", "luk")]

    T = panel.dates[320]
    cut = DailyPanel(
        **{f: getattr(panel, f).loc[:T] for f in ("open", "high", "low", "close", "volume")}
    )
    cut_feats = features.compute_features(cut, cfg)
    cut_wl = signals.build_watchlists(cut, cut_feats, cfg)
    part = engine.run_backtest(cut, cut_feats, cut_wl, cfg, intraday_store=store)

    pd.testing.assert_frame_equal(
        full.equity.loc[: T - pd.Timedelta(days=1)], part.equity.iloc[:-1]
    )
    entry_cols = ["symbol", "setup", "entry_date", "entry_ts", "entry_fill", "initial_stop", "qty"]
    f_ent = _entries_until(full, T, entry_cols)
    assert len(f_ent) > 0
    pd.testing.assert_frame_equal(f_ent, _entries_until(part, T, entry_cols))
    closed_cols = [*entry_cols, "exit_date", "exit_price_avg", "exit_reason", "pnl", "legs"]
    f_closed = full.trades[full.trades["exit_date"] < T][closed_cols]
    p_closed = part.trades[part.trades["exit_date"] < T][closed_cols]
    assert len(f_closed) > 0
    pd.testing.assert_frame_equal(
        f_closed.sort_values(["entry_date", "symbol"]).reset_index(drop=True),
        p_closed.sort_values(["entry_date", "symbol"]).reset_index(drop=True),
    )
    f_sk = full.skips[full.skips["day"] <= T].reset_index(drop=True)
    pd.testing.assert_frame_equal(f_sk, part.skips.reset_index(drop=True))


def _entries_until(res, T: pd.Timestamp, cols: list[str]) -> pd.DataFrame:
    e = res.trades[res.trades["entry_date"] <= T][cols]
    return e.sort_values(["entry_date", "symbol"]).reset_index(drop=True)


def _perturb_day(panel: DailyPanel, i: int) -> DailyPanel:
    """Copy of `panel` with high/low/close/volume of row i changed (open_i kept)."""
    f = {k: getattr(panel, k).copy() for k in ("open", "high", "low", "close", "volume")}
    c = f["close"].iloc[i] * 1.07
    f["close"].iloc[i] = c
    f["high"].iloc[i] = np.maximum(f["high"].iloc[i], c) * 1.05
    f["low"].iloc[i] = np.minimum(f["low"].iloc[i], c) * 0.93
    f["volume"].iloc[i] = f["volume"].iloc[i] * 3.0
    return DailyPanel(**f)


@pytest.mark.parametrize("exit_mode", ["luk", "contract"])
def test_e2e_day_t_close_data_never_reaches_day_t_entries(world, runs, exit_mode):
    """Intraday mode: entries on day T may use only data <= T-1, open_T and the 5-min bars.
    Perturbing high/low/close/volume of day T (bars unchanged) must leave every entry and
    skip up to and including T, and the equity curve before T, unchanged."""
    panel, cfg, _, _, store = world
    c0, base = runs[("intraday", exit_mode)]
    entry_days = sorted(set(base.trades["entry_date"]))
    picks = [d for d in entry_days if d > panel.dates[START_ROW + 5]][
        :: max(1, len(entry_days) // 4)
    ]
    assert picks
    cols = ["symbol", "setup", "entry_date", "entry_ts", "entry_fill", "initial_stop", "qty"]
    for T in picks[:4]:
        i = panel.dates.get_loc(T)
        alt = _perturb_day(panel, i)
        alt_feats = features.compute_features(alt, c0)
        alt_wl = signals.build_watchlists(alt, alt_feats, c0)
        res = engine.run_backtest(alt, alt_feats, alt_wl, c0, intraday_store=store)

        pd.testing.assert_frame_equal(_entries_until(base, T, cols), _entries_until(res, T, cols))
        pd.testing.assert_frame_equal(
            base.skips[base.skips["day"] <= T].reset_index(drop=True),
            res.skips[res.skips["day"] <= T].reset_index(drop=True),
        )
        pd.testing.assert_frame_equal(
            base.equity.loc[: T - pd.Timedelta(days=1)], res.equity.loc[: T - pd.Timedelta(days=1)]
        )


@pytest.mark.parametrize("exit_mode", ["luk", "contract"])
def test_e2e_intraday_entries_replay_from_bars_up_to_trigger(world, runs, exit_mode):
    """Each intraday entry is reproduced exactly from the bars up to and including its
    trigger bar (nothing later in the session influenced fill or stop)."""
    _, _, _, wl, store = world
    cfg, res = runs[("intraday", exit_mode)]
    cands = _cand_index(wl)
    for t in res.trades.itertuples(index=False):
        cand = cands[(t.entry_date.date(), t.symbol, t.setup)]
        bars = store(t.entry_date.date())[t.symbol]
        trig = intraday.find_trigger(cand, bars[bars["ts"] <= t.entry_ts], cfg)
        assert isinstance(trig, Trigger), (t.symbol, t.entry_date, trig)
        assert trig.ts == t.entry_ts
        assert trig.fill == t.entry_fill
        assert trig.stop == t.initial_stop
