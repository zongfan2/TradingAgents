"""Collect only what can be analysed — specs/llm-cost.md R8."""

import json

import pytest

from pipeline.config import core_analysis_sessions
from pipeline.evaluator import EXIT_OK, batch_tickers
from pipeline.funnel_events import FunnelEvents
from pipeline.ticker_collector import collect_all
from tests.test_evaluator import FakeRunner, judgment, ticker_text
from tests.test_evaluator import _write_pool as write_eval_pool
from tests.test_evaluator import batch_dirs  # noqa: F401  (pytest fixture)
from tests.test_evaluator import write_brief as write_eval_brief
from tests.test_ticker_collector import AS_OF, FakeBackend, make_ticker_brief, write_pool
from tests.test_ticker_collector import dirs  # noqa: F401  (pytest fixture)


@pytest.mark.unit
def test_shared_parser():
    assert core_analysis_sessions({}) == ("cn", "us")
    assert core_analysis_sessions({"TRADINGAGENTS_CORE_ANALYSIS_SESSIONS": " US "}) == ("us",)
    with pytest.raises(ValueError):
        core_analysis_sessions({"TRADINGAGENTS_CORE_ANALYSIS_SESSIONS": "us,hk"})


@pytest.mark.unit
def test_core_off_session_collects_only_opportunity_and_records_the_skip(dirs, tmp_path, monkeypatch):  # noqa: F811
    _brief_dir, pool_dir = dirs
    monkeypatch.setenv("TRADINGAGENTS_CORE_ANALYSIS_SESSIONS", "us")
    write_pool(pool_dir, session="cn", core=("0700.HK",),
               opportunity=(("600519.SS", "earnings", "Q3 print"),))
    fake = FakeBackend({"600519.SS": [make_ticker_brief(ticker="600519.SS", session="cn")]})
    events = FunnelEvents(tmp_path / "state", "cn", AS_OF, "ticker_collectors")
    summary = collect_all("cn", as_of=AS_OF, runner=fake, funnel=events)
    assert [t for _b, t, _p in fake.calls] == ["600519.SS"]
    assert [o.ticker for o in summary.outcomes] == ["600519.SS"]
    codes = {(e["ticker"], e["outcome"], e["code"]) for e in events.emitted}
    assert ("0700.HK", "excluded", "not-collected-core-off") in codes
    # The us session (listed) still collects its core.
    write_pool(pool_dir, session="us", core=("NVDA",), opportunity=())
    fake_us = FakeBackend({"NVDA": [make_ticker_brief(ticker="NVDA")]})
    collect_all("us", as_of=AS_OF, runner=fake_us)
    assert [t for _b, t, _p in fake_us.calls] == ["NVDA"]


@pytest.mark.unit
def test_core_off_without_a_pool_is_nothing_to_do_not_an_error(dirs, monkeypatch):  # noqa: F811
    _brief_dir, pool_dir = dirs
    monkeypatch.setenv("TRADINGAGENTS_CORE_ANALYSIS_SESSIONS", "us")
    pool_dir.mkdir(parents=True, exist_ok=True)
    (pool_dir / "core.cn.yaml").write_text("- ticker: 0700.HK\n", encoding="utf-8")
    fake = FakeBackend({})
    summary = collect_all("cn", as_of=AS_OF, runner=fake)
    assert fake.calls == [] and summary.outcomes == [] and summary.success


@pytest.mark.unit
def test_evaluator_skips_core_briefs_of_a_core_off_session(batch_dirs, monkeypatch, capsys):  # noqa: F811
    pool_dir, brief_dir = batch_dirs
    monkeypatch.setenv("TRADINGAGENTS_CORE_ANALYSIS_SESSIONS", "us")
    day = "2026-08-03"
    write_eval_pool(pool_dir, "cn", day, core=("0700.HK",), opportunity=("600519.SS",))
    write_eval_brief(brief_dir / "0700.HK", f"{day}.md", ticker_text(ticker="0700.HK", session="cn"))
    write_eval_brief(brief_dir / "600519.SS", f"{day}.md",
                     ticker_text(ticker="600519.SS", session="cn", catalyst_score=8.0))
    runner = FakeRunner(judgment())
    assert batch_tickers("cn", date_str=day, runner=runner) == EXIT_OK
    assert runner.calls == 1  # only the opportunity brief
    summary = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert summary["requested"] == 1
