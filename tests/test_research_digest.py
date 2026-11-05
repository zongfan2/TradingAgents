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


def tstage(code):
    return {"code": code}


def entry(ticker, collect="written", evaluate=None, decide=None, trigger=None):
    stages = {"collect": stage(collect)}
    if evaluate:
        stages["evaluate"] = stage(evaluate)
    if trigger:
        stages["trigger"] = tstage(trigger)
    if decide:
        stages["decide"] = decide
    return {"ticker": ticker, "stages": stages}


@pytest.mark.unit
def test_the_digest_groups_tickers_by_outcome_in_plain_words(tmp_path):
    cfg = config(tmp_path)
    write_slot(cfg, briefs={
        "NVDA": ticker_brief("NVDA", 7.0, "mixed — positive tape into the print."),
        "HUM": ticker_brief("HUM", 8.0, "neutral — no verified news.", window="2026-11-06"),
        "AAPL": ticker_brief("AAPL", 3.0, "bullish — strong quarter.", window="none"),
        "AMD": ticker_brief("AMD", 3.0, "mixed — m.", window="2026-11-03"),  # already happened
        "LITE": ticker_brief("LITE", 9.0, "mixed — deal-bound.", window="2026-11-05..2026-11-06"),
        "FN": ticker_brief("FN", 4.0, "mixed — f."),
    }, tickers=[
        entry("AAPL", evaluate="warn", trigger="core-unchanged"),
        entry("AMD", evaluate="warn", trigger="core-unchanged"),
        entry("LITE", evaluate="fail", trigger="eval-fail"),
        entry("FN", evaluate="skipped-below-threshold", trigger="below-threshold"),
        entry("NVDA", evaluate="warn", trigger="core",
              decide=stage("BUY", arm="brief", pm_rating="Overweight", plan_valid=True, execute_eligible=True)),
        entry("HUM", evaluate="warn", trigger="catalyst",
              decide=stage("HOLD", arm="brief", pm_rating="Underweight", plan_valid=True, execute_eligible=False)),
        entry("META", collect="not-collected-watch"),  # no brief: not listed
    ])
    lines = rd.build_digest(cfg, "us", DAY).splitlines()
    assert lines == [
        "📊 研究摘要 · 美股 · 11-04 ｜ 计划下单：NVDA 买入",  # no trades book: no position segment
        "宏观（评估 warn）：利率 偏空 · 增长 分歧",
        "",
        "已分析（2）",
        "• HUM 8.0 财报 11-06 → 不买（PM 看淡；无持仓，不做空）",
        "• NVDA 7.0 财报 11-17 → 买入（PM 看多；下单）",
        "",
        "被拦下（1）",
        "• LITE 9.0 财报 11-05~11-06 → 简报评估 fail，未分析",
        "",
        "未触发（3）",
        "• 近几日已分析且无新变化，不重复：AAPL 3.0 · AMD 3.0",  # AMD's past window dropped
        "• 机会池分数 <7，未评估：FN 4.0",
    ]


@pytest.mark.unit
@pytest.mark.parametrize(("code", "rating", "held", "extra", "shown"), [
    ("HOLD", "Hold", False, {}, "不买（PM 中性）"),
    ("HOLD", "Underweight", False, {}, "不买（PM 看淡；无持仓，不做空）"),
    ("HOLD", "Hold", True, {}, "继续持有（PM 中性）"),
    ("BUY", "Overweight", True, {"execute_eligible": True}, "加仓（PM 看多；09:45 ET 下单）"),
    ("SELL", "Sell", True, {"execute_eligible": True}, "卖出（PM 看空；09:45 ET 下单）"),
    ("SELL", "Sell", False, {}, "不买（PM 看空；无持仓，不做空）"),
    ("BUY", "Overweight", False, {"execute_eligible": False}, "看多，不下单（PM 看多；不满足下单条件）"),
    ("BUY", "Overweight", False, {"plan_valid": False}, "看多，不下单（PM 看多；计划无效，不下单）"),
    ("BUY", "Overweight", False, {"arm": "feeds"}, "看多，不下单（PM 看多；对照分析，不下单）"),
    # A stage rebuilt from files carries no execute_eligible: unknown, not "no order".
    ("BUY", None, False, {"plan_valid": True}, "买入（09:45 ET 下单）"),
])
def test_actions_are_plain_words_and_hold_never_means_holding_nothing(code, rating, held, extra, shown):
    details = {"arm": "brief", "pm_rating": rating, **extra}
    assert rd.action_text({"code": code, "details": details}, session="us", held=held,
                          execution_time="09:45") == shown


@pytest.mark.unit
def test_cn_decisions_say_no_order_and_errors_never_leak(tmp_path):
    cfg = config(tmp_path)
    error = stage("ERROR", error="run-one: Error code: 401 - Your api key: ****48b5 is invalid")
    write_slot(cfg, "cn", briefs={
        "0981.HK": ticker_brief("0981.HK", 7.5, "bullish — guidance raise."),
        "600256.SS": ticker_brief("600256.SS", 7.0, "neutral — quiet."),
    }, tickers=[
        entry("0981.HK", evaluate="pass", decide=stage("BUY", arm="brief", pm_rating="Overweight")),
        entry("600256.SS", evaluate="warn", decide=error),
    ], macro_verdict=None)
    text = rd.build_digest(cfg, "cn", DAY)
    assert text.splitlines()[0] == "📊 研究摘要 · A股/港股 · 11-04 ｜ A股不下单"
    assert "宏观（未评估）" in text
    assert "• 0981.HK 7.5 财报 11-17 → 看多，不下单（PM 看多；A股不下单）" in text
    assert "• 600256.SS 7.0 财报 11-17 → 分析出错" in text
    assert "401" not in text and "48b5" not in text and "api key" not in text


@pytest.mark.unit
def test_positions_come_from_the_trades_book(tmp_path):
    cfg = config(tmp_path)
    write_slot(cfg, briefs={"MU": ticker_brief("MU", 4.5, "mixed — m.")}, tickers=[
        entry("MU", decide=stage("HOLD", arm="brief", pm_rating="Hold"))])
    book = tmp_path / "trade_notify" / "entries.json"
    book.parent.mkdir(parents=True, exist_ok=True)
    book.write_text(json.dumps({}), encoding="utf-8")
    assert rd.build_digest(cfg, "us", DAY).splitlines()[0] == "📊 研究摘要 · 美股 · 11-04 ｜ 持仓：无 ｜ 计划下单：无"
    book.write_text(json.dumps({"MU": {"avg": 893.92, "qty": 5}}), encoding="utf-8")
    text = rd.build_digest(cfg, "us", DAY)
    assert "持仓：MU" in text.splitlines()[0] and "• MU 4.5 财报 11-17 → 继续持有（PM 中性）" in text


@pytest.mark.unit
def test_a_uniform_macro_read_is_one_phrase_and_a_stale_eval_does_not_count(tmp_path):
    cfg = config(tmp_path)
    six = "".join(f"## {title}\n\n- x\n\n**Impact**: mixed — r.\n\n" for title in rd.MACRO_SECTION_NAMES)
    write_slot(cfg, briefs={}, tickers=[], macro="---\nas_of_date: 2026-11-04\nsession: us\n---\n\n" + six,
               macro_verdict=None)
    (cfg.macro_brief_dir / f"{DAY}.us.eval.json").write_text(
        json.dumps({"brief_sha256": "another revision", "verdict": "pass"}), encoding="utf-8")
    assert rd.macro_read(cfg, "us", DAY)[0] == "宏观（未评估）：6 个板块均为分歧"


@pytest.mark.unit
def test_missing_inputs_still_give_a_digest(tmp_path):
    text = rd.build_digest(config(tmp_path), "us", DAY)
    assert text.splitlines() == ["📊 研究摘要 · 美股 · 11-04 ｜ 计划下单：无", "宏观：无简报", "个股：无简报"]
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
def test_chinese_summaries_follow_their_lines_and_their_absence_costs_nothing(tmp_path, monkeypatch):
    cfg = config(tmp_path)
    write_slot(cfg, briefs={"HUM": ticker_brief("HUM", 8.0, "neutral — n."),
                            "LITE": ticker_brief("LITE", 9.0, "mixed — d.")}, tickers=[
        entry("HUM", decide=stage("HOLD", arm="brief", pm_rating="Underweight")),
        entry("LITE", evaluate="fail", trigger="eval-fail")])
    prompts = []

    def run(prompt):
        prompts.append(prompt)
        return json.dumps({"macro": "利率压力是主要风险。", "tickers": [
            {"ticker": "HUM", "summary": "财报前无新消息。"}, {"ticker": "LITE", "summary": "简报未通过核查。"},
            {"ticker": "ZZZ", "summary": "invented"}]})

    monkeypatch.setattr(rd, "_run_summary", run)
    lines = rd.build_digest(cfg, "us", DAY).splitlines()
    assert lines[2] == "  要点：利率压力是主要风险。"
    assert lines[lines.index("• HUM 8.0 财报 11-17 → 不买（PM 看淡；无持仓，不做空）") + 1] == "  财报前无新消息。"
    assert lines[lines.index("• LITE 9.0 财报 11-17 → 简报评估 fail，未分析") + 1] == "  简报未通过核查。"
    assert "invented" not in "\n".join(lines)
    # The model gets the view in words, never the internal HOLD.
    assert "Portfolio manager's view: 看淡" in prompts[0] and "HOLD" not in prompts[0].split("[macro]")[1]
    assert "failed its accuracy evaluation" in prompts[0]


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
     "不买（PM 中性）"),
    (stage("BUY", arm="feeds", pm_rating="Overweight", plan_valid=True), "看多，不下单（PM 看多；对照分析，不下单）"),
])
def test_the_decision_shown_is_the_trading_arms(tmp_path, decide, shown):
    cfg = config(tmp_path)
    write_slot(cfg, briefs={"NVDA": ticker_brief("NVDA", 7.0, "mixed — m.")},
               tickers=[entry("NVDA", evaluate="warn", decide=decide)])
    line = next(x for x in rd.build_digest(cfg, "us", DAY).splitlines() if x.startswith("• NVDA"))
    assert line.endswith("→ " + shown)


@pytest.mark.unit
def test_an_existing_brief_and_a_decision_without_a_brief_are_listed(tmp_path):
    cfg = config(tmp_path)
    write_slot(cfg, briefs={"MU": ticker_brief("MU", 4.5, "mixed — m.")}, tickers=[
        entry("MU", collect="exists", evaluate="warn", trigger="core-unchanged"),  # a re-fired slot found it
        entry("AAPL", collect="failed",
              decide=stage("HOLD", arm="feeds", pm_rating="Hold")),  # core: analysed on feeds
    ])
    text = rd.build_digest(cfg, "us", DAY)
    assert "• AAPL – （无简报） → 不买（PM 中性）" in text
    assert "• 近几日已分析且无新变化，不重复：MU 4.5" in text


@pytest.mark.unit
def test_macro_sections_follow_the_contracts_heading_rule(tmp_path):
    cfg = config(tmp_path)
    tight = ("---\nas_of_date: 2026-11-04\nsession: us\n---\n## Monetary Policy & Rates\n\n- a\n\n"
             "**Impact**: bearish — r.\n\n##  Growth & Earnings\n\n- b\n\n**Impact**: bullish — g.\n")
    write_slot(cfg, briefs={}, tickers=[], macro=tight, macro_verdict=None)
    assert rd.macro_read(cfg, "us", DAY)[0] == "宏观（未评估）：利率 偏空 · 增长 偏多"


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
    assert "其他：MU 4.5" in rd.build_digest(cfg, "us", DAY)


@pytest.mark.unit
def test_every_digest_log_line_is_in_the_runbook():
    import re

    root = Path(__file__).resolve().parents[1]
    section = root.joinpath("RUNBOOK.md").read_text(encoding="utf-8").split("## 7a.")[1].split("## 7b.")[0]
    section = " ".join(section.split())
    sources = [root / "pipeline" / "research_digest.py", root / "pipeline" / "orchestrator.py"]
    pieces = []
    for source in sources:
        for literal in re.findall(r'logger\.(?:warning|error)\(\s*"(research digest[^"]*)"', source.read_text()):
            # Every literal piece between the format placeholders.
            pieces += [p.strip(" :()") for p in re.split(r"%[-.0-9]*[sdf]", literal) if len(p.strip(" :()")) > 3]
    assert len(pieces) >= 8
    for piece in pieces:
        assert piece in section, piece
    # The one line nothing retries says so.
    failed = section[section.index("`research digest failed`"):][:120]
    assert "slot end only" in failed and "not retried" in failed


@pytest.mark.unit
def test_a_wrong_session_nominee_is_not_listed(tmp_path):
    cfg = config(tmp_path)
    write_slot(cfg, briefs={"0981.HK": ticker_brief("0981.HK", 7.0, "mixed — cn brief."),
                            "MU": ticker_brief("MU", 4.5, "mixed — m.")},
               tickers=[{"ticker": "0981.HK", "stages": {"nominate": stage("wrong-session")}}, entry("MU")])
    text = rd.build_digest(cfg, "us", DAY)
    assert "0981.HK" not in text and "未触发（1）" in text


@pytest.mark.unit
def test_a_damaged_macro_frontmatter_costs_only_the_macro_line(tmp_path):
    cfg = config(tmp_path)
    write_slot(cfg, briefs={"MU": ticker_brief("MU", 4.5, "mixed — m.")}, tickers=[entry("MU")],
               macro="---\nas_of_date: 2026-13-45\nsession: us\n---\n\n## Monetary Policy & Rates\n",
               macro_verdict=None)
    text = rd.build_digest(cfg, "us", DAY)
    assert text.splitlines()[1] == "宏观：简报无法读取"  # PyYAML raises ValueError on 2026-13-45
    assert "其他：MU 4.5" in text
