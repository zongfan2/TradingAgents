"""Pool builder behavior (specs/pool-builder.md stages 1-5, R1-R5, AC1-AC3).

Fully offline: the deep-search backend and the price fetcher are exercised
only through injected fakes; gate math is checked against hand-computed
fixtures.
"""

import json
import math
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest
import yaml

from pipeline import pool_builder
from pipeline.contracts.base import ContractError
from pipeline.contracts.pool import BollBands, PoolFile, read_pool
from pipeline.pool_builder import (
    BuilderError,
    GateRules,
    bollinger,
    build_pool,
    evaluate_gate,
    liquidity_metrics,
    load_gate_rules,
    main,
    parse_nominations,
    technical_gate,
    weekly_closes,
)

RUN_DATE = date(2026, 8, 10)  # a Monday
FRIDAY = date(2026, 8, 7)  # the trading day before, across the weekend
DATE = RUN_DATE.isoformat()

# Close series with hand-verified gate outcomes (population sigma, 20-window):
# 110 gently-rising bars: daily close 110.9 <= upper*1.02 (113.33), weekly
# close 110.9 >= lower (100.38) => pass.
PASS_CLOSES = [100 + 0.1 * i for i in range(110)]
# Flat 100 then a spike: daily close 200 > upper*1.02 (151.56); weekly close
# 200 >= lower (61.41) => only the weekly leg holds => watch.
OVERHEAT_CLOSES = [100.0] * 109 + [200.0]
# Flat 100 then a collapse: daily close 30 <= upper*1.02 (110.28); weekly
# close 30 < lower (39.51) => only the daily leg holds => watch.
WEEKLY_BROKEN_CLOSES = [100.0] * 105 + [30.0] * 15
# 70 bars (>=60) with a spike: daily overheated AND weekly bands uncomputable
# (14 weeks < 20) => neither leg holds => fail.
NEITHER_CLOSES = [100.0] * 69 + [200.0]
# 59 bars: below the 60-bar minimum => fail regardless of structure.
SHORT_CLOSES = [100.0] * 59

# Default fixture volume profile (gate v1.1): liquid and volume-confirmed so
# the band fixtures above keep exercising the *structure* legs. Base 300k with
# the last 5 bars at 600k => 20d avg volume 375k (avg dollar volume ~= close
# x 375k, >= $20M for close >= ~54) and 5d/20d ratio 600k/375k = 1.6 >= 1.2.
LIQUID_BASE_VOLUME = 300_000.0
LIQUID_CONFIRM_VOLUME = 600_000.0
LIQUID_AVG_VOLUME_20D = 375_000.0  # (15*300k + 5*600k) / 20
LIQUID_VOLUME_RATIO = 1.6  # 600k / 375k


def make_bars(closes, start=date(2026, 3, 2), volumes=None):
    """Daily bars on consecutive weekdays starting at ``start`` (a Monday).

    ``volumes`` (parallel to ``closes``) overrides the default liquid +
    volume-confirmed profile for tests that exercise the v1.1 liquidity legs.
    """
    if volumes is None:
        volumes = [LIQUID_BASE_VOLUME] * len(closes)
        confirm = min(5, len(closes))
        volumes[len(closes) - confirm :] = [LIQUID_CONFIRM_VOLUME] * confirm
    bars = []
    day = start
    for close, volume in zip(closes, volumes, strict=True):
        while day.weekday() >= 5:
            day += timedelta(days=1)
        bars.append((day, close, close + 1.0, close - 1.0, close, volume))
        day += timedelta(days=1)
    return bars


class FakeRunner:
    """Injected in place of the backend subprocess; replays queued outputs."""

    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.calls = []  # list of (backend, prompt)

    def __call__(self, backend, prompt):
        self.calls.append((backend, prompt))
        result = self.outputs.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


class FakeFetcher:
    """Injected price fetcher: {ticker: closes list | prebuilt bars | exception}."""

    def __init__(self, series=None, default=PASS_CLOSES):
        self.series = dict(series or {})
        self.default = default
        self.calls = []

    def __call__(self, ticker):
        self.calls.append(ticker)
        result = self.series.get(ticker, self.default)
        if isinstance(result, BaseException):
            raise result
        if result and isinstance(result[0], tuple):
            return result  # prebuilt bars (custom volume profiles)
        return make_bars(result)


def nominee(ticker="AVGO", score=7.5, catalyst_type="earnings", **overrides):
    entry = {
        "ticker": ticker,
        "score": score,
        "catalyst_type": catalyst_type,
        "rationale": "Q3 earnings on 2026-08-12",
        "citations": ["https://example.com/a"],
    }
    entry.update(overrides)
    return entry


def nomination_json(*entries):
    return json.dumps(list(entries))


def prior_opportunity(ticker="AVGO", score=7.5, low=0, fail=0, entered="2026-08-03"):
    return {
        "ticker": ticker,
        "score": score,
        "catalyst_type": "earnings",
        "rationale": "carried rationale",
        "citations": ["https://example.com/prior"],
        "entered_on": entered,
        "low_score_streak": low,
        "gate_fail_streak": fail,
        "technical": {"gate": "pass"},
    }


def prior_watch(ticker="MRVL", score=6.2):
    return {
        "ticker": ticker,
        "score": score,
        "catalyst_type": "product",
        "rationale": "watching",
        "citations": ["https://example.com/w"],
        "technical": {"gate": "watch"},
    }


def write_prior_pool(pool_dir, date_str, opportunity=(), watch=(), removed=(), session="us"):
    payload = {
        "as_of_date": date_str,
        "session": session,
        "generated_at": f"{date_str}T12:31:00Z",
        "generator": "claude-deep-search",
        "core": [],
        "opportunity": list(opportunity),
        "watch": list(watch),
        "removed": list(removed),
    }
    path = pool_dir / session / f"{date_str}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def write_core(pool_dir, entries, session="us"):
    pool_dir.mkdir(parents=True, exist_ok=True)
    path = pool_dir / f"core.{session}.yaml"
    path.write_text(yaml.safe_dump(entries), encoding="utf-8")
    return path


@pytest.fixture()
def pool_dir(tmp_path, monkeypatch):
    directory = tmp_path / "pools"
    directory.mkdir()
    monkeypatch.setenv(pool_builder.POOL_DIR_ENV, str(directory))
    # These tests' fixtures are claude-flavored; the shipped default backend
    # is codex per D19, so pin the config env (the resolution itself is
    # covered by the dedicated default-backend test below).
    monkeypatch.setenv("TRADINGAGENTS_COLLECT_BACKEND", "claude")
    return directory


def read_written_pool(pool_dir, session="us", date_str=DATE):
    raw = (pool_dir / session / f"{date_str}.json").read_text(encoding="utf-8")
    # Strict writer-mode validation: the builder's output must be
    # contract-valid with zero leniency (AC3).
    return PoolFile.model_validate(json.loads(raw))


def builder_log(pool_dir):
    return (pool_dir / "builder.log").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Gate math: Bollinger hand fixtures (AC1)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_bollinger_hand_computed():
    bands = bollinger(list(range(1, 21)))
    std = math.sqrt(33.25)  # population sigma of 1..20
    assert bands.close == 20.0
    assert bands.mid == pytest.approx(10.5)
    assert bands.upper == pytest.approx(10.5 + 2 * std)
    assert bands.lower == pytest.approx(10.5 - 2 * std)


@pytest.mark.unit
def test_bollinger_uses_trailing_window_only():
    padded = [1_000.0] * 30 + list(range(1, 21))
    assert bollinger(padded) == bollinger(list(range(1, 21)))


@pytest.mark.unit
def test_bollinger_insufficient_history_is_none():
    assert bollinger(list(range(19))) is None
    assert bollinger([]) is None
    assert bollinger(list(range(5)), window=5) is not None


# ---------------------------------------------------------------------------
# Gate math: weekly resample hand fixture (AC1)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_weekly_resample_calendar_weeks_last_close():
    bars = []
    # Full week Mon 2026-03-02 .. Fri 2026-03-06: closes 1..5 -> 5.
    for i, day in enumerate(range(2, 7)):
        bars.append((date(2026, 3, day), 0, 0, 0, float(i + 1), 0))
    # Holiday-short week Mon 2026-03-09 .. Wed 2026-03-11: closes 6..8 -> 8.
    for i, day in enumerate(range(9, 12)):
        bars.append((date(2026, 3, day), 0, 0, 0, float(i + 6), 0))
    # Single Friday bar in the week of 2026-03-16: close 9 -> 9; the empty
    # week in between contributes nothing.
    bars.append((date(2026, 3, 20), 0, 0, 0, 9.0, 0))
    assert weekly_closes(bars) == [5.0, 8.0, 9.0]
    # Input order does not matter (deterministic resample, R2).
    assert weekly_closes(list(reversed(bars))) == [5.0, 8.0, 9.0]


@pytest.mark.unit
def test_weekly_resample_friday_to_monday_starts_a_new_week():
    friday = (date(2026, 8, 7), 0, 0, 0, 10.0, 0)
    monday = (date(2026, 8, 10), 0, 0, 0, 20.0, 0)
    assert weekly_closes([friday, monday]) == [10.0, 20.0]


# ---------------------------------------------------------------------------
# Gate decision matrix (AC1: pass/watch/fail incl. <60 bars)
# ---------------------------------------------------------------------------


def bands(close, upper, lower):
    return BollBands(close=close, mid=(upper + lower) / 2, upper=upper, lower=lower)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("n_bars", "daily", "weekly", "expected"),
    [
        # Both legs hold -> pass.
        (100, bands(100, 110, 90), bands(100, 120, 80), "pass"),
        # Daily overheated only -> watch.
        (100, bands(200, 110, 90), bands(200, 120, 80), "watch"),
        # Weekly broken only -> watch.
        (100, bands(50, 110, 30), bands(50, 120, 80), "watch"),
        # Neither holds -> fail.
        (100, bands(200, 110, 90), bands(200, 300, 250), "fail"),
        # Boundary equalities hold their leg: close == upper*1.02 and
        # close == weekly lower are both still ok.
        (100, bands(112.2, 110, 90), bands(112.2, 130, 112.2), "pass"),
        # A frame with uncomputable bands cannot hold its condition.
        (100, bands(100, 110, 90), None, "watch"),
        (100, None, bands(100, 120, 80), "watch"),
        (100, None, None, "fail"),
        # <60 daily bars -> fail regardless of band structure.
        (59, bands(100, 110, 90), bands(100, 120, 80), "fail"),
    ],
)
def test_evaluate_gate_matrix(n_bars, daily, weekly, expected):
    assert evaluate_gate(n_bars, daily, weekly) == expected


@pytest.mark.unit
@pytest.mark.parametrize(
    ("closes", "expected"),
    [
        (PASS_CLOSES, "pass"),
        (OVERHEAT_CLOSES, "watch"),
        (WEEKLY_BROKEN_CLOSES, "watch"),
        (NEITHER_CLOSES, "fail"),
        (SHORT_CLOSES, "fail"),
    ],
)
def test_technical_gate_on_fixture_series(closes, expected):
    block = technical_gate(make_bars(closes))
    assert block.gate == expected
    # The band snapshot is recorded whenever computable, even on fail.
    assert block.boll_daily is not None
    assert block.boll_daily.close == closes[-1]


@pytest.mark.unit
def test_technical_gate_records_weekly_bands_only_when_computable():
    assert technical_gate(make_bars(NEITHER_CLOSES)).boll_weekly is None  # 14 weeks
    assert technical_gate(make_bars(PASS_CLOSES)).boll_weekly is not None  # 22 weeks


@pytest.mark.unit
def test_exactly_sixty_bars_is_not_a_short_history_fail():
    # 60 flat bars: daily leg holds (sigma 0), weekly bands uncomputable
    # (12 weeks) -> watch, proving the <60 rule did not fire at the boundary.
    assert technical_gate(make_bars([100.0] * 60)).gate == "watch"
    assert technical_gate(make_bars([100.0] * 59)).gate == "fail"


@pytest.mark.unit
def test_gate_thresholds_come_from_config():
    # R2: thresholds are config, not constants — a stricter multiplier flips
    # the same series from pass to watch.
    strict = GateRules(daily_upper_mult=0.99)
    assert technical_gate(make_bars(PASS_CLOSES)).gate == "pass"
    assert technical_gate(make_bars(PASS_CLOSES), strict).gate == "watch"


# ---------------------------------------------------------------------------
# Gate v1.1: liquidity metrics (hand fixtures)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_liquidity_metrics_hand_computed():
    dollar, ratio = liquidity_metrics(make_bars([100.0] * 20))
    assert dollar == pytest.approx(100.0 * LIQUID_AVG_VOLUME_20D)
    assert ratio == pytest.approx(LIQUID_VOLUME_RATIO)
    # Input order does not matter (R2 determinism, same as the resample).
    assert liquidity_metrics(list(reversed(make_bars([100.0] * 20)))) == (dollar, ratio)
    # Fewer than 20 bars: both metrics uncomputable — None, never fabricated.
    assert liquidity_metrics(make_bars([100.0] * 19)) == (None, None)
    assert liquidity_metrics([]) == (None, None)


@pytest.mark.unit
def test_liquidity_metrics_zero_volume_tape():
    dollar, ratio = liquidity_metrics(make_bars([100.0] * 25, volumes=[0.0] * 25))
    assert dollar == 0.0  # a real observation for the veto...
    assert ratio is None  # ...but 0/0 is not a ratio


@pytest.mark.unit
def test_liquidity_metrics_use_trailing_windows_only():
    # 20d window ignores older bars; the 5d window sits inside the 20d one.
    closes = [1_000.0] * 30 + [100.0] * 20
    volumes = [9e9] * 30 + [300_000.0] * 15 + [600_000.0] * 5
    dollar, ratio = liquidity_metrics(make_bars(closes, volumes=volumes))
    assert dollar == pytest.approx(100.0 * 375_000.0)
    assert ratio == pytest.approx(1.6)


@pytest.mark.unit
def test_liquidity_metrics_nan_volume_yields_none_never_nan():
    # A single NaN volume bar inside the trailing window (yfinance emits
    # these on thin tapes): both metrics are uncomputable ⇒ None. NaN must
    # never leak out — the contract rejects it, and NaN < floor is False,
    # which would silently bypass the liquidity veto.
    volumes = [300_000.0] * 25
    volumes[-3] = float("nan")
    assert liquidity_metrics(make_bars([100.0] * 25, volumes=volumes)) == (None, None)
    # A NaN volume OUTSIDE the trailing 20d window is invisible.
    old_gap = [float("nan")] * 5 + [300_000.0] * 20
    dollar, ratio = liquidity_metrics(make_bars([100.0] * 25, volumes=old_gap))
    assert dollar == pytest.approx(100.0 * 300_000.0)
    assert ratio == pytest.approx(1.0)


@pytest.mark.unit
def test_nan_volume_bar_fails_gate_without_crashing():
    # Reproduces the R4 violation: a ≥60-bar tape with one NaN volume used
    # to raise ValidationError out of technical_gate (NaN volume_ratio ≥ 0
    # is False). Now the liquidity snapshot is None and the us floor vetoes.
    volumes = [LIQUID_BASE_VOLUME] * 70
    volumes[-3] = float("nan")
    block = technical_gate(make_bars([100.0] * 70, volumes=volumes), session="us")
    assert block.gate == "fail"  # uncomputable liquidity counts as below floor
    assert block.avg_dollar_volume_20d is None
    assert block.volume_ratio_5d_20d is None


# ---------------------------------------------------------------------------
# Gate v1.1: liquidity floor (hard veto, per-session, checked before structure)
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    ("session", "avg", "expected"),
    [
        ("us", 19_999_999.0, "fail"),  # below the us floor
        ("us", 20_000_000.0, "pass"),  # exactly at the floor is not a veto
        ("us", 20_000_001.0, "pass"),
        ("cn", 99_999_999.0, "fail"),  # below the cn floor
        ("cn", 100_000_000.0, "pass"),  # exactly at the cn floor
        ("us", None, "fail"),  # uncomputable counts as below the floor
        (None, 1.0, "pass"),  # no session context => no floor (unit callers)
    ],
)
def test_liquidity_floor_boundaries_per_session(session, avg, expected):
    verdict = evaluate_gate(
        100,
        bands(100, 110, 90),
        bands(100, 120, 80),
        session=session,
        avg_dollar_volume_20d=avg,
    )
    assert verdict == expected


@pytest.mark.unit
def test_same_dollar_volume_passes_us_but_fails_cn():
    args = (100, bands(100, 110, 90), bands(100, 120, 80))
    kwargs = {"avg_dollar_volume_20d": 50_000_000.0}
    assert evaluate_gate(*args, session="us", **kwargs) == "pass"
    assert evaluate_gate(*args, session="cn", **kwargs) == "fail"


@pytest.mark.unit
def test_liquidity_veto_is_terminal_and_hard():
    # Below the floor => fail regardless of everything else: a would-be-watch
    # structure and a screaming volume ratio never soften the veto.
    verdict = evaluate_gate(
        100,
        bands(200, 110, 90),  # daily overheated: structure alone => watch
        bands(200, 120, 80),
        session="us",
        avg_dollar_volume_20d=1_000.0,
        volume_ratio_5d_20d=9.9,
    )
    assert verdict == "fail"


@pytest.mark.unit
def test_liquidity_floor_comes_from_rules_mapping():
    args = (100, bands(100, 110, 90), bands(100, 120, 80))
    # A session absent from the mapping has no floor.
    no_floor = GateRules(min_avg_dollar_volume={})
    assert evaluate_gate(*args, no_floor, session="us", avg_dollar_volume_20d=1.0) == "pass"
    # A custom floor is honored verbatim.
    custom = GateRules(min_avg_dollar_volume={"us": 2.0})
    assert evaluate_gate(*args, custom, session="us", avg_dollar_volume_20d=1.0) == "fail"
    assert evaluate_gate(*args, custom, session="us", avg_dollar_volume_20d=2.0) == "pass"


@pytest.mark.unit
def test_technical_gate_liquidity_veto_records_full_snapshot():
    # A structurally sound series on an illiquid tape (~$110k/day): hard veto
    # in the us session, with bands AND liquidity numbers still recorded.
    bars = make_bars(PASS_CLOSES, volumes=[1_000.0] * len(PASS_CLOSES))
    block = technical_gate(bars, session="us")
    assert block.gate == "fail"
    assert block.boll_daily is not None and block.boll_weekly is not None
    assert block.avg_dollar_volume_20d == pytest.approx(109.95 * 1_000.0, rel=1e-3)
    assert block.volume_ratio_5d_20d == pytest.approx(1.0)
    # Without a session there is no floor: the flat tape is merely
    # unconfirmed, so the structural pass demotes to watch instead.
    assert technical_gate(bars).gate == "watch"


@pytest.mark.unit
def test_zero_volume_tape_vetoes_without_fabricating_snapshot():
    bars = make_bars(PASS_CLOSES, volumes=[0.0] * len(PASS_CLOSES))
    block = technical_gate(bars, session="us")
    assert block.gate == "fail"
    # The contract records only positive dollar volume; 0/0 is not a ratio.
    assert block.avg_dollar_volume_20d is None
    assert block.volume_ratio_5d_20d is None


# ---------------------------------------------------------------------------
# Gate v1.1: volume confirmation (soft demotion, pass -> watch only)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_unconfirmed_volume_demotes_pass_to_watch():
    # Liquid (~$110M/day) but flat volume (ratio 1.0 < 1.2): structural pass
    # demotes to watch — never to fail.
    flat = [1_000_000.0] * len(PASS_CLOSES)
    block = technical_gate(make_bars(PASS_CLOSES, volumes=flat), session="us")
    assert block.gate == "watch"
    assert block.volume_ratio_5d_20d == pytest.approx(1.0)
    assert block.avg_dollar_volume_20d > 20_000_000.0  # liquidity was fine


@pytest.mark.unit
def test_volume_ratio_exactly_at_boundary_is_confirmed():
    # ratio == volume_confirm_ratio exactly: 15 bars at 700k + 5 at 900k
    # => 20d avg 750k, 900k/750k = 1.2. Demotion is strictly-below only.
    volumes = [700_000.0] * (len(PASS_CLOSES) - 5) + [900_000.0] * 5
    block = technical_gate(make_bars(PASS_CLOSES, volumes=volumes), session="us")
    assert block.volume_ratio_5d_20d == pytest.approx(1.2)
    assert block.gate == "pass"


@pytest.mark.unit
def test_volume_demotion_never_touches_watch_or_fail():
    flat_watch = [1_000_000.0] * len(OVERHEAT_CLOSES)
    watch_block = technical_gate(make_bars(OVERHEAT_CLOSES, volumes=flat_watch), session="us")
    assert watch_block.gate == "watch"  # watch stays watch, never lower
    flat_fail = [1_000_000.0] * len(NEITHER_CLOSES)
    fail_block = technical_gate(make_bars(NEITHER_CLOSES, volumes=flat_fail), session="us")
    assert fail_block.gate == "fail"  # fail is never affected
    # ...and a confirmed ratio never rescues a structural fail either.
    assert technical_gate(make_bars(NEITHER_CLOSES), session="us").gate == "fail"


@pytest.mark.unit
def test_volume_confirm_ratio_comes_from_rules():
    flat = make_bars(PASS_CLOSES, volumes=[1_000_000.0] * len(PASS_CLOSES))
    assert technical_gate(flat, session="us").gate == "watch"  # default 1.2
    lenient = GateRules(volume_confirm_ratio=1.0)
    assert technical_gate(flat, lenient, session="us").gate == "pass"


# ---------------------------------------------------------------------------
# Gate v1.1: snapshot recorded on every path where computable
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_snapshot_records_liquidity_fields_on_structural_fail():
    block = technical_gate(make_bars(NEITHER_CLOSES), session="us")
    assert block.gate == "fail"
    assert block.avg_dollar_volume_20d is not None
    assert block.volume_ratio_5d_20d == pytest.approx(LIQUID_VOLUME_RATIO)
    # The <60-bar fail still records the computable 20d numbers...
    short = technical_gate(make_bars(SHORT_CLOSES), session="us")
    assert short.gate == "fail"
    assert short.avg_dollar_volume_20d == pytest.approx(100.0 * LIQUID_AVG_VOLUME_20D)
    assert short.volume_ratio_5d_20d == pytest.approx(LIQUID_VOLUME_RATIO)
    # ...while a sub-20-bar history records None, never a fabricated number.
    tiny = technical_gate(make_bars([100.0] * 10), session="us")
    assert tiny.gate == "fail"
    assert tiny.avg_dollar_volume_20d is None and tiny.volume_ratio_5d_20d is None


# ---------------------------------------------------------------------------
# Gate v1.1: config/env overrides (load_gate_rules)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_load_gate_rules_spec_defaults():
    rules = load_gate_rules({})
    assert rules.min_avg_dollar_volume == {"us": 20_000_000.0, "cn": 100_000_000.0}
    assert rules.volume_confirm_ratio == 1.2


@pytest.mark.unit
def test_load_gate_rules_env_overrides():
    rules = load_gate_rules(
        {
            "TRADINGAGENTS_MIN_AVG_DOLLAR_VOLUME_US": "5000000",
            "TRADINGAGENTS_VOLUME_CONFIRM_RATIO": "1.5",
        }
    )
    assert rules.min_avg_dollar_volume == {"us": 5_000_000.0, "cn": 100_000_000.0}
    assert rules.volume_confirm_ratio == 1.5
    cn_only = load_gate_rules({"TRADINGAGENTS_MIN_AVG_DOLLAR_VOLUME_CN": "1"})
    assert cn_only.min_avg_dollar_volume == {"us": 20_000_000.0, "cn": 1.0}
    assert cn_only.volume_confirm_ratio == 1.2
    # Blank values fall back to the defaults (unset-equivalent).
    blank = load_gate_rules({"TRADINGAGENTS_VOLUME_CONFIRM_RATIO": "  "})
    assert blank.volume_confirm_ratio == 1.2


@pytest.mark.unit
def test_load_gate_rules_loud_on_junk():
    with pytest.raises(ValueError):
        load_gate_rules({"TRADINGAGENTS_VOLUME_CONFIRM_RATIO": "lots"})
    with pytest.raises(ValueError):
        load_gate_rules({"TRADINGAGENTS_MIN_AVG_DOLLAR_VOLUME_US": "20M"})


@pytest.mark.unit
def test_gate_env_overrides_flow_into_build_pool(pool_dir, monkeypatch):
    # A structurally sound but illiquid nominee (~$110k/day) fails the
    # default us floor and routes to watch (fail + score >= entry)...
    illiquid = make_bars(PASS_CLOSES, volumes=[1_000.0] * len(PASS_CLOSES))
    fetcher = FakeFetcher({"AVGO": illiquid})
    build_pool(
        "us",
        as_of=RUN_DATE,
        runner=FakeRunner([nomination_json(nominee("AVGO", 7.5))]),
        fetch_ohlcv=fetcher,
    )
    pool = read_written_pool(pool_dir)
    assert pool.opportunity == []
    (watched,) = pool.watch
    assert watched.ticker == "AVGO" and watched.technical.gate == "fail"
    assert watched.technical.avg_dollar_volume_20d == pytest.approx(
        109.95 * 1_000.0, rel=1e-3
    )
    # ...and enters once the env lowers the floor and the confirm ratio
    # (same-day rerun rides the nomination cache — no second deep search).
    monkeypatch.setenv("TRADINGAGENTS_MIN_AVG_DOLLAR_VOLUME_US", "50000")
    monkeypatch.setenv("TRADINGAGENTS_VOLUME_CONFIRM_RATIO", "1.0")
    build_pool("us", as_of=RUN_DATE, runner=FakeRunner([]), fetch_ohlcv=fetcher)
    (entered,) = read_written_pool(pool_dir).opportunity
    assert entered.ticker == "AVGO"
    assert entered.technical.gate == "pass"


@pytest.mark.unit
def test_injected_gate_rules_beat_env(pool_dir, monkeypatch):
    monkeypatch.setenv("TRADINGAGENTS_MIN_AVG_DOLLAR_VOLUME_US", str(10**15))
    rules = GateRules(min_avg_dollar_volume={}, volume_confirm_ratio=1.0)
    build_pool(
        "us",
        as_of=RUN_DATE,
        runner=FakeRunner([nomination_json(nominee())]),
        fetch_ohlcv=FakeFetcher(),
        gate_rules=rules,
    )
    assert read_written_pool(pool_dir).opportunity[0].ticker == "AVGO"


# ---------------------------------------------------------------------------
# Gate v1.1: hysteresis treats a liquidity fail exactly like a structural fail
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_single_liquidity_fail_advances_streak_without_exit(pool_dir):
    write_prior_pool(pool_dir, FRIDAY.isoformat(), opportunity=[prior_opportunity("AVGO")])
    illiquid = make_bars(PASS_CLOSES, volumes=[1_000.0] * len(PASS_CLOSES))
    build_pool(
        "us",
        as_of=RUN_DATE,
        runner=FakeRunner([nomination_json(nominee("AVGO", 8.0))]),
        fetch_ohlcv=FakeFetcher({"AVGO": illiquid}),
    )
    (avgo,) = read_written_pool(pool_dir).opportunity  # still a member
    assert avgo.gate_fail_streak == 1
    assert avgo.technical.gate == "fail"
    assert avgo.technical.avg_dollar_volume_20d is not None  # snapshot on fail


@pytest.mark.unit
def test_liquidity_fail_completes_the_two_pool_exit(pool_dir):
    # spec treats fail uniformly: liquidity-driven gate fail advances the
    # streak exactly like a structural fail and exits with the same reason.
    write_prior_pool(
        pool_dir, FRIDAY.isoformat(), opportunity=[prior_opportunity("AVGO", fail=1)]
    )
    illiquid = make_bars(PASS_CLOSES, volumes=[1_000.0] * len(PASS_CLOSES))
    build_pool(
        "us",
        as_of=RUN_DATE,
        runner=FakeRunner([nomination_json(nominee("AVGO", 8.0))]),
        fetch_ohlcv=FakeFetcher({"AVGO": illiquid}),
    )
    pool = read_written_pool(pool_dir)
    assert pool.opportunity == []
    (removed,) = pool.removed
    assert removed.reason == "technical gate fail for 2 pools"
    assert removed.last_score == 8.0


@pytest.mark.unit
def test_demoted_watch_verdict_resets_member_gate_fail_streak(pool_dir):
    # Demotion lands on watch, not fail: a member one step from the 2-pool
    # exit recovers its streak on an unconfirmed-but-liquid day.
    write_prior_pool(
        pool_dir, FRIDAY.isoformat(), opportunity=[prior_opportunity("AVGO", fail=1)]
    )
    flat = make_bars(PASS_CLOSES, volumes=[1_000_000.0] * len(PASS_CLOSES))
    build_pool(
        "us",
        as_of=RUN_DATE,
        runner=FakeRunner([nomination_json(nominee("AVGO", 8.0))]),
        fetch_ohlcv=FakeFetcher({"AVGO": flat}),
    )
    (avgo,) = read_written_pool(pool_dir).opportunity
    assert avgo.technical.gate == "watch"
    assert avgo.gate_fail_streak == 0


@pytest.mark.unit
def test_unconfirmed_volume_routes_new_nominee_to_watch(pool_dir):
    flat = make_bars(PASS_CLOSES, volumes=[1_000_000.0] * len(PASS_CLOSES))
    build_pool(
        "us",
        as_of=RUN_DATE,
        runner=FakeRunner([nomination_json(nominee("AVGO", 7.5))]),
        fetch_ohlcv=FakeFetcher({"AVGO": flat}),
    )
    pool = read_written_pool(pool_dir)
    assert pool.opportunity == []  # demoted verdict cannot enter
    (watched,) = pool.watch
    assert watched.technical.gate == "watch"
    assert watched.technical.volume_ratio_5d_20d == pytest.approx(1.0)


@pytest.mark.unit
def test_cn_session_build_applies_the_cn_floor(pool_dir):
    # ~$68M/day with a confirmed ratio: clears the us floor, not the cn one.
    volumes = [500_000.0] * (len(PASS_CLOSES) - 5) + [1_000_000.0] * 5
    bars = make_bars(PASS_CLOSES, volumes=volumes)
    assert technical_gate(bars, session="us").gate == "pass"
    assert technical_gate(bars, session="cn").gate == "fail"
    build_pool(
        "cn",
        as_of=RUN_DATE,
        runner=FakeRunner([nomination_json(nominee("0700.HK", 8.0))]),
        fetch_ohlcv=FakeFetcher({"0700.HK": bars}),
    )
    pool = read_written_pool(pool_dir, session="cn")
    assert pool.opportunity == []
    (watched,) = pool.watch  # liquidity fail + strong narrative => watch
    assert watched.ticker == "0700.HK" and watched.technical.gate == "fail"


@pytest.mark.unit
def test_fetch_error_snapshot_has_no_liquidity_numbers(pool_dir):
    # Streak-freeze semantics stay intact: a None snapshot stays None — the
    # builder never fabricates liquidity numbers for an unfetched ticker.
    write_prior_pool(
        pool_dir, FRIDAY.isoformat(), opportunity=[prior_opportunity("AVGO", fail=1)]
    )
    build_pool(
        "us",
        as_of=RUN_DATE,
        runner=FakeRunner([nomination_json(nominee("AVGO", 8.0))]),
        fetch_ohlcv=FakeFetcher({"AVGO": ConnectionError("vendor down")}),
    )
    (avgo,) = read_written_pool(pool_dir).opportunity
    assert avgo.gate_fail_streak == 1  # frozen — infra error, not a verdict
    assert avgo.technical.avg_dollar_volume_20d is None
    assert avgo.technical.volume_ratio_5d_20d is None
    assert avgo.technical.boll_daily is None


@pytest.mark.unit
def test_nan_volume_ticker_never_kills_the_pool_build(pool_dir):
    # R4 'degrade, don't die' end to end: one ticker's NaN-volume tape used
    # to crash build_pool (exit 1, no pool file, the whole slot down). Now
    # that ticker gates 'fail' on uncomputable liquidity (strong narrative ⇒
    # watch) while the rest of the slot proceeds normally.
    volumes = [LIQUID_BASE_VOLUME] * len(PASS_CLOSES)
    volumes[-3] = float("nan")
    build_pool(
        "us",
        as_of=RUN_DATE,
        runner=FakeRunner([nomination_json(nominee("AVGO", 8.0), nominee("MRVL", 7.9))]),
        fetch_ohlcv=FakeFetcher({"AVGO": make_bars(PASS_CLOSES, volumes=volumes)}),
    )
    pool = read_written_pool(pool_dir)
    (mrvl,) = pool.opportunity  # the healthy nominee entered normally
    assert mrvl.ticker == "MRVL" and mrvl.technical.gate == "pass"
    (avgo,) = pool.watch
    assert avgo.ticker == "AVGO" and avgo.technical.gate == "fail"
    assert avgo.technical.avg_dollar_volume_20d is None
    assert avgo.technical.volume_ratio_5d_20d is None


@pytest.mark.unit
def test_gate_computation_error_degrades_like_a_fetch_failure(pool_dir):
    # Defense in depth behind the NaN fix: if gate math raises on one
    # ticker's pathological bars, the build still completes and the member
    # freezes its streak exactly like an OHLCV fetch failure.
    write_prior_pool(
        pool_dir, FRIDAY.isoformat(), opportunity=[prior_opportunity("AVGO", fail=1)]
    )
    junk_bars = [(date(2026, 3, 2), 100.0, 101.0, 99.0, "not-a-close", 1.0)]
    build_pool(
        "us",
        as_of=RUN_DATE,
        runner=FakeRunner([nomination_json(nominee("AVGO", 8.0))]),
        fetch_ohlcv=FakeFetcher({"AVGO": junk_bars}),
    )
    (avgo,) = read_written_pool(pool_dir).opportunity
    assert avgo.gate_fail_streak == 1  # frozen — infra error, not a verdict
    assert avgo.technical.boll_daily is None
    assert avgo.technical.avg_dollar_volume_20d is None


# ---------------------------------------------------------------------------
# Nomination JSON validation (AC2)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_parse_nominations_accepts_strict_and_fenced_json():
    entries = parse_nominations(nomination_json(nominee()))
    assert entries[0].ticker == "AVGO"
    fenced = "Here you go:\n```json\n" + nomination_json(nominee()) + "\n```\n"
    assert parse_nominations(fenced)[0].ticker == "AVGO"


@pytest.mark.unit
def test_parse_nominations_drops_unknown_keys_leniently(caplog):
    with caplog.at_level("WARNING", logger="pipeline.contracts"):
        entries = parse_nominations(nomination_json(nominee(confidence="high")))
    assert entries[0].ticker == "AVGO"
    assert "confidence" in caplog.text


@pytest.mark.unit
def test_parse_nominations_collects_every_error():
    raw = nomination_json(
        nominee(ticker="AAA", catalyst_type="rumor"),
        nominee(ticker="BBB", score=11.0),
        nominee(ticker="CCC", citations=["ftp://example.com/x"]),
    )
    with pytest.raises(ContractError) as excinfo:
        parse_nominations(raw)
    errors = "; ".join(excinfo.value.errors)
    assert "nomination[0].catalyst_type" in errors
    assert "nomination[1].score" in errors
    assert "not an http(s) URL" in errors


@pytest.mark.unit
def test_parse_nominations_rejects_non_array_output():
    with pytest.raises(ContractError, match="no JSON array"):
        parse_nominations("I could not find any candidates today.")
    with pytest.raises(ContractError, match="not a JSON object"):
        parse_nominations('["AVGO", "MRVL"]')


# ---------------------------------------------------------------------------
# Fresh run: stages 1-5 end to end (AC3 dry run)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_fresh_run_writes_contract_valid_pool(pool_dir):
    write_core(pool_dir, [{"ticker": "NVDA", "note": "holding"}])
    runner = FakeRunner([nomination_json(nominee("AVGO", 7.5), nominee("NVDA", 6.8))])
    fetcher = FakeFetcher()

    result = build_pool("us", as_of=RUN_DATE, runner=runner, fetch_ohlcv=fetcher)

    assert result.outcome == "written"
    assert result.path == pool_dir / "us" / f"{DATE}.json"
    pool = read_written_pool(pool_dir)  # strict validation — AC3
    assert pool.as_of_date == RUN_DATE
    assert pool.session == "us"
    assert pool.generator == "claude-deep-search"
    assert pool.carried_forward is False
    # Core mirrors the yaml verbatim, annotated with score + technical.
    assert [(c.ticker, c.note) for c in pool.core] == [("NVDA", "holding")]
    assert pool.core[0].score == 6.8
    assert pool.core[0].technical.gate == "pass"
    # AVGO entered: gate pass + score >= 6.0, fresh hysteresis state.
    (avgo,) = pool.opportunity
    assert avgo.ticker == "AVGO"
    assert avgo.entered_on == RUN_DATE
    assert avgo.low_score_streak == 0 and avgo.gate_fail_streak == 0
    assert avgo.technical.gate == "pass"
    assert avgo.technical.boll_daily is not None and avgo.technical.boll_weekly is not None
    # One log line: date, session, per-layer counts, entries/exits.
    assert (
        f"{DATE} | us | core=1 opp=1 watch=0 removed=0 | enter=AVGO | exit=- "
        "| truncated=- | written" in builder_log(pool_dir)
    )
    # The prompt rendered every placeholder.
    backend, prompt = runner.calls[0]
    assert backend == "claude"
    assert f"us session on {DATE}" in prompt
    assert "NVDA" in prompt  # core listed for re-scoring
    assert "opportunity <= 5, watch <= 10" in prompt  # caps rendered
    assert "{{" not in prompt
    # The day's validated nomination is cached for same-day reruns (R3),
    # stamped with the originating backend (provenance for `generator`).
    cache = pool_builder.nomination_cache_path(pool_dir, "us", RUN_DATE)
    cached = json.loads(cache.read_text(encoding="utf-8"))
    assert cached["backend"] == "claude"
    assert cached["nominations"][0]["ticker"] == "AVGO"


@pytest.mark.unit
def test_default_backend_is_codex_when_env_unset(pool_dir, monkeypatch):
    # D19: with no TRADINGAGENTS_COLLECT_BACKEND set, nomination runs on codex
    # and both the pool's generator and the cache provenance record it.
    monkeypatch.delenv("TRADINGAGENTS_COLLECT_BACKEND", raising=False)
    runner = FakeRunner([nomination_json(nominee("AVGO", 7.5))])
    result = build_pool("us", as_of=RUN_DATE, runner=runner, fetch_ohlcv=FakeFetcher())
    assert result.outcome == "written"
    assert runner.calls[0][0] == "codex"
    assert read_written_pool(pool_dir).generator == "codex-deep-search"
    cache = pool_builder.nomination_cache_path(pool_dir, "us", RUN_DATE)
    assert json.loads(cache.read_text(encoding="utf-8"))["backend"] == "codex"


@pytest.mark.unit
def test_explicit_backend_beats_the_env_default(pool_dir):
    # pool_dir pins TRADINGAGENTS_COLLECT_BACKEND=claude; the explicit
    # argument still wins (CLI --backend routes through the same parameter).
    runner = FakeRunner([nomination_json(nominee("AVGO", 7.5))])
    build_pool("us", as_of=RUN_DATE, backend="codex", runner=runner, fetch_ohlcv=FakeFetcher())
    assert runner.calls[0][0] == "codex"
    assert read_written_pool(pool_dir).generator == "codex-deep-search"


@pytest.mark.unit
def test_written_pool_satisfies_consumer_reading_rule(pool_dir):
    write_core(pool_dir, [{"ticker": "NVDA"}])
    build_pool(
        "us",
        as_of=RUN_DATE,
        runner=FakeRunner([nomination_json(nominee())]),
        fetch_ohlcv=FakeFetcher(),
    )
    read = read_pool(pool_dir, "us", RUN_DATE)
    assert read.staleness == "fresh"
    assert read.pool.opportunity[0].ticker == "AVGO"


# ---------------------------------------------------------------------------
# R1 — core is read-only truth, mirrored verbatim
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_core_mirrored_verbatim_even_when_gate_fails(pool_dir):
    write_core(pool_dir, [{"ticker": "MSFT"}, {"ticker": "AAPL", "note": "long"}])
    core_yaml_before = (pool_dir / "core.us.yaml").read_text(encoding="utf-8")
    fetcher = FakeFetcher({"AAPL": SHORT_CLOSES})

    build_pool("us", as_of=RUN_DATE, runner=FakeRunner(["[]"]), fetch_ohlcv=fetcher)

    pool = read_written_pool(pool_dir)
    # Order and notes preserved; the failing gate never drops a core ticker.
    assert [(c.ticker, c.note) for c in pool.core] == [("MSFT", None), ("AAPL", "long")]
    assert pool.core[1].technical.gate == "fail"  # informative annotation only
    assert pool.core[0].score is None  # not re-scored by the empty nomination
    # The builder never writes the core yaml (R1).
    assert (pool_dir / "core.us.yaml").read_text(encoding="utf-8") == core_yaml_before


@pytest.mark.unit
def test_nominated_core_ticker_stays_core_only(pool_dir):
    write_core(pool_dir, [{"ticker": "NVDA"}])
    runner = FakeRunner([nomination_json(nominee("NVDA", 9.0))])
    build_pool("us", as_of=RUN_DATE, runner=runner, fetch_ohlcv=FakeFetcher())
    pool = read_written_pool(pool_dir)
    assert pool.core[0].score == 9.0
    assert pool.opportunity == [] and pool.watch == []  # no cross-layer duplicate


@pytest.mark.unit
def test_prior_member_promoted_to_core_folds_into_core(pool_dir):
    write_core(pool_dir, [{"ticker": "AVGO"}])
    write_prior_pool(pool_dir, FRIDAY.isoformat(), opportunity=[prior_opportunity("AVGO")])
    build_pool("us", as_of=RUN_DATE, runner=FakeRunner(["[]"]), fetch_ohlcv=FakeFetcher())
    pool = read_written_pool(pool_dir)
    assert [c.ticker for c in pool.core] == ["AVGO"]
    assert pool.opportunity == [] and pool.removed == []


@pytest.mark.unit
def test_missing_core_file_warns_and_builds_empty_core(pool_dir, capsys):
    result = build_pool(
        "us", as_of=RUN_DATE, runner=FakeRunner(["[]"]), fetch_ohlcv=FakeFetcher()
    )
    assert result.outcome == "written"
    assert read_written_pool(pool_dir).core == []
    assert "core.us.yaml" in capsys.readouterr().err  # stderr warning


@pytest.mark.unit
@pytest.mark.parametrize(
    "content",
    [
        "[unclosed",  # not valid YAML
        "ticker: NVDA",  # a mapping, not a list
        "- note: missing ticker",  # entry without a ticker
        "- ticker: 0700.HK",  # wrong market for the us session
    ],
)
def test_unreadable_core_is_a_hard_failure(pool_dir, content):
    (pool_dir / "core.us.yaml").write_text(content, encoding="utf-8")
    with pytest.raises(BuilderError, match="core file"):
        build_pool("us", as_of=RUN_DATE, runner=FakeRunner(["[]"]), fetch_ohlcv=FakeFetcher())
    assert not (pool_dir / "us" / f"{DATE}.json").exists()
    assert f"{DATE} | us | claude | - | failed" in builder_log(pool_dir)


@pytest.mark.unit
def test_corrupt_prior_pool_warns_on_stderr_and_starts_fresh(pool_dir, capsys):
    # An unreadable prior file drops every prior member's hysteresis state —
    # that must be loud (stderr, like the R4 warning), never log-only.
    path = pool_dir / "us" / f"{FRIDAY.isoformat()}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    result = build_pool(
        "us",
        as_of=RUN_DATE,
        runner=FakeRunner([nomination_json(nominee())]),
        fetch_ohlcv=FakeFetcher(),
    )
    assert result.outcome == "written"  # degrade, not die (R4)
    err = capsys.readouterr().err
    assert "pool-builder: warning: prior pool unreadable" in err
    (avgo,) = read_written_pool(pool_dir).opportunity
    assert avgo.entered_on == RUN_DATE  # fresh hysteresis state


# ---------------------------------------------------------------------------
# Hysteresis: enter fast (AC1)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_candidate_routing_decision_table(pool_dir):
    runner = FakeRunner(
        [
            nomination_json(
                nominee("AAA", 7.0),  # pass + >=6.0 -> opportunity
                nominee("BBB", 5.0),  # pass + [4,6) -> watch
                nominee("CCC", 8.0),  # gate watch -> watch
                nominee("DDD", 8.0),  # gate fail + >=6.0 -> watch
                nominee("EEE", 5.9),  # gate fail + <6.0 -> dropped
                nominee("FFF", 3.9),  # pass + <4.0 -> dropped
            )
        ]
    )
    fetcher = FakeFetcher({"CCC": OVERHEAT_CLOSES, "DDD": NEITHER_CLOSES, "EEE": NEITHER_CLOSES})
    build_pool("us", as_of=RUN_DATE, runner=runner, fetch_ohlcv=fetcher)
    pool = read_written_pool(pool_dir)
    assert [e.ticker for e in pool.opportunity] == ["AAA"]
    # Watch sorted by score desc, ticker asc on ties.
    assert [e.ticker for e in pool.watch] == ["CCC", "DDD", "BBB"]
    assert {e.ticker: e.technical.gate for e in pool.watch} == {
        "CCC": "watch",
        "DDD": "fail",
        "BBB": "pass",
    }
    all_tickers = {e.ticker for e in pool.opportunity + pool.watch + pool.removed}
    assert "EEE" not in all_tickers and "FFF" not in all_tickers


@pytest.mark.unit
def test_reentry_after_removal_via_normal_enter_rule(pool_dir):
    write_prior_pool(
        pool_dir,
        FRIDAY.isoformat(),
        removed=[{"ticker": "AVGO", "reason": "score<4 for 3 pools", "last_score": 3.1}],
    )
    build_pool(
        "us",
        as_of=RUN_DATE,
        runner=FakeRunner([nomination_json(nominee("AVGO", 7.5))]),
        fetch_ohlcv=FakeFetcher(),
    )
    (avgo,) = read_written_pool(pool_dir).opportunity
    assert avgo.entered_on == RUN_DATE  # fresh entry, fresh streaks
    assert avgo.low_score_streak == 0 and avgo.gate_fail_streak == 0


# ---------------------------------------------------------------------------
# Hysteresis: exit slow + decay (AC1)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_absent_member_decays_but_stays(pool_dir):
    write_prior_pool(pool_dir, FRIDAY.isoformat(), opportunity=[prior_opportunity("AVGO")])
    build_pool("us", as_of=RUN_DATE, runner=FakeRunner(["[]"]), fetch_ohlcv=FakeFetcher())
    (avgo,) = read_written_pool(pool_dir).opportunity
    assert avgo.score == 7.5  # silence keeps the last score
    assert avgo.rationale == "carried rationale"
    assert avgo.citations == ["https://example.com/prior"]
    assert avgo.low_score_streak == 1  # counts as below the exit threshold
    assert avgo.gate_fail_streak == 0  # today's gate passed
    assert avgo.entered_on == date(2026, 8, 3)  # membership origin preserved


@pytest.mark.unit
def test_low_score_exit_after_three_pools(pool_dir):
    write_prior_pool(
        pool_dir, FRIDAY.isoformat(), opportunity=[prior_opportunity("AVGO", low=2)]
    )
    build_pool(
        "us",
        as_of=RUN_DATE,
        runner=FakeRunner([nomination_json(nominee("AVGO", 3.0))]),
        fetch_ohlcv=FakeFetcher(),
    )
    pool = read_written_pool(pool_dir)
    assert pool.opportunity == []
    (removed,) = pool.removed
    assert removed.ticker == "AVGO"
    assert removed.reason == "score<4 for 3 pools"
    assert removed.last_score == 3.0
    assert "exit=AVGO" in builder_log(pool_dir)


@pytest.mark.unit
def test_absent_member_exits_after_three_silent_pools(pool_dir):
    write_prior_pool(
        pool_dir, FRIDAY.isoformat(), opportunity=[prior_opportunity("AVGO", low=2)]
    )
    build_pool("us", as_of=RUN_DATE, runner=FakeRunner(["[]"]), fetch_ohlcv=FakeFetcher())
    (removed,) = read_written_pool(pool_dir).removed
    assert removed.ticker == "AVGO"
    assert removed.last_score == 7.5  # the carried score, not a fabricated one


@pytest.mark.unit
def test_gate_fail_exit_after_two_pools_despite_high_score(pool_dir):
    write_prior_pool(
        pool_dir, FRIDAY.isoformat(), opportunity=[prior_opportunity("AVGO", fail=1)]
    )
    build_pool(
        "us",
        as_of=RUN_DATE,
        runner=FakeRunner([nomination_json(nominee("AVGO", 8.0))]),
        fetch_ohlcv=FakeFetcher({"AVGO": NEITHER_CLOSES}),
    )
    pool = read_written_pool(pool_dir)
    assert pool.opportunity == []
    (removed,) = pool.removed
    assert removed.reason == "technical gate fail for 2 pools"
    assert removed.last_score == 8.0


@pytest.mark.unit
def test_one_bad_pool_does_not_exit_and_streaks_track(pool_dir):
    write_prior_pool(pool_dir, FRIDAY.isoformat(), opportunity=[prior_opportunity("AVGO")])
    build_pool(
        "us",
        as_of=RUN_DATE,
        runner=FakeRunner([nomination_json(nominee("AVGO", 3.5))]),
        fetch_ohlcv=FakeFetcher({"AVGO": NEITHER_CLOSES}),
    )
    (avgo,) = read_written_pool(pool_dir).opportunity  # still a member
    assert avgo.low_score_streak == 1
    assert avgo.gate_fail_streak == 1
    assert avgo.technical.gate == "fail"  # today's snapshot recorded


@pytest.mark.unit
def test_streaks_reset_on_recovery(pool_dir):
    write_prior_pool(
        pool_dir, FRIDAY.isoformat(), opportunity=[prior_opportunity("AVGO", low=2, fail=1)]
    )
    build_pool(
        "us",
        as_of=RUN_DATE,
        runner=FakeRunner([nomination_json(nominee("AVGO", 6.5))]),
        fetch_ohlcv=FakeFetcher(),
    )
    (avgo,) = read_written_pool(pool_dir).opportunity
    assert avgo.low_score_streak == 0 and avgo.gate_fail_streak == 0
    assert avgo.score == 6.5


@pytest.mark.unit
def test_friday_to_monday_gap_carries_streaks_intact(pool_dir):
    # Streaks count generated pools, not calendar days: the weekend gap
    # advances the streak by exactly one, never resets it.
    write_prior_pool(
        pool_dir, FRIDAY.isoformat(), opportunity=[prior_opportunity("AVGO", low=1)]
    )
    build_pool("us", as_of=RUN_DATE, runner=FakeRunner(["[]"]), fetch_ohlcv=FakeFetcher())
    (avgo,) = read_written_pool(pool_dir).opportunity
    assert avgo.low_score_streak == 2


@pytest.mark.unit
def test_prior_watch_absent_from_nomination_disappears(pool_dir):
    # Watch carries no hysteresis state — it is rebuilt from each nomination.
    write_prior_pool(pool_dir, FRIDAY.isoformat(), watch=[prior_watch("MRVL")])
    runner = FakeRunner(["[]"])
    build_pool("us", as_of=RUN_DATE, runner=runner, fetch_ohlcv=FakeFetcher())
    assert read_written_pool(pool_dir).watch == []
    assert "MRVL" in runner.calls[0][1]  # but it was offered for re-scoring


# ---------------------------------------------------------------------------
# R3 — idempotent rerun, nomination cache, --force
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_same_day_rerun_reuses_cache_and_never_double_advances(pool_dir):
    write_prior_pool(
        pool_dir, FRIDAY.isoformat(), opportunity=[prior_opportunity("AVGO", low=1)]
    )
    build_pool("us", as_of=RUN_DATE, runner=FakeRunner(["[]"]), fetch_ohlcv=FakeFetcher())
    assert read_written_pool(pool_dir).opportunity[0].low_score_streak == 2

    rerun_runner = FakeRunner([])  # any backend call would crash the fake
    result = build_pool("us", as_of=RUN_DATE, runner=rerun_runner, fetch_ohlcv=FakeFetcher())
    assert result.outcome == "written"
    assert rerun_runner.calls == []  # cache hit — no second deep search (R5)
    # Hysteresis re-read Friday's file, never today's own output: still 2.
    assert read_written_pool(pool_dir).opportunity[0].low_score_streak == 2
    # The displaced same-day revision was archived (immutable revisions).
    archived = list((pool_dir / "us" / "archive").glob(f"{DATE}.*.json"))
    assert len(archived) == 1
    assert json.loads(archived[0].read_text(encoding="utf-8"))["as_of_date"] == DATE


@pytest.mark.unit
def test_force_reruns_nomination_and_overwrites_cache(pool_dir):
    build_pool(
        "us",
        as_of=RUN_DATE,
        runner=FakeRunner([nomination_json(nominee("AVGO", 7.5))]),
        fetch_ohlcv=FakeFetcher(),
    )
    forced = FakeRunner([nomination_json(nominee("MRVL", 8.0, "product"))])
    build_pool("us", as_of=RUN_DATE, force=True, runner=forced, fetch_ohlcv=FakeFetcher())
    assert len(forced.calls) == 1  # --force re-searched
    cache = pool_builder.nomination_cache_path(pool_dir, "us", RUN_DATE)
    assert json.loads(cache.read_text(encoding="utf-8"))["nominations"][0]["ticker"] == "MRVL"
    assert read_written_pool(pool_dir).opportunity[0].ticker == "MRVL"


@pytest.mark.unit
def test_cache_hit_stamps_the_originating_backend_as_generator(pool_dir):
    # A same-day `--backend codex` rerun reuses claude's cached nominations —
    # the pool's generator must record where the search actually came from,
    # not the backend the rerun merely requested.
    build_pool(
        "us",
        as_of=RUN_DATE,
        runner=FakeRunner([nomination_json(nominee())]),
        fetch_ohlcv=FakeFetcher(),
    )
    rerun = FakeRunner([])  # any backend call would crash the fake
    result = build_pool(
        "us", as_of=RUN_DATE, backend="codex", runner=rerun, fetch_ohlcv=FakeFetcher()
    )
    assert result.outcome == "written"
    assert rerun.calls == []  # cache hit — no second deep search (R3/R5)
    assert read_written_pool(pool_dir).generator == "claude-deep-search"


@pytest.mark.unit
@pytest.mark.parametrize(
    "content",
    [
        "{not json",  # unparseable
        '[{"ticker": "AVGO"}]',  # legacy bare-list payload — no provenance
        '{"backend": "gemini", "nominations": []}',  # unknown backend
    ],
)
def test_corrupt_cache_falls_back_to_a_fresh_search(pool_dir, caplog, content):
    cache = pool_builder.nomination_cache_path(pool_dir, "us", RUN_DATE)
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(content, encoding="utf-8")
    runner = FakeRunner([nomination_json(nominee())])
    with caplog.at_level("WARNING", logger="pipeline.pool_builder"):
        build_pool("us", as_of=RUN_DATE, runner=runner, fetch_ohlcv=FakeFetcher())
    assert len(runner.calls) == 1
    assert "corrupt nomination cache" in caplog.text


# ---------------------------------------------------------------------------
# Nomination retry (AC2) and R4 carried-forward degradation
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_nomination_retry_recovers_with_errors_appended(pool_dir):
    bad = nomination_json(nominee(catalyst_type="rumor"))
    runner = FakeRunner([bad, nomination_json(nominee())])
    result = build_pool("us", as_of=RUN_DATE, runner=runner, fetch_ohlcv=FakeFetcher())
    assert result.outcome == "written"
    assert len(runner.calls) == 2  # exactly one retry
    first_prompt, retry_prompt = runner.calls[0][1], runner.calls[1][1]
    assert retry_prompt.startswith(first_prompt)
    assert "catalyst_type" in retry_prompt[len(first_prompt):]  # errors fed back


@pytest.mark.unit
def test_validation_failure_after_retry_carries_forward(pool_dir, capsys):
    write_prior_pool(
        pool_dir,
        FRIDAY.isoformat(),
        opportunity=[prior_opportunity("AVGO", low=1, fail=1)],
        watch=[prior_watch("MRVL")],
    )
    runner = FakeRunner(["garbage", "still garbage"])
    fetcher = FakeFetcher()
    result = build_pool("us", as_of=RUN_DATE, runner=runner, fetch_ohlcv=fetcher)

    assert result.outcome == "carried_forward"
    assert len(runner.calls) == 2
    assert fetcher.calls == []  # no gating on the carried layers
    pool = read_written_pool(pool_dir)
    assert pool.carried_forward is True
    (avgo,) = pool.opportunity
    # Layers verbatim: streaks untouched, snapshot untouched.
    assert avgo.low_score_streak == 1 and avgo.gate_fail_streak == 1
    assert avgo.entered_on == date(2026, 8, 3)
    assert [w.ticker for w in pool.watch] == ["MRVL"]
    assert pool.removed == []
    assert "carrying opportunity/watch forward" in capsys.readouterr().err
    assert "carried_forward" in builder_log(pool_dir)


@pytest.mark.unit
def test_backend_crash_carries_forward_without_retry(pool_dir, capsys):
    write_prior_pool(pool_dir, FRIDAY.isoformat(), opportunity=[prior_opportunity("AVGO")])
    runner = FakeRunner([RuntimeError("backend down")])
    result = build_pool("us", as_of=RUN_DATE, runner=runner, fetch_ohlcv=FakeFetcher())
    assert result.outcome == "carried_forward"
    assert len(runner.calls) == 1  # a backend crash is not retried
    assert read_written_pool(pool_dir).opportunity[0].ticker == "AVGO"
    assert "backend down" in capsys.readouterr().err


@pytest.mark.unit
def test_carried_forward_with_no_prior_pool_still_covers_core(pool_dir):
    write_core(pool_dir, [{"ticker": "NVDA", "note": "holding"}])
    result = build_pool(
        "us", as_of=RUN_DATE, runner=FakeRunner([RuntimeError("down")]), fetch_ohlcv=FakeFetcher()
    )
    assert result.outcome == "carried_forward"
    pool = read_written_pool(pool_dir)
    assert [c.ticker for c in pool.core] == ["NVDA"]  # coverage survives
    assert pool.opportunity == [] and pool.watch == []


@pytest.mark.unit
def test_carried_forward_exits_zero_via_cli(pool_dir, capsys):
    write_prior_pool(pool_dir, FRIDAY.isoformat(), opportunity=[prior_opportunity("AVGO")])
    rc = main(
        ["--session", "us", "--date", DATE],
        runner=FakeRunner([RuntimeError("down")]),
        fetch_ohlcv=FakeFetcher(),
    )
    assert rc == 0  # R4: degraded, not dead
    captured = capsys.readouterr()
    assert "carried_forward" in captured.out
    assert "warning" in captured.err


# ---------------------------------------------------------------------------
# Defensive nominee filtering
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_wrong_session_and_duplicate_nominees_dropped(pool_dir, caplog):
    runner = FakeRunner(
        [
            nomination_json(
                nominee("0700.HK", 8.0),  # cn symbol in a us run
                nominee("AVGO", 7.5),
                nominee("AVGO", 9.9),  # duplicate — first occurrence wins
            )
        ]
    )
    with caplog.at_level("WARNING", logger="pipeline.pool_builder"):
        build_pool("us", as_of=RUN_DATE, runner=runner, fetch_ohlcv=FakeFetcher())
    pool = read_written_pool(pool_dir)
    (avgo,) = pool.opportunity
    assert avgo.score == 7.5
    assert not any(
        e.ticker == "0700.HK" for e in pool.opportunity + pool.watch + pool.removed
    )
    assert "not a 'us'-session symbol" in caplog.text
    assert "duplicate nomination" in caplog.text


@pytest.mark.unit
def test_fetch_failure_freezes_member_streak_and_routes_candidates(pool_dir, caplog):
    write_core(pool_dir, [{"ticker": "NVDA"}])
    write_prior_pool(pool_dir, FRIDAY.isoformat(), opportunity=[prior_opportunity("AVGO")])
    down = ConnectionError("yfinance unreachable")
    fetcher = FakeFetcher({"NVDA": down, "AVGO": down, "HIGH": down, "LOWW": down})
    runner = FakeRunner(
        [nomination_json(nominee("AVGO", 7.0), nominee("HIGH", 8.0), nominee("LOWW", 5.0))]
    )
    with caplog.at_level("WARNING", logger="pipeline.pool_builder"):
        build_pool("us", as_of=RUN_DATE, runner=runner, fetch_ohlcv=fetcher)
    pool = read_written_pool(pool_dir)
    assert pool.core[0].technical is None  # annotation skipped, ticker kept
    (avgo,) = pool.opportunity
    # A fetch error is an infra failure, not a structural verdict: the
    # member's gate_fail_streak freezes (nomination-absence precedent)
    # instead of advancing toward the 2-pool exit.
    assert avgo.gate_fail_streak == 0
    assert avgo.technical.gate == "fail" and avgo.technical.boll_daily is None
    assert [w.ticker for w in pool.watch] == ["HIGH"]  # fail + high score
    assert not any(e.ticker == "LOWW" for e in pool.watch)  # fail + low: dropped
    assert "OHLCV fetch failed" in caplog.text


@pytest.mark.unit
def test_vendor_outage_never_evicts_a_member_on_the_streak_edge(pool_dir):
    # A member one gate-fail away from eviction survives a fetch outage with
    # the streak frozen at 1 — a 2-day vendor outage can never empty the
    # opportunity layer with "technical gate fail" removal reasons.
    write_prior_pool(
        pool_dir, FRIDAY.isoformat(), opportunity=[prior_opportunity("AVGO", fail=1)]
    )
    build_pool(
        "us",
        as_of=RUN_DATE,
        runner=FakeRunner([nomination_json(nominee("AVGO", 8.0))]),
        fetch_ohlcv=FakeFetcher({"AVGO": ConnectionError("vendor down")}),
    )
    pool = read_written_pool(pool_dir)
    (avgo,) = pool.opportunity  # still a member
    assert avgo.gate_fail_streak == 1
    assert pool.removed == []


# ---------------------------------------------------------------------------
# Caps: lowest-score truncation, logged (AC1)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_opportunity_cap_truncates_lowest_scores_and_logs(pool_dir, caplog):
    entries = [nominee(f"TK{i}", 9.0 - i * 0.5) for i in range(6)]  # 9.0 .. 6.5
    with caplog.at_level("WARNING", logger="pipeline.pool_builder"):
        build_pool(
            "us",
            as_of=RUN_DATE,
            runner=FakeRunner([nomination_json(*entries)]),
            fetch_ohlcv=FakeFetcher(),
        )
    pool = read_written_pool(pool_dir)
    assert [e.ticker for e in pool.opportunity] == ["TK0", "TK1", "TK2", "TK3", "TK4"]
    assert "opportunity cap (5): dropping TK5" in caplog.text
    log = builder_log(pool_dir)
    assert "truncated=TK5" in log
    assert "enter=TK0,TK1,TK2,TK3,TK4 " in log  # the truncated one never entered


@pytest.mark.unit
def test_cap_truncated_prior_member_lands_in_removed(pool_dir):
    write_prior_pool(
        pool_dir, FRIDAY.isoformat(), opportunity=[prior_opportunity("AVGO", score=6.4)]
    )
    entries = [nominee("AVGO", 6.4)] + [nominee(f"TK{i}", 9.0 - i * 0.5) for i in range(5)]
    build_pool(
        "us",
        as_of=RUN_DATE,
        runner=FakeRunner([nomination_json(*entries)]),
        fetch_ohlcv=FakeFetcher(),
    )
    pool = read_written_pool(pool_dir)
    assert "AVGO" not in [e.ticker for e in pool.opportunity]
    (removed,) = pool.removed
    assert removed.ticker == "AVGO"
    assert "opportunity cap 5" in removed.reason
    assert removed.last_score == 6.4


@pytest.mark.unit
def test_watch_cap_truncates_lowest_scores_and_logs(pool_dir, caplog):
    # 12 candidates in the [4,6) watch band, descending scores.
    entries = [nominee(f"WCH{chr(65 + i)}", 5.9 - i * 0.1) for i in range(12)]
    with caplog.at_level("WARNING", logger="pipeline.pool_builder"):
        build_pool(
            "us",
            as_of=RUN_DATE,
            runner=FakeRunner([nomination_json(*entries)]),
            fetch_ohlcv=FakeFetcher(),
        )
    pool = read_written_pool(pool_dir)
    assert len(pool.watch) == 10
    kept = [e.ticker for e in pool.watch]
    assert "WCHK" not in kept and "WCHL" not in kept  # the two lowest dropped
    assert "watch cap (10): dropping WCHK" in caplog.text
    assert "watch cap (10): dropping WCHL" in caplog.text


# ---------------------------------------------------------------------------
# Hard failures (R4's only non-zero exits)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_unreadable_core_exits_nonzero_via_cli(pool_dir, capsys):
    (pool_dir / "core.us.yaml").write_text("[unclosed", encoding="utf-8")
    rc = main(
        ["--session", "us", "--date", DATE],
        runner=FakeRunner(["[]"]),
        fetch_ohlcv=FakeFetcher(),
    )
    assert rc == 1
    reasons = [
        line
        for line in capsys.readouterr().err.splitlines()
        if line.startswith("pool-builder:")
    ]
    assert len(reasons) == 1
    assert "core file" in reasons[0]
    assert not (pool_dir / "us" / f"{DATE}.json").exists()


@pytest.mark.unit
def test_write_failure_exits_nonzero_via_cli(pool_dir, capsys):
    (pool_dir / "us").write_text("not a directory", encoding="utf-8")
    rc = main(
        ["--session", "us", "--date", DATE],
        runner=FakeRunner(["[]"]),
        fetch_ohlcv=FakeFetcher(),
    )
    assert rc == 1
    assert "pool-builder:" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# CLI surface
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_cli_happy_path_and_backend_selection(pool_dir, capsys):
    runner = FakeRunner([nomination_json(nominee())])
    rc = main(
        ["--session", "us", "--date", DATE, "--backend", "codex"],
        runner=runner,
        fetch_ohlcv=FakeFetcher(),
    )
    assert rc == 0
    assert "written:" in capsys.readouterr().out
    assert runner.calls[0][0] == "codex"
    assert read_written_pool(pool_dir).generator == "codex-deep-search"


@pytest.mark.unit
def test_cli_default_date_resolves_in_session_timezone(pool_dir, monkeypatch):
    seen = {}

    def fake_session_date(session):
        seen["session"] = session
        return RUN_DATE

    monkeypatch.setattr(pool_builder, "session_date", fake_session_date)
    rc = main(
        ["--session", "cn"],
        runner=FakeRunner(["[]"]),
        fetch_ohlcv=FakeFetcher(),
    )
    assert rc == 0
    assert seen["session"] == "cn"
    assert (pool_dir / "cn" / f"{DATE}.json").exists()


@pytest.mark.unit
@pytest.mark.parametrize(
    "argv",
    [
        [],  # --session is required
        ["--session", "eu"],
        ["--session", "us", "--date", "08/10/2026"],
        ["--session", "us", "--backend", "gemini"],
    ],
)
def test_cli_rejects_invalid_arguments(argv):
    with pytest.raises(SystemExit) as excinfo:
        main(argv)
    assert excinfo.value.code == 2


@pytest.mark.unit
def test_module_invocable_via_python_dash_m():
    repo_root = Path(__file__).resolve().parents[1]
    proc = subprocess.run(
        [sys.executable, "-m", "pipeline.pool_builder", "--help"],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=repo_root,
    )
    assert proc.returncode == 0
    assert "--session" in proc.stdout
    assert "--force" in proc.stdout


# ---------------------------------------------------------------------------
# Default price fetcher (yfinance boundary, faked)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_default_fetch_daily_ohlcv_converts_yfinance_frame(monkeypatch):
    import pandas as pd
    import yfinance

    frame = pd.DataFrame(
        {
            "Open": [10.0, 11.0],
            "High": [12.0, 13.0],
            "Low": [9.0, 10.0],
            "Close": [11.0, 12.0],
            "Volume": [1000, 2000],
        },
        index=pd.to_datetime(["2026-08-06", "2026-08-07"]),
    )
    seen = {}

    class FakeTicker:
        def __init__(self, symbol):
            seen["symbol"] = symbol

        def history(self, **kwargs):
            seen["kwargs"] = kwargs
            return frame

    monkeypatch.setattr(yfinance, "Ticker", FakeTicker)
    bars = pool_builder.default_fetch_daily_ohlcv("NVDA")
    assert seen["symbol"] == "NVDA"
    assert seen["kwargs"]["interval"] == "1d"
    assert bars == [
        (date(2026, 8, 6), 10.0, 12.0, 9.0, 11.0, 1000.0),
        (date(2026, 8, 7), 11.0, 13.0, 10.0, 12.0, 2000.0),
    ]


# ---------------------------------------------------------------------------
# Pool directory resolution (shared config — no split-brain with the collector)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_resolve_pool_dir_agrees_with_shared_config(tmp_path, monkeypatch):
    # The contract names both TRADINGAGENTS_POOL_DIR and the config key
    # pool_dir (derived from state_dir): a STATE_DIR-only override must land
    # the builder in the same directory the ticker collector reads.
    monkeypatch.delenv(pool_builder.POOL_DIR_ENV, raising=False)
    monkeypatch.setenv("TRADINGAGENTS_STATE_DIR", str(tmp_path / "state"))
    assert pool_builder.resolve_pool_dir() == tmp_path / "state" / "pools"
    # The contract's dedicated env override still wins over the derivation.
    monkeypatch.setenv(pool_builder.POOL_DIR_ENV, str(tmp_path / "pools"))
    assert pool_builder.resolve_pool_dir() == tmp_path / "pools"
    # An explicit argument beats everything.
    assert pool_builder.resolve_pool_dir(tmp_path / "explicit") == tmp_path / "explicit"


# ---------------------------------------------------------------------------
# Backend timeout budget (R4 must stay reachable under the orchestrator)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_backend_timeout_fits_inside_the_component_budget(monkeypatch):
    monkeypatch.delenv("TRADINGAGENTS_TIMEOUT_POOL_BUILDER", raising=False)
    timeout = pool_builder.backend_timeout_seconds()
    assert timeout == 360.0  # (900 - 180) / 2
    # The R3 worst case (attempt + retry) plus the gate/write headroom fits
    # the orchestrator's 900s pool_builder budget — the R4 carried-forward
    # write can never be preempted by the component SIGKILL.
    assert (
        pool_builder.NOMINATION_ATTEMPTS * timeout + pool_builder.GATE_HEADROOM_SECONDS
        <= 900.0
    )
    # The env override that resizes the outer budget resizes the inner share.
    monkeypatch.setenv("TRADINGAGENTS_TIMEOUT_POOL_BUILDER", "2160")
    assert pool_builder.backend_timeout_seconds() == 990.0  # (2160 - 180) / 2
    # A pathologically small budget still leaves a usable attempt (floor).
    monkeypatch.setenv("TRADINGAGENTS_TIMEOUT_POOL_BUILDER", "60")
    assert (
        pool_builder.backend_timeout_seconds()
        == pool_builder.MIN_BACKEND_TIMEOUT_SECONDS
    )


@pytest.mark.unit
def test_default_runner_uses_the_budget_sized_timeout(monkeypatch):
    monkeypatch.delenv("TRADINGAGENTS_TIMEOUT_POOL_BUILDER", raising=False)
    seen = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        seen["timeout"] = kwargs.get("timeout")
        return subprocess.CompletedProcess(cmd, 0, stdout="[]", stderr="")

    monkeypatch.setattr(pool_builder.subprocess, "run", fake_run)
    assert pool_builder.default_runner("claude", "PROMPT") == "[]"
    assert seen["cmd"][0] == "claude"
    assert seen["timeout"] == 360.0  # budget-derived, not the outer 900s


@pytest.mark.unit
def test_default_runner_codex_command_is_a_working_invocation(monkeypatch):
    seen = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        seen["input"] = kwargs.get("input")
        return subprocess.CompletedProcess(cmd, 0, stdout="[]", stderr="")

    monkeypatch.setattr(pool_builder.subprocess, "run", fake_run)
    assert pool_builder.default_runner("codex", "PROMPT") == "[]"
    # D19 (codex is the collection default): web search on, git-repo trust
    # check skipped (components inherit an arbitrary cwd), prompt over stdin.
    assert seen["cmd"] == ["codex", "exec", "-c", "tools.web_search=true", "--skip-git-repo-check", "-"]
    assert seen["input"] == "PROMPT"
