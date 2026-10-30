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


def select(core, rows, positions, *, score=None, session="us", **settings):
    return ar.select_core_runs(
        core, session=session, slot_date=FRIDAY,
        settings=ar.RunnerSettings(core_skip_unchanged=True, **settings),
        decision_rows=rows, positions=positions, inspect_ticker=brief(score),
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
    assert aaa[3] == {"last_decision_date": "2026-08-06", "age": 1, "score": 6.5, "threshold": 7.0}
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
        inspect_ticker=brief(5.0),
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
