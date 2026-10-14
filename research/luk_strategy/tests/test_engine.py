"""Offline tests for the portfolio engine (SPEC §6-§8).

Most tests replace the engine's references to the intraday helpers with tiny
fakes driven by ``Candidate.meta`` (fill/stop/minute/exit/skip), so engine
logic is tested independently of trigger logic. Two smoke tests at the end run
the real ``intraday`` functions to check the wiring.
"""

from __future__ import annotations

import json
import math
from datetime import date

import numpy as np
import pandas as pd
import pytest

from research.luk_strategy import engine
from research.luk_strategy.config import Config
from research.luk_strategy.models import Candidate, DailyPanel, Features, SkipEvent, Trigger

ET = "America/New_York"
DATES = pd.bdate_range("2021-01-04", periods=12)  # tz-naive sessions
BASE = Config(start=DATES[0].date(), entry_mode="daily", slip_bps=0.0)


def day(k: int) -> date:
    return DATES[k].date()


def ts(d: date, minute: int) -> pd.Timestamp:
    return pd.Timestamp(d.year, d.month, d.day, 9, 30, tz=ET) + pd.Timedelta(minutes=minute)


# --------------------------------------------------------------------------- builders
class PanelBuilder:
    """Quiet default bars: open 100, high 101, low 99.5, close 100."""

    def __init__(self, symbols: list[str]):
        def frame(v: float) -> pd.DataFrame:
            return pd.DataFrame(v, index=DATES, columns=symbols, dtype="float64")

        self.f = {
            "open": frame(100.0),
            "high": frame(101.0),
            "low": frame(99.5),
            "close": frame(100.0),
            "volume": frame(1e6),
        }
        self.trail = frame(0.0)  # trail MA far below price -> never trails by default

    def bar(self, sym: str, k: int, o=None, h=None, lo=None, c=None) -> PanelBuilder:
        for name, v in (("open", o), ("high", h), ("low", lo), ("close", c)):
            if v is not None:
                self.f[name].iloc[k, self.f[name].columns.get_loc(sym)] = v
        return self

    def nan(self, sym: str, ks) -> PanelBuilder:
        for name in self.f:
            for k in ks:
                self.f[name].iloc[k, self.f[name].columns.get_loc(sym)] = np.nan
        return self

    def panel(self) -> DailyPanel:
        return DailyPanel(**{k: v.copy() for k, v in self.f.items()})

    def feats(self) -> Features:
        flags = pd.Series(True, index=DATES)
        return Features(
            frames={"sma10": self.trail.copy(), "sma20": self.trail.copy()},
            risk_on_index=flags,
            leaders_count=pd.Series(10.0, index=DATES),
            risk_on_breadth=flags,
        )


def cand(
    k: int, sym: str, setup: str = "BO", rank: float = 0.9, ro_index=True, ro_breadth=True, **meta
) -> Candidate:
    return Candidate(
        day=day(k),
        symbol=sym,
        setup=setup,
        level=100.0 if setup != "EP" else None,
        prev_close=100.0,
        prev_high=101.0,
        prev_low=99.0,
        open_today=100.0,
        adr=0.05,
        atr=2.0,
        adv_shares=1e6,
        rank_key=rank,
        risk_on_index=ro_index,
        risk_on_breadth=ro_breadth,
        meta=dict(meta),
    )


def watch(*cands: Candidate) -> dict[date, list[Candidate]]:
    out: dict[date, list[Candidate]] = {}
    for c in cands:
        out.setdefault(c.day, []).append(c)
    return out


def _fake_trigger(c: Candidate) -> Trigger | SkipEvent:
    m = c.meta
    if "skip" in m:
        return SkipEvent(day=c.day, symbol=c.symbol, setup=c.setup, reason=m["skip"])
    minute = m.get("minute", 5)
    return Trigger(
        candidate=c,
        ts=ts(c.day, minute),
        fill=m.get("fill", 100.0),
        stop=m.get("stop", 95.0),
        bar_pos=minute // 5,
    )


@pytest.fixture
def fakes(monkeypatch):
    calls: dict[str, list] = {"find_trigger": [], "find_trigger_daily": [], "entry_day_exit": []}

    def find_trigger(c, bars, cfg):
        calls["find_trigger"].append((c, bars))
        if bars is None or len(bars) == 0:
            return SkipEvent(day=c.day, symbol=c.symbol, setup=c.setup, reason="no_data")
        return _fake_trigger(c)

    def find_trigger_daily(c, day_bar, cfg):
        calls["find_trigger_daily"].append((c, day_bar))
        return _fake_trigger(c)

    def entry_day_exit(trig, bars, day_bar, cfg):
        calls["entry_day_exit"].append((trig, bars, day_bar))
        ex = trig.candidate.meta.get("exit")
        return None if ex is None else (None, ex[0], ex[1])

    def contract_target(trig, cfg):
        return trig.fill + cfg.contract_target_r * (trig.fill - trig.stop)

    monkeypatch.setattr(engine, "find_trigger", find_trigger)
    monkeypatch.setattr(engine, "find_trigger_daily", find_trigger_daily)
    monkeypatch.setattr(engine, "entry_day_exit", entry_day_exit)
    monkeypatch.setattr(engine, "contract_target", contract_target)
    return calls


def run(pb: PanelBuilder, wl, cfg: Config = BASE, store=None):
    return engine.run_backtest(pb.panel(), pb.feats(), wl, cfg, intraday_store=store)


def trade(res, sym: str) -> pd.Series:
    t = res.trades[res.trades["symbol"] == sym]
    assert len(t) == 1, f"expected one trade for {sym}, got {len(t)}"
    return t.iloc[0]


def skip_reasons(res, sym: str | None = None) -> list[str]:
    s = res.skips if sym is None else res.skips[res.skips["symbol"] == sym]
    return list(s["reason"])


def bars_store(symbols: list[str]):
    """Intraday store returning a non-empty dummy frame for each symbol (fakes ignore it)."""

    def store(d: date):
        return {
            s: pd.DataFrame(
                {
                    "symbol": [s],
                    "ts": [ts(d, 0)],
                    "open": [100.0],
                    "high": [101.0],
                    "low": [99.0],
                    "close": [100.0],
                    "volume": [1e5],
                }
            )
            for s in symbols
        }

    return store


# --------------------------------------------------------------------------- exits (luk)
def test_gap_down_through_stop_exits_at_open_with_slippage(fakes):
    pb = PanelBuilder(["A"]).bar("A", 1, o=90.0, h=92.0, lo=89.0, c=91.0)
    res = run(pb, watch(cand(0, "A")), BASE.with_overrides(slip_bps=10.0))
    t = trade(res, "A")
    assert t["qty"] == 100  # 0.5% * 100k / (100 - 95)
    assert t["exit_reason"] == "stop"
    assert t["exit_date"] == DATES[1]
    assert t["exit_price_avg"] == pytest.approx(90.0 * 0.999)
    assert t["pnl"] == pytest.approx(100 * (89.91 - 100.0))
    assert t["r_multiple"] == pytest.approx(t["pnl"] / 500.0)
    assert t["hold_sessions"] == 1
    legs = json.loads(t["legs"])
    assert legs == [{"date": day(1).isoformat(), "qty": 100, "price": pytest.approx(89.91), "reason": "stop"}]


def test_daily_low_through_stop_exits_at_stop(fakes):
    pb = PanelBuilder(["A"]).bar("A", 1, o=99.0, h=100.0, lo=94.0, c=96.0)
    t = trade(run(pb, watch(cand(0, "A"))), "A")
    assert (t["exit_reason"], t["exit_price_avg"], t["pnl"]) == ("stop", 95.0, -500.0)
    assert t["r_multiple"] == pytest.approx(-1.0)
    assert t["return_pct"] == pytest.approx(-0.05)


def test_partial_then_breakeven_stop(fakes):
    pb = (
        PanelBuilder(["A"])
        .bar("A", 3, h=106.0, c=105.0)  # partial day (3rd session after entry)
        .bar("A", 4, o=101.0, lo=99.0, c=100.5)  # breakeven stop hit intraday
    )
    t = trade(run(pb, watch(cand(0, "A"))), "A")
    legs = json.loads(t["legs"])
    assert [(leg["qty"], leg["price"], leg["reason"]) for leg in legs] == [
        (33, 105.0, "partial"),
        (67, 100.0, "stop"),
    ]
    assert t["exit_reason"] == "stop"
    assert t["exit_date"] == DATES[4]
    assert t["hold_sessions"] == 4
    assert t["pnl"] == pytest.approx(33 * 5.0)


def test_no_partial_or_breakeven_when_close_not_above_entry(fakes):
    pb = PanelBuilder(["A"]).bar("A", 3, c=100.0).bar("A", 4, lo=99.0)  # 99 > stop 95
    t = trade(run(pb, watch(cand(0, "A"))), "A")
    assert t["exit_reason"] == "end_of_test"
    assert len(json.loads(t["legs"])) == 1


def test_partial_taken_at_first_bar_after_a_missing_partial_day(fakes):
    # F8: no bar on the 3rd session after entry -> the partial/breakeven rule runs at the
    # next session with a bar instead of being skipped for good.
    pb = (
        PanelBuilder(["A"])
        .nan("A", [3])
        .bar("A", 4, h=106.0, c=105.0)
        .bar("A", 5, o=101.0, lo=99.0, c=100.5)
    )
    t = trade(run(pb, watch(cand(0, "A"))), "A")
    legs = json.loads(t["legs"])
    assert [(leg["date"], leg["qty"], leg["price"], leg["reason"]) for leg in legs] == [
        (day(4).isoformat(), 33, 105.0, "partial"),
        (day(5).isoformat(), 67, 100.0, "stop"),
    ]


def test_partial_rule_is_evaluated_only_once(fakes):
    # Close not above entry on the partial day -> no partial later either (unchanged rule).
    pb = PanelBuilder(["A"]).bar("A", 3, c=100.0).bar("A", 4, h=106.0, c=105.0)
    t = trade(run(pb, watch(cand(0, "A"))), "A")
    assert [leg["reason"] for leg in json.loads(t["legs"])] == ["end_of_test"]


def test_partial_needs_remaining_shares_but_still_raises_breakeven(fakes):
    # 1 share: no partial is possible, the breakeven raise still applies.
    pb = PanelBuilder(["A"]).bar("A", 3, c=105.0).bar("A", 4, lo=99.0)
    cfg = BASE.with_overrides(risk_per_trade=5.0 / 100_000)  # $5 risk / $5 per share -> 1
    t = trade(run(pb, watch(cand(0, "A")), cfg), "A")
    assert t["qty"] == 1
    assert [leg["reason"] for leg in json.loads(t["legs"])] == ["stop"]
    assert t["exit_price_avg"] == 100.0


def test_trail_exit_at_close_not_on_entry_day_day1_ablation(fakes):
    pb = PanelBuilder(["A"]).bar("A", 2, lo=98.5, c=99.0)
    pb.trail.loc[DATES[0], "A"] = 500.0  # entry day: trail rule must not apply
    pb.trail.loc[DATES[2], "A"] = 99.5
    cfg = BASE.with_overrides(trail_after_partial=False)
    t = trade(run(pb, watch(cand(0, "A")), cfg), "A")
    assert (t["exit_reason"], t["exit_date"], t["exit_price_avg"]) == ("trail", DATES[2], 99.0)


def test_trail_starts_at_partial_day_by_default(fakes):
    # Qullamaggie/Luk order: only the stop applies until the partial session; the MA trail
    # manages the position from then on (Fidelity F1).
    pb = PanelBuilder(["A"]).bar("A", 1, c=99.0).bar("A", 2, c=99.0).bar("A", 3, c=99.0)
    for k in (1, 2, 3):
        pb.trail.loc[DATES[k], "A"] = 99.5  # close below the MA on sessions 1-3
    t = trade(run(pb, watch(cand(0, "A"))), "A")
    assert (t["exit_reason"], t["exit_date"], t["hold_sessions"]) == ("trail", DATES[3], 3)


def test_trail_ma_selection(fakes):
    pb = PanelBuilder(["A"])
    pb.trail.loc[DATES[3], "A"] = 100.5  # applied to both sma10/sma20 frames
    res = run(pb, watch(cand(0, "A")), BASE.with_overrides(trail_ma="sma20"))
    assert trade(res, "A")["exit_reason"] == "trail"
    with pytest.raises(KeyError):
        run(pb, watch(cand(0, "A")), BASE.with_overrides(trail_ma="ema21"))


def test_time_stop(fakes):
    pb = PanelBuilder(["A"]).bar("A", 4, c=102.0)
    t = trade(run(pb, watch(cand(0, "A")), BASE.with_overrides(max_hold=4)), "A")
    assert (t["exit_reason"], t["hold_sessions"], t["pnl"]) == ("time", 4, 200.0)


def test_data_end_waits_for_the_gap_counter_not_the_panel_end(fakes):
    # LA-5: A's data ends after day 2. On day 3 the engine cannot know the gap is permanent,
    # so the position (and its slot) is held until data_gap_sessions (5) missing bars.
    pb = PanelBuilder(["A", "B"]).bar("A", 2, c=103.0).nan("A", range(3, len(DATES)))
    cfg = BASE.with_overrides(max_open_positions=1)
    res = run(pb, watch(cand(0, "A"), cand(4, "B")), cfg)
    t = trade(res, "A")
    assert (t["exit_reason"], t["exit_date"], t["exit_price_avg"]) == ("data_end", DATES[7], 103.0)
    assert res.equity.loc[DATES[3], "n_positions"] == 1
    assert res.equity.loc[DATES[7], "equity"] == pytest.approx(100_000 + 300.0)
    assert skip_reasons(res, "B") == ["cap_open"]  # slot not freed early


def test_data_gap_sessions_and_mark_kept_while_missing(fakes):
    pb = PanelBuilder(["A"]).bar("A", 1, c=102.0).nan("A", [2, 3])
    res = run(pb, watch(cand(0, "A")), BASE.with_overrides(data_gap_sessions=2))
    t = trade(res, "A")
    assert (t["exit_reason"], t["exit_date"], t["exit_price_avg"]) == ("data_end", DATES[3], 102.0)
    # Day 2: bar missing -> no checks, position marked at the last valid close.
    assert res.equity.loc[DATES[2], "gross"] == pytest.approx(100 * 102.0)
    assert res.equity.loc[DATES[2], "n_positions"] == 1


def test_missing_bar_skips_checks_then_resumes(fakes):
    # Day 2 missing (gap limit 5); day 3 back with a low through the stop.
    pb = PanelBuilder(["A"]).nan("A", [2]).bar("A", 3, o=98.0, lo=94.0, c=96.0)
    t = trade(run(pb, watch(cand(0, "A"))), "A")
    assert (t["exit_reason"], t["exit_date"], t["exit_price_avg"]) == ("stop", DATES[3], 95.0)


def test_end_of_test_liquidation_with_slippage(fakes):
    pb = PanelBuilder(["A"]).bar("A", len(DATES) - 1, c=104.0)
    res = run(pb, watch(cand(0, "A")), BASE.with_overrides(slip_bps=10.0))
    t = trade(res, "A")
    assert t["exit_reason"] == "end_of_test"
    assert t["exit_date"] == DATES[-1]
    assert t["exit_price_avg"] == pytest.approx(104.0 * 0.999)
    last = res.equity.iloc[-1]
    assert last["n_positions"] == 0 and last["gross"] == 0
    assert last["equity"] == pytest.approx(last["cash"])


def test_cfg_end_limits_sessions(fakes):
    pb = PanelBuilder(["A"]).bar("A", 5, c=103.0)
    res = run(pb, watch(cand(0, "A")), BASE.with_overrides(end=day(5)))
    assert list(res.equity.index) == list(DATES[:6])
    t = trade(res, "A")
    assert (t["exit_reason"], t["exit_date"], t["exit_price_avg"]) == ("end_of_test", DATES[5], 103.0)


# --------------------------------------------------------------------------- sizing
def test_sizing_position_cap(fakes):
    pb = PanelBuilder(["A"])
    t = trade(run(pb, watch(cand(0, "A", stop=99.0))), "A")
    assert t["qty"] == 250  # risk qty 500, capped at 25% * 100k / 100
    assert t["risk_dollars"] == pytest.approx(250.0)


def test_sizing_gross_cap(fakes):
    pb = PanelBuilder(list("ABCD"))
    wl = watch(*(cand(0, s, stop=99.0, minute=5 * (n + 1)) for n, s in enumerate("ABCD")))
    res = run(pb, wl, BASE.with_overrides(max_gross=0.6))
    qty = dict(zip(res.trades["symbol"], res.trades["qty"], strict=True))
    assert qty == {"A": 250, "B": 250, "C": 100}  # C: (60k - 50k) / 100
    assert skip_reasons(res, "D") == ["cap_exposure"]


def test_sizing_cash_cap(fakes):
    pb = PanelBuilder(list("ABC"))
    wl = watch(*(cand(0, s, stop=99.5, minute=5 * (n + 1)) for n, s in enumerate("ABC")))
    res = run(pb, wl, BASE.with_overrides(max_position_pct=0.4, max_gross=2.0))
    qty = dict(zip(res.trades["symbol"], res.trades["qty"], strict=True))
    assert qty == {"A": 400, "B": 400, "C": 200}  # C limited by the $20k cash left
    assert res.equity.iloc[0]["cash"] == pytest.approx(0.0)


def test_sizing_counts_as_traded_shares_so_later_reverse_splits_do_not_matter(fakes):
    """Codex P2 (round 4): a later 1:10,000 reverse split inflates split-adjusted history
    10,000x. Rounding whole shares in adjusted units would turn this into qty_zero; sizing in
    as-traded shares (via the candidate's point-in-time tick) keeps the trade identical."""

    def scenario(scale: float) -> pd.Series:
        pb = PanelBuilder(["A"]).bar("A", 3, h=106.0, c=105.0).bar("A", 4, o=101.0, lo=99.0, c=100.5)
        for name in ("open", "high", "low", "close"):
            pb.f[name] *= scale
        c = cand(0, "A", fill=100.0 * scale, stop=95.0 * scale)
        c.tick = 0.01 * scale  # one as-traded cent in adjusted units
        return trade(run(pb, watch(c)), "A")

    plain, inflated = scenario(1.0), scenario(1e4)
    assert inflated["pnl"] == pytest.approx(plain["pnl"]) == pytest.approx(33 * 5.0)
    assert inflated["risk_dollars"] == pytest.approx(plain["risk_dollars"])
    assert inflated["qty"] * 1e4 == pytest.approx(plain["qty"])  # 100 as-traded shares
    legs = json.loads(inflated["legs"])
    assert [round(leg["qty"] * 1e4) for leg in legs] == [33, 67]


def test_sizing_qty_zero(fakes):
    pb = PanelBuilder(["A"])
    res = run(pb, watch(cand(0, "A")), BASE.with_overrides(risk_per_trade=1e-5))  # $1 / $5
    assert res.trades.empty
    assert skip_reasons(res, "A") == ["qty_zero"]


def test_sizing_uses_prior_close_equity(fakes):
    # E_1 = 100k + 100 * (150 - 100) = 105k; day-2 close (200) must not affect day-2 sizing.
    pb = PanelBuilder(["A", "B"]).bar("A", 1, h=151.0, c=150.0).bar("A", 2, o=150.0, h=201.0, c=200.0)
    res = run(pb, watch(cand(0, "A"), cand(2, "B")))
    assert trade(res, "B")["qty"] == 105  # 0.5% * 105k / 5
    assert res.equity.loc[DATES[1], "equity"] == pytest.approx(105_000.0)


# --------------------------------------------------------------------------- ordering / caps
def test_cap_new_follows_trigger_time_and_tie_breaks(fakes):
    syms = list("AFEB")
    pb = PanelBuilder(syms)
    wl = watch(
        cand(0, "B", minute=10),
        cand(0, "A", rank=0.90, minute=5),
        cand(0, "F", rank=0.99, minute=5),
        cand(0, "E", setup="EP", rank=0.20, minute=5),
    )
    cfg = BASE.with_overrides(entry_mode="intraday", max_new_per_day=2)
    res = run(pb, wl, cfg, store=bars_store(syms))
    assert list(res.trades["symbol"]) == ["E", "F"]  # same ts: EP first, then rank desc
    assert skip_reasons(res, "A") == ["cap_new"]
    assert skip_reasons(res, "B") == ["cap_new"]
    # Intraday entries see intraday bars only; entry-day exits get no daily bar.
    assert all(bars is not None for _, bars in fakes["find_trigger"])
    assert fakes["find_trigger_daily"] == []
    assert all(day_bar is None for _, _, day_bar in fakes["entry_day_exit"])


def test_cap_open_counts_existing_positions(fakes):
    syms = ["Z", "A", "B"]
    pb = PanelBuilder(syms)
    wl = watch(cand(0, "Z"), cand(1, "A", minute=10), cand(1, "B", minute=5))
    cfg = BASE.with_overrides(entry_mode="intraday", max_open_positions=2)
    res = run(pb, wl, cfg, store=bars_store(syms))
    assert set(res.trades["symbol"]) == {"Z", "B"}
    assert skip_reasons(res, "A") == ["cap_open"]


def test_same_symbol_multiple_setups_earliest_wins_ties_ep(fakes):
    pb = PanelBuilder(["A", "B"])
    wl = watch(
        cand(0, "A", setup="EP", minute=10),
        cand(0, "A", setup="BO", minute=5),
        cand(0, "B", setup="PB", minute=5),
        cand(0, "B", setup="EP", minute=5),
    )
    res = run(pb, wl, BASE.with_overrides(entry_mode="intraday"), store=bars_store(["A", "B"]))
    assert trade(res, "A")["setup"] == "BO"
    assert trade(res, "B")["setup"] == "EP"
    dup = res.skips[res.skips["reason"] == "dup_setup"]
    assert sorted(zip(dup["symbol"], dup["setup"], strict=True)) == [("A", "EP"), ("B", "PB")]


def test_already_held_and_trigger_skips_recorded(fakes):
    pb = PanelBuilder(["A", "B"])
    wl = watch(cand(0, "A"), cand(1, "A"), cand(1, "B", skip="too_wide"), cand(2, "X"))
    res = run(pb, wl)
    assert len(res.trades) == 1
    assert skip_reasons(res, "A") == ["already_held"]
    assert skip_reasons(res, "B") == ["too_wide"]
    assert skip_reasons(res, "X") == ["no_data"]  # symbol not in panel


def test_timestamp_keyed_watchlists_are_normalized(fakes):
    pb = PanelBuilder(["A"])
    res = run(pb, {DATES[0]: [cand(0, "A")]})  # pd.Timestamp key instead of date
    assert len(res.trades) == 1


def test_intraday_no_bars_is_no_data(fakes):
    pb = PanelBuilder(["A"])
    cfg = BASE.with_overrides(entry_mode="intraday")
    res = run(pb, watch(cand(0, "A")), cfg, store=lambda d: None)
    assert res.trades.empty and skip_reasons(res) == ["no_data"]


def test_intraday_requires_store():
    pb = PanelBuilder(["A"])
    with pytest.raises(ValueError, match="intraday_store"):
        run(pb, {}, BASE.with_overrides(entry_mode="intraday"))


# --------------------------------------------------------------------------- regime / setups
def test_regime_index_risk_off_blocks_bo_pb_and_halves_ep(fakes):
    pb = PanelBuilder(["A", "B", "C"])
    wl = watch(
        cand(0, "A", setup="BO", ro_index=False),
        cand(0, "B", setup="PB", ro_index=False),
        cand(0, "C", setup="EP", ro_index=False),
    )
    res = run(pb, wl)
    assert list(res.trades["symbol"]) == ["C"]
    assert trade(res, "C")["qty"] == 50  # 0.5 * 100 shares
    assert skip_reasons(res, "A") == ["regime"] and skip_reasons(res, "B") == ["regime"]

    res = run(pb, wl, BASE.with_overrides(ep_in_risk_off=False))
    assert res.trades.empty and skip_reasons(res) == ["regime"] * 3

    res = run(pb, wl, BASE.with_overrides(regime_mode="none"))
    assert sorted(res.trades["qty"]) == [100, 100, 100]


def test_regime_breadth_and_both(fakes):
    pb = PanelBuilder(["A", "B"])
    wl = watch(
        cand(0, "A", ro_index=False, ro_breadth=True),
        cand(0, "B", ro_index=True, ro_breadth=False),
    )
    res = run(pb, wl, BASE.with_overrides(regime_mode="breadth"))
    assert list(res.trades["symbol"]) == ["A"] and skip_reasons(res, "B") == ["regime"]
    res = run(pb, wl, BASE.with_overrides(regime_mode="both"))
    assert res.trades.empty and skip_reasons(res) == ["regime", "regime"]
    res = run(pb, wl, BASE.with_overrides(regime_mode="index"))
    assert list(res.trades["symbol"]) == ["B"]


def test_regime_nan_flag_is_risk_off(fakes):
    pb = PanelBuilder(["A"])
    res = run(pb, watch(cand(0, "A", ro_index=float("nan"))))
    assert res.trades.empty and skip_reasons(res) == ["regime"]


def test_setup_disabled(fakes):
    pb = PanelBuilder(["A", "B"])
    wl = watch(cand(0, "A", setup="EP"), cand(0, "B", setup="BO"))
    res = run(pb, wl, BASE.with_overrides(setups=("BO",)))
    assert list(res.trades["symbol"]) == ["B"]
    assert skip_reasons(res, "A") == ["setup_disabled"]


# --------------------------------------------------------------------------- contract exits
def test_contract_exits_target_stop_time(fakes):
    pb = (
        PanelBuilder(list("ABCD"))
        .bar("A", 2, h=111.0)  # target via high -> 110
        .bar("B", 2, o=112.0, h=113.0, c=112.5)  # opens above target -> 112
        .bar("C", 2, h=111.0, lo=94.0)  # stop and target same bar -> stop first
        .bar("D", 3, c=101.0)  # time exit at horizon
    )
    wl = watch(*(cand(0, s, minute=5 * (n + 1)) for n, s in enumerate("ABCD")))
    res = run(pb, wl, BASE.with_overrides(exit_mode="contract", contract_horizon=3))
    a, b, c, d = (trade(res, s) for s in "ABCD")
    assert (a["exit_reason"], a["exit_price_avg"], a["exit_date"]) == ("target", 110.0, DATES[2])
    assert (b["exit_reason"], b["exit_price_avg"]) == ("target", 112.0)
    assert (c["exit_reason"], c["exit_price_avg"]) == ("stop", 95.0)
    assert (d["exit_reason"], d["exit_price_avg"], d["hold_sessions"]) == ("time", 101.0, 3)
    assert a["r_multiple"] == pytest.approx(2.0)


def test_contract_mode_has_no_partial_or_trail(fakes):
    pb = PanelBuilder(["A"]).bar("A", 3, c=105.0)
    pb.trail.loc[DATES[2], "A"] = 200.0
    res = run(pb, watch(cand(0, "A")), BASE.with_overrides(exit_mode="contract"))
    t = trade(res, "A")
    assert (t["exit_reason"], t["hold_sessions"]) == ("time", 10)
    assert len(json.loads(t["legs"])) == 1


# --------------------------------------------------------------------------- capital timing
def test_entry_day_stop_capital_not_reused_same_day(fakes):
    syms = ["A", "B", "C"]
    pb = PanelBuilder(syms)
    wl = watch(
        cand(0, "A", stop=99.9, minute=5, exit=(99.9, "stop")),  # 900 sh, stopped at 09:50
        cand(0, "B", stop=98.0, minute=30),  # risk qty 250
        cand(1, "C", stop=98.0, minute=5),
    )
    cfg = BASE.with_overrides(entry_mode="intraday", max_position_pct=0.9)
    res = run(pb, wl, cfg, store=bars_store(syms))
    a, b, c = (trade(res, s) for s in syms)
    assert (a["qty"], a["exit_reason"], a["hold_sessions"]) == (900, "stop", 0)
    assert b["qty"] == 100  # only the $10k left; A's proceeds not reusable on day 0
    assert c["qty"] == 249  # capital back next day: floor(0.5% * E_0 (99,910) / 2)


def test_open_gap_capital_is_reused_same_day(fakes):
    pb = PanelBuilder(["Z", "Y"]).bar("Z", 1, o=99.0, lo=98.5)
    wl = watch(cand(0, "Z", stop=99.9), cand(1, "Y", stop=98.0))
    res = run(pb, wl, BASE.with_overrides(max_position_pct=0.9))
    z, y = trade(res, "Z"), trade(res, "Y")
    assert (z["qty"], z["exit_reason"], z["exit_date"], z["exit_price_avg"]) == (
        900,
        "stop",
        DATES[1],
        99.0,
    )
    assert y["qty"] == 250  # would be 100 if Z's open-exit cash were not available


def test_daily_mode_passes_day_bar(fakes):
    pb = PanelBuilder(["A"]).bar("A", 0, o=100.5, h=102.0, lo=99.0, c=101.0)
    run(pb, watch(cand(0, "A")))
    [(_, day_bar)] = fakes["find_trigger_daily"]
    assert day_bar == {"open": 100.5, "high": 102.0, "low": 99.0, "close": 101.0}
    [(_, bars, edb)] = fakes["entry_day_exit"]
    assert bars is None and edb == day_bar


# --------------------------------------------------------------------------- accounting
def test_equity_accounting_identity(fakes):
    syms = list("ABCDEFG")
    pb = (
        PanelBuilder(syms)
        .bar("A", 1, o=90.0, lo=89.0, c=91.0)  # gap stop
        .bar("B", 3, h=108.0, c=107.0)  # partial, stop -> breakeven 100
        .bar("B", 4, o=106.0, h=107.0, lo=104.0, c=105.0)
        .bar("B", 5, o=104.0, h=105.0, lo=101.0, c=102.0)
        .bar("C", 2, lo=94.0)  # intraday stop
        .bar("E", 4, h=120.0, c=118.0)
        .nan("F", range(6, len(DATES)))  # data end
    )
    pb.trail.loc[DATES[5], "B"] = 103.0  # trail B at day 5
    wl = watch(
        cand(0, "A"),
        cand(0, "B", minute=10),
        cand(1, "C", setup="PB"),
        cand(1, "D", setup="EP", exit=(96.0, "stop")),
        cand(2, "E", fill=101.0, stop=97.0),
        cand(3, "F", stop=98.0),
        cand(4, "G", stop=99.0),
    )
    res = run(pb, wl, BASE.with_overrides(slip_bps=10.0))
    assert len(res.trades) == len(syms)
    eq = res.equity
    assert np.allclose(eq["equity"], eq["cash"] + eq["gross"])
    assert eq["equity"].iloc[-1] == pytest.approx(100_000 + res.trades["pnl"].sum(), abs=1e-6)
    assert eq["n_positions"].iloc[-1] == 0
    reasons = dict(zip(res.trades["symbol"], res.trades["exit_reason"], strict=True))
    assert reasons == {
        "A": "stop",
        "B": "trail",
        "C": "stop",
        "D": "stop",
        "E": "end_of_test",
        "F": "data_end",
        "G": "end_of_test",
    }
    # pnl per trade = proceeds from legs - cost
    for _, t in res.trades.iterrows():
        legs = json.loads(t["legs"])
        assert sum(leg["qty"] for leg in legs) == t["qty"]
        proceeds = sum(leg["qty"] * leg["price"] for leg in legs)
        assert t["pnl"] == pytest.approx(proceeds - t["qty"] * t["entry_fill"])
    assert list(res.trades.columns) == engine.TRADE_COLUMNS
    assert list(res.skips.columns) == engine.SKIP_COLUMNS
    assert list(eq.columns) == engine.EQUITY_COLUMNS
    json.dumps(res.config)


def test_empty_run_shapes(fakes):
    res = run(PanelBuilder(["A"]), {})
    assert res.trades.empty and list(res.trades.columns) == engine.TRADE_COLUMNS
    assert res.skips.empty and len(res.equity) == len(DATES)
    assert (res.equity["equity"] == 100_000).all()


# --------------------------------------------------------------------------- real intraday wiring
def test_smoke_real_daily_helpers():
    pb = PanelBuilder(["A"]).bar("A", 0, o=100.5, h=102.0, lo=99.0, c=101.0)
    t = trade(run(pb, watch(cand(0, "A"))), "A")
    assert t["entry_fill"] == pytest.approx(100.5)  # max(pivot + 0.01, open)
    assert t["initial_stop"] == pytest.approx(98.99)  # max(prev_low 99, fill * 0.95) - 0.01
    assert math.isfinite(t["pnl"])


def test_smoke_real_intraday_helpers():
    d = day(0)
    bars = pd.DataFrame(
        {
            "symbol": ["A"] * 3,
            "ts": [ts(d, 0), ts(d, 5), ts(d, 10)],
            "open": [100.0, 100.3, 100.8],
            "high": [100.5, 101.5, 101.0],
            "low": [99.6, 100.2, 99.0],
            "close": [100.3, 101.2, 99.2],
            "volume": [5e5, 3e5, 3e5],
        }
    )
    pb = PanelBuilder(["A"])
    cfg = BASE.with_overrides(entry_mode="intraday")
    res = run(pb, watch(cand(0, "A")), cfg, store=lambda day_: {"A": bars} if day_ == d else None)
    t = trade(res, "A")
    assert t["entry_fill"] == pytest.approx(100.51)  # max(pivot, ORH) + 0.01
    assert t["exit_reason"] == "stop" and t["hold_sessions"] == 0
    assert t["exit_price_avg"] == pytest.approx(t["initial_stop"])
