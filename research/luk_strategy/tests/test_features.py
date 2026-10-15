"""Offline tests for features.py (SPEC §3, §4). Synthetic data only."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from research.luk_strategy.config import Config
from research.luk_strategy.features import (
    FEATURE_KEYS,
    bars_since_rolling_max,
    compute_features,
    history_length,
)
from research.luk_strategy.models import DailyPanel

N = 220


def make_panel(spec: dict[str, dict], n: int = N) -> DailyPanel:
    """spec: {symbol: {close, [high], [low], [open], [volume]}} arrays of length n."""
    dates = pd.bdate_range("2016-01-04", periods=n)
    fields: dict[str, dict] = {f: {} for f in ("open", "high", "low", "close", "volume")}
    for s, d in spec.items():
        c = np.asarray(d["close"], dtype=float)
        fields["close"][s] = c
        fields["high"][s] = np.asarray(d.get("high", c * 1.02), dtype=float)
        fields["low"][s] = np.asarray(d.get("low", c * 0.98), dtype=float)
        fields["open"][s] = np.asarray(d.get("open", np.r_[c[0], c[:-1]]), dtype=float)
        fields["volume"][s] = np.asarray(d.get("volume", np.full(n, 2e6)), dtype=float)
    cols = sorted(spec)
    return DailyPanel(**{f: pd.DataFrame(v, index=dates)[cols] for f, v in fields.items()})


def random_spec(n_sym: int = 30, n: int = N, seed: int = 1) -> dict[str, dict]:
    rng = np.random.default_rng(seed)
    spec = {}
    for k in range(n_sym):
        c = 20 * np.exp(np.cumsum(0.0005 * k + 0.02 * rng.standard_normal(n)))
        hi = c * (1 + rng.uniform(0.005, 0.04, n))
        lo = c * (1 - rng.uniform(0.005, 0.04, n))
        op = np.clip(c * (1 + 0.01 * rng.standard_normal(n)), lo, hi)
        spec[f"S{k:02d}"] = {
            "close": c,
            "high": hi,
            "low": lo,
            "open": op,
            "volume": rng.uniform(5e5, 3e6, n),
        }
    q = 100 * np.exp(np.cumsum(0.0005 + 0.01 * rng.standard_normal(n)))
    spec["QQQ"] = {"close": q}
    spec["SPY"] = {"close": q * 2}
    return spec


@pytest.fixture
def cfg() -> Config:
    return Config(start=pd.Timestamp("2016-01-04").date())


# --------------------------------------------------------------------------- primitives
def test_bars_since_rolling_max_hand_example():
    h = np.array([[1.0], [5.0], [2.0], [3.0], [5.0], [1.0], [0.5], [9.0]])
    hmax = pd.DataFrame(h).rolling(3, min_periods=3).max().to_numpy()
    got = bars_since_rolling_max(h, hmax, 3)[:, 0]
    # windows: [1,5,2]->5 at lag1; [5,2,3]->5 lag2; [2,3,5]->lag0; [3,5,1]->lag1;
    # [5,1,.5]->lag2; [1,.5,9]->lag0
    expected = [np.nan, np.nan, 1, 2, 0, 1, 2, 0]
    np.testing.assert_array_equal(got, np.array(expected, dtype=np.float32))


def test_bars_since_ties_pick_most_recent():
    h = np.array([[4.0], [4.0], [1.0], [4.0], [2.0]])
    hmax = pd.DataFrame(h).rolling(3, min_periods=3).max().to_numpy()
    got = bars_since_rolling_max(h, hmax, 3)[:, 0]
    np.testing.assert_array_equal(got[2:], np.array([1, 0, 1], dtype=np.float32))


def test_feature_values_match_reference(cfg):
    spec = random_spec(n_sym=5)
    panel = make_panel(spec)
    f = compute_features(panel, cfg)
    assert set(FEATURE_KEYS) <= set(f.frames)
    for k, df in f.frames.items():
        assert df.index.equals(panel.close.index) and list(df.columns) == panel.symbols, k

    s = "S03"
    c, h, lo, v = (panel.close[s], panel.high[s], panel.low[s], panel.volume[s])
    t = 180
    win = slice(t - 19, t + 1)
    np.testing.assert_allclose(f["sma10"][s].iloc[t], c.iloc[t - 9 : t + 1].mean(), rtol=1e-6)
    np.testing.assert_allclose(f["sma50"][s].iloc[t], c.iloc[t - 49 : t + 1].mean(), rtol=1e-6)
    np.testing.assert_allclose(
        f["adr20"][s].iloc[t], (h.iloc[win] / lo.iloc[win] - 1).mean(), rtol=1e-5
    )
    np.testing.assert_allclose(f["adv20"][s].iloc[t], (c * v).iloc[win].mean(), rtol=1e-5)
    np.testing.assert_allclose(f["adv20_shares"][s].iloc[t], v.iloc[win].mean(), rtol=1e-5)
    np.testing.assert_allclose(f["ret63"][s].iloc[t], c.iloc[t] / c.iloc[t - 63] - 1, rtol=1e-5)
    assert f["high63"][s].iloc[t] == h.iloc[t - 62 : t + 1].max()  # exact (float64)
    assert f["high10"][s].iloc[t] == h.iloc[t - 9 : t + 1].max()
    rng5 = (h.iloc[t - 4 : t + 1].max() - lo.iloc[t - 4 : t + 1].min()) / c.iloc[t]
    np.testing.assert_allclose(f["range5"][s].iloc[t], rng5, rtol=1e-5)
    assert f["hist_len"][s].iloc[t] == t + 1

    # EMA (adjust=False recursion seeded by the first value) and its warmup
    a = 2 / (21 + 1)
    e = c.iloc[0]
    for x in c.iloc[1 : t + 1]:
        e = a * x + (1 - a) * e
    np.testing.assert_allclose(f["ema21"][s].iloc[t], e, rtol=1e-5)
    assert np.isnan(f["ema21"][s].iloc[19]) and not np.isnan(f["ema21"][s].iloc[20])

    # Wilder ATR14
    pc = c.shift(1)
    tr = np.maximum.reduce(
        [(h - lo).to_numpy(), (h - pc).abs().to_numpy(), (lo - pc).abs().to_numpy()]
    )
    tr[0] = h.iloc[0] - lo.iloc[0]
    atr = tr[0]
    for x in tr[1 : t + 1]:
        atr = atr + (x - atr) / 14
    np.testing.assert_allclose(f["atr14"][s].iloc[t], atr, rtol=1e-5)

    # numeric frames are float32 except the exact price levels
    assert f["sma10"].dtypes.eq(np.float32).all()
    assert f["universe"].dtypes.eq(bool).all() and f["ep_universe"].dtypes.eq(bool).all()


def test_bars_since_high10_on_panel(cfg):
    c = np.full(N, 50.0)
    h = c * 1.01
    h[200] = 60.0  # spike
    panel = make_panel({"A": {"close": c, "high": h}})
    f = compute_features(panel, cfg)
    b = f["bars_since_high10"]["A"]
    assert b.iloc[200] == 0 and b.iloc[203] == 3 and b.iloc[209] == 9
    assert b.iloc[210] == 0  # spike left the window; ties -> most recent bar
    assert f["bars_since_high63"]["A"].iloc[205] == 5


# --------------------------------------------------------------------------- universe / RS
def test_universe_thresholds(cfg):
    n = N
    ok = np.linspace(20, 24, n)
    spec = {
        "OK": {"close": ok},
        "CHEAP": {"close": np.full(n, 4.0), "volume": np.full(n, 1e7)},  # price < 5
        "THIN": {"close": ok, "volume": np.full(n, 3e5)},  # ADV ~$6.6M: EP yes, main no
        "QUIET": {"close": ok, "high": ok * 1.01, "low": ok * 0.99},  # ADR ~2%
        "QQQ": {"close": ok},  # benchmark never tradable
        "SPY": {"close": ok},
    }
    young = np.full(n, np.nan)
    young[n - 100 :] = 30.0
    spec["YOUNG"] = {"close": young}  # 100 bars < 150
    panel = make_panel(spec)
    f = compute_features(panel, cfg)
    u, ep = f["universe"].iloc[-1], f["ep_universe"].iloc[-1]
    assert u.to_dict() == {
        "CHEAP": False, "OK": True, "QQQ": False, "QUIET": False, "SPY": False,
        "THIN": False, "YOUNG": False,
    }  # fmt: skip
    assert ep["THIN"] and ep["QUIET"] and ep["OK"]
    assert not ep["CHEAP"] and not ep["YOUNG"] and not ep["QQQ"] and not ep["SPY"]
    # history threshold is point-in-time: OK enters at its 150th bar
    assert not f["universe"]["OK"].iloc[148] and f["universe"]["OK"].iloc[149]


def test_history_length_restarts_after_a_long_gap():
    # F4: a ticker reused by a new issuer after > 20 missing sessions starts over; short
    # halts (<= 20 sessions) keep counting.
    v = np.r_[np.full(160, 10.0), np.full(30, np.nan), np.full(10, 12.0),
              np.full(20, np.nan), np.full(5, 13.0)]  # fmt: skip
    close = pd.DataFrame({"X": v}, index=pd.bdate_range("2016-01-04", periods=len(v)))
    h = history_length(close)["X"].to_numpy()
    assert h[159] == 160
    assert h[189] == 160  # carried through the gap (no new bars)
    assert h[190] == 1 and h[199] == 10  # restarted after the 30-session gap
    assert h[220] == 11 and h[224] == 15  # a 20-session gap does not restart it


def test_reused_ticker_does_not_inherit_old_history(cfg):
    n = N
    ok = np.linspace(20, 24, n)
    reused = np.full(n, np.nan)
    reused[:150] = 20.0  # old issuer: 150 bars
    reused[150 + 25 :] = np.linspace(30, 33, n - 175)  # new issuer after a 25-session gap
    spec = {"OK": {"close": ok}, "REUSE": {"close": reused}, "QQQ": {"close": ok},
            "SPY": {"close": ok}}  # fmt: skip
    f = compute_features(make_panel(spec), cfg)
    assert f["hist_len"]["REUSE"].iloc[-1] == n - 175
    assert not f["ep_universe"]["REUSE"].iloc[175:].any()  # old code: in from row ~195
    assert not f["universe"]["REUSE"].iloc[175:].any()


def test_rs_score_ranks_within_universe_only(cfg):
    n = N
    k = np.arange(n)
    spec = {f"U{i}": {"close": 20 * (1 + 0.001 * i) ** k} for i in range(10)}
    spec["OUT"] = {"close": 20 * 1.02**k, "volume": np.full(n, 1e2)}  # best returns, not liquid
    spec["QQQ"] = {"close": 100 * 1.03**k}
    panel = make_panel(spec)
    f = compute_features(panel, cfg)
    rs = f["rs_score"].iloc[-1]
    assert np.isnan(rs["OUT"]) and np.isnan(rs["QQQ"])
    np.testing.assert_allclose(rs[[f"U{i}" for i in range(10)]].to_numpy(), np.arange(1, 11) / 10)
    assert rs["U9"] == 1.0
    # rs_score is NaN wherever universe is False
    assert f["rs_score"].where(~f["universe"]).isna().all().all()


# --------------------------------------------------------------------------- regime
def test_risk_on_index_follows_qqq(cfg):
    n = N
    q = np.r_[100 * 1.002 ** np.arange(150), 100 * 1.002**149 * 0.99 ** np.arange(1, n - 149)]
    spec = {"A": {"close": np.full(n, 20.0)}, "QQQ": {"close": q}, "SPY": {"close": q}}
    f = compute_features(make_panel(spec), cfg)
    roi = f.risk_on_index
    assert roi.dtype == bool and roi.index.equals(make_panel(spec).dates)
    assert not roi.iloc[:19].any()  # SMA20 warmup
    assert roi.iloc[140] and not roi.iloc[-1]

    f2 = compute_features(make_panel({"A": {"close": np.full(n, 20.0)}}), cfg)
    assert not f2.risk_on_index.any()


def test_leaders_count_and_breadth(cfg):
    n = N
    k = np.arange(n)
    rising = 20 * 1.004**k  # ret63 ~ 28.6% < 30%: not a breadth leader
    spec = {
        "L1": {"close": 20 * 1.005**k},
        "L2": {"close": 20 * 1.006**k},
        "FLAT": {"close": np.full(n, 20.0)},
        "SLOW": {"close": rising},
        "ILLQ": {"close": 20 * 1.01**k, "volume": np.full(n, 1e3)},  # leader-like, not universe
        "QQQ": {"close": 20 * 1.01**k},  # benchmark, never counted
        "SPY": {"close": 20 * 1.01**k},
    }
    f = compute_features(make_panel(spec), cfg)
    lc = f.leaders_count
    assert lc.iloc[-1] == 2.0
    assert lc.iloc[100] == 0.0  # before min_history nobody is in the universe
    assert f.risk_on_breadth.dtype == bool
    assert not f.risk_on_breadth.iloc[:19].any()  # SMA20 warmup -> False
    assert f.risk_on_breadth.iloc[-1]  # 2 >= mean of last 20 counts (<= 2)


# --------------------------------------------------------------------------- causality
def test_features_are_causal_under_truncation(cfg):
    panel = make_panel(random_spec(n_sym=25, seed=7))
    full = compute_features(panel, cfg)
    for t in (150, 199):
        sub = DailyPanel(
            **{
                k: getattr(panel, k).iloc[: t + 1]
                for k in ("open", "high", "low", "close", "volume")
            }
        )
        part = compute_features(sub, cfg)
        for k in FEATURE_KEYS:
            pd.testing.assert_frame_equal(part[k], full[k].iloc[: t + 1], obj=k)
        pd.testing.assert_series_equal(part.risk_on_index, full.risk_on_index.iloc[: t + 1])
        pd.testing.assert_series_equal(part.leaders_count, full.leaders_count.iloc[: t + 1])
        pd.testing.assert_series_equal(part.risk_on_breadth, full.risk_on_breadth.iloc[: t + 1])


def test_features_ignore_open(cfg):
    panel = make_panel(random_spec(n_sym=10, seed=3))
    base = compute_features(panel, cfg)
    panel2 = DailyPanel(
        open=panel.open * 1.37,
        high=panel.high,
        low=panel.low,
        close=panel.close,
        volume=panel.volume,
    )
    other = compute_features(panel2, cfg)
    for k in FEATURE_KEYS:
        pd.testing.assert_frame_equal(base[k], other[k], obj=k)


def test_price_floor_uses_raw_close_not_split_adjusted():
    """A future 10:1 split makes adjusted history look like a $3 stock; the
    point-in-time universe must use the as-traded $30 close instead."""
    import numpy as np
    import pandas as pd

    from research.luk_strategy.config import Config
    from research.luk_strategy.features import compute_features
    from research.luk_strategy.models import DailyPanel

    idx = pd.bdate_range("2020-01-01", periods=200)
    cols = ["AAA", "SPY", "QQQ"]
    rng = np.random.default_rng(0)
    adj = pd.DataFrame(3.0 + rng.normal(0, 0.01, (200, 3)).cumsum(0), index=idx, columns=cols)
    vol = pd.DataFrame(1e8, index=idx, columns=cols)
    panel_kw = {"open": adj, "high": adj * 1.05, "low": adj * 0.99, "close": adj, "volume": vol}
    cfg = Config(min_history=20, min_adr=0.0, min_adv=0.0)
    no_raw = compute_features(DailyPanel(**panel_kw), cfg)
    assert not no_raw["universe"]["AAA"].any()
    with_raw = compute_features(DailyPanel(**panel_kw, raw_close=adj * 10), cfg)
    assert with_raw["universe"]["AAA"].iloc[-1]
