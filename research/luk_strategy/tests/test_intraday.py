"""Offline tests for intraday.py (SPEC §5 entries, §8.1 entry-day exits)."""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from research.luk_strategy.config import Config
from research.luk_strategy.intraday import (
    contract_target,
    entry_day_exit,
    find_trigger,
    find_trigger_daily,
)
from research.luk_strategy.models import Candidate, SkipEvent, Trigger

DAY = date(2024, 3, 5)
ET = "America/New_York"
CFG0 = Config(slip_bps=0.0)  # no slippage: exact arithmetic in most tests


def ts(hhmm: str, day: date = DAY) -> pd.Timestamp:
    return pd.Timestamp(f"{day.isoformat()} {hhmm}", tz=ET)


def bars(rows: list[tuple], symbol: str = "TEST", day: date = DAY) -> pd.DataFrame:
    """rows: (hhmm, open, high, low, close[, volume])."""
    recs = []
    for r in rows:
        hhmm, o, h, lo, c = r[:5]
        v = r[5] if len(r) > 5 else 1_000_000.0
        recs.append(
            {"symbol": symbol, "ts": ts(hhmm, day), "open": o, "high": h, "low": lo,
             "close": c, "volume": float(v)}
        )
    return pd.DataFrame(recs)


def cand(setup: str = "BO", **kw) -> Candidate:
    base = {
        "day": DAY,
        "symbol": "TEST",
        "setup": setup,
        "level": 100.0 if setup == "BO" else (100.5 if setup == "PB" else None),
        "prev_close": 99.0,
        "prev_high": 100.5,
        "prev_low": 97.0,
        "open_today": 99.5,
        "adr": 0.05,
        "atr": 4.0,
        "adv_shares": 1_000_000.0,
        "rank_key": 0.97,
        "risk_on_index": True,
        "risk_on_breadth": True,
    }
    base.update(kw)
    return Candidate(**base)


# Standard BO day: OR bar 09:30 (ORH 99.8, low 99.0); 09:40 crosses L = 100.01.
BO_ROWS = [
    ("09:30", 99.5, 99.8, 99.0, 99.6),
    ("09:35", 99.7, 99.9, 99.4, 99.8),
    ("09:40", 99.9, 100.5, 99.8, 100.4),
    ("09:45", 100.4, 100.9, 100.2, 100.8),
]


def as_trigger(x) -> Trigger:
    assert isinstance(x, Trigger), x
    return x


def as_skip(x, reason: str) -> SkipEvent:
    assert isinstance(x, SkipEvent), x
    assert x.reason == reason, x
    return x


# --------------------------------------------------------------------------- BO intraday
def test_bo_basic_trigger_level_fill_stop():
    t = as_trigger(find_trigger(cand("BO"), bars(BO_ROWS), CFG0))
    assert t.ts == ts("09:40")
    assert t.bar_pos == 2
    assert t.fill == pytest.approx(100.01)  # max(L, open 99.9)
    assert t.stop == pytest.approx(98.99)  # LOD 99.0 (OR bar) - 0.01
    assert str(t.ts.tz) == ET


def test_slippage_applied_to_fill():
    t = as_trigger(find_trigger(cand("BO"), bars(BO_ROWS), Config(slip_bps=10.0)))
    assert t.fill == pytest.approx(100.01 * 1.001)


def test_gap_open_above_level_fills_at_bar_open():
    rows = [
        ("09:30", 99.5, 99.8, 99.0, 99.6),
        ("09:35", 101.0, 101.5, 100.8, 101.2),  # opens above L = 100.01
    ]
    t = as_trigger(find_trigger(cand("BO"), bars(rows), CFG0))
    assert t.ts == ts("09:35")
    assert t.fill == pytest.approx(101.0)


def test_orh_above_pivot_sets_level():
    rows = [
        ("09:30", 99.5, 100.8, 99.0, 100.5),  # ORH 100.8 > pivot 100
        ("09:35", 100.5, 100.7, 100.1, 100.6),  # above pivot but below ORH+0.01
        ("09:40", 100.6, 101.0, 100.5, 100.9),
    ]
    t = as_trigger(find_trigger(cand("BO"), bars(rows), CFG0))
    assert t.ts == ts("09:40")
    assert t.fill == pytest.approx(100.81)


def test_opening_range_bar_never_triggers():
    rows = [("09:30", 99.5, 101.0, 99.0, 100.9), ("09:35", 100.9, 101.0, 100.5, 100.8)]
    # L = max(100, 101.0) + 0.01 = 101.01; only the OR bar went above the pivot.
    as_skip(find_trigger(cand("BO"), bars(rows), CFG0), "no_trigger")


def test_cutoff_boundary_bar_start_before_cutoff_triggers():
    rows = [("09:30", 99.5, 99.8, 99.0, 99.6)]
    rows += [(f"{h:02d}:{m:02d}", 99.6, 99.9, 99.3, 99.7) for h in (9, 10) for m in range(0, 60, 5)
             if (h, m) > (9, 30) and (h, m) < (10, 55)]
    rows += [("10:55", 99.8, 100.2, 99.7, 100.1)]
    t = as_trigger(find_trigger(cand("BO"), bars(rows), CFG0))
    assert t.ts == ts("10:55")


def test_cutoff_boundary_bar_starting_at_cutoff_does_not_trigger():
    rows = [
        ("09:30", 99.5, 99.8, 99.0, 99.6),
        ("10:55", 99.6, 99.9, 99.3, 99.7),
        ("11:00", 99.8, 100.2, 99.7, 100.1),  # starts AT the 11:00 cutoff
    ]
    as_skip(find_trigger(cand("BO"), bars(rows), CFG0), "after_cutoff")
    rows_never = rows[:2]
    as_skip(find_trigger(cand("BO"), bars(rows_never), CFG0), "no_trigger")


def test_cutoff_override_extends_window():
    rows = [("09:30", 99.5, 99.8, 99.0, 99.6), ("11:00", 99.8, 100.2, 99.7, 100.1)]
    t = as_trigger(find_trigger(cand("BO"), bars(rows), CFG0.with_overrides(bo_cutoff="15:30")))
    assert t.ts == ts("11:00")


def test_trigger_bar_low_never_widens_stop_and_is_checked_as_entry_day_stop():
    # LA-1/F6: the trigger bar trades 98.2, possibly after the fill. The stop uses only
    # pre-fill prices (OR low 99.0, bar open 99.6); the bar's low then stops the trade out
    # inside the trigger bar (order unknown -> conservative stop-out).
    rows = [
        ("09:30", 99.5, 99.8, 99.0, 99.6),
        ("09:35", 99.6, 100.3, 98.2, 100.2),
    ]
    t = as_trigger(find_trigger(cand("BO"), bars(rows), CFG0))
    assert t.stop == pytest.approx(98.99)  # was 98.19 (post-fill low included)
    assert entry_day_exit(t, bars(rows), None, CFG0) == (ts("09:35"), pytest.approx(98.99), "stop")


def test_trigger_bar_opening_above_level_then_new_low_is_certain_in_bar_stop():
    # Fill is the trigger bar's open (first trade), so its whole low prints after the fill:
    # a real stop at the pre-trigger LOD - 0.01 is hit inside the bar.
    rows = [
        ("09:30", 99.5, 99.8, 99.0, 99.6),
        ("09:35", 100.2, 100.6, 98.5, 100.4),
        ("09:40", 100.4, 100.9, 100.2, 100.8),
    ]
    t = as_trigger(find_trigger(cand("BO"), bars(rows), CFG0))
    assert t.fill == pytest.approx(100.2)
    assert t.stop == pytest.approx(98.99)
    assert entry_day_exit(t, bars(rows), None, CFG0) == (ts("09:35"), pytest.approx(98.99), "stop")


def test_width_filter_decided_on_pre_fill_prices_only():
    # ADR 2%: the post-fill low 97.5 would make the stop ~2.7% wide (old: too_wide skip,
    # avoiding the loss); on pre-fill data the stop is 1.2% wide and the trade is taken.
    rows = [
        ("09:30", 99.5, 99.8, 99.0, 99.6),
        ("09:35", 100.2, 100.6, 97.5, 100.4),
    ]
    c = cand("BO", adr=0.02)
    t = as_trigger(find_trigger(c, bars(rows), CFG0))
    assert t.stop == pytest.approx(98.99)
    assert entry_day_exit(t, bars(rows), None, CFG0) == (ts("09:35"), pytest.approx(98.99), "stop")


def test_trigger_bar_open_is_part_of_the_pre_fill_low():
    # The trigger bar opens below the OR low and rallies to the level: its open is a known
    # pre-fill price, so the stop sits below it (not above a price that already traded).
    rows = [
        ("09:30", 99.5, 99.8, 99.0, 99.6),
        ("09:35", 98.5, 100.3, 98.5, 100.2),
    ]
    t = as_trigger(find_trigger(cand("BO", adr=0.03), bars(rows), CFG0))
    assert t.stop == pytest.approx(98.49)
    assert entry_day_exit(t, bars(rows), None, CFG0) is None  # bar low 98.5 > stop


def test_point_in_time_tick_scales_levels_and_stops():
    # A later 40:1 split makes one as-traded cent 0.00025 in adjusted units (LA-6/F3).
    tick = 0.01 / 40
    t = as_trigger(find_trigger(cand("BO", tick=tick), bars(BO_ROWS), CFG0))
    assert t.fill == pytest.approx(100.0 + tick)
    assert t.stop == pytest.approx(99.0 - tick)
    ep_rows = [("09:30", 110.0, 111.0, 109.0, 110.5, 500_000), ("09:35", 110.5, 112, 110, 111)]
    t_ep = as_trigger(find_trigger(cand("EP", tick=tick), bars(ep_rows), CFG0))
    assert t_ep.fill == pytest.approx(111.0 + tick)
    c = cand("BO", prev_low=97.0, tick=tick)
    t_d = as_trigger(find_trigger_daily(c, {"open": 99.5, "high": 101.0, "low": 98.0}, DAILY))
    assert t_d.fill == pytest.approx(100.0 + tick)
    assert t_d.stop == pytest.approx(97.0 - tick)
    # Missing / invalid tick falls back to one cent.
    t_bad = as_trigger(find_trigger(cand("BO", tick=float("nan")), bars(BO_ROWS), CFG0))
    assert t_bad.fill == pytest.approx(100.01)


def test_min_stop_pct_floor_widens_stop():
    rows = [
        ("09:30", 99.97, 100.0, 99.96, 99.99),
        ("09:35", 99.99, 100.2, 99.98, 100.1),
    ]
    t = as_trigger(find_trigger(cand("BO"), bars(rows), CFG0))
    # LOD stop 99.95 is only 0.06% below the fill -> floored to 1%.
    assert t.fill == pytest.approx(100.01)
    assert t.stop == pytest.approx(100.01 * 0.99)
    assert t.stop_pct == pytest.approx(0.01)


def test_width_filter_skip_uses_adr():
    rows = [("09:30", 99.0, 99.8, 97.0, 99.6), ("09:35", 99.6, 100.3, 99.5, 100.2)]
    # stop 96.99 -> ~3.0% wide; ADR 2% -> limit min(1.0*0.02, 0.08) = 2%
    as_skip(find_trigger(cand("BO", adr=0.02), bars(rows), CFG0), "too_wide")
    as_trigger(find_trigger(cand("BO", adr=0.04), bars(rows), CFG0))


def test_width_filter_caps_at_max_stop_pct():
    rows = [("09:30", 99.0, 99.8, 91.0, 99.6), ("09:35", 99.6, 100.3, 99.5, 100.2)]
    # ~9% wide; ADR 20% but max_stop_pct 8% binds
    as_skip(find_trigger(cand("BO", adr=0.20), bars(rows), CFG0), "too_wide")


def test_chase_guard_from_gap_and_from_orh():
    gap = [("09:30", 99.5, 99.8, 99.0, 99.6), ("09:35", 106.0, 106.5, 105.8, 106.2)]
    # chase limit = 100 * (1 + 1.0 * 0.05) = 105
    as_skip(find_trigger(cand("BO"), bars(gap), CFG0), "chase")
    orh = [("09:30", 103.0, 105.5, 102.0, 105.0), ("09:35", 105.0, 105.8, 104.9, 105.6)]
    as_skip(find_trigger(cand("BO"), bars(orh), CFG0), "chase")
    ok = [("09:30", 103.5, 104.0, 103.0, 103.8), ("09:35", 104.9, 105.5, 104.5, 105.2)]
    t = as_trigger(find_trigger(cand("BO"), bars(ok), CFG0))  # raw fill 104.9 <= 105
    assert t.fill == pytest.approx(104.9)


def test_chase_guard_uses_raw_fill_not_slipped_fill():
    ok = [("09:30", 99.5, 99.8, 99.0, 99.6), ("09:35", 105.0, 105.5, 104.5, 105.2)]
    t = as_trigger(find_trigger(cand("BO", adr=0.08), bars(ok), Config(slip_bps=50.0)))
    assert t.fill == pytest.approx(105.0 * 1.005)


# --------------------------------------------------------------------------- EP intraday
def test_ep_volume_skip_and_pass():
    rows = [
        ("09:30", 110.0, 111.0, 109.0, 110.5, 50_000),
        ("09:35", 110.5, 111.5, 110.2, 111.3, 80_000),
    ]
    c = cand("EP", prev_close=100.0, open_today=110.0, adv_shares=1_000_000.0)
    as_skip(find_trigger(c, bars(rows), CFG0), "ep_volume")  # 50k < 10% * 1M
    rows[0] = ("09:30", 110.0, 111.0, 109.0, 110.5, 150_000)
    t = as_trigger(find_trigger(c, bars(rows), CFG0))
    assert t.fill == pytest.approx(111.01)  # ORH + 0.01
    assert t.stop == pytest.approx(108.99)


def test_ep_width_ignores_adr_uses_ep_max_stop_pct():
    rows = [("09:30", 110.0, 111.0, 106.0, 110.5, 500_000), ("09:35", 110.5, 111.5, 110, 111)]
    c = cand("EP", adr=0.01, open_today=110.0)
    t = as_trigger(find_trigger(c, bars(rows), CFG0))  # ~4.5% wide, ADR 1% ignored
    assert t.stop == pytest.approx(105.99)
    wide = [("09:30", 110.0, 111.0, 101.0, 110.5, 500_000), ("09:35", 110.5, 111.5, 110, 111)]
    as_skip(find_trigger(c, bars(wide), CFG0), "too_wide")  # ~9% > 8%


# --------------------------------------------------------------------------- PB intraday
def test_pb_gap_above_skip_even_without_bars():
    c = cand("PB", open_today=100.5, prev_high=100.5)
    as_skip(find_trigger(c, None, CFG0), "pb_gap_above")
    as_skip(find_trigger(cand("PB", open_today=101.0), bars(BO_ROWS), CFG0), "pb_gap_above")


def test_pb_trigger_afternoon_and_cutoff():
    rows = [
        ("09:30", 99.5, 99.9, 99.2, 99.8),
        ("12:00", 99.8, 100.2, 99.6, 100.0),
        ("14:00", 100.2, 100.8, 100.1, 100.7),  # crosses prev_high + 0.01 = 100.51
    ]
    t = as_trigger(find_trigger(cand("PB"), bars(rows), CFG0))
    assert t.ts == ts("14:00")
    assert t.fill == pytest.approx(100.51)
    assert t.stop == pytest.approx(99.19)
    late = rows[:2] + [("15:30", 100.2, 100.8, 100.1, 100.7)]
    as_skip(find_trigger(cand("PB"), bars(late), CFG0), "after_cutoff")


# --------------------------------------------------------------------------- data handling
def test_no_data_cases():
    as_skip(find_trigger(cand("BO"), None, CFG0), "no_data")
    empty = pd.DataFrame(columns=["symbol", "ts", "open", "high", "low", "close", "volume"])
    as_skip(find_trigger(cand("BO"), empty, CFG0), "no_data")
    late_start = [("09:45", 99.5, 99.8, 99.0, 99.6), ("09:50", 99.9, 100.5, 99.8, 100.4)]
    as_skip(find_trigger(cand("BO"), bars(late_start), CFG0), "no_data")
    other_day = bars(BO_ROWS, day=date(2024, 3, 4))
    as_skip(find_trigger(cand("BO"), other_day, CFG0), "no_data")
    as_skip(find_trigger(cand("BO", adr=float("nan")), bars(BO_ROWS), CFG0), "no_data")


def test_missing_0930_bar_uses_earliest_bar_before_0945():
    rows = [
        ("09:35", 99.5, 100.3, 99.0, 100.1),  # becomes the OR bar -> cannot trigger
        ("09:40", 100.1, 100.2, 99.9, 100.1),  # below L = max(100, 100.3) + 0.01
        ("09:45", 100.2, 100.5, 100.1, 100.4),
    ]
    t = as_trigger(find_trigger(cand("BO"), bars(rows), CFG0))
    assert t.ts == ts("09:45")
    assert t.fill == pytest.approx(100.31)
    assert t.stop == pytest.approx(98.99)


def test_unsorted_and_foreign_rows_are_ignored_and_bar_pos_maps_to_input():
    df = bars(BO_ROWS)
    noise = bars([("09:40", 50, 60, 40, 55)], symbol="OTHER")
    pre = bars([("09:25", 90, 200, 10, 95)])  # pre-market, must be dropped
    mixed = pd.concat([df.iloc[[3, 1]], noise, pre, df.iloc[[2, 0]]], ignore_index=True)
    t = as_trigger(find_trigger(cand("BO"), mixed, CFG0))
    assert t.ts == ts("09:40")
    assert t.stop == pytest.approx(98.99)
    assert mixed.iloc[t.bar_pos]["ts"] == ts("09:40")
    assert mixed.iloc[t.bar_pos]["symbol"] == "TEST"


def test_no_lookahead_bars_after_trigger_do_not_change_entry():
    full = bars(BO_ROWS + [("09:50", 100.8, 130.0, 50.0, 60.0), ("15:55", 60, 61, 1.0, 2.0)])
    t_full = as_trigger(find_trigger(cand("BO"), full, CFG0))
    t_trunc = as_trigger(find_trigger(cand("BO"), full.iloc[: t_full.bar_pos + 1], CFG0))
    assert (t_full.ts, t_full.fill, t_full.stop, t_full.bar_pos) == (
        t_trunc.ts, t_trunc.fill, t_trunc.stop, t_trunc.bar_pos
    )


def test_unknown_setup_raises():
    with pytest.raises(ValueError):
        find_trigger(cand("XX", level=100.0), bars(BO_ROWS), CFG0)


# --------------------------------------------------------------------------- contract floor
def test_contract_floor_applied_after_width_filter():
    cfg = CFG0.with_overrides(exit_mode="contract")
    t = as_trigger(find_trigger(cand("BO", atr=4.0), bars(BO_ROWS), cfg))
    assert t.stop == pytest.approx(100.01 - 2.0)  # min(98.99, fill - 0.5*ATR)
    # ATR floor makes it 5% wide vs the 3% ADR limit, but the floor comes after the filter
    t2 = as_trigger(find_trigger(cand("BO", adr=0.03, atr=10.0), bars(BO_ROWS), cfg))
    assert t2.stop == pytest.approx(100.01 - 5.0)
    # floor never tightens: small ATR keeps the LOD stop
    t3 = as_trigger(find_trigger(cand("BO", atr=0.5), bars(BO_ROWS), cfg))
    assert t3.stop == pytest.approx(98.99)


def test_contract_target_helper():
    t = as_trigger(find_trigger(cand("BO"), bars(BO_ROWS), CFG0))
    cfg = CFG0.with_overrides(exit_mode="contract", contract_target_r=2.0)
    assert contract_target(t, cfg) == pytest.approx(t.fill + 2.0 * (t.fill - t.stop))


# --------------------------------------------------------------------------- entry-day exit
def test_entry_day_exit_stop_touch_and_gap_through():
    touch = bars(BO_ROWS + [("09:50", 99.5, 99.7, 98.9, 99.0)])
    t = as_trigger(find_trigger(cand("BO"), touch, CFG0))
    assert entry_day_exit(t, touch, None, CFG0) == (ts("09:50"), pytest.approx(98.99), "stop")
    gap = bars(BO_ROWS + [("09:50", 100.5, 100.6, 100.3, 100.4), ("09:55", 98.5, 98.7, 98.0, 98.2)])
    t = as_trigger(find_trigger(cand("BO"), gap, CFG0))
    res = entry_day_exit(t, gap, None, CFG0)
    assert res == (ts("09:55"), pytest.approx(98.5), "stop")  # open below stop


def test_entry_day_exit_none_and_luk_ignores_target():
    rows = bars(BO_ROWS + [("10:00", 100.8, 140.0, 100.5, 139.0)])
    t = as_trigger(find_trigger(cand("BO"), rows, CFG0))
    assert entry_day_exit(t, rows, None, CFG0) is None


def test_entry_day_exit_scans_only_after_trigger_bar():
    # Trigger bar itself runs far above the contract target; it must not count.
    rows = bars([("09:30", 99.5, 99.8, 99.0, 99.6), ("09:35", 99.9, 110.0, 99.8, 109.0)])
    cfg = CFG0.with_overrides(exit_mode="contract")
    t = as_trigger(find_trigger(cand("BO", atr=1.0), rows, cfg))
    assert contract_target(t, cfg) < 110.0
    assert entry_day_exit(t, rows, None, cfg) is None


def test_contract_target_and_stop_same_bar_takes_stop():
    cfg = CFG0.with_overrides(exit_mode="contract")
    c = cand("BO", atr=1.0)
    t = as_trigger(find_trigger(c, bars(BO_ROWS), cfg))  # fill 100.01, stop 98.99, R 1.02
    tgt = contract_target(t, cfg)
    assert tgt == pytest.approx(102.05)
    both = bars(BO_ROWS + [("09:50", 101.0, 103.0, 98.5, 102.0)])
    assert entry_day_exit(t, both, None, cfg) == (ts("09:50"), pytest.approx(98.99), "stop")
    hi = bars(BO_ROWS + [("09:50", 101.0, 103.0, 100.5, 102.0)])
    assert entry_day_exit(t, hi, None, cfg) == (ts("09:50"), pytest.approx(tgt), "target")
    gap = bars(BO_ROWS + [("09:50", 103.5, 104.0, 103.0, 103.8)])
    assert entry_day_exit(t, gap, None, cfg) == (ts("09:50"), pytest.approx(103.5), "target")


def test_entry_day_exit_falls_back_to_daily_low_without_bars():
    t = as_trigger(find_trigger(cand("BO"), bars(BO_ROWS), CFG0))
    assert entry_day_exit(t, None, {"low": 98.0}, CFG0) == (None, pytest.approx(98.99), "stop")
    assert entry_day_exit(t, None, None, CFG0) is None


# --------------------------------------------------------------------------- daily mode
DAILY = CFG0.with_overrides(entry_mode="daily")


def test_daily_bo_trigger_and_stop():
    c = cand("BO", prev_low=97.0, adr=0.05)
    t = as_trigger(find_trigger_daily(c, {"open": 99.5, "high": 101.0, "low": 98.0, "close": 100.5}, DAILY))
    assert t.bar_pos == -1
    assert t.ts == ts("09:30")
    assert t.fill == pytest.approx(100.01)
    # band: 100.01 * 0.95 = 95.0095 < prev_low 97 -> stop = 97 - 0.01
    assert t.stop == pytest.approx(96.99)


def test_daily_bo_band_binds_not_rejected_by_tick():
    c = cand("BO", prev_low=90.0, adr=0.05)
    t = as_trigger(find_trigger_daily(c, {"open": 99.5, "high": 101.0, "low": 98.0, "close": 100}, DAILY))
    assert t.stop == pytest.approx(100.01 * 0.95 - 0.01)


def test_daily_gap_open_fill_and_no_trigger_and_chase():
    c = cand("BO")
    t = as_trigger(find_trigger_daily(c, {"open": 102.0, "high": 103.0, "low": 101.5, "close": 102.5}, DAILY))
    assert t.fill == pytest.approx(102.0)
    as_skip(find_trigger_daily(c, {"open": 99.0, "high": 100.0, "low": 98.0, "close": 99.5}, DAILY),
            "no_trigger")
    as_skip(find_trigger_daily(c, {"open": 106.0, "high": 107.0, "low": 105.0, "close": 106.0}, DAILY),
            "chase")
    as_skip(find_trigger_daily(c, {}, DAILY), "no_data")
    as_skip(find_trigger_daily(c, {"open": float("nan"), "high": 101.0}, DAILY), "no_data")


def test_daily_pb_gap_above_and_trigger():
    c = cand("PB", prev_high=100.5, prev_low=98.5, adr=0.05)
    as_skip(find_trigger_daily(c, {"open": 100.5, "high": 101.0, "low": 100.0, "close": 100.8}, DAILY),
            "pb_gap_above")
    t = as_trigger(find_trigger_daily(c, {"open": 99.8, "high": 101.0, "low": 99.0, "close": 100.8}, DAILY))
    assert t.fill == pytest.approx(100.51)
    assert t.stop == pytest.approx(98.49)


def test_daily_ep_fill_at_open_and_gap_check():
    c = cand("EP", prev_close=100.0, adr=0.06)
    t = as_trigger(find_trigger_daily(c, {"open": 112.0, "high": 113.0, "low": 108.0, "close": 111}, DAILY))
    assert t.fill == pytest.approx(112.0)
    assert t.stop == pytest.approx(112.0 * 0.94)
    big_adr = cand("EP", prev_close=100.0, adr=0.15)  # band capped at ep_max_stop_pct 8%
    t2 = as_trigger(find_trigger_daily(big_adr, {"open": 112.0, "high": 113.0, "low": 108.0}, DAILY))
    assert t2.stop == pytest.approx(112.0 * 0.92)
    as_skip(find_trigger_daily(c, {"open": 105.0, "high": 113.0, "low": 104.0}, DAILY), "no_trigger")


def test_daily_entry_day_exit_same_day_stop():
    c = cand("BO", prev_low=97.0)
    day_bar = {"open": 99.5, "high": 101.0, "low": 96.5, "close": 100.5}
    t = as_trigger(find_trigger_daily(c, day_bar, DAILY))
    assert entry_day_exit(t, None, day_bar, DAILY) == (None, pytest.approx(96.99), "stop")
    ok_bar = dict(day_bar, low=97.5)
    assert entry_day_exit(t, None, ok_bar, DAILY) is None


def test_daily_bo_pb_stop_never_above_open():
    # LA-2: open 99.0 is below prev_low 99.9, so the old stop (99.89) sat above a price that
    # traded before the buy-stop at 100.51 filled, and low_t <= open_t booked a sure -1R.
    c = cand("PB", prev_high=100.5, prev_low=99.9, adr=0.05)
    day_bar = {"open": 99.0, "high": 101.0, "low": 99.0, "close": 100.8}
    t = as_trigger(find_trigger_daily(c, day_bar, DAILY))
    assert t.fill == pytest.approx(100.51)
    assert t.stop == pytest.approx(98.99)  # min(band 99.9, open 99.0) - 0.01
    assert entry_day_exit(t, None, day_bar, DAILY) is None
    # A low below the capped stop is still the ablation's pessimistic same-day stop.
    assert entry_day_exit(t, None, dict(day_bar, low=98.9), DAILY) == (
        None, pytest.approx(98.99), "stop"
    )
    # The width filter is re-applied to the capped stop.
    narrow = cand("BO", prev_low=99.9, adr=0.03)
    as_skip(find_trigger_daily(narrow, {"open": 96.0, "high": 101.0, "low": 95.5}, DAILY),
            "too_wide")


def test_daily_contract_target_not_checked_on_entry_day():
    cfg = DAILY.with_overrides(exit_mode="contract")
    c = cand("BO", prev_low=99.0, atr=1.0)
    day_bar = {"open": 99.5, "high": 130.0, "low": 99.5, "close": 129.0}
    t = as_trigger(find_trigger_daily(c, day_bar, cfg))
    assert contract_target(t, cfg) < 130.0
    assert entry_day_exit(t, None, day_bar, cfg) is None


def test_daily_contract_floor():
    cfg = DAILY.with_overrides(exit_mode="contract")
    c = cand("BO", prev_low=99.5, atr=6.0, adr=0.05)
    t = as_trigger(find_trigger_daily(c, {"open": 99.8, "high": 101.0, "low": 99.6}, cfg))
    assert t.stop == pytest.approx(100.01 - 3.0)


def test_required_field_checks_return_no_data():
    ep_rows = bars([("09:30", 110.0, 111.0, 109.0, 110.5, 500_000), ("09:35", 110.5, 112, 110, 111)])
    as_skip(find_trigger(cand("EP", adv_shares=float("nan")), ep_rows, CFG0), "no_data")
    as_skip(find_trigger(cand("EP", adv_shares=0.0), ep_rows, CFG0), "no_data")
    as_trigger(find_trigger(cand("EP", adr=float("nan")), ep_rows, CFG0))  # ADR unused intraday
    contract = CFG0.with_overrides(exit_mode="contract")
    as_skip(find_trigger(cand("BO", atr=float("nan")), bars(BO_ROWS), contract), "no_data")
    as_skip(find_trigger(cand("BO", level=None), bars(BO_ROWS), CFG0), "no_data")
    as_trigger(find_trigger(cand("BO", level=None, meta={"pivot": 100.0}), bars(BO_ROWS), CFG0))
    as_skip(find_trigger_daily(cand("EP", prev_close=float("nan")), {"open": 1, "high": 2}, DAILY),
            "no_data")


def test_ep_cutoff_and_level_from_orh():
    rows = [
        ("09:30", 110.0, 111.0, 109.0, 110.5, 500_000),
        ("10:55", 110.5, 110.9, 110.0, 110.8),
        ("11:00", 110.8, 112.0, 110.7, 111.9),
    ]
    as_skip(find_trigger(cand("EP"), bars(rows), CFG0), "after_cutoff")
    t = as_trigger(find_trigger(cand("EP"), bars(rows), CFG0.with_overrides(ep_cutoff="11:05")))
    assert t.ts == ts("11:00")
    assert t.fill == pytest.approx(111.01)
    assert t.stop == pytest.approx(108.99)


def test_naive_timestamps_are_treated_as_eastern():
    df = bars(BO_ROWS)
    df["ts"] = df["ts"].dt.tz_localize(None)
    t = as_trigger(find_trigger(cand("BO"), df, CFG0))
    assert t.ts == ts("09:40")


def test_price_scale_mismatch_between_intraday_and_daily_open_is_skipped():
    # Intraday cache adjusted for a 2:1 split the daily cache does not know about.
    half = [(h, o / 2, hi / 2, lo / 2, c / 2) for h, o, hi, lo, c in BO_ROWS]
    as_skip(find_trigger(cand("BO"), bars(half), CFG0), "price_mismatch")
    ep_half = [("09:30", 55.0, 55.5, 54.5, 55.2, 500_000), ("09:35", 55.2, 56.0, 55.0, 55.9)]
    as_skip(find_trigger(cand("EP", open_today=110.0), bars(ep_half), CFG0), "price_mismatch")
    # Within tolerance (first trade vs official open differ a little) -> unaffected.
    as_trigger(find_trigger(cand("BO", open_today=99.5 * 1.1), bars(BO_ROWS), CFG0))
    # Unknown daily open -> no verdict; BO does not need open_today.
    as_trigger(find_trigger(cand("BO", open_today=float("nan")), bars(BO_ROWS), CFG0))
