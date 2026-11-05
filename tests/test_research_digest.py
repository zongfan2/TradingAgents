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
        entry("NVDA", evaluate="warn", decide=stage("BUY", pm_rating="Overweight")),
        entry("HUM", evaluate="warn", decide=stage("HOLD", pm_rating="Hold")),
        entry("META", collect="not-collected-watch"),  # no brief: not listed
    ])
    text = rd.build_digest(cfg, "us", DAY)
    lines = text.splitlines()
    assert lines[0] == "📊 研究摘要 · 美股 · 2026-11-04"
    assert lines[1] == "宏观（评估 warn）：利率 偏空 · 增长 分歧"  # Watchlist has no impact read
    assert lines[2] == "个股简报 4 份，分析 2 只："
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
    (cfg.state_dir / "funnel" / "us" / f"{DAY}.json").write_text("{not json", encoding="utf-8")
    assert "个股：无简报" in rd.build_digest(cfg, "us", DAY)


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
