"""Offline tests for signals.py (SPEC §2, §5). Synthetic data only."""

from __future__ import annotations

import math
from dataclasses import replace

import numpy as np
import pandas as pd
import pytest

from research.luk_strategy import signals as S
from research.luk_strategy.config import Config
from research.luk_strategy.features import compute_features
from research.luk_strategy.models import Candidate, DailyPanel, Features

N = 220
T = N - 1  # session under test (last row); its pre-open row is J
J = N - 2
FIELDS = ("open", "high", "low", "close", "volume")


# --------------------------------------------------------------------------- builders
def make_panel(spec: dict[str, dict], n: int = N) -> DailyPanel:
    dates = pd.bdate_range("2016-01-04", periods=n)
    fields: dict[str, dict] = {f: {} for f in FIELDS}
    for s, d in spec.items():
        c = np.asarray(d["close"], dtype=float)
        fields["close"][s] = c
        fields["high"][s] = np.asarray(d.get("high", c * 1.02), dtype=float)
        fields["low"][s] = np.asarray(d.get("low", c * 0.98), dtype=float)
        fields["open"][s] = np.asarray(d.get("open", np.r_[c[0], c[:-1]]), dtype=float)
        fields["volume"][s] = np.asarray(d.get("volume", np.full(n, 2e6)), dtype=float)
    cols = sorted(spec)
    return DailyPanel(**{f: pd.DataFrame(v, index=dates)[cols] for f, v in fields.items()})


def copy_panel(panel: DailyPanel) -> DailyPanel:
    return DailyPanel(**{f: getattr(panel, f).copy() for f in FIELDS})


def fillers(n_fill: int = 40, seed: int = 0) -> dict[str, dict]:
    """Liquid, volatile, unremarkable stocks (form the RS cross-section) + benchmarks."""
    rng = np.random.default_rng(seed)
    out = {}
    for k in range(n_fill):
        r = -0.001 + 0.002 * k / n_fill + 0.005 * rng.standard_normal(N)
        out[f"F{k:02d}"] = {"close": 20 * np.exp(np.cumsum(r))}
    q = 100 * 1.001 ** np.arange(N)
    out["QQQ"] = {"close": q}
    out["SPY"] = {"close": q * 2}
    return out


def bo_series(slope=0.005, K=N - 6, cons=(0.990, 0.992, 0.989, 0.991), pivot_mult=1.03):
    """Uptrend to row K (pivot bar, high = 1.03 C), then a tight, slightly lower base."""
    k = np.arange(N)
    c = 10 * (1 + slope) ** np.minimum(k, K)
    C = c[K]
    m = N - 1 - K
    c[K + 1 :] = C * np.resize(np.asarray(cons), m)
    h, lo = c * 1.02, c * 0.98
    h[K] = C * pivot_mult
    h[K + 1 :] = c[K + 1 :] * 1.015
    lo[K + 1 :] = c[K + 1 :] * 0.985
    return {"close": c, "high": h, "low": lo}


def pb_series(slope=0.01, K=N - 5, pull=(0.985, 0.97, 0.955), vol_pull=1e6, low_mult=0.98):
    """Strong uptrend peaking at row K, then a 3-day light-volume pullback toward EMA9."""
    k = np.arange(N)
    c = 10 * (1 + slope) ** np.minimum(k, K)
    C = c[K]
    c[K + 1 : K + 4] = C * np.asarray(pull)
    h, lo = c * 1.02, c * 0.98
    h[K + 1 : K + 4] = c[K + 1 : K + 4] * 1.01
    lo[K + 1 : K + 4] = c[K + 1 : K + 4] * low_mult
    v = np.full(N, 2e6)
    v[K + 1 : K + 4] = vol_pull
    c[K + 4 :] = c[K + 3]
    h[K + 4 :], lo[K + 4 :] = c[K + 4 :] * 1.01, c[K + 4 :] * 0.99
    return {"close": c, "high": h, "low": lo, "volume": v}


def cfg_for(panel: DailyPanel, **kw) -> Config:
    return Config(start=panel.dates[0].date(), **kw)


def build(panel: DailyPanel, cfg: Config):
    feats = compute_features(panel, cfg)
    return feats, S.build_watchlists(panel, feats, cfg)


def day(panel: DailyPanel, i: int):
    return panel.dates[i].date()


def by_setup(cands: list[Candidate], setup: str) -> list[str]:
    return [c.symbol for c in cands if c.setup == setup]


def norm(c: Candidate, drop: tuple[str, ...] = ()) -> tuple:
    """Hashable, NaN-safe view of a candidate (optionally without some fields)."""

    def fx(v):
        if isinstance(v, float) and math.isnan(v):
            return "nan"
        return v

    d = {k: fx(v) for k, v in vars(c).items() if k not in drop and k != "meta"}
    meta = {k: fx(v) for k, v in c.meta.items() if f"meta.{k}" not in drop}
    return tuple(sorted(d.items())) + tuple(sorted(meta.items()))


# --------------------------------------------------------------------------- BO
def test_bo_fires_with_pre_open_fields():
    spec = fillers()
    spec["BOX"] = bo_series()
    panel = make_panel(spec)
    cfg = cfg_for(panel)
    feats, wl = build(panel, cfg)
    cands = [c for c in wl[day(panel, T)] if c.symbol == "BOX" and c.setup == "BO"]
    assert len(cands) == 1
    c = cands[0]
    C_K = panel.close["BOX"].iloc[N - 6]
    assert c.level == pytest.approx(C_K * 1.03) and c.level == feats["high10"]["BOX"].iloc[J]
    assert c.level == panel.high["BOX"].iloc[N - 6]  # pivot is an exact traded high
    assert c.meta["pivot"] == c.level
    assert c.prev_close == panel.close["BOX"].iloc[J]
    assert c.prev_high == panel.high["BOX"].iloc[J]
    assert c.prev_low == panel.low["BOX"].iloc[J]
    assert c.open_today == panel.open["BOX"].iloc[T]
    assert c.adr == float(feats["adr20"]["BOX"].iloc[J])
    assert c.atr == float(feats["atr14"]["BOX"].iloc[J])
    assert c.adv_shares == float(feats["adv20_shares"]["BOX"].iloc[J])
    assert c.rank_key == float(feats["rs_score"]["BOX"].iloc[J]) == c.meta["rs_score"]
    assert c.meta["ret63"] == pytest.approx(float(feats["ret63"]["BOX"].iloc[J]))
    assert c.meta["gap"] == pytest.approx(c.open_today / c.prev_close - 1)
    assert c.risk_on_index == bool(feats.risk_on_index.iloc[J])
    assert c.risk_on_breadth == bool(feats.risk_on_breadth.iloc[J])
    assert c.day == day(panel, T)
    # the fillers never qualify as leaders/BO
    assert by_setup(wl[day(panel, T)], "BO") == ["BOX"]


@pytest.mark.parametrize(
    "variant",
    [
        {"K": N - 3},  # pivot set at t-2: only 1 quiet session after it
        {"slope": 0.001},  # not a leader: max return < 30%
        {"cons": (0.990, 0.992, 0.989, 0.88)},  # close at t-1 far below the pivot
        {"pivot_mult": 1.10},  # range5 > 2 x ADR and close < pivot x (1 - 1.5 ADR)
    ],
)
def test_bo_not_fired_when_a_condition_breaks(variant):
    spec = fillers()
    spec["BOX"] = bo_series(**variant)
    panel = make_panel(spec)
    _, wl = build(panel, cfg_for(panel))
    assert "BOX" not in by_setup(wl[day(panel, T)], "BO")


def test_bo_range_condition_alone():
    """Widen only the t-1 bar's low so range5 > 2 x ADR while everything else holds."""
    spec = fillers()
    spec["BOX"] = bo_series()
    panel = make_panel(spec)
    cfg = cfg_for(panel)
    feats = compute_features(panel, cfg)
    assert S.bo_mask(panel, feats, cfg)["BOX"].iloc[J]
    panel.low.iloc[J, panel.symbols.index("BOX")] *= 0.92
    feats = compute_features(panel, cfg)
    assert feats["range5"]["BOX"].iloc[J] > 2 * feats["adr20"]["BOX"].iloc[J]
    assert not S.bo_mask(panel, feats, cfg)["BOX"].iloc[J]
    assert S.leader_mask(feats, cfg)["BOX"].iloc[J]


# --------------------------------------------------------------------------- PB
def test_pb_fires_with_level_prev_high():
    spec = fillers()
    spec["PBX"] = pb_series()
    panel = make_panel(spec)
    feats, wl = build(panel, cfg_for(panel))
    cands = [c for c in wl[day(panel, T)] if c.symbol == "PBX" and c.setup == "PB"]
    assert len(cands) == 1
    c = cands[0]
    assert c.level == panel.high["PBX"].iloc[J] == c.prev_high == c.meta["pivot"]
    assert c.rank_key == float(feats["rs_score"]["PBX"].iloc[J])
    assert by_setup(wl[day(panel, T)], "PB") == ["PBX"]


@pytest.mark.parametrize(
    "variant",
    [
        {"vol_pull": 3e6},  # pullback on heavy volume (>= ADV20 shares)
        {"pull": (0.95, 0.90, 0.85)},  # too deep: close < 0.97 x EMA21
        {"pull": (1.0, 1.0, 0.999), "low_mult": 0.998},  # low never comes near EMA9
        {"slope": 0.001},  # not a leader
    ],
)
def test_pb_not_fired_when_a_condition_breaks(variant):
    spec = fillers()
    spec["PBX"] = pb_series(**variant)
    panel = make_panel(spec)
    _, wl = build(panel, cfg_for(panel))
    assert "PBX" not in by_setup(wl[day(panel, T)], "PB")


def test_pb_requires_close_below_prior_five_closes():
    spec = fillers()
    spec["PBX"] = pb_series()
    panel = make_panel(spec)
    cfg = cfg_for(panel)
    feats = compute_features(panel, cfg)
    assert S.pb_mask(panel, feats, cfg)["PBX"].iloc[J]
    # At the peak row the close is the highest of the last 6 -> never a PB row.
    assert not S.pb_mask(panel, feats, cfg)["PBX"].iloc[N - 5]


def test_pb_requires_fresh_high63():
    """Peak 20 sessions before t-1 (stale leader): the 15-session freshness test fails."""
    spec = fillers()
    pb = pb_series(K=N - 22)
    spec["PBX"] = pb
    panel = make_panel(spec)
    cfg = cfg_for(panel)
    feats = compute_features(panel, cfg)
    mask = S.pb_mask(panel, feats, cfg)["PBX"]
    assert mask.iloc[N - 22 + 3]  # the fresh pullback row itself qualifies
    assert feats["bars_since_high63"]["PBX"].iloc[J] >= 15
    assert not mask.iloc[J]


# --------------------------------------------------------------------------- EP
def _ep_panel(gaps: dict[str, float], extra: dict | None = None) -> DailyPanel:
    spec = fillers()
    for s, g in gaps.items():
        c = 20 * 1.0005 ** np.arange(N)
        o = np.r_[c[0], c[:-1]]
        o[T] = c[J] * (1 + g)
        spec[s] = {"close": c, "open": o}
    spec.update(extra or {})
    return make_panel(spec)


def test_ep_gap_threshold_and_fields():
    thin_c = 20 * 1.0005 ** np.arange(N)
    thin_o = np.r_[thin_c[0], thin_c[:-1]]
    thin_o[T] = thin_c[J] * 1.3
    panel = _ep_panel(
        {"EPX": 0.15, "SMALL": 0.05},
        extra={"THIN": {"close": thin_c, "open": thin_o, "volume": np.full(N, 1e5)}},
    )
    feats, wl = build(panel, cfg_for(panel))
    eps = [c for c in wl[day(panel, T)] if c.setup == "EP"]
    assert [c.symbol for c in eps] == ["EPX"]
    c = eps[0]
    assert c.level is None and c.meta["pivot"] is None
    assert c.rank_key == pytest.approx(0.15) and c.meta["gap"] == pytest.approx(0.15)
    assert c.open_today == panel.open["EPX"].iloc[T]
    assert c.prev_close == panel.close["EPX"].iloc[J]
    assert c.meta["rs_score"] == pytest.approx(float(feats["rs_score"]["EPX"].iloc[J]))
    # no EP on the previous day (no gap there)
    assert by_setup(wl[day(panel, J)], "EP") == []


def test_ep_gap_rechecked_on_raw_prices_when_adjustment_factor_moves():
    # LA-4: Alpaca books some spin-offs as splits (XRX 2017-01-03: adjusted +13.6%, raw
    # -25%). When raw/adjusted changes between close_{t-1} and open_t, the raw gap must pass.
    gaps = {"REAL": 0.15, "SPIN": 0.15, "SPLITUP": 0.15, "NORAW": 0.15}
    panel = _ep_panel(gaps)
    _, wl = build(panel, cfg_for(panel))
    assert sorted(by_setup(wl[day(panel, T)], "EP")) == sorted(gaps)  # no raw data: unchanged
    raw_c, raw_o = panel.close * 3.0, panel.open * 3.0  # constant factor
    raw_o.loc[raw_o.index[T], "SPIN"] = raw_c["SPIN"].iloc[J] * 0.75  # raw gap -25%
    raw_o.loc[raw_o.index[T], "SPLITUP"] = raw_c["SPLITUP"].iloc[J] * 1.12  # factor moves, raw +12%
    raw_o.loc[raw_o.index[T], "NORAW"] = np.nan  # no raw row: no verdict
    panel2 = replace(panel, raw_close=raw_c, raw_open=raw_o)
    _, wl2 = build(panel2, cfg_for(panel2))
    eps = [c for c in wl2[day(panel2, T)] if c.setup == "EP"]
    assert sorted(c.symbol for c in eps) == ["NORAW", "REAL", "SPLITUP"]
    assert all(c.rank_key == pytest.approx(0.15) for c in eps)  # ranking keeps the adjusted gap


def test_candidate_tick_is_one_as_traded_cent_at_t_minus_1():
    # LA-6/F3: adjusted 0.01 would be $0.40 as traded after a later 40:1 split.
    panel = _ep_panel({"EPX": 0.15})
    _, wl = build(panel, cfg_for(panel))
    (c,) = [c for c in wl[day(panel, T)] if c.setup == "EP"]
    assert c.tick == pytest.approx(0.01)  # no raw cache -> one cent
    raw_c = panel.close * 40.0
    raw_c.loc[raw_c.index[J], "EPX"] = panel.close["EPX"].iloc[J] * 20.0  # factor at t-1 only
    panel2 = replace(panel, raw_close=raw_c)
    _, wl2 = build(panel2, cfg_for(panel2))
    (c2,) = [c for c in wl2[day(panel2, T)] if c.setup == "EP"]
    assert c2.tick == pytest.approx(0.01 / 20.0)


def test_ep_neglect_filter():
    hot = bo_series()  # ret63 > 30% at t-1
    o = np.r_[hot["close"][0], hot["close"][:-1]]
    o[T] = hot["close"][J] * 1.2
    hot["open"] = o
    panel = _ep_panel({"COLD": 0.12}, extra={"HOT": hot})
    _, wl = build(panel, cfg_for(panel))
    assert sorted(by_setup(wl[day(panel, T)], "EP")) == ["COLD", "HOT"]
    _, wl2 = build(panel, cfg_for(panel, ep_require_neglect=True))
    assert by_setup(wl2[day(panel, T)], "EP") == ["COLD"]


# --------------------------------------------------------------------------- caps / ranking
def test_ep_cap_keeps_largest_gaps_in_order():
    gaps = {f"G{i:02d}": 0.11 + 0.01 * i for i in range(20)}
    panel = _ep_panel(gaps)
    _, wl = build(panel, cfg_for(panel, ep_max_candidates=5))
    eps = [c for c in wl[day(panel, T)] if c.setup == "EP"]
    assert [c.symbol for c in eps] == ["G19", "G18", "G17", "G16", "G15"]
    assert [c.rank_key for c in eps] == sorted((c.rank_key for c in eps), reverse=True)
    _, wl0 = build(panel, cfg_for(panel, ep_max_candidates=0))
    assert by_setup(wl0[day(panel, T)], "EP") == []


def test_bo_pb_caps_rank_by_rs_score():
    spec = fillers()
    for i in range(5):
        spec[f"B{i}"] = bo_series(slope=0.005 + 0.0005 * i)
        spec[f"P{i}"] = pb_series(slope=0.008 + 0.0005 * i)
    panel = make_panel(spec)
    cfg = cfg_for(panel, leader_rs=0.0, bo_max_candidates=3, pb_max_candidates=2)
    feats, wl = build(panel, cfg)
    cands = wl[day(panel, T)]
    bo = [c for c in cands if c.setup == "BO"]
    pb = [c for c in cands if c.setup == "PB"]
    # without caps every one of them qualifies
    full = S.build_watchlists(
        panel, feats, replace(cfg, bo_max_candidates=99, pb_max_candidates=99)
    )
    assert {f"B{i}" for i in range(5)} <= set(by_setup(full[day(panel, T)], "BO"))
    assert {f"P{i}" for i in range(5)} <= set(by_setup(full[day(panel, T)], "PB"))
    rs = feats["rs_score"].iloc[J]
    exp_bo = rs[by_setup(full[day(panel, T)], "BO")].sort_values(ascending=False, kind="stable")
    assert [c.symbol for c in bo] == list(exp_bo.index[:3])
    assert [c.rank_key for c in pb] == sorted((c.rank_key for c in pb), reverse=True)
    assert len(pb) == 2
    exp_pb = rs[by_setup(full[day(panel, T)], "PB")].sort_values(ascending=False, kind="stable")
    assert [c.symbol for c in pb] == list(exp_pb.index[:2])


def test_rank_ties_break_by_symbol():
    syms = np.asarray(["ZZ", "AA", "MM", "BB", "CC"], dtype=object)
    keys = np.array([0.9, 0.9, 1.0, 0.5, 0.9], dtype=np.float32)
    got = S._top(np.arange(5), keys, syms, 4)
    assert [syms[k] for k in got] == ["MM", "AA", "CC", "ZZ"]
    assert S._top(np.arange(5), keys, syms, 0).size == 0


def test_setups_always_built_and_ordered():
    spec = fillers()
    spec["BOX"] = bo_series()
    spec["PBX"] = pb_series()
    c = 20 * 1.0005 ** np.arange(N)
    o = np.r_[c[0], c[:-1]]
    o[T] = c[J] * 1.2
    spec["EPX"] = {"close": c, "open": o}
    panel = make_panel(spec)
    _, wl = build(panel, cfg_for(panel, setups=("EP",)))
    cands = wl[day(panel, T)]
    assert [(c.setup, c.symbol) for c in cands] == [("EP", "EPX"), ("BO", "BOX"), ("PB", "PBX")]


# --------------------------------------------------------------------------- regime flags
def test_regime_flags_are_read_at_t_minus_1():
    spec = fillers()
    spec["BOX"] = bo_series()
    panel = make_panel(spec)
    cfg = cfg_for(panel)
    feats = compute_features(panel, cfg)
    dates = panel.dates
    for prev_flag in (True, False):
        roi = pd.Series(not prev_flag, index=dates)
        roi.iloc[J] = prev_flag
        rob = pd.Series(prev_flag, index=dates)
        rob.iloc[J] = not prev_flag
        f2 = Features(frames=feats.frames, risk_on_index=roi, leaders_count=feats.leaders_count,
                      risk_on_breadth=rob)  # fmt: skip
        wl = S.build_watchlists(panel, f2, cfg)
        (c,) = [c for c in wl[day(panel, T)] if c.symbol == "BOX"]
        assert c.risk_on_index is prev_flag
        assert c.risk_on_breadth is (not prev_flag)


# --------------------------------------------------------------------------- window / manifest
def test_window_and_manifest():
    spec = fillers()
    spec["BOX"] = bo_series()
    spec["PBX"] = pb_series()
    panel = make_panel(spec)
    cfg = Config(start=day(panel, 0), end=day(panel, T))
    feats, wl = build(panel, cfg)
    assert list(wl) == [d.date() for d in panel.dates]
    assert wl[day(panel, 0)] == []  # no pre-open history for the first row
    cfg2 = Config(start=day(panel, 200), end=day(panel, 210))
    wl2 = S.build_watchlists(panel, feats, cfg2)
    assert list(wl2) == [d.date() for d in panel.dates[200:211]]
    for d in wl2:
        assert [norm(c) for c in wl2[d]] == [norm(c) for c in wl[d]]

    # same symbol in two setups appears once in the manifest
    fake = {
        day(panel, 5): [replace(wl[day(panel, T)][0], setup="EP"), wl[day(panel, T)][0]],
        day(panel, 6): [],
    }
    man = S.intraday_manifest(fake)
    assert man == {day(panel, 5): [wl[day(panel, T)][0].symbol]}
    man_full = S.intraday_manifest(wl)
    assert man_full[day(panel, T)] == ["BOX", "PBX"]
    assert all(v == sorted(set(v)) and v for v in man_full.values())

    df = S.watchlists_to_frame(wl)
    assert set(df["setup"]) <= {"EP", "BO", "PB"} and len(df) == sum(map(len, wl.values()))


# --------------------------------------------------------------------------- look-ahead
def _lookahead_panel(seed: int = 11) -> DailyPanel:
    rng = np.random.default_rng(seed)
    spec = fillers(seed=seed)
    for k in range(120):
        r = 0.003 * rng.standard_normal() + 0.025 * rng.standard_normal(N)
        c = 20 * np.exp(np.cumsum(r))
        hi = c * (1 + rng.uniform(0.0, 0.05, N))
        lo = c * (1 - rng.uniform(0.0, 0.05, N))
        o = np.r_[c[0], c[:-1]] * (1 + 0.04 * rng.standard_normal(N))
        spec[f"R{k:03d}"] = {
            "close": c,
            "high": hi,
            "low": lo,
            "open": o,
            "volume": rng.uniform(5e5, 4e6, N),
        }
    spec["BOX"] = bo_series()
    spec["PBX"] = pb_series()
    return make_panel(spec)


LOOKAHEAD_CFG = {"leader_rs": 0.5, "leader_min_ret": 0.0, "min_adr": 0.0, "ep_min_gap": 0.03}


def test_lookahead_day_t_hlcv_never_changes_day_t_watchlist():
    panel = _lookahead_panel()
    cfg = cfg_for(panel, **LOOKAHEAD_CFG)
    feats, base = build(panel, cfg)
    rng = np.random.default_rng(5)
    tested = {"EP": 0, "BO": 0, "PB": 0}
    for t in (170, 185, 200, 213, T):
        pert = copy_panel(panel)
        shape = pert.close.shape[1]
        for f in ("high", "low", "close", "volume"):
            getattr(pert, f).iloc[t] *= rng.uniform(0.3, 2.0, shape)
        f2, wl2 = build(pert, cfg)
        # sanity: the perturbation really changed day-t features
        assert not np.allclose(f2["sma10"].iloc[t], feats["sma10"].iloc[t], equal_nan=True)
        for i in range(1, t + 1):
            d = day(panel, i)
            assert [norm(c) for c in wl2[d]] == [norm(c) for c in base[d]], (t, d)
        for c in base[day(panel, t)]:
            tested[c.setup] += 1
    assert all(v > 0 for v in tested.values()), tested


def test_lookahead_day_t_open_only_changes_ep():
    panel = _lookahead_panel()
    cfg = cfg_for(panel, **LOOKAHEAD_CFG)
    feats, base = build(panel, cfg)
    rng = np.random.default_rng(9)
    ep_changed = 0
    for t in (185, 200, T):
        pert = copy_panel(panel)
        n_sym = pert.open.shape[1]
        pert.open.iloc[t] = pert.close.iloc[t - 1].to_numpy() * rng.uniform(0.85, 1.25, n_sym)
        # force one EP-eligible symbol that was not an EP to gap +50%
        elig = feats["ep_universe"].iloc[t - 1]
        old_ep = set(by_setup(base[day(panel, t)], "EP"))
        newcomer = next(s for s in panel.symbols if elig[s] and s not in old_ep)
        pert.open.iloc[t, panel.symbols.index(newcomer)] = panel.close[newcomer].iloc[t - 1] * 1.5
        _, wl2 = build(pert, cfg)
        d = day(panel, t)
        drop = ("open_today", "meta.gap")
        for setup in ("BO", "PB"):
            a = [norm(c, drop) for c in base[d] if c.setup == setup]
            b = [norm(c, drop) for c in wl2[d] if c.setup == setup]
            assert a == b and a, (t, setup)
        assert newcomer in by_setup(wl2[d], "EP")
        ep_changed += by_setup(wl2[d], "EP") != by_setup(base[d], "EP")
        for i in range(1, t):  # earlier days untouched
            assert [norm(c) for c in wl2[day(panel, i)]] == [norm(c) for c in base[day(panel, i)]]
    assert ep_changed == 3


def test_day_t_watchlist_from_open_only_information_set():
    """Rebuild day t from a panel that ends at t and knows only open_t on row t."""
    panel = _lookahead_panel(seed=21)
    cfg = cfg_for(panel, **LOOKAHEAD_CFG)
    _, base = build(panel, cfg)
    for t in (190, T):
        sub = DailyPanel(**{f: getattr(panel, f).iloc[: t + 1].copy() for f in FIELDS})
        for f in ("high", "low", "close", "volume"):
            getattr(sub, f).iloc[t] = np.nan
        _, wl = build(sub, cfg)
        d = day(panel, t)
        assert base[d] and [norm(c) for c in wl[d]] == [norm(c) for c in base[d]]


def test_day_t_watchlist_depends_on_t_minus_1():
    """Sensitivity check for the shift itself: breaking the t-1 bar removes the BO."""
    spec = fillers()
    spec["BOX"] = bo_series()
    panel = make_panel(spec)
    cfg = cfg_for(panel)
    _, wl = build(panel, cfg)
    assert "BOX" in by_setup(wl[day(panel, T)], "BO")
    pert = copy_panel(panel)
    pert.close.iloc[J, panel.symbols.index("BOX")] *= 0.85  # t-1 close collapses
    _, wl2 = build(pert, cfg)
    assert "BOX" not in by_setup(wl2[day(panel, T)], "BO")
