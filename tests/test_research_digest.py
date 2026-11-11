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
    """The production shape: us step 7 deferred to 09:45 ET."""
    from pipeline.config import SessionSchedule

    overrides.setdefault("sessions", {"cn": SessionSchedule(), "us": SessionSchedule(execution_time="09:45")})
    return PipelineConfig(state_dir=tmp_path, **overrides)


#: 07:00 ET on the slot day: after the slot, before the 09:45 execution.
SLOT_END = __import__("datetime").datetime(2026, 11, 4, 12, 0, tzinfo=__import__("datetime").timezone.utc)


def build(cfg, session="us", day=None, **kw):
    kw.setdefault("now", SLOT_END)
    return rd.build_digest(cfg, session, day or DAY, **kw)


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
    lines = build(cfg, "us").splitlines()
    assert lines == [
        "📊 研究摘要 · 美股 · 11-04 ｜ 持仓：无 ｜ 计划下单：NVDA 买入",
        "宏观（评估 warn）：利率 偏空 · 增长 分歧",
        "",
        "已分析（2）",
        "• HUM 8.0 财报 11-06 → 不买（PM 看淡；无持仓，不做空）",
        "• NVDA 7.0 财报 11-17 → 买入（PM 看多；09:45 ET 下单）",
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
    ("BUY", "Overweight", False, {"blocker": "unknown"}, "看多（PM 看多；下单待核实）", None),
    ("BUY", "Overweight", None, {"mode": "模拟"}, "看多（PM 看多；模拟）", False),
    ("SELL", "Sell", None, {"mode": "执行已暂停"}, "看空（PM 看空；执行已暂停）", False),
    ("BUY", "Overweight", False, {"mode": rd.LATE}, "看多（PM 看多；已过执行时间，下单待核实）", None),
    ("BUY", "Overweight", False, {"mode": rd.UNKNOWN_STATE}, "看多（PM 看多；执行状态未知）", None),
    ("SELL", "Sell", True, {"mode": rd.UNKNOWN_STATE}, "看空（PM 看空；执行状态未知）", None),
    ("BUY", "Overweight", False, {"mode": "模拟"}, "买入（PM 看多；模拟）", False),
    ("BUY", "Overweight", False, {"mode": "执行已暂停"}, "买入（PM 看多；执行已暂停）", False),
    ("SELL", "Sell", True, {}, "卖出（PM 看空；09:45 ET 下单）", True),
    ("SELL", "Sell", False, {}, "不买（PM 看空；无持仓，不做空）", False),
    ("SELL", "Sell", None, {}, "看空（PM 看空；持仓未知，是否下单待定）", None),
    # A blocked SELL is no order whatever the (unknown) position.
    ("SELL", "Sell", None, {"blocker": "ticker-eval-fail"}, "看空，不下单（PM 看空；简报评估 fail）", False),
    ("SELL", "Sell", None, {"blocker": "feeds-arm"}, "看空，不下单（PM 看空；对照分析）", False),
    ("SELL", "Sell", False, {"blocker": "feeds-arm"}, "看空，不下单（PM 看空；对照分析）", False),
    ("SELL", "Sell", False, {"blocker": "ticker-eval-fail"}, "看空，不下单（PM 看空；简报评估 fail）", False),
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
    text = build(cfg, "us")
    assert "• NVDA 7.0 财报 11-17 → 看多，不下单（PM 看多；计划与结论不一致）" in text
    assert "• MU 7.0 财报 11-17 → 继续持有（PM 看多；不加仓）" in text
    assert "• AMD 7.0 财报 11-17 → 看多，不下单（PM 看多；简报评估 fail）" in text
    assert "• TSLA 7.0 财报 11-17 → 可能加仓（PM 看多；09:45 ET 按加仓规则）" in text
    assert text.splitlines()[0] == "📊 研究摘要 · 美股 · 11-04 ｜ 持仓：MU、TSLA ｜ 计划下单：TSLA 可能加仓"


@pytest.mark.unit
def test_dry_run_and_halt_are_stated(tmp_path, monkeypatch):
    cfg = config(tmp_path)
    book(cfg)
    write_slot(cfg, briefs={"NVDA": ticker_brief("NVDA", 7.0, "mixed — m.")},
               tickers=[entry("NVDA", decide=decided(cfg, "NVDA", "BUY", "Overweight"))])
    monkeypatch.delenv("TRADINGAGENTS_EXECUTION_ENABLED", raising=False)
    assert "计划下单（模拟）：无" in build(cfg, "us")
    monkeypatch.setenv("TRADINGAGENTS_EXECUTION_ENABLED", "true")
    (cfg.state_dir / "EXECUTION_HALT").write_text("", encoding="utf-8")
    text = build(cfg, "us")
    assert "计划下单（执行已暂停）：无" in text and "→ 买入（PM 看多；执行已暂停）" in text


@pytest.mark.unit
def test_unknown_positions_are_never_read_as_none(tmp_path, live):
    cfg = config(tmp_path)
    write_slot(cfg, briefs={"MU": ticker_brief("MU", 4.5, "mixed — m.")},
               tickers=[entry("MU", decide=decided(cfg, "MU", "SELL", "Sell", plan=SELL_PLAN))])
    for state in (None, "{not json", b"\xff\xfe", '{"MU": {"avg": "x", "qty": 5}}'):
        path = cfg.state_dir / "trade_notify" / "entries.json"
        if state is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(state if isinstance(state, bytes) else state.encode())
        text = build(cfg, "us")
        assert "持仓：" not in text.splitlines()[0], state
        assert "• MU 4.5 财报 11-17 → 看空（PM 看空；持仓未知，是否下单待定）" in text, state
        assert "不买" not in text and "无持仓" not in text
        assert "计划下单：待核实：MU" in text.splitlines()[0]  # never "无" on an unread book


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
    text = build(cfg, "cn")
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
        entry("MU", decide=decided(cfg, "MU", "HOLD", "Hold", plan=None))])
    book(cfg)
    assert build(cfg, "us").splitlines()[0] == "📊 研究摘要 · 美股 · 11-04 ｜ 持仓：无 ｜ 计划下单（模拟）：无"
    book(cfg, held=["MU"])
    text = build(cfg, "us")
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
    text = build(config(tmp_path), "us")
    assert text.splitlines()[1:] == ["宏观：无简报", "个股：无简报"]
    assert "持仓：" not in text.splitlines()[0]  # no book: positions unknown, not "无"
    cfg = config(tmp_path)
    (cfg.state_dir / "funnel" / "us").mkdir(parents=True)
    for corrupt in ("{not json", "[]", "3", '{"tickers": {"MU": 1}}', '{"tickers": 3}',
                    '{"tickers": [{"ticker": "MU", "stages": []}]}',
                    '{"tickers": [{"ticker": "MU", "stages": {"decide": 1}}]}',
                    '{"tickers": [{"ticker": "MU", "stages": {"decide": {"code": "BUY", "details": 1}}}]}',
                    '{"tickers": [{"ticker": "MU", "stages": {"trigger": {"code": ["x"]}}}]}',
                    '{"tickers": [{"ticker": "MU", "stages": {"trigger": "x"}}]}'):
        (cfg.state_dir / "funnel" / "us" / f"{DAY}.json").write_text(corrupt, encoding="utf-8")
        text = build(cfg, "us")
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
    lines = build(cfg, "us").splitlines()
    assert lines[2] == "  要点：利率压力是主要风险。"
    assert lines[lines.index("• HUM 8.0 财报 11-17 → 不买（PM 看淡；无持仓，不做空）") + 1] == "  财报前无新消息。"
    assert lines[lines.index("• LITE 9.0 财报 11-17 → 简报评估 fail，未分析") + 1] == "  简报未通过核查。"
    assert "invented" not in "\n".join(lines)
    # The model gets the view in words, never the internal HOLD.
    assert "Portfolio manager's view: 看淡" in prompts[0] and "HOLD" not in prompts[0].split("[macro]")[1]
    assert "failed its accuracy evaluation" in prompts[0]
    assert "Write every number in Arabic digits (0-9)" in prompts[0]


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
    text = build(cfg, "us")
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
    assert "• 机会池分数 <6.5，未评估：FN 4.0" in build(cfg, "us")


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
    line = next(x for x in build(cfg, "us").splitlines() if x.startswith("• NVDA"))
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
    text = build(cfg, "us")
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
    assert "其他：MU 4.5" in build(cfg, "us")


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
    text = build(cfg, "us")
    assert "0981.HK" not in text and "未触发（1）" in text


@pytest.mark.unit
def test_a_damaged_macro_frontmatter_costs_only_the_macro_line(tmp_path):
    cfg = config(tmp_path)
    write_slot(cfg, briefs={"MU": ticker_brief("MU", 4.5, "mixed — m.")}, tickers=[entry("MU")],
               macro="---\nas_of_date: 2026-13-45\nsession: us\n---\n\n## Monetary Policy & Rates\n",
               macro_verdict=None)
    text = build(cfg, "us")
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
    text = build(cfg, "us")
    assert "[AAA]" not in prompts[0] and "[BBB]" not in prompts[0] and "[CCC]" in prompts[0]
    assert "• AAA 7.0 财报 11-17 → 不买（PM 中性）；简报未采用（评估 fail）" in text


@pytest.mark.unit
@pytest.mark.parametrize(("summary", "source", "kept"), [
    ("股价目标 20 美元。", "catalyst on 2026-11-17 [r](https://x/120)", False),  # 20 only inside 2026 / a URL
    ("财报在11月17日。", "catalyst on 2026-11-17", True),
    ("股价目标 520.", "catalyst on 2026-11-17", False),  # a sentence-ending period is not part of it
    ("建议买入。", "x", False), ("等待下单时机。", "x", False), ("不宜做空。", "x", False),
    ("建议抛售。", "x", False), ("可以增仓。", "x", False), ("应当平仓。", "x", False), ("宜买进。", "x", False),
    ("目标价五百亿美元。", "x", False), ("详见 https://example.com 。", "x", False),
    ("涨幅达百分之五十。", "x", False), ("预计上涨五成。", "x", False), ("详见 example.com/x。", "x", False),
    ("HTTPS://EVIL.COM", "x", False), ("详见 EXAMPLE.COM。", "x", False), ("详见 evil.ai。", "x", False),
    ("详见 bit.ly/x。", "x", False), ("详见example.com查看。", "x", False), ("下跌十五个百分点。", "x", False),
    ("规模五十多亿。", "x", False), ("三十美分。", "x", False),
    ("0700.HK 财报前无新消息。", "0700.HK", True), ("600256.SS 走势平稳。", "600256.SS", True),
    ("详见 evil.hk/x。", "x", False), ("三分之一的收入。", "x", False), ("五億美元。", "x", False),
    ("详见 0700.hk/login。", "0700.HK", False), ("目标价九港元。", "x", False), ("九块钱。", "x", False),
    ("五欧元。", "x", False), ("详见 1.2.3.4/login。", "x", False), ("目标 520.5.1。", "x", False),
    ("详见 例子.com。", "x", False), ("详见 0700.hk.de。", "0700.HK", False), ("详见 evil.0700.hk。", "0700.HK", False),
    ("详见 evil.biz.", "x", False), ("详见 evil.dev.", "x", False), ("0700.HK 财报前无新消息.", "0700.HK", True),
    ("目标价九澳元。", "x", False), ("五美金。", "x", False), ("五 美元。", "x", False), ("规模兩億。", "x", False),
    ("伍佰亿。", "x", False), ("目标价五韩元。", "x", False), ("五卢布。", "x", False), ("五比特币。", "x", False),
    ("加息五个基点。", "x", False), ("一季度持平，进一步观察。", "x", True), ("市场预期一致。", "x", True),
    ("两日大跌原因未能核实。", "x", True),
    ("11月6日财报是关键。", "the Nov 6 print", True), ("8月指引。", "the August guide", True),
    ("6日财报。", "on 2026-11-06", True), ("12月财报。", "the Nov 6 print", False),
    ("下跌12%。", "the market may decline", False), ("增长3倍。", "strong margin", False),
    ("下跌11%。", "the Nov 6 print", False), ("5月财报。", "it may decline", False), ("9月指引。", "the Sept print", True),
    ("建議買入。", "x", False), ("應當賣出。", "x", False), ("可以加倉。", "x", False), ("等待下單。", "x", False),
    ("評級上調。", "x", False), ("建議減持。", "x", False),
    ("目标价 200 美元。", "target $1,200", False), ("目标价 1,520 美元。", "item 1 of 520", False),
    ("目标价 1,200 美元。", "target $1,200", True), ("目标价 1200 美元。", "target $1,200", True),
    ("涨幅超预期,520 美元。", "x", False), ("增5%,30%的涨幅。", "5", False), ("跌.5%。", "x", False),
    ("目标 1,20 美元。", "1", False), ("市盈率 3.14 倍。", "3.14", True),
    ('<at user_id="all">所有人</at>注意。', "x", False), ("统一口径，一季度持平。", "x", True),
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
    text = build(cfg, "us")
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
    text = build(cfg, "us")
    assert "已触发未分析（2）" in text and "未触发" not in text and "其他" not in text
    assert "• 因预算上限未分析：LITE 7.5" in text and "• 无分析结果（运行失败或被中止）：HUM 8.0" in text


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
    from datetime import datetime, timedelta, timezone

    from pipeline.config import SessionSchedule

    cfg = PipelineConfig(state_dir=tmp_path, sessions={
        "cn": SessionSchedule(), "us": SessionSchedule(execution_time="09:45")})
    book(cfg)
    write_slot(cfg, briefs={"NVDA": ticker_brief("NVDA", 7.0, "m — m.")},
               tickers=[entry("NVDA", decide=decided(cfg, "NVDA", "BUY", "Overweight"))])
    before = rd.build_digest(cfg, "us", DAY, now=datetime(2026, 11, 4, 12, 0, tzinfo=timezone.utc))  # 07:00 ET
    assert "计划下单：NVDA 买入" in before and "09:45 ET 下单" in before
    for minute in (50, 90):  # 09:50 ET (inside the old tolerance band) and 10:30 ET
        now = datetime(2026, 11, 4, 14, 45, tzinfo=timezone.utc) + timedelta(minutes=minute - 45)
        after = rd.build_digest(cfg, "us", DAY, now=now)
        assert "计划下单（已过执行时间，下单待核实）：待核实：NVDA" in after, minute
        assert "09:45 ET 下单" not in after and "→ 看多（PM 看多；已过执行时间，下单待核实）" in after


@pytest.mark.unit
def test_a_later_manual_attempt_shadows_the_scheduled_decision(tmp_path, live):
    cfg = config(tmp_path)
    book(cfg)
    decide = decided(cfg, "NVDA", "BUY", "Overweight")
    write_decision(cfg, ticker="NVDA", n=2, trigger="manual", plan=BUY_PLAN, date_iso=DAY.isoformat())
    write_slot(cfg, briefs={"NVDA": ticker_brief("NVDA", 7.0, "m — m.")}, tickers=[entry("NVDA", decide=decide)])
    text = build(cfg, "us")
    assert "• NVDA 7.0 财报 11-17 → 看多，不下单（PM 看多；被手动重跑取代）" in text and "计划下单：无" in text


@pytest.mark.unit
@pytest.mark.parametrize("code", ["stale-quote", "no-position", "maintain", "total-risk", "gross-exposure",
                                  "max-positions", "max-orders-per-slot", "buying-power", "per-ticker-notional-cap",
                                  "guard-unavailable", "market-closed", "not-tradable", "zero-qty", "max-tranches",
                                  "pyramid-up-only", "add-spacing", "cancel-failed", "recovery-unavailable",
                                  "submit-error", "adapter-error", "rejected", "manual-shadowed", "dedupe",
                                  "below-entry-zone", "some-new-code"])
def test_execute_stage_reasons_read_in_chinese(code):
    import re

    text = rd.executed_text("BUY", "Overweight", final="excluded", execute={"code": code})
    assert text.startswith("看多（PM 看多；未下单") and not re.search(r"[a-z]+-[a-z]+", text), text


@pytest.mark.unit
def test_unread_decision_inputs_never_read_as_no_order(tmp_path, live, monkeypatch):
    cfg = config(tmp_path)
    book(cfg)
    decided(cfg, "NVDA", "BUY", "Overweight")
    write_slot(cfg, briefs={}, tickers=[])
    (cfg.state_dir / "funnel" / "us" / f"{DAY}.json").write_text("{corrupt", encoding="utf-8")
    head = build(cfg, "us").splitlines()[0]
    assert "计划下单：待核实（决策数据未读到）" in head and "计划下单：无" not in head
    write_slot(cfg, briefs={"NVDA": ticker_brief("NVDA", 7.0, "m — m.")},
               tickers=[entry("NVDA", decide=stage("BUY", arm="brief", pm_rating="Overweight",
                                                   run_id=f"{DAY.isoformat()}-us-NVDA-brief-1"))])
    monkeypatch.setattr(rd, "_records", lambda c: (_ for _ in ()).throw(OSError("ledger")))
    text = build(cfg, "us")
    assert "• NVDA 7.0 财报 11-17 → 看多（PM 看多；下单待核实）" in text
    assert "待核实（决策数据未读到）" in text.splitlines()[0] and "计划下单：无" not in text.splitlines()[0]


@pytest.mark.unit
def test_an_unknown_execution_outcome_is_never_read_as_no_order(tmp_path, live):
    cfg = config(tmp_path)
    book(cfg)
    write_slot(cfg, briefs={"NVDA": ticker_brief("NVDA", 7.0, "m — m.")},
               tickers=[{**entry("NVDA", decide=decided(cfg, "NVDA", "BUY", "Overweight")), "final": "unknown"}])
    path = cfg.state_dir / "funnel" / "us" / f"{DAY}.json"
    snap = json.loads(path.read_text())
    snap["context"] = {"execute_ran": True}
    path.write_text(json.dumps(snap), encoding="utf-8")
    text = build(cfg, "us")
    assert "→ 看多（PM 看多；下单结果待核实）" in text and "已执行：待核实：NVDA" in text.splitlines()[0]
    assert "未下单" not in text and "无下单" not in text


@pytest.mark.unit
def test_an_unrecorded_trigger_has_its_own_label(tmp_path):
    cfg = config(tmp_path)
    write_slot(cfg, briefs={"HUM": ticker_brief("HUM", 8.0, "m — m.")}, tickers=[
        {"ticker": "HUM", "stages": {"collect": stage("written"),
                                     "trigger": {"code": None, "outcome": "excluded",
                                                 "reason": "met the trigger rules but has no decision row"}}}])
    text = build(cfg, "us")
    assert "已触发未分析（1）" in text and "• 无分析结果（原因未记录）：HUM 8.0" in text


@pytest.mark.unit
def test_inline_execution_that_left_no_outcome_promises_nothing(tmp_path, live):
    from pipeline.config import SessionSchedule

    cfg = config(tmp_path, sessions={"cn": SessionSchedule(), "us": SessionSchedule()})  # step 7 inline
    book(cfg)
    write_slot(cfg, briefs={"NVDA": ticker_brief("NVDA", 7.0, "m — m.")},
               tickers=[entry("NVDA", decide=decided(cfg, "NVDA", "BUY", "Overweight"))])
    text = build(cfg, "us")
    assert "→ 看多（PM 看多；下单结果待核实）" in text and "计划下单（下单结果待核实）：待核实：NVDA" in text
    assert "→ 买入" not in text


@pytest.mark.unit
def test_a_skipped_row_or_a_missing_ledger_is_never_no_order(tmp_path, live):
    cfg = config(tmp_path)
    book(cfg)
    write_slot(cfg, briefs={"NVDA": ticker_brief("NVDA", 7.0, "m — m.")},
               tickers=[{"ticker": "BAD", "stages": []}, entry("NVDA", decide=stage("HOLD", arm="brief",
                                                                                   pm_rating="Hold"))])
    head = build(cfg, "us").splitlines()[0]
    assert "待核实（决策数据未读到）" in head and "计划下单：无" not in head  # a dropped row and no ledger


@pytest.mark.unit
def test_an_unknown_execution_state_lists_orders_as_undetermined(tmp_path, live, monkeypatch):
    cfg = config(tmp_path)
    book(cfg)
    write_slot(cfg, briefs={"NVDA": ticker_brief("NVDA", 7.0, "m — m.")},
               tickers=[entry("NVDA", decide=decided(cfg, "NVDA", "BUY", "Overweight"))])
    monkeypatch.setattr(rd, "_execution_mode", lambda c: (_ for _ in ()).throw(OSError("halt file")))
    head = build(cfg, "us").splitlines()[0]
    assert "待核实：NVDA" in head and not head.endswith("：无")


@pytest.mark.unit
def test_after_execution_an_unread_input_keeps_the_executed_header(tmp_path, live):
    cfg = config(tmp_path)
    book(cfg)
    write_slot(cfg, briefs={"NVDA": ticker_brief("NVDA", 7.0, "m — m.")}, tickers=[
        {**entry("NVDA", decide=decided(cfg, "NVDA", "BUY", "Overweight")), "final": "order-live"},
        {"ticker": "BAD", "stages": "not-a-dict"}])  # a malformed row: skipped, so unread
    path = cfg.state_dir / "funnel" / "us" / f"{DAY}.json"
    snap = json.loads(path.read_text())
    snap["context"] = {"execute_ran": True}
    path.write_text(json.dumps(snap), encoding="utf-8")
    head = build(cfg, "us").splitlines()[0]
    assert "已执行：NVDA 已下单；待核实（决策数据未读到）" in head and "计划下单" not in head


@pytest.mark.unit
def test_after_execution_an_unread_row_never_reads_as_no_order(tmp_path, live):
    cfg = config(tmp_path)
    book(cfg)
    write_slot(cfg, briefs={"TSLA": ticker_brief("TSLA", 7.0, "m — m.")}, tickers=[
        {**entry("TSLA", decide=decided(cfg, "TSLA", "HOLD", "Hold", plan=None)), "final": "excluded"},
        {"ticker": "BAD", "stages": "not-a-dict"}])
    path = cfg.state_dir / "funnel" / "us" / f"{DAY}.json"
    snap = json.loads(path.read_text())
    snap["context"] = {"execute_ran": True}
    path.write_text(json.dumps(snap), encoding="utf-8")
    head = build(cfg, "us").splitlines()[0]
    assert "已执行：待核实（决策数据未读到）" in head and "无下单" not in head


@pytest.mark.unit
@pytest.mark.parametrize(("halted", "header"), [(False, "计划下单（模拟）：无"), (True, "计划下单（执行已暂停）：无")])
def test_a_lost_brief_arm_event_under_dry_run_or_halt_is_no_live_order(tmp_path, monkeypatch, halted, header):
    if halted:
        monkeypatch.setenv("TRADINGAGENTS_EXECUTION_ENABLED", "true")
    else:
        monkeypatch.delenv("TRADINGAGENTS_EXECUTION_ENABLED", raising=False)
    cfg = config(tmp_path)
    book(cfg)
    if halted:
        (cfg.state_dir / "EXECUTION_HALT").parent.mkdir(parents=True, exist_ok=True)
        (cfg.state_dir / "EXECUTION_HALT").write_text("", encoding="utf-8")
    decided(cfg, "NVDA", "BUY", "Overweight")
    feeds = stage("HOLD", arm="feeds", pm_rating="Hold", run_id=f"{DAY.isoformat()}-us-NVDA-feeds-1")
    write_slot(cfg, briefs={"NVDA": ticker_brief("NVDA", 7.0, "m — m.")}, tickers=[entry("NVDA", decide=feeds)])
    assert header in build(cfg, "us").splitlines()[0]


@pytest.mark.unit
@pytest.mark.parametrize(("triggers", "shown"), [(("manual", "core"), "下单待核实（有更新的分析记录）"),
                                                 (("core", "manual"), "被手动重跑取代")])
def test_the_greatest_later_attempt_decides(tmp_path, live, triggers, shown):
    cfg = config(tmp_path)
    book(cfg)
    decide = decided(cfg, "NVDA", "BUY", "Overweight")
    for n, trigger in enumerate(triggers, start=2):
        write_decision(cfg, ticker="NVDA", n=n, trigger=trigger, plan=BUY_PLAN, date_iso=DAY.isoformat())
    write_slot(cfg, briefs={"NVDA": ticker_brief("NVDA", 7.0, "m — m.")}, tickers=[entry("NVDA", decide=decide)])
    assert shown in build(cfg, "us")


@pytest.mark.unit
@pytest.mark.parametrize("feeds_code", ["BUY", "HOLD", "ERROR"])
def test_a_lost_brief_arm_event_never_reads_as_no_order(tmp_path, live, feeds_code):
    cfg = config(tmp_path)
    book(cfg)
    decided(cfg, "NVDA", "BUY", "Overweight")  # the brief arm's ledger row
    write_decision(cfg, ticker="NVDA", arm="feeds", plan=BUY_PLAN if feeds_code == "BUY" else None,
                   date_iso=DAY.isoformat(), decision=feeds_code)
    feeds_only = stage(feeds_code, arm="feeds", pm_rating="Overweight", plan_valid=True,
                       run_id=f"{DAY.isoformat()}-us-NVDA-feeds-1")
    write_slot(cfg, briefs={"NVDA": ticker_brief("NVDA", 7.0, "m — m.")}, tickers=[entry("NVDA", decide=feeds_only)])
    text = build(cfg, "us")
    assert "→ 下单待核实（简报一路的记录缺失）" in text and "计划下单：待核实：NVDA" in text.splitlines()[0]
    assert "不买" not in text and "不操作" not in text


@pytest.mark.unit
@pytest.mark.parametrize(("code", "shown"), [("HOLD", "已下单，见交易消息（最新分析：PM 中性）"),
                                             ("ERROR", "已下单，见交易消息（最新分析：分析出错）")])
def test_a_live_order_is_told_even_when_a_later_run_decided_otherwise(code, shown):
    assert rd.executed_text(code, "Hold" if code == "HOLD" else None, final="order-live", execute=None) == shown


@pytest.mark.unit
def test_the_summary_call_is_bounded_and_every_failure_costs_only_the_summaries(monkeypatch):
    import subprocess

    from pipeline import claude_cli

    calls, recorded = [], []
    monkeypatch.setattr(claude_cli, "claude_command", lambda *a, **k: calls.append((a, k)) or ["claude"])
    monkeypatch.setattr(claude_cli, "call_env", lambda command: {"E": "1"})
    monkeypatch.setattr(claude_cli, "record_failed_call", lambda *a: recorded.append(("failed", a[0])))
    monkeypatch.setattr(claude_cli, "record_usage", lambda *a, **k: recorded.append(("usage", k.get("outcome"))))
    seen = {}

    def run(cmd, **kw):
        seen.update(kw)
        return outcome["proc"]()

    monkeypatch.setattr(subprocess, "run", run)
    good = {"structured_output": {"macro": "m", "tickers": []}}
    cases = {
        "ok": (lambda: subprocess.CompletedProcess([], 0, stdout="{}", stderr=""), good),
        "exit": (lambda: subprocess.CompletedProcess([], 1, stdout="", stderr="x"), good),
        "envelope": (lambda: subprocess.CompletedProcess([], 0, stdout="junk", stderr=""), None),
        "no-output": (lambda: subprocess.CompletedProcess([], 0, stdout="{}", stderr=""), {"result": "x"}),
        "timeout": (lambda: (_ for _ in ()).throw(subprocess.TimeoutExpired("claude", 45, output="")), good),
    }
    outcome = {}
    for name, (proc, envelope) in cases.items():
        outcome["proc"] = proc
        if envelope is None:
            monkeypatch.setattr(claude_cli, "parse_envelope",
                                lambda stdout: (_ for _ in ()).throw(claude_cli.EnvelopeError("bad")))
        else:
            monkeypatch.setattr(claude_cli, "parse_envelope", lambda stdout, e=envelope: e)
        monkeypatch.setattr(rd, "_run_summary", rd.REAL_RUN_SUMMARY)
        macro, notes = rd.summaries("macro text", {"HUM": "source"})
        assert (macro is None and notes == {}) is (name != "ok"), name
    assert calls[0][0][:2] == ("claude-haiku-5-5", "low") and calls[0][1] == {"role": "collect"}
    assert seen["timeout"] == rd.DIGEST_SUMMARY_TIMEOUT_S == 45.0 and seen["env"] == {"E": "1"}
    assert ("failed", "research_digest") in recorded and ("usage", "error") in recorded


@pytest.mark.unit
@pytest.mark.parametrize("first", ["BUY", "HOLD", "ERROR"])
def test_a_later_scheduled_attempt_is_undetermined_not_shadowed(tmp_path, live, first):
    cfg = config(tmp_path)
    book(cfg)
    decide = decided(cfg, "NVDA", first, "Overweight", plan=BUY_PLAN if first == "BUY" else None)
    write_decision(cfg, ticker="NVDA", n=2, plan=BUY_PLAN, date_iso=DAY.isoformat())  # scheduled attempt 2
    write_slot(cfg, briefs={"NVDA": ticker_brief("NVDA", 7.0, "m — m.")}, tickers=[entry("NVDA", decide=decide)])
    text = build(cfg, "us")
    assert "→ 下单待核实（有更新的分析记录）" in text and "计划下单：待核实：NVDA" in text.splitlines()[0]
    assert "被手动重跑取代" not in text and "计划下单：无" not in text


@pytest.mark.unit
def test_a_cn_lost_event_never_suggests_an_order(tmp_path, live):
    cfg = config(tmp_path)
    write_decision(cfg, ticker="0981.HK", session="cn", plan=BUY_PLAN, date_iso=DAY.isoformat())
    feeds = stage("BUY", arm="feeds", pm_rating="Overweight", plan_valid=True)
    write_slot(cfg, "cn", briefs={"0981.HK": ticker_brief("0981.HK", 7.0, "m — m.")},
               tickers=[entry("0981.HK", decide=feeds)])
    text = build(cfg, "cn")
    assert "下单待核实" not in text and "A股不下单" in text


@pytest.mark.unit
def test_a_later_feeds_attempt_is_never_an_open_order(tmp_path, live):
    cfg = config(tmp_path)
    book(cfg)
    write_decision(cfg, ticker="NVDA", arm="feeds", plan=BUY_PLAN, date_iso=DAY.isoformat())
    write_decision(cfg, ticker="NVDA", arm="feeds", n=2, plan=BUY_PLAN, date_iso=DAY.isoformat())
    feeds = stage("BUY", arm="feeds", pm_rating="Overweight", plan_valid=True,
                  run_id=f"{DAY.isoformat()}-us-NVDA-feeds-1")
    write_slot(cfg, briefs={"NVDA": ticker_brief("NVDA", 7.0, "m — m.")}, tickers=[entry("NVDA", decide=feeds)])
    text = build(cfg, "us")
    assert "下单待核实" not in text and "待核实：NVDA" not in text and "对照分析" in text


@pytest.mark.unit
def test_a_manual_brief_run_is_not_a_lost_event(tmp_path, live):
    cfg = config(tmp_path)
    book(cfg)
    write_decision(cfg, ticker="NVDA", trigger="manual", plan=BUY_PLAN, date_iso=DAY.isoformat())
    write_decision(cfg, ticker="NVDA", arm="feeds", plan=None, date_iso=DAY.isoformat(), decision="HOLD")
    feeds = stage("HOLD", arm="feeds", pm_rating="Hold", run_id=f"{DAY.isoformat()}-us-NVDA-feeds-1")
    write_slot(cfg, briefs={"NVDA": ticker_brief("NVDA", 7.0, "m — m.")}, tickers=[entry("NVDA", decide=feeds)])
    text = build(cfg, "us")
    assert "简报一路的记录缺失" not in text and "待核实：NVDA" not in text


@pytest.mark.unit
@pytest.mark.parametrize(("halted", "mode"), [(False, "模拟"), (True, "执行已暂停")])
def test_a_later_attempt_under_dry_run_or_halt_is_no_live_order(tmp_path, monkeypatch, halted, mode):
    if halted:
        monkeypatch.setenv("TRADINGAGENTS_EXECUTION_ENABLED", "true")
    else:
        monkeypatch.delenv("TRADINGAGENTS_EXECUTION_ENABLED", raising=False)
    cfg = config(tmp_path)
    book(cfg)
    if halted:
        (cfg.state_dir / "EXECUTION_HALT").parent.mkdir(parents=True, exist_ok=True)
        (cfg.state_dir / "EXECUTION_HALT").write_text("", encoding="utf-8")
    decide = decided(cfg, "NVDA", "HOLD", "Hold", plan=None)
    write_decision(cfg, ticker="NVDA", n=2, plan=BUY_PLAN, date_iso=DAY.isoformat())
    write_slot(cfg, briefs={"NVDA": ticker_brief("NVDA", 7.0, "m — m.")}, tickers=[entry("NVDA", decide=decide)])
    text = build(cfg, "us")
    assert f"→ 不下单（{mode}）" in text and f"计划下单（{mode}）：无" in text.splitlines()[0]


@pytest.mark.unit
def test_a_scheduled_decision_missing_from_a_valid_snapshot_is_undetermined(tmp_path, live):
    cfg = config(tmp_path)
    book(cfg)
    write_slot(cfg, briefs={}, tickers=[])
    decided(cfg, "NVDA", "BUY", "Overweight")
    head = build(cfg, "us").splitlines()[0]
    assert "计划下单：待核实：NVDA" in head and "计划下单：无" not in head
    cfg2 = config(tmp_path / "manual")
    book(cfg2)
    write_slot(cfg2, briefs={}, tickers=[])
    write_decision(cfg2, ticker="NVDA", trigger="manual", plan=BUY_PLAN, date_iso=DAY.isoformat())
    assert "计划下单：无" in build(cfg2, "us").splitlines()[0]  # a manual row is not step 7's


@pytest.mark.unit
def test_after_execution_the_brief_arms_own_skip_reason_is_told(tmp_path, live):
    cfg = config(tmp_path)
    book(cfg)
    execute = {"code": "feeds-arm", "outcome": "excluded", "details": {"arm": "feeds"},
               "events": [{"code": "feeds-arm", "details": {"arm": "feeds"}},
                          {"code": "total-risk", "details": {"arm": "brief"}}]}
    row = {**entry("NVDA", decide=decided(cfg, "NVDA", "BUY", "Overweight")), "final": "excluded"}
    row["stages"]["execute"] = execute
    write_slot(cfg, briefs={"NVDA": ticker_brief("NVDA", 7.0, "m — m.")}, tickers=[row])
    path = cfg.state_dir / "funnel" / "us" / f"{DAY}.json"
    snap = json.loads(path.read_text())
    snap["context"] = {"execute_ran": True}
    path.write_text(json.dumps(snap), encoding="utf-8")
    assert "→ 看多（PM 看多；未下单：总风险超限）" in build(cfg, "us")


@pytest.mark.unit
def test_after_execution_a_missing_brief_arm_outcome_is_undetermined(tmp_path, live):
    cfg = config(tmp_path)
    book(cfg)
    row = {**entry("NVDA", decide=decided(cfg, "NVDA", "BUY", "Overweight")), "final": "excluded"}
    row["stages"]["execute"] = {"code": "feeds-arm", "outcome": "excluded", "details": {"arm": "feeds"},
                                "events": [{"code": "feeds-arm", "details": {"arm": "feeds"}}]}
    write_slot(cfg, briefs={"NVDA": ticker_brief("NVDA", 7.0, "m — m.")}, tickers=[row])
    path = cfg.state_dir / "funnel" / "us" / f"{DAY}.json"
    snap = json.loads(path.read_text())
    snap["context"] = {"execute_ran": True}
    path.write_text(json.dumps(snap), encoding="utf-8")
    text = build(cfg, "us")
    assert "→ 看多（PM 看多；下单结果待核实）" in text and "已执行：待核实：NVDA" in text.splitlines()[0]
    assert "对照分析" not in text and "无下单" not in text


@pytest.mark.unit
def test_a_superseded_note_never_stands_for_the_brief_arms_outcome(tmp_path, live):
    cfg = config(tmp_path)
    book(cfg)
    decided(cfg, "NVDA", "BUY", "Overweight")
    decide = decided(cfg, "NVDA", "BUY", "Overweight", n=2)
    row = {**entry("NVDA", decide=decide), "final": "excluded"}
    row["stages"]["execute"] = {
        "code": "feeds-arm", "outcome": "excluded", "details": {"arm": "feeds"},
        "events": [{"code": "superseded", "outcome": "info",
                    "details": {"arm": "brief", "run_id": f"{DAY.isoformat()}-us-NVDA-brief-1"}},
                   {"code": "feeds-arm", "outcome": "excluded", "details": {"arm": "feeds"}}]}
    write_slot(cfg, briefs={"NVDA": ticker_brief("NVDA", 7.0, "m — m.")}, tickers=[row])
    path = cfg.state_dir / "funnel" / "us" / f"{DAY}.json"
    snap = json.loads(path.read_text())
    snap["context"] = {"execute_ran": True}
    path.write_text(json.dumps(snap), encoding="utf-8")
    text = build(cfg, "us")
    assert "下单结果待核实" in text and "已执行：待核实：NVDA" in text.splitlines()[0] and "无下单" not in text


@pytest.mark.unit
@pytest.mark.parametrize("feeds_code", ["HOLD", "BUY"])
def test_after_execution_a_lost_brief_event_is_undetermined(tmp_path, live, feeds_code):
    cfg = config(tmp_path)
    book(cfg)
    decided(cfg, "NVDA", "BUY", "Overweight")  # the brief arm's ledger row
    write_decision(cfg, ticker="NVDA", arm="feeds", plan=BUY_PLAN if feeds_code == "BUY" else None,
                   date_iso=DAY.isoformat(), decision=feeds_code)
    feeds = stage(feeds_code, arm="feeds", pm_rating="Hold", run_id=f"{DAY.isoformat()}-us-NVDA-feeds-1")
    row = {**entry("NVDA", decide=feeds), "final": "excluded"}
    row["stages"]["execute"] = {"code": "feeds-arm", "outcome": "excluded", "details": {"arm": "feeds"},
                                "events": [{"code": "feeds-arm", "outcome": "excluded", "details": {"arm": "feeds"}}]}
    write_slot(cfg, briefs={"NVDA": ticker_brief("NVDA", 7.0, "m — m.")}, tickers=[row])
    path = cfg.state_dir / "funnel" / "us" / f"{DAY}.json"
    snap = json.loads(path.read_text())
    snap["context"] = {"execute_ran": True}
    path.write_text(json.dumps(snap), encoding="utf-8")
    text = build(cfg, "us")
    assert "→ 下单结果待核实（简报一路的记录缺失）" in text and "已执行：待核实：NVDA" in text.splitlines()[0]
    assert "无下单" not in text and "不操作" not in text


@pytest.mark.unit
def test_cn_never_suggests_an_order_even_with_execution_state_unknown(tmp_path, monkeypatch):
    text, ordered = rd.action_text("BUY", "Overweight", session="cn", held=False, blocker="unknown",
                                   add_intent=False, mode=rd.UNKNOWN_STATE, execution_time=None)
    assert "A股不下单" in text and ordered is False
    from pipeline import execution_adapter

    monkeypatch.setattr(execution_adapter, "load_settings", lambda *a: (_ for _ in ()).throw(OSError("env")))
    cfg = config(tmp_path)
    write_slot(cfg, "cn", briefs={"0981.HK": ticker_brief("0981.HK", 7.0, "m — m.")},
               tickers=[entry("0981.HK", decide=stage("BUY", arm="brief", pm_rating="Overweight"))])
    out = build(cfg, "cn")
    assert "A股不下单" in out and "执行状态未知" not in out


@pytest.mark.unit
def test_a_summary_may_name_its_own_numeric_ticker(tmp_path, monkeypatch):
    cfg = config(tmp_path)
    write_decision(cfg, ticker="0700.HK", session="cn", plan=None, date_iso=DAY.isoformat(), decision="HOLD")
    decide = stage("HOLD", arm="brief", pm_rating="Hold", run_id=f"{DAY.isoformat()}-cn-0700.HK-brief-1")
    write_slot(cfg, "cn", briefs={"0700.HK": ticker_brief("0700.HK", 7.0, "neutral — quiet week.")},
               tickers=[entry("0700.HK", decide=decide)])
    monkeypatch.setattr(rd, "_run_summary", lambda p: json.dumps(
        {"macro": "", "tickers": [{"ticker": "0700.HK", "summary": "0700.HK 本周平静。"}]}))
    assert "  0700.HK 本周平静。" in build(cfg, "cn").splitlines()


@pytest.mark.unit
def test_an_unknown_execution_state_still_sees_a_newer_attempt(tmp_path, live, monkeypatch):
    cfg = config(tmp_path)
    book(cfg)
    decide = decided(cfg, "NVDA", "HOLD", "Hold", plan=None)
    write_decision(cfg, ticker="NVDA", n=2, plan=BUY_PLAN, date_iso=DAY.isoformat())
    write_slot(cfg, briefs={"NVDA": ticker_brief("NVDA", 7.0, "m — m.")}, tickers=[entry("NVDA", decide=decide)])
    monkeypatch.setattr(rd, "_execution_mode", lambda c: (_ for _ in ()).throw(OSError("halt file")))
    text = build(cfg, "us")
    assert "下单待核实（有更新的分析记录）" in text and "待核实：NVDA" in text.splitlines()[0]
    assert "：无" not in text.splitlines()[0].split("｜")[-1]


@pytest.mark.unit
@pytest.mark.parametrize(("session", "ticker", "market"), [("us", "MSFT", "美股"), ("cn", "0700.HK", "A股/港股")])
def test_the_core_session_off_label_names_the_slots_market(tmp_path, session, ticker, market):
    cfg = config(tmp_path)
    write_slot(cfg, session, briefs={ticker: ticker_brief(ticker, 3.0, "m — m.")},
               tickers=[entry(ticker, trigger="core-session-off")])
    assert f"• {market}核心股本时段不分析：{ticker} 3.0" in build(cfg, session)


@pytest.mark.unit
def test_held_names_needing_a_look_are_flagged_at_the_top(tmp_path):
    cfg = config(tmp_path)
    write_slot(cfg, briefs={"MSFT": ticker_brief("MSFT", 6.0, "bearish — guidance cut."),
                            "NVDA": ticker_brief("NVDA", 7.0, "bearish — x."),
                            "AMD": ticker_brief("AMD", 7.0, "bullish — y."),
                            "MU": ticker_brief("MU", 7.0, "bearish — z.")},
               tickers=[entry("MSFT", evaluate="warn", trigger="core", decide=decided(cfg, "MSFT", "HOLD", "Hold")),
                        entry("NVDA", evaluate="warn", trigger="core", decide=decided(cfg, "NVDA", "ERROR", None)),
                        entry("AMD", evaluate="warn", trigger="core", decide=decided(cfg, "AMD", "HOLD", "Hold")),
                        entry("MU", evaluate="warn", trigger="core", decide=decided(cfg, "MU", "SELL", "Sell"))])
    book(cfg, held=["MSFT", "NVDA", "AMD", "MU", "TSLA"])
    lines = rd.build_digest(cfg, "us", DAY).splitlines()
    alerts = [line for line in lines if line.startswith("⚠️ 持仓提醒")]
    assert alerts == [
        "⚠️ 持仓提醒：MSFT 简报偏空，但分析没有给出卖出——是否卖出由你决定",
        "⚠️ 持仓提醒：NVDA 分析出错，今日没有结论——是否卖出由你决定",
        "⚠️ 持仓提醒：TSLA 今日没有分析——是否卖出由你决定",
    ]  # AMD bullish and MU already SELL: nothing to flag
    assert lines.index(alerts[0]) == 1  # right under the header


@pytest.mark.unit
def test_no_held_alert_for_cn_or_unknown_holdings(tmp_path):
    assert rd.held_alerts("cn", {"0700.HK"}, []) == []
    assert rd.held_alerts("us", None, []) == [] and rd.held_alerts("us", set(), []) == []
