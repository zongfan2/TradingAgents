"""Research digest — specs/research-digest.md."""

import json
from datetime import date
from pathlib import Path

import pytest

from pipeline import research_digest as rd
from pipeline.common import sha256_text
from pipeline.config import PipelineConfig
from tests.test_execution_adapter import ADDON_PLAN, BUY_PLAN, SELL_PLAN, write_decision

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


def book(cfg, held=()):
    path = cfg.state_dir / "trade_notify" / "entries.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({t: {"avg": 100.0, "qty": 5} for t in held}), encoding="utf-8")


def decided(cfg, ticker, code, rating, *, plan=BUY_PLAN, eligible=True, **row):
    """A ledger row and its decide stage, linked by run_id."""
    write_decision(cfg, ticker=ticker, plan=plan, date_iso=DAY.isoformat(), decision=code, **row)
    return stage(code, arm="brief", pm_rating=rating, plan_valid=True, execute_eligible=eligible,
                 run_id=f"{DAY.isoformat()}-us-{ticker}-brief-1")


@pytest.fixture
def live(monkeypatch):
    monkeypatch.setenv("TRADINGAGENTS_EXECUTION_ENABLED", "true")


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
def test_the_digest_groups_tickers_by_outcome_in_plain_words(tmp_path, live):
    cfg = config(tmp_path)
    book(cfg)
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
        entry("NVDA", evaluate="warn", trigger="core", decide=decided(cfg, "NVDA", "BUY", "Overweight")),
        entry("HUM", evaluate="warn", trigger="catalyst",
              decide=decided(cfg, "HUM", "HOLD", "Underweight", plan=None, eligible=False)),
        entry("META", collect="not-collected-watch"),  # no brief: not listed
    ])
    lines = rd.build_digest(cfg, "us", DAY).splitlines()
    assert lines == [
        "📊 研究摘要 · 美股 · 11-04 ｜ 持仓：无 ｜ 计划下单：NVDA 买入",
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
        "• 机会池分数 <触发线，未评估：FN 4.0",  # no threshold in the snapshot
    ]


@pytest.mark.unit
@pytest.mark.parametrize(("code", "rating", "held", "kw", "shown", "ordered"), [
    ("HOLD", "Hold", False, {}, "不买（PM 中性）", False),
    ("HOLD", "Underweight", False, {}, "不买（PM 看淡；无持仓，不做空）", False),
    ("HOLD", "Hold", True, {}, "继续持有（PM 中性）", False),
    ("HOLD", "Hold", None, {}, "不操作（PM 中性；持仓未知）", False),
    ("BUY", "Overweight", False, {}, "买入（PM 看多；09:45 ET 下单）", True),
    # A held BUY is a view refresh unless the plan asks to add (the adapter maintains).
    ("BUY", "Overweight", True, {}, "继续持有（PM 看多；不加仓）", False),
    ("BUY", "Overweight", True, {"add_intent": True}, "可能加仓（PM 看多；09:45 ET 按加仓规则）", True),
    ("BUY", "Overweight", None, {}, "看多（PM 看多；持仓未知，是否下单待定）", None),
    ("BUY", "Overweight", False, {"blocker": "action-mismatch"}, "看多，不下单（PM 看多；计划与结论不一致）", False),
    ("BUY", "Overweight", False, {"blocker": "unknown"}, "看多，不下单（PM 看多；下单待核实）", False),
    ("BUY", "Overweight", False, {"mode": "模拟"}, "买入（PM 看多；模拟）", False),
    ("BUY", "Overweight", False, {"mode": "执行已暂停"}, "买入（PM 看多；执行已暂停）", False),
    ("SELL", "Sell", True, {}, "卖出（PM 看空；09:45 ET 下单）", True),
    ("SELL", "Sell", False, {}, "不买（PM 看空；无持仓，不做空）", False),
    ("SELL", "Sell", None, {}, "看空（PM 看空；持仓未知，是否下单待定）", None),
    # A blocked SELL is no order whatever the (unknown) position.
    ("SELL", "Sell", None, {"blocker": "ticker-eval-fail"}, "看空，不下单（PM 看空；简报评估 fail）", False),
    ("SELL", "Sell", None, {"blocker": "feeds-arm"}, "看空，不下单（PM 看空；对照分析）", False),
    ("ERROR", None, False, {}, "分析出错", False),
])
def test_actions_are_the_adapters_outcome_in_plain_words(code, rating, held, kw, shown, ordered):
    args = {"blocker": None, "add_intent": False, "mode": None, **kw}
    assert rd.action_text(code, rating, session="us", held=held, execution_time="09:45", **args) == (shown, ordered)


@pytest.mark.unit
def test_an_order_follows_only_what_the_adapter_would_place(tmp_path, live):
    cfg = config(tmp_path)
    book(cfg, held=["MU"])
    mismatch = {**BUY_PLAN, "action": "HOLD"}
    write_slot(cfg, briefs={t: ticker_brief(t, 7.0, "mixed — m.") for t in ("NVDA", "MU", "AMD", "TSLA")}, tickers=[
        entry("NVDA", decide=decided(cfg, "NVDA", "BUY", "Overweight", plan=mismatch)),
        entry("MU", decide=decided(cfg, "MU", "BUY", "Overweight")),  # held, no add_intent
        entry("AMD", decide=decided(cfg, "AMD", "BUY", "Overweight", ticker_v="fail")),
        entry("TSLA", decide=decided(cfg, "TSLA", "BUY", "Overweight", plan=ADDON_PLAN)),
    ])
    book(cfg, held=["MU", "TSLA"])
    text = rd.build_digest(cfg, "us", DAY)
    assert "• NVDA 7.0 财报 11-17 → 看多，不下单（PM 看多；计划与结论不一致）" in text
    assert "• MU 7.0 财报 11-17 → 继续持有（PM 看多；不加仓）" in text
    assert "• AMD 7.0 财报 11-17 → 看多，不下单（PM 看多；简报评估 fail）" in text
    assert "• TSLA 7.0 财报 11-17 → 可能加仓（PM 看多；按加仓规则）" in text
    assert text.splitlines()[0] == "📊 研究摘要 · 美股 · 11-04 ｜ 持仓：MU、TSLA ｜ 计划下单：TSLA 可能加仓"


@pytest.mark.unit
def test_dry_run_and_halt_are_stated(tmp_path, monkeypatch):
    cfg = config(tmp_path)
    book(cfg)
    write_slot(cfg, briefs={"NVDA": ticker_brief("NVDA", 7.0, "mixed — m.")},
               tickers=[entry("NVDA", decide=decided(cfg, "NVDA", "BUY", "Overweight"))])
    monkeypatch.delenv("TRADINGAGENTS_EXECUTION_ENABLED", raising=False)
    assert "计划下单（模拟）：无" in rd.build_digest(cfg, "us", DAY)
    monkeypatch.setenv("TRADINGAGENTS_EXECUTION_ENABLED", "true")
    (cfg.state_dir / "EXECUTION_HALT").write_text("", encoding="utf-8")
    text = rd.build_digest(cfg, "us", DAY)
    assert "计划下单（执行已暂停）：无" in text and "→ 买入（PM 看多；执行已暂停）" in text


@pytest.mark.unit
def test_unknown_positions_are_never_read_as_none(tmp_path, live):
    cfg = config(tmp_path)
    write_slot(cfg, briefs={"MU": ticker_brief("MU", 4.5, "mixed — m.")},
               tickers=[entry("MU", decide=decided(cfg, "MU", "SELL", "Sell", plan=SELL_PLAN))])
    for state in (None, "{not json", b"\xff\xfe"):
        path = cfg.state_dir / "trade_notify" / "entries.json"
        if state is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(state if isinstance(state, bytes) else state.encode())
        text = rd.build_digest(cfg, "us", DAY)
        assert "持仓：" not in text.splitlines()[0], state
        assert "• MU 4.5 财报 11-17 → 看空（PM 看空；持仓未知，是否下单待定）" in text, state
        assert "不买" not in text and "无持仓" not in text
        assert "计划下单：待定：MU（持仓未知）" in text.splitlines()[0]  # never "无" on an unread book


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
def test_positions_come_from_the_trades_book(tmp_path, monkeypatch):
    monkeypatch.delenv("TRADINGAGENTS_EXECUTION_ENABLED", raising=False)
    cfg = config(tmp_path)
    write_slot(cfg, briefs={"MU": ticker_brief("MU", 4.5, "mixed — m.")}, tickers=[
        entry("MU", decide=stage("HOLD", arm="brief", pm_rating="Hold"))])
    book(cfg)
    assert rd.build_digest(cfg, "us", DAY).splitlines()[0] == "📊 研究摘要 · 美股 · 11-04 ｜ 持仓：无 ｜ 计划下单（模拟）：无"
    book(cfg, held=["MU"])
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
    assert text.splitlines()[1:] == ["宏观：无简报", "个股：无简报"]
    assert "持仓：" not in text.splitlines()[0]  # no book: positions unknown, not "无"
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
    book(cfg)
    write_slot(cfg, briefs={"HUM": ticker_brief("HUM", 8.0, "neutral — n."),
                            "LITE": ticker_brief("LITE", 9.0, "mixed — d.")}, tickers=[
        entry("HUM", decide=decided(cfg, "HUM", "HOLD", "Underweight", plan=None, eligible=False)),
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
def test_summaries_are_screened_and_names_without_source_are_not_asked(tmp_path, monkeypatch):
    cfg = config(tmp_path)
    write_slot(cfg, briefs={"HUM": ticker_brief("HUM", 8.0, "neutral — 11-06 print."),
                            "NVDA": ticker_brief("NVDA", 7.0, "mixed — m.")}, tickers=[
        entry("HUM", decide=decided(cfg, "HUM", "HOLD", "Underweight", plan=None)),
        entry("NVDA", decide=decided(cfg, "NVDA", "HOLD", "Hold", plan=None)),
        entry("AAPL", collect="failed", decide=stage("HOLD", arm="feeds", pm_rating="Hold")),  # no brief
        entry("IBM", decide=stage("ERROR"))])
    prompts = []

    def run(prompt):
        prompts.append(prompt)
        return json.dumps({"macro": "建议持有观望。", "tickers": [
            {"ticker": "HUM", "summary": "财报在 11-06，股价目标 520 美元。"},  # 520 is in no source
            {"ticker": "NVDA", "summary": "维持HOLD。"}]})

    monkeypatch.setattr(rd, "_run_summary", run)
    text = rd.build_digest(cfg, "us", DAY)
    assert "[AAPL]" not in prompts[0] and "[IBM]" not in prompts[0]
    assert "520" not in text and "HOLD" not in text and "持有观望" not in text
    assert "data quoted from" in prompts[0]


@pytest.mark.unit
def test_a_rerun_after_the_digest_went_out_makes_no_model_call(tmp_path, monkeypatch):
    from pipeline import trade_monitor, trade_notify

    cfg = config(tmp_path, lark_trades_webhook_url=HOOK)
    write_slot(cfg, briefs={"HUM": ticker_brief("HUM", 8.0, "n — n.")},
               tickers=[entry("HUM", decide=stage("HOLD", arm="brief", pm_rating="Hold"))])
    calls = []
    monkeypatch.setattr(rd, "_run_summary", lambda prompt: calls.append(1) or json.dumps({"macro": "", "tickers": []}))
    monkeypatch.setattr(trade_monitor, "make_trades_sender", lambda c: lambda title, text: None)
    assert rd.send_digest(cfg, "us", DAY) == 1 and len(calls) == 1
    assert rd.send_digest(cfg, "us", DAY) == 0 and len(calls) == 1  # handled: not rebuilt
    assert trade_notify.SentStore(tmp_path).outbox() == []


@pytest.mark.unit
def test_the_below_threshold_label_uses_the_threshold_in_force(tmp_path):
    cfg = config(tmp_path)
    write_slot(cfg, briefs={"FN": ticker_brief("FN", 4.0, "m — m.")},
               tickers=[entry("FN", trigger="below-threshold")])
    path = cfg.state_dir / "funnel" / "us" / f"{DAY}.json"
    snap = json.loads(path.read_text())
    snap["context"] = {"thresholds": {"trigger": 6.5}}
    path.write_text(json.dumps(snap), encoding="utf-8")
    assert "• 机会池分数 <6.5，未评估：FN 4.0" in rd.build_digest(cfg, "us", DAY)


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
    (stage("BUY", arm="feeds", pm_rating="Overweight", plan_valid=True), "看多，不下单（PM 看多；对照分析）"),
])
def test_the_decision_shown_is_the_trading_arms(tmp_path, decide, shown):
    cfg = config(tmp_path)
    book(cfg)
    write_slot(cfg, briefs={"NVDA": ticker_brief("NVDA", 7.0, "mixed — m.")},
               tickers=[entry("NVDA", evaluate="warn", decide=decide)])
    line = next(x for x in rd.build_digest(cfg, "us", DAY).splitlines() if x.startswith("• NVDA"))
    assert line.endswith("→ " + shown)


@pytest.mark.unit
def test_an_existing_brief_and_a_decision_without_a_brief_are_listed(tmp_path):
    cfg = config(tmp_path)
    book(cfg)
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


@pytest.mark.unit
def test_a_brief_the_pm_never_read_is_never_summarized_as_its_reason(tmp_path, monkeypatch):
    cfg = config(tmp_path)
    book(cfg)
    write_slot(cfg, briefs={t: ticker_brief(t, 7.0, f"mixed — {t} text.") for t in ("AAA", "BBB", "CCC")}, tickers=[
        entry("AAA", decide=decided(cfg, "AAA", "HOLD", "Hold", plan=None, ticker_v="fail")),  # withheld
        entry("BBB", decide=decided(cfg, "BBB", "HOLD", "Hold", plan=None, macro="fail")),  # feeds-only day
        entry("CCC", decide=decided(cfg, "CCC", "HOLD", "Hold", plan=None))])
    prompts = []
    monkeypatch.setattr(rd, "_run_summary", lambda p: prompts.append(p) or json.dumps({"macro": "", "tickers": []}))
    text = rd.build_digest(cfg, "us", DAY)
    assert "[AAA]" not in prompts[0] and "[BBB]" not in prompts[0] and "[CCC]" in prompts[0]
    assert "• AAA 7.0 财报 11-17 → 不买（PM 中性）；简报未采用（评估 fail）" in text


@pytest.mark.unit
@pytest.mark.parametrize(("summary", "source", "kept"), [
    ("股价目标 20 美元。", "catalyst on 2026-11-17 [r](https://x/120)", False),  # 20 only inside 2026 / a URL
    ("财报在11月17日。", "catalyst on 2026-11-17", True),
    ("股价目标 520.", "catalyst on 2026-11-17", False),  # a sentence-ending period is not part of it
    ("建议买入。", "x", False), ("等待下单时机。", "x", False), ("不宜做空。", "x", False),
])
def test_the_summary_screen(summary, source, kept):
    assert (rd._screened(summary, source) is not None) is kept


@pytest.mark.unit
def test_a_summary_call_that_used_web_tools_is_discarded(monkeypatch):
    import subprocess

    from pipeline import claude_cli

    envelope = {"structured_output": {"macro": "x", "tickers": []},
                "_tool_calls": {"web_search_queries": ["q"], "web_fetch_urls": [], "web_fetch_requests": 0}}
    monkeypatch.setattr(claude_cli, "claude_command", lambda *a, **k: ["claude"])
    monkeypatch.setattr(claude_cli, "call_env", lambda command: None)
    monkeypatch.setattr(claude_cli, "parse_envelope", lambda stdout: envelope)
    monkeypatch.setattr(claude_cli, "record_usage", lambda *a, **k: None)
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout="{}", stderr=""))
    with pytest.raises(RuntimeError, match="web tools"):
        rd.REAL_RUN_SUMMARY("prompt")  # the real call, captured before the offline stub


@pytest.mark.unit
def test_a_digest_built_after_execution_tells_each_names_own_outcome(tmp_path, live):
    cfg = config(tmp_path)
    book(cfg, held=["NVDA"])  # today's fill is already in the book
    write_slot(cfg, briefs={t: ticker_brief(t, 7.0, "m — m.") for t in ("NVDA", "AMD", "MU", "INTC", "TSLA")},
               tickers=[
        {**entry("NVDA", decide=decided(cfg, "NVDA", "BUY", "Overweight")), "final": "order-live",
         "stages": {**entry("NVDA", decide=decided(cfg, "NVDA", "BUY", "Overweight", n=2))["stages"],
                    "execute": stage("submitted")}},
        {**entry("AMD", decide=decided(cfg, "AMD", "BUY", "Overweight")), "final": "order-dry"},
        {**entry("MU", decide={**stage("BUY", arm="feeds", pm_rating="Overweight")}), "final": "excluded"},
        {"ticker": "INTC", "final": "excluded", "stages": {
            "collect": stage("written"), "decide": decided(cfg, "INTC", "BUY", "Overweight", ticker_v="fail"),
            "execute": stage("ticker-eval-fail")}},
        {**entry("TSLA", decide=decided(cfg, "TSLA", "HOLD", "Hold", plan=None)), "final": "excluded"},
    ])
    path = cfg.state_dir / "funnel" / "us" / f"{DAY}.json"
    snap = json.loads(path.read_text())
    snap["context"] = {"execute_ran": True}
    path.write_text(json.dumps(snap), encoding="utf-8")
    text = rd.build_digest(cfg, "us", DAY)
    assert "• NVDA 7.0 财报 11-17 → 看多（PM 看多；已下单，见交易消息）" in text
    assert "• AMD 7.0 财报 11-17 → 看多（PM 看多；模拟单）" in text
    assert "• MU 7.0 财报 11-17 → 看多（PM 看多；未下单）" in text
    assert "• INTC 7.0 财报 11-17 → 看多（PM 看多；未下单：简报评估 fail）" in text
    assert "• TSLA 7.0 财报 11-17 → 不操作（PM 中性）" in text
    assert "继续持有" not in text and "已执行：NVDA 已下单" in text.splitlines()[0]


@pytest.mark.unit
def test_triggered_names_without_a_decision_say_so(tmp_path):
    cfg = config(tmp_path)
    write_slot(cfg, briefs={"HUM": ticker_brief("HUM", 8.0, "m — m."), "LITE": ticker_brief("LITE", 7.5, "m — m.")},
               tickers=[{"ticker": "HUM", "stages": {"collect": stage("written"),
                                                     "trigger": {"code": "catalyst", "outcome": "passed"}}},
                        {"ticker": "LITE", "stages": {"collect": stage("written"),
                                                      "trigger": {"code": "budget-dropped", "outcome": "passed"}}}])
    text = rd.build_digest(cfg, "us", DAY)
    assert "• 已触发，因预算上限未分析：LITE 7.5" in text
    assert "• 已触发但无分析结果（运行失败或被中止）：HUM 8.0" in text and "其他" not in text


@pytest.mark.unit
def test_a_billed_search_without_query_text_also_discards(monkeypatch):
    import subprocess

    from pipeline import claude_cli

    envelope = {"structured_output": {"macro": "x", "tickers": []},
                "modelUsage": {"m": {"webSearchRequests": 1}}, "_tool_calls": {}}
    monkeypatch.setattr(claude_cli, "claude_command", lambda *a, **k: ["claude"])
    monkeypatch.setattr(claude_cli, "call_env", lambda command: None)
    monkeypatch.setattr(claude_cli, "parse_envelope", lambda stdout: envelope)
    monkeypatch.setattr(claude_cli, "record_usage", lambda *a, **k: None)
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout="{}", stderr=""))
    with pytest.raises(RuntimeError, match="web tools"):
        rd.REAL_RUN_SUMMARY("prompt")


@pytest.mark.unit
def test_after_the_execution_window_no_timed_order_is_promised(tmp_path, live):
    from datetime import datetime, timezone

    from pipeline.config import SessionSchedule

    cfg = PipelineConfig(state_dir=tmp_path, sessions={
        "cn": SessionSchedule(), "us": SessionSchedule(execution_time="09:45")})
    book(cfg)
    write_slot(cfg, briefs={"NVDA": ticker_brief("NVDA", 7.0, "m — m.")},
               tickers=[entry("NVDA", decide=decided(cfg, "NVDA", "BUY", "Overweight"))])
    before = rd.build_digest(cfg, "us", DAY, now=datetime(2026, 11, 4, 12, 0, tzinfo=timezone.utc))  # 07:00 ET
    assert "计划下单：NVDA 买入" in before and "09:45 ET 下单" in before
    after = rd.build_digest(cfg, "us", DAY, now=datetime(2026, 11, 4, 15, 30, tzinfo=timezone.utc))  # 10:30 ET
    assert "计划下单（执行时间已过，未执行）：无" in after and "09:45 ET 下单" not in after


@pytest.mark.unit
def test_a_later_manual_attempt_shadows_the_scheduled_decision(tmp_path, live):
    cfg = config(tmp_path)
    book(cfg)
    decide = decided(cfg, "NVDA", "BUY", "Overweight")
    write_decision(cfg, ticker="NVDA", n=2, trigger="manual", plan=BUY_PLAN, date_iso=DAY.isoformat())
    write_slot(cfg, briefs={"NVDA": ticker_brief("NVDA", 7.0, "m — m.")}, tickers=[entry("NVDA", decide=decide)])
    text = rd.build_digest(cfg, "us", DAY)
    assert "• NVDA 7.0 财报 11-17 → 看多，不下单（PM 看多；被手动重跑取代）" in text and "计划下单：无" in text


@pytest.mark.unit
@pytest.mark.parametrize("code", ["stale-quote", "no-position", "maintain", "total-risk", "gross-exposure",
                                  "max-positions", "max-orders-per-slot", "buying-power", "per-ticker-notional-cap",
                                  "guard-unavailable", "market-closed", "not-tradable", "zero-qty", "max-tranches",
                                  "pyramid-up-only", "add-spacing", "cancel-failed", "recovery-unavailable",
                                  "submit-error", "adapter-error", "rejected", "manual-shadowed", "dedupe",
                                  "some-new-code"])
def test_execute_stage_reasons_read_in_chinese(code):
    import re

    text = rd.executed_text("BUY", "Overweight", final="excluded", execute={"code": code})
    assert text.startswith("看多（PM 看多；未下单") and not re.search(r"[a-z]+-[a-z]+", text), text
