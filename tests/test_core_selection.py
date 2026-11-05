"""Core-run cuts — specs/llm-cost.md R6 (cn catalyst-only, unchanged HOLD skip)."""

from datetime import date, datetime, timezone

import pytest

from pipeline import analysis_runner as ar
from pipeline.contracts.ledger import PositionEntry, PositionsSnapshot
from pipeline.funnel_events import read_events
from tests.test_analysis_runner import (
    SLOT,
    FakeChild,
    make_config,
    make_resolvers,
    read_records,
    run,
    write_brief,
    write_eval,
    write_pool,
)

FRIDAY = date(2026, 8, 7)


def snapshot(*held, as_of=datetime(2026, 8, 6, 21, tzinfo=timezone.utc)):
    return PositionsSnapshot(
        as_of=as_of, equity=100_000.0,
        positions=[
            PositionEntry(ticker=t, qty=10, avg_entry=100.0, last_fill_at=as_of,
                          market_value=1000.0, unrealized_pl=0.0, tranches=1)
            for t in held
        ],
    )


def row(ticker, day, decision="HOLD", *, n=1, session="us", arm="brief", trigger="core"):
    return {"run_id": f"{day}-{session}-{ticker}-{arm}-{n}", "ticker": ticker, "date": day,
            "session": session, "arm": arm, "trigger": trigger, "decision": decision}


def brief(score, verdict="pass"):
    def inspect(ticker):
        value = score.get(ticker) if isinstance(score, dict) else score
        if isinstance(value, ar.BriefInfo):
            return value
        return ar.BriefInfo(verdict, catalyst_score=value)
    return inspect


def flat(ticker, since, slot_date):
    return 0.0, 0.0  # no price move since the HOLD


def select(core, rows, positions, *, score=None, session="us", move=flat, **settings):
    return ar.select_core_runs(
        core, session=session, slot_date=FRIDAY,
        settings=ar.RunnerSettings(core_skip_unchanged=True, **settings),
        decision_rows=rows, positions=positions, inspect_ticker=brief(score), price_move=move,
    )


@pytest.mark.unit
def test_session_off_skips_every_core_ticker():
    run_, skipped = select(["AAA", "BBB"], [], snapshot(), session="cn",
                           core_analysis_sessions=("us",))
    assert run_ == [] and [s[:2] for s in skipped] == [("AAA", "core-session-off"), ("BBB", "core-session-off")]


@pytest.mark.unit
def test_unchanged_hold_is_skipped_and_every_doubt_runs():
    rows = [
        row("AAA", "2026-08-06"),                      # HOLD yesterday -> skip
        row("BBB", "2026-08-06", "BUY"),               # not a HOLD -> run
        row("CCC", "2026-07-31"),                      # HOLD 5 trading days ago -> weekly run
        row("DDD", "2026-08-06"),                      # held position -> run
        row("EEE", "2026-08-06"),                      # catalyst today -> run
        row("GGG", "2026-08-05", "BUY"), row("GGG", "2026-08-06", "HOLD"),  # latest is HOLD -> skip
        row("HHH", "2026-08-06", "HOLD", n=1), row("HHH", "2026-08-06", "BUY", n=2),  # latest attempt BUY
        row("III", "2026-08-06", trigger="manual"),    # manual rows do not count -> run
        row("JJJ", "2026-08-06", arm="feeds"),         # feeds arm does not count -> run
        row("KKK", "2026-08-07"),                      # same-day row ignored -> run
    ]
    core = ["AAA", "BBB", "CCC", "DDD", "EEE", "FFF", "GGG", "HHH", "III", "JJJ", "KKK"]
    run_, skipped = select(core, rows, snapshot("DDD"), score={"EEE": 8.0, "AAA": 6.5, "GGG": 5.0})
    assert [s[0] for s in skipped] == ["AAA", "GGG"]
    assert run_ == ["BBB", "CCC", "DDD", "EEE", "FFF", "HHH", "III", "JJJ", "KKK"]
    aaa = skipped[0]
    assert aaa[1] == "core-unchanged"
    assert aaa[3] == {"last_decision_date": "2026-08-06", "age": 1, "score": 6.5, "threshold": 7.0,
                      "max_session_move_pct": 0.0, "total_move_pct": 0.0}
    assert "HOLD on 2026-08-06 (1 trading day(s) ago)" in aaa[2] and "eval pass" in aaa[2]


@pytest.mark.unit
def test_age_counts_trading_days_across_the_weekend():
    # Monday slot, HOLD on the previous Tuesday = 4 trading days -> still skipped;
    # previous Monday = 5 -> runs.
    rows = [row("AAA", "2026-08-04"), row("BBB", "2026-08-03")]
    out = ar.select_core_runs(
        ["AAA", "BBB"], session="us", slot_date=date(2026, 8, 10),
        settings=ar.RunnerSettings(core_skip_unchanged=True), decision_rows=rows,
        positions=snapshot(as_of=datetime(2026, 8, 7, 21, tzinfo=timezone.utc)),
        inspect_ticker=brief(5.0), price_move=flat,
    )
    assert out[0] == ["BBB"] and [s[0] for s in out[1]] == ["AAA"]


@pytest.mark.unit
def test_no_skip_without_a_fresh_positions_snapshot_or_when_off():
    rows = [row("AAA", "2026-08-06")]
    assert select(["AAA"], rows, None)[0] == ["AAA"]
    stale = snapshot(as_of=datetime(2026, 8, 3, 21, tzinfo=timezone.utc))
    assert select(["AAA"], rows, stale)[0] == ["AAA"]
    off = ar.select_core_runs(
        ["AAA"], session="us", slot_date=FRIDAY, settings=ar.RunnerSettings(),
        decision_rows=rows, positions=snapshot(), inspect_ticker=brief(None),
    )
    assert off == (["AAA"], [])


@pytest.mark.unit
def test_settings_env():
    settings = ar.load_settings({"TRADINGAGENTS_CORE_ANALYSIS_SESSIONS": "US ",
                                 "TRADINGAGENTS_CORE_SKIP_UNCHANGED": "on"})
    assert settings.core_analysis_sessions == ("us",) and settings.core_skip_unchanged is True
    defaults = ar.load_settings({})
    assert set(defaults.core_analysis_sessions) == {"cn", "us"} and not defaults.core_skip_unchanged
    with pytest.raises(ValueError):
        ar.load_settings({"TRADINGAGENTS_CORE_ANALYSIS_SESSIONS": "us,hk"})


@pytest.mark.unit
def test_cn_slot_runs_catalysts_only_and_records_the_skips(tmp_path):
    config = make_config(tmp_path)
    write_pool(config, "cn", SLOT, core=("0700.HK",), opportunity=(("600519.SS", 8.0),))
    macro = write_brief(config.macro_brief_dir / "2026-08-07.cn.md")
    write_eval(macro, "pass")
    opp = write_brief(config.ticker_brief_dir / "600519.SS" / "2026-08-07.md", catalyst_score=8.0)
    write_eval(opp, "pass")
    child = FakeChild()
    settings = ar.RunnerSettings(ab_pairing="off", core_analysis_sessions=("us",))
    run(config, make_resolvers(macro, {"600519.SS": opp}), child, session="cn", settings=settings)
    assert [spec.ticker for spec in child.specs] == ["600519.SS"]
    assert [r.trigger for r in read_records(config)] == ["catalyst"]
    events = [e for e in read_events(config.state_dir, "cn", SLOT) if e["stage"] == "trigger"]
    assert {(e["ticker"], e["outcome"], e["code"]) for e in events} >= {
        ("0700.HK", "excluded", "core-session-off"),
    }


@pytest.mark.unit
def test_doubt_about_todays_brief_runs_the_ticker():
    """Review 713ef97: no brief, no score, or a failed/missing eval is doubt."""
    rows = [row(t, "2026-08-06") for t in ("AAA", "BBB", "CCC", "DDD", "EEE")]
    scores = {
        "AAA": ar.BriefInfo("missing"),                       # no brief today
        "BBB": ar.BriefInfo("pass", catalyst_score=None),     # no parseable score
        "CCC": ar.BriefInfo("fail", catalyst_score=3.0),      # contaminated brief
        "DDD": ar.BriefInfo("missing", catalyst_score=3.0),   # brief, no eval
        "EEE": ar.BriefInfo("warn", catalyst_score=3.0),      # vouched, low -> skip
    }
    run_, skipped = select(list(scores), rows, snapshot(), score=scores)
    assert run_ == ["AAA", "BBB", "CCC", "DDD"] and [s[0] for s in skipped] == ["EEE"]


@pytest.mark.unit
def test_a_ticker_decided_today_is_never_relabelled_skipped():
    """Review 713ef97: a same-day rerun leaves it to the already-done rule."""
    rows = [row("AAA", "2026-08-06"), row("AAA", "2026-08-07", "BUY")]
    assert select(["AAA"], rows, snapshot(), score=3.0)[0] == ["AAA"]
    manual_today = [row("AAA", "2026-08-06"), row("AAA", "2026-08-07", trigger="manual")]
    assert select(["AAA"], manual_today, snapshot(), score=3.0)[0] == []


@pytest.mark.unit
def test_held_position_match_is_case_insensitive():
    """Review 713ef97: a held ticker is never skipped, whatever its case."""
    assert select(["AAA"], [row("AAA", "2026-08-06")], snapshot("aaa"), score=3.0)[0] == ["AAA"]


@pytest.mark.unit
def test_every_trigger_code_is_in_the_contract_spec():
    """Contract prose and model must not drift (specs/README.md rule 5)."""
    import re
    from pathlib import Path

    from pipeline.contracts.funnel import CODES

    spec = (Path(__file__).resolve().parent.parent / "specs" / "decision-funnel.md").read_text()
    for stage, codes in CODES.items():
        if codes is None:
            continue
        row = next(line for line in spec.splitlines() if line.startswith(f"| `{stage}` ("))
        listed = set(re.findall(r"`([a-zA-Z-]+)`", row))
        assert codes <= listed, (stage, sorted(codes - listed))


@pytest.mark.unit
def test_snapshot_shows_a_cost_cut_core_ticker_from_its_event_or_as_unknown(tmp_path):
    """decision-funnel R3 note: with its recorded event the skip shows as
    core-session-off; without one the trigger stage is unknown (no code)."""
    from pipeline import funnel
    from pipeline.funnel_events import events_path

    config = make_config(tmp_path)
    write_pool(config, "cn", SLOT, core=("0700.HK",))
    macro = write_brief(config.macro_brief_dir / "2026-08-07.cn.md")
    write_eval(macro, "pass")
    settings = ar.RunnerSettings(ab_pairing="off", core_analysis_sessions=("us",))
    run(config, make_resolvers(macro), FakeChild(), session="cn", settings=settings)

    def trigger(snap):
        entry = next(t for t in snap["tickers"] if t["ticker"] == "0700.HK")
        stage = entry["stages"]["trigger"]
        return stage["outcome"], stage["code"], stage["provenance"]

    assert trigger(funnel.build_snapshot(config, "cn", SLOT)) == (
        "excluded", "core-session-off", "recorded",
    )
    events_path(config.state_dir, "cn", SLOT).unlink()  # a backfill without events
    assert trigger(funnel.build_snapshot(config, "cn", SLOT)) == ("info", None, "unknown")


@pytest.mark.unit
@pytest.mark.parametrize(("move", "runs"), [
    ((4.9, 7.9), False),          # below both bars: still unchanged
    ((4.996, 1.0), True),         # shows as 5.00%, like the tape's ⚠
    ((1.0, 7.996), True),
    ((5.0, 1.0), True),           # one session at the bar
    ((3.0, 8.0), True),           # a slow grind to the total bar
    (None, True),                 # unknown move is doubt
])
def test_a_price_move_since_the_hold_forces_a_run(move, runs):
    seen = []

    def price(ticker, since, slot_date):
        seen.append((ticker, since, slot_date))
        return move

    run_, skipped = select(["AAA"], [row("AAA", "2026-08-05")], snapshot(), score=5.0, move=price)
    assert (run_ == ["AAA"]) is runs and (skipped == []) is runs
    assert seen == [("AAA", date(2026, 8, 5), FRIDAY)]  # sessions since the HOLD's day


@pytest.mark.unit
def test_a_failing_price_read_runs_the_ticker():
    def boom(ticker, since, slot_date):
        raise OSError("yfinance down")

    assert select(["AAA"], [row("AAA", "2026-08-06")], snapshot(), score=5.0, move=boom)[0] == ["AAA"]


def bars(*closes, start=date(2026, 7, 27)):
    """Consecutive weekday closes from ``start``."""
    from datetime import timedelta

    out, day = [], start
    for close in closes:
        while day.weekday() >= 5:
            day += timedelta(days=1)
        out.append((day, close))
        day += timedelta(days=1)
    return out


@pytest.mark.unit
def test_price_move_since_reads_the_sessions_after_the_hold(monkeypatch):
    from pipeline import price_tape

    # 07-27 .. 08-06 (9 sessions); HOLD decided before the open on 08-04.
    closes = bars(100, 100, 100, 100, 100, 100, 94, 95, 96.9)
    monkeypatch.setattr(price_tape, "fetch_daily_closes", lambda t: closes)
    largest, total = ar.price_move_since("AAA", date(2026, 8, 4), FRIDAY)
    assert largest == pytest.approx(6.0) and total == pytest.approx(3.1)
    # A tape behind the last weekday, or no session before the HOLD: unknown.
    monkeypatch.setattr(price_tape, "fetch_daily_closes", lambda t: closes[:-1])
    assert ar.price_move_since("AAA", date(2026, 8, 4), FRIDAY) is None
    monkeypatch.setattr(price_tape, "fetch_daily_closes", lambda t: closes[6:])
    assert ar.price_move_since("AAA", date(2026, 8, 4), FRIDAY) is None
    # A gap among the sessions since (an unusable close) is unknown too.
    gapped = closes[:7] + [(closes[7][0], float("nan"))] + closes[8:]
    monkeypatch.setattr(price_tape, "fetch_daily_closes", lambda t: gapped)
    assert ar.price_move_since("AAA", date(2026, 8, 4), FRIDAY) is None


@pytest.mark.unit
def test_by_default_a_core_skip_needs_real_price_data():
    # The offline fixture makes every price read fail: no ticker is skipped.
    out = ar.select_core_runs(
        ["AAA"], session="us", slot_date=FRIDAY, settings=ar.RunnerSettings(core_skip_unchanged=True),
        decision_rows=[row("AAA", "2026-08-06")], positions=snapshot(), inspect_ticker=brief(5.0))
    assert out == (["AAA"], [])


@pytest.mark.unit
def test_the_skip_reason_shows_the_move_as_judged():
    run_, skipped = select(["AAA"], [row("AAA", "2026-08-06")], snapshot(), score=5.0,
                           move=lambda *_: (4.96, 7.96))
    assert run_ == [] and "price moved 7.96% since (max session 4.96%)" in skipped[0][2]
