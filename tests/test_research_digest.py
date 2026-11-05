"""Research digest — specs/research-digest.md."""

import json
from datetime import date
from pathlib import Path

import pytest

from pipeline import research_digest as rd
from pipeline.common import sha256_text
from pipeline.config import PipelineConfig

DAY = date(2026, 11, 4)
HOOK = "https://open.feishu.cn/open-apis/bot/v2/hook/abc"


def config(tmp_path: Path, **overrides) -> PipelineConfig:
    return PipelineConfig(state_dir=tmp_path, **overrides)


def ticker_brief(ticker: str, score: float, impact: str, catalyst="earnings", window="2026-11-17") -> str:
    return (f"---\nas_of_date: {DAY}\nticker: {ticker}\nsession: us\ncatalyst_score: {score}\n"
            f"catalyst_type: {catalyst}\ncatalyst_window: {window}\n---\n\n## Risks\n\n- x\n\n**Impact**: {impact}\n")


MACRO = ("---\nas_of_date: 2026-11-04\nsession: us\n---\n\n"
         "## Monetary Policy & Rates\n\n- a\n\n**Impact**: bearish — yields rose.\n\n"
         "## Growth & Earnings\n\n- b\n\n**Impact**: mixed — split.\n\n"
         "## Surprises & Watchlist\n\n- c\n")


def stage(code, **details):
    return {"code": code, "details": details}


def entry(ticker, collect="written", evaluate=None, decide=None):
    stages = {"collect": stage(collect)}
    if evaluate:
        stages["evaluate"] = stage(evaluate)
    if decide:
        stages["decide"] = decide
    return {"ticker": ticker, "stages": stages}


def write_slot(cfg: PipelineConfig, session="us", *, briefs, tickers, macro=MACRO, macro_verdict="warn"):
    cfg.macro_brief_dir.mkdir(parents=True, exist_ok=True)
    macro_path = cfg.macro_brief_dir / f"{DAY}.{session}.md"
    macro_path.write_text(macro, encoding="utf-8")
    if macro_verdict:
        macro_path.with_name(f"{DAY}.{session}.eval.json").write_text(json.dumps(
            {"brief_sha256": sha256_text(macro), "verdict": macro_verdict}), encoding="utf-8")
    for ticker, text in briefs.items():
        path = cfg.ticker_brief_dir / ticker / f"{DAY}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    funnel = cfg.state_dir / "funnel" / session
    funnel.mkdir(parents=True, exist_ok=True)
    (funnel / f"{DAY}.json").write_text(json.dumps({"tickers": tickers}), encoding="utf-8")


@pytest.mark.unit
def test_the_digest_lists_macro_reads_and_briefs_analysed_first(tmp_path):
    cfg = config(tmp_path)
    write_slot(cfg, briefs={
        "NVDA": ticker_brief("NVDA", 7.0, "mixed — positive tape into the print."),
        "HUM": ticker_brief("HUM", 8.0, "neutral — no verified news."),
        "AAPL": ticker_brief("AAPL", 3.0, "bullish — strong quarter.", window="none"),
        "LITE": ticker_brief("LITE", 8.0, "mixed — deal-bound."),
    }, tickers=[
        entry("AAPL", evaluate="warn"),
        entry("LITE", evaluate="fail"),
        entry("NVDA", evaluate="warn", decide=stage("BUY", pm_rating="Overweight", execute_eligible=True)),
        entry("HUM", evaluate="warn", decide=stage("HOLD", pm_rating="Hold")),
        entry("META", collect="not-collected-watch"),  # no brief: not listed
    ])
    text = rd.build_digest(cfg, "us", DAY)
    lines = text.splitlines()
    assert lines[0] == "📊 研究摘要 · 美股 · 2026-11-04"
    assert lines[1] == "宏观（评估 warn）：利率 偏空 · 增长 分歧"  # Watchlist has no impact read
    assert lines[2] == "个股 4 只，分析 2 只："
    assert lines[3] == "• HUM 8.0 earnings 2026-11-17 · 中性 — no verified news.｜评估 warn｜分析 HOLD（PM Hold）"
    assert lines[4] == ("• NVDA 7.0 earnings 2026-11-17 · 分歧 — positive tape into the print."
                        "｜评估 warn｜分析 BUY（PM Overweight）")
    assert lines[5].startswith("• LITE 8.0") and lines[5].endswith("｜评估 fail")
    assert lines[6] == "• AAPL 3.0 earnings · 偏多 — strong quarter.｜评估 warn"  # "none" window dropped
    assert "META" not in text


@pytest.mark.unit
def test_cn_decisions_say_no_order_and_errors_never_leak(tmp_path):
    cfg = config(tmp_path)
    error = stage("ERROR", error="run-one: Error code: 401 - Your api key: ****48b5 is invalid")
    write_slot(cfg, "cn", briefs={
        "0981.HK": ticker_brief("0981.HK", 7.5, "bullish — guidance raise."),
        "600256.SS": ticker_brief("600256.SS", 7.0, "neutral — quiet."),
    }, tickers=[
        entry("0981.HK", evaluate="pass", decide=stage("BUY", pm_rating="Overweight")),
        entry("600256.SS", evaluate="warn", decide=error),
    ], macro_verdict=None)
    text = rd.build_digest(cfg, "cn", DAY)
    assert "📊 研究摘要 · A股/港股" in text and "宏观（未评估）" in text
    assert "分析 BUY（PM Overweight），不下单" in text
    assert "600256.SS 7.0" in text and text.endswith("｜评估 warn｜分析 出错")
    assert "401" not in text and "48b5" not in text and "api key" not in text


@pytest.mark.unit
def test_a_stale_eval_does_not_count_and_long_rationales_are_cut(tmp_path):
    cfg = config(tmp_path)
    write_slot(cfg, briefs={"MU": ticker_brief("MU", 4.5, "mixed — " + "x" * 300)},
               tickers=[entry("MU")])
    (cfg.macro_brief_dir / f"{DAY}.us.eval.json").write_text(
        json.dumps({"brief_sha256": "another revision", "verdict": "pass"}), encoding="utf-8")
    text = rd.build_digest(cfg, "us", DAY)
    assert "宏观（未评估）" in text  # revision-bound: a pass for another revision is no verdict
    mu = next(line for line in text.splitlines() if line.startswith("• MU"))
    assert "…" in mu and len(mu) < 200 and mu.endswith("｜未评估")


@pytest.mark.unit
def test_missing_inputs_still_give_a_digest(tmp_path):
    text = rd.build_digest(config(tmp_path), "us", DAY)
    assert text.splitlines() == ["📊 研究摘要 · 美股 · 2026-11-04", "宏观：无简报", "个股：无简报"]
    cfg = config(tmp_path)
    (cfg.state_dir / "funnel" / "us").mkdir(parents=True)
    for corrupt in ("{not json", "[]", "3", '{"tickers": {"MU": 1}}', '{"tickers": 3}',
                    '{"tickers": [{"ticker": "MU", "stages": []}]}',
                    '{"tickers": [{"ticker": "MU", "stages": {"decide": 1}}]}',
                    '{"tickers": [{"ticker": "MU", "stages": {"decide": {"code": "BUY", "details": 1}}}]}'):
        (cfg.state_dir / "funnel" / "us" / f"{DAY}.json").write_text(corrupt, encoding="utf-8")
        text = rd.build_digest(cfg, "us", DAY)
        assert text.startswith("📊 研究摘要") and "宏观：无简报" in text, corrupt


@pytest.mark.unit
def test_the_digest_goes_to_the_trades_outbox_once_per_slot(tmp_path, monkeypatch):
    from pipeline import trade_monitor

    sent = []
    monkeypatch.setattr(trade_monitor, "make_trades_sender", lambda cfg: lambda title, text: sent.append(text))
    assert rd.send_digest(config(tmp_path), "us", DAY) == 0  # no trades webhook: nothing anywhere
    cfg = config(tmp_path, lark_trades_webhook_url=HOOK)
    write_slot(cfg, briefs={"MU": ticker_brief("MU", 4.5, "mixed — m.")}, tickers=[entry("MU")])
    assert rd.send_digest(cfg, "us", DAY) == 1
    assert rd.send_digest(cfg, "us", DAY) == 0  # a re-run or the execution reopen: not again
    assert len(sent) == 1 and sent[0].startswith("📊 研究摘要 · 美股")
    assert rd.send_digest(config(tmp_path, lark_trades_webhook_url=HOOK, notifications_enabled=False),
                          "us", date(2026, 11, 5)) == 0


@pytest.mark.unit
def test_a_failing_send_never_raises(tmp_path, monkeypatch):
    from pipeline import trade_notify

    def boom(*a, **k):
        raise OSError("disk")

    monkeypatch.setattr(trade_notify, "enqueue", boom)
    assert rd.send_digest(config(tmp_path, lark_trades_webhook_url=HOOK), "us", DAY) == 0


def event(code, **details):
    return {"code": code, "details": details}


@pytest.mark.unit
@pytest.mark.parametrize(("decide", "shown"), [
    # Paired: the stage keeps the strongest arm (feeds BUY), the brief arm trades.
    ({**stage("BUY", arm="feeds", pm_rating="Overweight"),
      "events": [event("HOLD", arm="brief", pm_rating="Hold"), event("BUY", arm="feeds", pm_rating="Overweight")]},
     "分析 HOLD（PM Hold）"),
    (stage("BUY", arm="feeds", pm_rating="Overweight", plan_valid=True), "分析 feeds 对照 BUY（PM Overweight），不下单"),
    (stage("BUY", arm="brief", pm_rating="Overweight", plan_valid=False, execute_eligible=False),
     "分析 BUY（PM Overweight），计划无效，不下单"),
    (stage("SELL", arm="brief", pm_rating="Underweight", plan_valid=True, execute_eligible=True),
     "分析 SELL（PM Underweight）"),
    # A stage rebuilt from files carries no execute_eligible: unknown, not "no order".
    (stage("BUY", arm="brief", run_id="r", pair_id=None, plan_valid=True), "分析 BUY"),
    (stage("BUY", arm="brief", pm_rating="Overweight", plan_valid=True, execute_eligible=False),
     "分析 BUY（PM Overweight），不下单"),
])
def test_the_decision_shown_is_the_trading_arms_and_says_when_no_order_follows(tmp_path, decide, shown):
    cfg = config(tmp_path)
    write_slot(cfg, briefs={"NVDA": ticker_brief("NVDA", 7.0, "mixed — m.")},
               tickers=[entry("NVDA", evaluate="warn", decide=decide)])
    line = next(x for x in rd.build_digest(cfg, "us", DAY).splitlines() if x.startswith("• NVDA"))
    assert line.endswith("｜评估 warn｜" + shown)


@pytest.mark.unit
def test_an_existing_brief_and_a_decision_without_a_brief_are_listed(tmp_path):
    cfg = config(tmp_path)
    write_slot(cfg, briefs={"MU": ticker_brief("MU", 4.5, "mixed — m.")}, tickers=[
        entry("MU", collect="exists", evaluate="warn"),  # a re-fired slot found it on disk
        entry("AAPL", collect="failed",
              decide=stage("HOLD", arm="feeds", pm_rating="Hold")),  # core: analysed on feeds
    ])
    text = rd.build_digest(cfg, "us", DAY)
    assert "个股 2 只，分析 1 只：" in text
    assert "• AAPL – （无简报）｜未评估｜分析 feeds 对照 HOLD（PM Hold）" in text
    assert "• MU 4.5 earnings 2026-11-17 · 分歧 — m.｜评估 warn" in text


@pytest.mark.unit
def test_macro_sections_follow_the_contracts_heading_rule(tmp_path):
    cfg = config(tmp_path)
    tight = ("---\nas_of_date: 2026-11-04\nsession: us\n---\n## Monetary Policy & Rates\n\n- a\n\n"
             "**Impact**: bearish — r.\n\n##  Growth & Earnings\n\n- b\n\n**Impact**: bullish — g.\n")
    write_slot(cfg, briefs={}, tickers=[], macro=tight, macro_verdict=None)
    assert rd.macro_line(cfg, "us", DAY) == "宏观（未评估）：利率 偏空 · 增长 偏多"


@pytest.mark.unit
def test_a_lark_failure_is_logged_as_the_runbook_says_and_kept(tmp_path, monkeypatch, caplog):
    from pipeline import trade_monitor
    from pipeline.orchestrator import LarkError

    def refuse(title, text):
        raise LarkError("code 19021: sign match fail")

    monkeypatch.setattr(trade_monitor, "make_trades_sender", lambda cfg: refuse)
    cfg = config(tmp_path, lark_trades_webhook_url=HOOK)
    write_slot(cfg, briefs={}, tickers=[])
    with caplog.at_level("WARNING"):
        assert rd.send_digest(cfg, "us", DAY) == 0
    assert "trade notification research:us:2026-11-04 not sent" in caplog.text
    from pipeline.trade_notify import SentStore

    assert [item["key"] for item in SentStore(tmp_path).outbox()] == ["research:us:2026-11-04"]  # retried later


@pytest.mark.unit
def test_a_digest_not_queued_stays_pending_until_the_watchdog_queues_it(tmp_path, monkeypatch):
    from pipeline import trade_monitor, trade_notify

    sent = []
    monkeypatch.setattr(trade_monitor, "make_trades_sender", lambda cfg: lambda title, text: sent.append(text))
    cfg = config(tmp_path, lark_trades_webhook_url=HOOK)
    write_slot(cfg, briefs={}, tickers=[])
    rd.mark_pending(cfg, "us", DAY)
    real_enqueue = trade_notify.enqueue

    def no_lock(*a, **k):
        raise TimeoutError("trades lock")

    monkeypatch.setattr(trade_notify, "enqueue", no_lock)
    assert rd.send_digest(cfg, "us", DAY) == 0
    pending = tmp_path / "trade_notify" / "research_pending" / "us_2026-11-04"
    assert pending.exists()  # not queued: still due
    monkeypatch.setattr(trade_notify, "enqueue", real_enqueue)
    assert rd.retry_pending(cfg, date(2026, 11, 4)) == 1
    assert not pending.exists() and len(sent) == 1
    assert rd.retry_pending(cfg, date(2026, 11, 4)) == 0  # nothing left, and the key is spent


@pytest.mark.unit
def test_an_old_pending_digest_is_dropped_with_an_error(tmp_path, caplog):
    cfg = config(tmp_path, lark_trades_webhook_url=HOOK)
    rd.mark_pending(cfg, "cn", date(2026, 10, 30))
    with caplog.at_level("ERROR"):
        assert rd.retry_pending(cfg, date(2026, 11, 4)) == 0
    assert "research digest for cn 2026-10-30 dropped" in caplog.text
    assert not list((tmp_path / "trade_notify" / "research_pending").iterdir())


@pytest.mark.unit
def test_one_malformed_row_does_not_cost_the_others(tmp_path):
    cfg = config(tmp_path)
    write_slot(cfg, briefs={"MU": ticker_brief("MU", 4.5, "mixed — m.")},
               tickers=[{"ticker": "BAD", "stages": []}, entry("MU", evaluate="warn")])
    assert "• MU 4.5" in rd.build_digest(cfg, "us", DAY)
