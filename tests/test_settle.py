"""Settle step (``pipeline/settle.py``) — specs/orchestrator.md "Settle step
(0)" + the ledger contract's outcomes schema, fully offline.

The daily-bars fetcher, the adapter-refresh subprocess, and the clock are all
faked. Covers: the return-anchor semantics (pre-open, post-close, weekend
decisions; no lookahead through the injectable clock), horizon nulls until
computable with merge-on-refresh (a transient fetch failure never regresses a
settled horizon), the plan-replay matrix (entry never hit, stop first, target
first, same-bar ambiguous, strictly-after-decided_at bar eligibility, invalid
plan / SELL / HOLD ⇒ null), suffix-based benchmark resolution pinned to
``tradingagents``' benchmark_map, the paper join (dry-run ⇒ null; filled
close ⇒ realized P&L), idempotent re-runs appending nothing, ERROR-row and
cross-session skips, the adapter-refresh warn degrade (settle's only
subprocess — it never constructs a broker client), the settle log, and the
CLI. Every appended row is validated against the ``OutcomeRecord`` contract.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone

import pytest

from pipeline import settle
from pipeline.config import PipelineConfig
from pipeline.contracts.ledger import OutcomeRecord

JUN1 = date(2026, 6, 1)  # a Monday; June = EDT, so NY close 16:00 = 20:00 UTC
NOW_LATE = datetime(2026, 6, 11, 0, 0, tzinfo=timezone.utc)  # bars usable thru Jun 10

#: Contract-valid BUY plan under replay: entry 100, stop 90, first target 110.
BUY_PLAN = {
    "action": "BUY",
    "conviction": 0.7,
    "add_intent": False,
    "add_rationale": None,
    "entry_zone": [99.0, 100.0],
    "stop": 90.0,
    "targets": [110.0, 120.0],
    "horizon_days": 5,
    "invalidation": "daily close below the weekly mid-band",
    "sizing": {"risk_pct": 1.0},
    "source_levels": "entry = daily BOLL mid, stop = below daily lower band",
}
SELL_PLAN = {
    "action": "SELL",
    "conviction": 0.6,
    "add_intent": False,
    "add_rationale": None,
    "entry_zone": [95.0, 97.0],
    "stop": None,
    "targets": None,
    "horizon_days": None,
    "invalidation": None,
    "sizing": None,
    "source_levels": None,
}


def make_config(tmp_path) -> PipelineConfig:
    return PipelineConfig(state_dir=tmp_path / "state")


def write_decision(
    config,
    *,
    ticker="NVDA",
    session="us",
    date_iso="2026-06-01",
    decided_at="2026-06-01T12:00:00Z",  # 08:00 New York — pre-open
    decision="HOLD",
    plan=None,
    plan_valid=False,
    n=1,
    arm="brief",
):
    """Append one contract-shaped decisions.jsonl row."""
    row = {
        "run_id": f"{date_iso}-{session}-{ticker}-{arm}-{n}",
        "pair_id": None,
        "date": date_iso,
        "session": session,
        "ticker": ticker,
        "arm": arm,
        "preset": "test",
        "trigger": "core",
        "catalyst_score": None,
        "macro_eval_verdict": "pass",
        "ticker_eval_verdict": "pass",
        "inputs": {"macro_brief": None, "ticker_brief": None, "pool": None, "config_digest": "d"},
        "decided_at": decided_at,
        "decision": decision,
        "plan": plan,
        "plan_valid": plan_valid,
    }
    path = config.ledger_dir / "decisions.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(row) + "\n")
    return row


def write_order(config, **fields):
    """Raw orders.jsonl fixture row (paper-join seeding)."""
    row = {
        "run_id": "2026-06-01-us-NVDA-brief-1",
        "written_at": "2026-06-01T14:00:00Z",
        "session": "us",
        "ticker": "NVDA",
        "kind": "submitted",
        "dry_run": False,
        "reason": None,
        "client_order_id": "2026-06-01-us-NVDA-entry",
        "order_kind": "bracket",
        "tif": "gtc",
        "qty": 10.0,
        "limit_price": 100.0,
        "stop_price": 90.0,
        "target_price": 110.0,
        "broker_status": None,
        "filled_avg_price": None,
        "filled_qty": None,
        **fields,
    }
    path = config.ledger_dir / "orders.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(row) + "\n")
    return row


def bar(day, close, low=None, high=None, open_=None):
    open_ = close if open_ is None else open_
    low = min(close, open_) if low is None else low
    high = max(close, open_) if high is None else high
    return settle.DailyBar(
        date=day,
        open=open_,
        high=max(high, close, open_),
        low=min(low, close, open_),
        close=close,
    )


def weekday_bars(start, closes):
    """One bar per consecutive weekday from ``start`` (low = high = close)."""
    bars, day = [], start
    for close in closes:
        while day.weekday() >= 5:
            day += timedelta(days=1)
        bars.append(bar(day, close))
        day += timedelta(days=1)
    return bars


#: Jun 1, 2, 3, 4, 5, 8, 9, 10.
NVDA_BARS = weekday_bars(JUN1, [100.0, 102.0, 103.0, 104.0, 105.0, 106.0, 107.0, 108.0])
SPY_BARS = weekday_bars(JUN1, [50.0, 51.0, 52.0, 53.0, 54.0, 55.0, 56.0, 57.0])


class FakeBars:
    """Injectable bars fetcher: symbol → series, honoring the date window."""

    def __init__(self, series):
        self.series = series
        self.calls = []

    def __call__(self, symbol, start, end):
        self.calls.append((symbol, start, end))
        return [b for b in self.series.get(symbol, []) if start <= b.date <= end]

    @property
    def symbols(self):
        return {symbol for symbol, _, _ in self.calls}


class FakeRefresh:
    """Injectable adapter-refresh subprocess boundary."""

    def __init__(self, returncode=0, stdout='{"entrypoint": "refresh"}\n', stderr="", exc=None):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.exc = exc
        self.calls = []

    def __call__(self, argv, timeout):
        self.calls.append((list(argv), timeout))
        if self.exc is not None:
            raise self.exc
        return self.returncode, self.stdout, self.stderr


def run(config, fetcher, *, session="us", now=NOW_LATE, refresh=None, slot_date=None):
    refresh = refresh or FakeRefresh()
    summary = settle.run_settle(
        config=config,
        session=session,
        slot_date=slot_date,
        bars_fetcher=fetcher,
        refresh_runner=refresh,
        clock=lambda: now,
    )
    return summary, refresh


def read_outcomes(config):
    """Every appended row, contract-validated, in physical order."""
    path = config.ledger_dir / "outcomes.jsonl"
    if not path.exists():
        return []
    return [
        OutcomeRecord.model_validate(json.loads(line))
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


# ---------------------------------------------------------------------------
# Return anchor semantics (contract: d0 = first close at/after decided_at)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_pre_open_decision_anchors_to_same_day_close(tmp_path):
    config = make_config(tmp_path)
    write_decision(config)  # decided 08:00 NY, pre-open ⇒ d0 = Jun 1 (close 100)
    fetcher = FakeBars({"NVDA": NVDA_BARS, "SPY": SPY_BARS})
    summary, refresh = run(config, fetcher, slot_date=JUN1)
    assert summary == {
        "entrypoint": "settle",
        "date": "2026-06-01",
        "session": "us",
        "settled": 1,
        "skipped": 0,
        "refresh_ok": True,
        "refresh_error": None,
    }
    assert len(refresh.calls) == 1
    (record,) = read_outcomes(config)
    assert record.run_id == "2026-06-01-us-NVDA-brief-1"
    assert record.returns.d1 == pytest.approx(102.0 / 100.0 - 1)
    assert record.returns.d5 == pytest.approx(106.0 / 100.0 - 1)
    assert record.returns.d20 is None  # horizon null until computable
    assert record.benchmark_returns.d1 == pytest.approx(51.0 / 50.0 - 1)
    assert record.benchmark_returns.d5 == pytest.approx(55.0 / 50.0 - 1)
    assert record.benchmark_returns.d20 is None
    assert record.plan_replay is None  # HOLD ⇒ null
    assert record.paper is None  # never executed ⇒ null


@pytest.mark.unit
def test_post_close_decision_anchors_to_next_trading_day(tmp_path):
    config = make_config(tmp_path)
    # 21:00 UTC = 17:00 New York — after the 16:00 close ⇒ d0 = Jun 2 (102).
    write_decision(config, decided_at="2026-06-01T21:00:00Z")
    fetcher = FakeBars({"NVDA": NVDA_BARS, "SPY": SPY_BARS})
    run(config, fetcher)
    (record,) = read_outcomes(config)
    assert record.returns.d1 == pytest.approx(103.0 / 102.0 - 1)
    assert record.benchmark_returns.d1 == pytest.approx(52.0 / 51.0 - 1)


@pytest.mark.unit
def test_weekend_decision_anchors_to_monday(tmp_path):
    config = make_config(tmp_path)
    # Saturday decision (manual run) ⇒ d0 = Monday Jun 8 (close 106).
    write_decision(config, date_iso="2026-06-06", decided_at="2026-06-06T12:00:00Z")
    fetcher = FakeBars({"NVDA": NVDA_BARS, "SPY": SPY_BARS})
    run(config, fetcher)
    (record,) = read_outcomes(config)
    assert record.run_id == "2026-06-06-us-NVDA-brief-1"
    assert record.returns.d1 == pytest.approx(107.0 / 106.0 - 1)
    assert record.returns.d5 is None  # only 2 bars after Jun 8 in the series


@pytest.mark.unit
def test_no_lookahead_a_bar_before_its_close_instant_is_unusable(tmp_path):
    config = make_config(tmp_path)
    write_decision(config)
    fetcher = FakeBars({"NVDA": NVDA_BARS, "SPY": SPY_BARS})
    # 19:00 UTC on Jun 1: the decision day's session has not CLOSED yet (close
    # 20:00 UTC) — the fetcher's Jun 1 bar must not anchor anything.
    summary, _ = run(config, fetcher, now=datetime(2026, 6, 1, 19, 0, tzinfo=timezone.utc))
    assert summary["settled"] == 0
    assert summary["skipped"] == 1
    assert read_outcomes(config) == []


# ---------------------------------------------------------------------------
# Horizons fill in over re-runs; idempotent re-runs append nothing
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_horizons_fill_in_and_reruns_append_nothing(tmp_path):
    config = make_config(tmp_path)
    write_decision(config)
    fetcher = FakeBars({"NVDA": NVDA_BARS, "SPY": SPY_BARS})

    # Day 2: only d1 computable.
    early = datetime(2026, 6, 3, 0, 0, tzinfo=timezone.utc)
    summary, _ = run(config, fetcher, now=early)
    assert summary["settled"] == 1
    (first,) = read_outcomes(config)
    assert first.returns.d1 == pytest.approx(0.02)
    assert first.returns.d5 is None

    # Idempotent re-run at the same instant: nothing newly computable.
    summary, _ = run(config, fetcher, now=early)
    assert (summary["settled"], summary["skipped"]) == (0, 1)
    assert len(read_outcomes(config)) == 1

    # A week later d5 becomes computable — one refreshed record appended,
    # d1 carried over; the current record is the greatest settled_at.
    summary, _ = run(config, fetcher, now=NOW_LATE)
    assert summary["settled"] == 1
    records = read_outcomes(config)
    assert len(records) == 2
    latest = max(records, key=lambda r: r.settled_at)
    assert latest.returns.d1 == pytest.approx(0.02)
    assert latest.returns.d5 == pytest.approx(0.06)
    assert latest.returns.d20 is None

    # A transient fetch failure (no bars at all) must not regress the settled
    # horizons into nulls: computed nulls merge with the stored record.
    summary, _ = run(config, FakeBars({}), now=NOW_LATE)
    assert (summary["settled"], summary["skipped"]) == (0, 1)
    assert len(read_outcomes(config)) == 2


@pytest.mark.unit
def test_fully_settled_records_skip_the_bars_fetch(tmp_path):
    """Once a stored record can never change (all horizons on both series,
    terminal replay, final paper) the settle loop must not refetch its ticker
    or benchmark bars — otherwise every historical row refetches on every
    slot and yfinance call volume grows without bound."""
    config = make_config(tmp_path / "terminal")
    write_decision(config)  # HOLD, never executed ⇒ replay/paper stay null forever
    outcome = {
        "run_id": "2026-06-01-us-NVDA-brief-1",
        "settled_at": "2026-07-01T00:00:00Z",
        "returns": {"d1": 0.01, "d5": 0.02, "d20": 0.03},
        "benchmark_returns": {"d1": 0.0, "d5": 0.0, "d20": 0.0},
        "plan_replay": None,
        "paper": None,
    }
    with open(config.ledger_dir / "outcomes.jsonl", "a", encoding="utf-8") as handle:
        handle.write(json.dumps(outcome) + "\n")
    fetcher = FakeBars({"NVDA": NVDA_BARS, "SPY": SPY_BARS})
    summary, _ = run(config, fetcher, now=datetime(2026, 7, 10, tzinfo=timezone.utc))
    assert (summary["settled"], summary["skipped"]) == (0, 1)
    assert fetcher.calls == []  # neither ticker nor benchmark refetched
    assert len(read_outcomes(config)) == 1

    # An entered-but-unexited replay is NOT terminal (the exit scan is
    # unbounded), so that record still fetches.
    config = make_config(tmp_path / "pending")
    write_decision(config, decision="BUY", plan=BUY_PLAN, plan_valid=True)
    pending = {
        **outcome,
        "plan_replay": {
            "entry_hit": True, "stop_hit_first": False, "target_hit_first": False,
            "ambiguous": False, "realized_rr": None,
        },
    }
    with open(config.ledger_dir / "outcomes.jsonl", "a", encoding="utf-8") as handle:
        handle.write(json.dumps(pending) + "\n")
    fetcher = FakeBars({"NVDA": NVDA_BARS, "SPY": SPY_BARS})
    run(config, fetcher, now=datetime(2026, 7, 10, tzinfo=timezone.utc))
    assert fetcher.calls  # exit still pending ⇒ bars fetched again


# ---------------------------------------------------------------------------
# Plan replay matrix (BUY + plan_valid only; bars strictly after decided_at)
# ---------------------------------------------------------------------------


def buy_decision(config, **overrides):
    return write_decision(config, decision="BUY", plan=BUY_PLAN, plan_valid=True, **overrides)


def replay_of(config, bars, *, now=NOW_LATE):
    fetcher = FakeBars({"NVDA": bars, "SPY": SPY_BARS})
    run(config, fetcher, now=now)
    (record,) = read_outcomes(config)
    return record.plan_replay


@pytest.mark.unit
def test_replay_entry_never_hit_within_horizon(tmp_path):
    config = make_config(tmp_path)
    buy_decision(config)
    # Lows stay above entry_zone[1]=100 through the horizon window (d0=Jun 1 +
    # 5 trading days = Jun 8); the Jun 9 crash is BEYOND the window and must
    # not count as an entry.
    bars = weekday_bars(JUN1, [101.0, 102.0, 103.0, 104.0, 105.0, 106.0]) + [
        bar(date(2026, 6, 9), 60.0, low=50.0)
    ]
    replay = replay_of(config, bars)
    assert replay is not None
    assert replay.entry_hit is False
    assert replay.stop_hit_first is False
    assert replay.target_hit_first is False
    assert replay.ambiguous is False
    assert replay.realized_rr is None


@pytest.mark.unit
def test_replay_entry_undetermined_until_window_elapses(tmp_path):
    config = make_config(tmp_path)
    buy_decision(config)
    bars = weekday_bars(JUN1, [101.0, 102.0, 103.0, 104.0, 105.0, 106.0])  # no touch
    fetcher = FakeBars({"NVDA": bars, "SPY": SPY_BARS})
    # Only 3 of the 5+1 entry-window bars have printed and none touched the
    # zone: entry_hit is UNDETERMINED — the replay must stay null (the
    # contract's null-until-computable posture) instead of asserting false,
    # which would deflate entry-hit-rate denominators for recent runs.
    run(config, fetcher, now=datetime(2026, 6, 4, 0, 0, tzinfo=timezone.utc))
    (record,) = read_outcomes(config)
    assert record.plan_replay is None
    assert record.returns.d1 is not None  # returns still settle
    # Once the whole window has elapsed, the no-entry verdict lands and the
    # merge advances the stored null replay.
    run(config, fetcher, now=NOW_LATE)
    latest = max(read_outcomes(config), key=lambda r: r.settled_at)
    assert latest.plan_replay is not None
    assert latest.plan_replay.entry_hit is False


@pytest.mark.unit
def test_replay_stop_first(tmp_path):
    config = make_config(tmp_path)
    buy_decision(config)
    bars = [
        bar(JUN1, 101.0, low=99.0),  # entry hit (low ≤ 100) — pre-open decision
        bar(date(2026, 6, 2), 95.0, low=89.0, high=100.0),  # stop 90 touched first
        bar(date(2026, 6, 3), 120.0, high=125.0),  # later target touch is irrelevant
    ]
    replay = replay_of(config, bars)
    assert replay.entry_hit is True
    assert replay.stop_hit_first is True
    assert replay.target_hit_first is False
    assert replay.ambiguous is False
    # realized_rr = (exit − entry) / (entry − stop) = (90 − 100) / (100 − 90)
    assert replay.realized_rr == pytest.approx(-1.0)


@pytest.mark.unit
def test_replay_target_first_and_entry_bar_never_exits(tmp_path):
    config = make_config(tmp_path)
    buy_decision(config)
    bars = [
        # Entry bar's own high crosses the target — first-touch ordering runs
        # on SUBSEQUENT bars only, so this must not resolve the exit.
        bar(JUN1, 101.0, low=99.0, high=115.0),
        bar(date(2026, 6, 2), 108.0, low=95.0, high=111.0),  # target 110 touched
    ]
    replay = replay_of(config, bars)
    assert replay.entry_hit is True
    assert replay.target_hit_first is True
    assert replay.stop_hit_first is False
    assert replay.ambiguous is False
    assert replay.realized_rr == pytest.approx((110.0 - 100.0) / (100.0 - 90.0))


@pytest.mark.unit
def test_replay_same_bar_both_touched_is_ambiguous(tmp_path):
    config = make_config(tmp_path)
    buy_decision(config)
    bars = [
        bar(JUN1, 101.0, low=99.0),
        bar(date(2026, 6, 2), 100.0, low=89.0, high=111.0),  # stop AND target
    ]
    replay = replay_of(config, bars)
    assert replay.entry_hit is True
    assert replay.ambiguous is True
    assert replay.stop_hit_first is False
    assert replay.target_hit_first is False
    assert replay.realized_rr is None  # excluded from R:R aggregates


@pytest.mark.unit
def test_replay_entered_but_no_exit_yet_keeps_rr_null(tmp_path):
    config = make_config(tmp_path)
    buy_decision(config)
    bars = [
        bar(JUN1, 101.0, low=99.0),
        bar(date(2026, 6, 2), 102.0, low=95.0, high=105.0),  # neither level touched
    ]
    replay = replay_of(config, bars)
    assert replay.entry_hit is True
    assert replay.stop_hit_first is False
    assert replay.target_hit_first is False
    assert replay.ambiguous is False
    assert replay.realized_rr is None


@pytest.mark.unit
def test_replay_never_regresses_on_a_partial_bars_fetch(tmp_path):
    config = make_config(tmp_path)
    buy_decision(config)
    full = [
        bar(JUN1, 101.0, low=99.0),
        bar(date(2026, 6, 2), 95.0, low=89.0, high=100.0),  # stop-first resolved
    ]
    run(config, FakeBars({"NVDA": full, "SPY": SPY_BARS}))
    (record,) = read_outcomes(config)
    assert record.plan_replay.stop_hit_first is True

    # A later run whose fetch drops the exit bar computes a merely-entered
    # replay — the stored resolved replay must win and nothing is appended.
    summary, _ = run(config, FakeBars({"NVDA": full[:1], "SPY": SPY_BARS}))
    assert summary["settled"] == 0
    assert len(read_outcomes(config)) == 1


@pytest.mark.unit
def test_replay_null_for_invalid_plan_sell_and_hold(tmp_path):
    # Invalid plan: the decision stands as an opinion; the plan never replays.
    config = make_config(tmp_path / "invalid")
    write_decision(config, decision="BUY", plan=BUY_PLAN, plan_valid=False)
    fetcher = FakeBars({"NVDA": NVDA_BARS, "SPY": SPY_BARS})
    run(config, fetcher)
    (record,) = read_outcomes(config)
    assert record.plan_replay is None
    assert record.returns.d1 is not None  # returns still settle

    # SELL: replay is BUY-only.
    config = make_config(tmp_path / "sell")
    write_decision(config, decision="SELL", plan=SELL_PLAN, plan_valid=True)
    run(config, FakeBars({"NVDA": NVDA_BARS, "SPY": SPY_BARS}))
    (record,) = read_outcomes(config)
    assert record.plan_replay is None

    # HOLD is covered by test_pre_open_decision_anchors_to_same_day_close.


@pytest.mark.unit
def test_replay_excludes_the_bar_containing_decided_at(tmp_path):
    config = make_config(tmp_path)
    # 15:00 UTC = 11:00 New York — mid-session. The Jun 1 bar contains the
    # decision instant: its low touching the entry zone must NOT count (bars
    # strictly after decided_at), while the SAME day's close still anchors the
    # returns (the close is in the decision's future).
    buy_decision(config, decided_at="2026-06-01T15:00:00Z")
    bars = weekday_bars(JUN1, [101.0, 102.0, 103.0, 104.0, 105.0, 106.0])
    bars[0] = bar(JUN1, 101.0, low=99.0)  # entry touch on the decision day only
    fetcher = FakeBars({"NVDA": bars, "SPY": SPY_BARS})
    run(config, fetcher)
    (record,) = read_outcomes(config)
    assert record.plan_replay.entry_hit is False
    assert record.returns.d1 == pytest.approx(102.0 / 101.0 - 1)


# ---------------------------------------------------------------------------
# Benchmark resolution (suffix map, benchmark's own calendar)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_benchmark_per_suffix_on_the_cn_session(tmp_path):
    config = make_config(tmp_path)
    # 01:00 UTC = 09:00 Hong Kong / Shanghai — pre-open for both markets.
    write_decision(
        config, ticker="0700.HK", session="cn", decided_at="2026-06-01T01:00:00Z"
    )
    write_decision(
        config, ticker="600519.SS", session="cn", decided_at="2026-06-01T01:00:00Z"
    )
    fetcher = FakeBars(
        {
            "0700.HK": weekday_bars(JUN1, [500.0, 510.0]),
            "^HSI": weekday_bars(JUN1, [25000.0, 25250.0]),
            "600519.SS": weekday_bars(JUN1, [1400.0, 1414.0]),
            "000001.SS": weekday_bars(JUN1, [3000.0, 3060.0]),
        }
    )
    summary, _ = run(config, fetcher, session="cn", now=datetime(2026, 6, 3, tzinfo=timezone.utc))
    assert summary["settled"] == 2
    assert fetcher.symbols == {"0700.HK", "^HSI", "600519.SS", "000001.SS"}
    by_run = {record.run_id: record for record in read_outcomes(config)}
    hk = by_run["2026-06-01-cn-0700.HK-brief-1"]
    assert hk.returns.d1 == pytest.approx(510.0 / 500.0 - 1)
    assert hk.benchmark_returns.d1 == pytest.approx(25250.0 / 25000.0 - 1)
    ss = by_run["2026-06-01-cn-600519.SS-brief-1"]
    assert ss.returns.d1 == pytest.approx(1414.0 / 1400.0 - 1)
    assert ss.benchmark_returns.d1 == pytest.approx(3060.0 / 3000.0 - 1)


@pytest.mark.unit
def test_us_ticker_benchmarks_against_spy(tmp_path):
    config = make_config(tmp_path)
    write_decision(config)
    fetcher = FakeBars({"NVDA": NVDA_BARS, "SPY": SPY_BARS})
    run(config, fetcher)
    assert "SPY" in fetcher.symbols


@pytest.mark.unit
def test_benchmark_map_mirrors_tradingagents_default_config():
    """The settle map is a reimplementation (pipeline modules never import the
    heavy package at module scope) — drift against the source of truth is a
    contract break for the A/B aggregates."""
    from tradingagents.default_config import DEFAULT_CONFIG

    assert DEFAULT_CONFIG["benchmark_map"] == settle.BENCHMARK_MAP
    assert settle.resolve_benchmark("NVDA") == "SPY"
    assert settle.resolve_benchmark("0700.HK") == "^HSI"
    assert settle.resolve_benchmark("600519.SS") == "000001.SS"
    assert settle.resolve_benchmark("000625.SZ") == "399001.SZ"
    assert settle.resolve_benchmark("BRK.B") == "SPY"  # unknown suffix ⇒ US default


@pytest.mark.unit
def test_market_hours_cover_every_benchmark_map_suffix():
    """The New York fallback on a BENCHMARK_MAP suffix would let a post-close
    decision anchor to an already-elapsed close (lookahead): the real close
    would map to a future NY instant. Every mapped market needs its own hours."""
    for suffix in settle.BENCHMARK_MAP:
        if suffix:
            assert settle.market_hours(f"XXX{suffix}") is settle._MARKET_HOURS[suffix]
    assert settle.market_hours("NVDA") is settle._DEFAULT_MARKET_HOURS


@pytest.mark.unit
def test_tokyo_post_close_decision_anchors_to_next_day(tmp_path):
    config = make_config(tmp_path)
    # 07:00 UTC = 16:00 JST — after the 15:30 Tokyo close. Under a New York
    # fallback that day's already-elapsed close would map to 20:00 UTC and
    # anchor d0 BEFORE decided_at; with Tokyo hours d0 = Jun 2 (close 102).
    write_decision(config, ticker="7203.T", decided_at="2026-06-01T07:00:00Z")
    fetcher = FakeBars(
        {
            "7203.T": weekday_bars(JUN1, [100.0, 102.0, 103.0]),
            "^N225": weekday_bars(JUN1, [30000.0, 30300.0, 30600.0]),
        }
    )
    run(config, fetcher)
    (record,) = read_outcomes(config)
    assert record.returns.d1 == pytest.approx(103.0 / 102.0 - 1)
    assert record.benchmark_returns.d1 == pytest.approx(30600.0 / 30300.0 - 1)
    assert "^N225" in fetcher.symbols


# ---------------------------------------------------------------------------
# Paper block (orders.jsonl join)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_paper_is_null_for_dry_run_rows(tmp_path):
    config = make_config(tmp_path)
    write_decision(config, decision="BUY", plan=BUY_PLAN, plan_valid=True)
    write_order(config, dry_run=True)  # would-place row only — never executed
    fetcher = FakeBars({"NVDA": NVDA_BARS, "SPY": SPY_BARS})
    run(config, fetcher)
    (record,) = read_outcomes(config)
    assert record.paper is None


@pytest.mark.unit
def test_paper_realizes_pnl_from_filled_close(tmp_path):
    config = make_config(tmp_path)
    write_decision(config, decision="BUY", plan=BUY_PLAN, plan_valid=True)
    write_order(config, broker_status="accepted")  # live entry submitted
    write_order(  # entry filled @ 100 × 10 (refresh row, latest written_at wins)
        config,
        kind="refresh",
        order_kind=None,
        written_at="2026-06-01T18:00:00Z",
        broker_status="filled",
        filled_avg_price=100.0,
        filled_qty=10.0,
    )
    # The close was submitted under a later SELL run's id — the join still
    # realizes this position's P&L at the ticker level.
    close_common = {
        "run_id": "2026-06-02-us-NVDA-brief-1",
        "client_order_id": "2026-06-02-us-NVDA-close-1",
        "order_kind": "close",
        "tif": "day",
        "limit_price": 110.0,
        "stop_price": None,
        "target_price": None,
    }
    write_order(config, written_at="2026-06-02T15:00:00Z", **close_common)
    write_order(
        config,
        kind="refresh",
        written_at="2026-06-02T20:30:00Z",
        broker_status="filled",
        filled_avg_price=110.0,
        filled_qty=10.0,
        **{**close_common, "order_kind": None},
    )
    fetcher = FakeBars({"NVDA": NVDA_BARS, "SPY": SPY_BARS})
    run(config, fetcher)
    (record,) = read_outcomes(config)
    assert record.paper is not None
    assert record.paper.realized_pnl == pytest.approx((110.0 - 100.0) * 10.0)
    assert record.paper.closed is True


@pytest.mark.unit
def test_paper_partial_close_realizes_partial_pnl_and_stays_open(tmp_path):
    config = make_config(tmp_path)
    write_decision(config, decision="BUY", plan=BUY_PLAN, plan_valid=True)
    write_order(config, broker_status="accepted")
    write_order(
        config,
        kind="refresh",
        order_kind=None,
        written_at="2026-06-01T18:00:00Z",
        broker_status="filled",
        filled_avg_price=100.0,
        filled_qty=10.0,
    )
    write_order(  # expired DAY close with a 4-share partial fill
        config,
        kind="refresh",
        order_kind=None,
        run_id="2026-06-02-us-NVDA-brief-1",
        client_order_id="2026-06-02-us-NVDA-close-1",
        written_at="2026-06-02T20:30:00Z",
        broker_status="expired",
        filled_avg_price=110.0,
        filled_qty=4.0,
    )
    # A refresh row alone is not order state — seed its submitted row too.
    write_order(
        config,
        run_id="2026-06-02-us-NVDA-brief-1",
        client_order_id="2026-06-02-us-NVDA-close-1",
        order_kind="close",
        tif="day",
        written_at="2026-06-02T15:00:00Z",
    )
    fetcher = FakeBars({"NVDA": NVDA_BARS, "SPY": SPY_BARS})
    run(config, fetcher)
    (record,) = read_outcomes(config)
    assert record.paper.realized_pnl == pytest.approx((110.0 - 100.0) * 4.0)
    assert record.paper.closed is False


@pytest.mark.unit
def test_paper_is_null_while_nothing_is_closed(tmp_path):
    config = make_config(tmp_path)
    write_decision(config, decision="BUY", plan=BUY_PLAN, plan_valid=True)
    write_order(config, broker_status="accepted")  # live, but no fill evidence yet
    fetcher = FakeBars({"NVDA": NVDA_BARS, "SPY": SPY_BARS})
    run(config, fetcher)
    (record,) = read_outcomes(config)
    assert record.paper is None


def _write_round_trip(config, *, date_iso, entry_at, entry_price, entry_qty,
                      close_date, close_at, close_price, close_qty,
                      close_status="filled"):
    """One live NVDA round trip: entry submitted+filled on ``date_iso``, close
    submitted under a later run's id (ticker-level close attribution)."""
    entry_cid = f"{date_iso}-us-NVDA-entry"
    run_id = f"{date_iso}-us-NVDA-brief-1"
    write_order(config, run_id=run_id, client_order_id=entry_cid,
                written_at=f"{date_iso}T14:00:00Z", broker_status="accepted")
    write_order(config, run_id=run_id, client_order_id=entry_cid,
                kind="refresh", order_kind=None,
                written_at=entry_at, broker_status="filled",
                filled_avg_price=entry_price, filled_qty=entry_qty)
    close_cid = f"{close_date}-us-NVDA-close-1"
    close_run = f"{close_date}-us-NVDA-brief-1"
    write_order(config, run_id=close_run, client_order_id=close_cid,
                order_kind="close", tif="day", written_at=f"{close_date}T15:00:00Z",
                limit_price=close_price, stop_price=None, target_price=None)
    write_order(config, run_id=close_run, client_order_id=close_cid,
                kind="refresh", order_kind=None, written_at=close_at,
                broker_status=close_status,
                filled_avg_price=close_price, filled_qty=close_qty)


@pytest.mark.unit
def test_paper_attributes_each_round_trip_to_its_own_entry(tmp_path):
    """A later re-entry on the same ticker (a new date — entries are never
    re-submitted) must not rewrite an older run's realized P&L: each record
    settles against ITS OWN date's entry, not the ticker's latest fill."""
    config = make_config(tmp_path)
    write_decision(config, decision="BUY", plan=BUY_PLAN, plan_valid=True)
    write_decision(config, date_iso="2026-06-10", decided_at="2026-06-10T12:00:00Z",
                   decision="BUY", plan=BUY_PLAN, plan_valid=True)
    # Run A: entry Jun 1 @100×10, closed Jun 5 @110 ⇒ its own P&L is +100.
    _write_round_trip(config, date_iso="2026-06-01",
                      entry_at="2026-06-01T18:00:00Z", entry_price=100.0, entry_qty=10.0,
                      close_date="2026-06-05", close_at="2026-06-05T20:30:00Z",
                      close_price=110.0, close_qty=10.0)
    # Run B: re-entry Jun 10 @120×5, closed Jun 11 @115 ⇒ its own P&L is −25.
    _write_round_trip(config, date_iso="2026-06-10",
                      entry_at="2026-06-10T18:00:00Z", entry_price=120.0, entry_qty=5.0,
                      close_date="2026-06-11", close_at="2026-06-11T20:30:00Z",
                      close_price=115.0, close_qty=5.0)
    fetcher = FakeBars({"NVDA": NVDA_BARS, "SPY": SPY_BARS})
    run(config, fetcher, now=datetime(2026, 6, 20, 0, 0, tzinfo=timezone.utc))
    by_run = {record.run_id: record for record in read_outcomes(config)}
    a = by_run["2026-06-01-us-NVDA-brief-1"]
    assert a.paper.realized_pnl == pytest.approx((110.0 - 100.0) * 10.0)
    assert a.paper.closed is True
    b = by_run["2026-06-10-us-NVDA-brief-1"]
    assert b.paper.realized_pnl == pytest.approx((115.0 - 120.0) * 5.0)
    assert b.paper.closed is True


@pytest.mark.unit
def test_paper_not_duplicated_onto_the_closing_sell_run(tmp_path):
    """The SELL run that submitted the close executed live, but the position's
    P&L belongs to the entry-owning BUY run alone — attributing it twice would
    double-count the position in the per-arm paper totals."""
    config = make_config(tmp_path)
    write_decision(config, decision="BUY", plan=BUY_PLAN, plan_valid=True)
    write_decision(config, date_iso="2026-06-05", decided_at="2026-06-05T12:00:00Z",
                   decision="SELL", plan=SELL_PLAN, plan_valid=True)
    _write_round_trip(config, date_iso="2026-06-01",
                      entry_at="2026-06-01T18:00:00Z", entry_price=100.0, entry_qty=10.0,
                      close_date="2026-06-05", close_at="2026-06-05T20:30:00Z",
                      close_price=110.0, close_qty=10.0)
    fetcher = FakeBars({"NVDA": NVDA_BARS, "SPY": SPY_BARS})
    run(config, fetcher)
    by_run = {record.run_id: record for record in read_outcomes(config)}
    buy = by_run["2026-06-01-us-NVDA-brief-1"]
    assert buy.paper.realized_pnl == pytest.approx(100.0)
    sell = by_run["2026-06-05-us-NVDA-brief-1"]
    assert sell.paper is None  # its own date's entry never filled


@pytest.mark.unit
def test_paper_close_window_is_bounded_by_the_next_entry(tmp_path):
    """Closes at/after the ticker's NEXT filled entry belong to that later
    position: run A's partially-closed position must not absorb run B's close."""
    config = make_config(tmp_path)
    write_decision(config, decision="BUY", plan=BUY_PLAN, plan_valid=True)
    write_decision(config, date_iso="2026-06-10", decided_at="2026-06-10T12:00:00Z",
                   decision="BUY", plan=BUY_PLAN, plan_valid=True)
    # Run A: entry @100×10, expired DAY close carries a 4-share partial fill.
    _write_round_trip(config, date_iso="2026-06-01",
                      entry_at="2026-06-01T18:00:00Z", entry_price=100.0, entry_qty=10.0,
                      close_date="2026-06-05", close_at="2026-06-05T20:30:00Z",
                      close_price=110.0, close_qty=4.0, close_status="expired")
    # Run B: re-entry @120×5, fully closed @115 — 5 shares run A must NOT take.
    _write_round_trip(config, date_iso="2026-06-10",
                      entry_at="2026-06-10T18:00:00Z", entry_price=120.0, entry_qty=5.0,
                      close_date="2026-06-11", close_at="2026-06-11T20:30:00Z",
                      close_price=115.0, close_qty=5.0)
    fetcher = FakeBars({"NVDA": NVDA_BARS, "SPY": SPY_BARS})
    run(config, fetcher, now=datetime(2026, 6, 20, 0, 0, tzinfo=timezone.utc))
    by_run = {record.run_id: record for record in read_outcomes(config)}
    a = by_run["2026-06-01-us-NVDA-brief-1"]
    assert a.paper.realized_pnl == pytest.approx((110.0 - 100.0) * 4.0)
    assert a.paper.closed is False  # 6 shares still open — B's close excluded
    b = by_run["2026-06-10-us-NVDA-brief-1"]
    assert b.paper.realized_pnl == pytest.approx((115.0 - 120.0) * 5.0)
    assert b.paper.closed is True


# ---------------------------------------------------------------------------
# Row selection: ERROR rows, session disjointness
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_error_rows_are_skipped(tmp_path):
    config = make_config(tmp_path)
    write_decision(config, decision="ERROR", plan=None, plan_valid=False)
    fetcher = FakeBars({"NVDA": NVDA_BARS, "SPY": SPY_BARS})
    summary, _ = run(config, fetcher)
    assert (summary["settled"], summary["skipped"]) == (0, 1)
    assert read_outcomes(config) == []


@pytest.mark.unit
def test_sessions_settle_disjoint_rows(tmp_path):
    config = make_config(tmp_path)
    write_decision(config)  # us NVDA
    write_decision(config, ticker="600519.SS", session="cn", decided_at="2026-06-01T01:00:00Z")
    fetcher = FakeBars(
        {
            "NVDA": NVDA_BARS,
            "SPY": SPY_BARS,
            "600519.SS": weekday_bars(JUN1, [1400.0, 1414.0]),
            "000001.SS": weekday_bars(JUN1, [3000.0, 3060.0]),
        }
    )
    summary, _ = run(config, fetcher, session="us")
    assert summary["settled"] == 1
    assert [r.run_id for r in read_outcomes(config)] == ["2026-06-01-us-NVDA-brief-1"]
    summary, _ = run(config, fetcher, session="cn")
    assert summary["settled"] == 1
    assert {r.run_id for r in read_outcomes(config)} == {
        "2026-06-01-us-NVDA-brief-1",
        "2026-06-01-cn-600519.SS-brief-1",
    }


# ---------------------------------------------------------------------------
# Adapter refresh: sole subprocess, warn degrade
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_refresh_invokes_the_adapter_cli_and_nothing_else(tmp_path):
    config = make_config(tmp_path)
    write_decision(config)
    fetcher = FakeBars({"NVDA": NVDA_BARS, "SPY": SPY_BARS})
    _, refresh = run(config, fetcher)
    # Settle NEVER constructs a broker client: its one and only subprocess is
    # the adapter's refresh entrypoint (the sole broker gateway).
    assert len(refresh.calls) == 1
    argv, timeout = refresh.calls[0]
    assert argv == [
        str(config.python_executable), "-m", "pipeline.execution_adapter", "refresh",
    ]
    assert timeout == settle.REFRESH_TIMEOUT_S


@pytest.mark.unit
def test_refresh_failure_degrades_to_warn_and_outcomes_still_compute(tmp_path):
    # Adapter absent / spawn failure.
    config = make_config(tmp_path / "absent")
    write_decision(config)
    fetcher = FakeBars({"NVDA": NVDA_BARS, "SPY": SPY_BARS})
    summary, _ = run(config, fetcher, refresh=FakeRefresh(exc=OSError("no interpreter")))
    assert summary["refresh_ok"] is False
    assert "could not run" in summary["refresh_error"]
    assert summary["settled"] == 1  # outcomes computation proceeded
    assert len(read_outcomes(config)) == 1

    # Adapter present but failing (e.g. keys not configured) — stderr one-liner
    # surfaces in the reason.
    config = make_config(tmp_path / "failing")
    write_decision(config)
    summary, _ = run(
        config,
        FakeBars({"NVDA": NVDA_BARS, "SPY": SPY_BARS}),
        refresh=FakeRefresh(
            returncode=1,
            stderr="execution-adapter: ALPACA_API_KEY / ALPACA_SECRET_KEY not configured in .env\n",
        ),
    )
    assert summary["refresh_ok"] is False
    assert "exited 1" in summary["refresh_error"]
    assert "ALPACA_API_KEY" in summary["refresh_error"]
    assert summary["settled"] == 1


# ---------------------------------------------------------------------------
# Settle log + CLI
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_settle_log_gets_one_line_per_settled_run(tmp_path):
    config = make_config(tmp_path)
    write_decision(config)
    fetcher = FakeBars({"NVDA": NVDA_BARS, "SPY": SPY_BARS})
    run(config, fetcher)
    log_path = config.ledger_dir / "settle.log"
    lines = log_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert "2026-06-01-us-NVDA-brief-1" in lines[0]
    assert "settled" in lines[0]
    # An idempotent re-run settles nothing and logs nothing.
    run(config, fetcher)
    assert len(log_path.read_text(encoding="utf-8").splitlines()) == 1


@pytest.mark.unit
def test_run_settle_rejects_unknown_session(tmp_path):
    config = make_config(tmp_path)
    with pytest.raises(ValueError, match="unknown session"):
        settle.run_settle(
            config=config,
            session="uk",
            bars_fetcher=FakeBars({}),
            refresh_runner=FakeRefresh(),
            clock=lambda: NOW_LATE,
        )


@pytest.mark.unit
def test_cli_main_prints_the_summary_line(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("TRADINGAGENTS_STATE_DIR", str(tmp_path / "state"))
    config = make_config(tmp_path)
    write_decision(config)
    fetcher = FakeBars({"NVDA": NVDA_BARS, "SPY": SPY_BARS})
    exit_code = settle.main(
        ["--session", "us", "--date", "2026-06-01"],
        bars_fetcher=fetcher,
        refresh_runner=FakeRefresh(),
        clock=lambda: NOW_LATE,
    )
    assert exit_code == 0
    out_lines = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
    summary = json.loads(out_lines[-1])
    assert summary["entrypoint"] == "settle"
    assert summary["date"] == "2026-06-01"
    assert summary["session"] == "us"
    assert summary["settled"] == 1
    assert summary["refresh_ok"] is True
    assert (config.ledger_dir / "outcomes.jsonl").exists()
