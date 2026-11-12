"""Daily post-close review and archive — specs/daily-review.md."""

import json
from datetime import date, datetime, timedelta, timezone

import pytest

from pipeline import daily_review as dr
from tests.test_execution_adapter import BUY_PLAN
from tests.test_research_digest import (
    DAY,
    book,
    config,
    decided,
    entry,
    stage,
    ticker_brief,
    write_slot,
)

SECRET_PROSE = "a rationale only the brief body carries"


def prices_for(*tickers, moves=None):
    """Two sessions: the day before and DAY, with each ticker's % move."""
    moves = moves or {}
    out = {}
    for t in (*tickers, *dr.BENCHMARKS.values()):
        m = moves.get(t, 0.0)
        out[t] = [(date(2026, 11, 3), 100.0), (DAY, 100.0 * (1 + m / 100))]
    return out


@pytest.fixture(autouse=True)
def offline_broker(monkeypatch):
    """No test reaches Alpaca: the developer's .env keys survive conftest."""
    from pipeline import trade_notify

    built = []

    def refuse(*a, **k):
        built.append(a)
        raise RuntimeError("offline")

    monkeypatch.setattr(trade_notify, "AlpacaReadOnly", refuse)

    def no_quote(ticker):
        raise RuntimeError("offline")

    no_quote.real = dr._yahoo_quote  # for the one test that pins the reader against a fake yfinance
    monkeypatch.setattr(dr, "_yahoo_quote", no_quote)
    monkeypatch.setattr(trade_notify, "alpaca_keys", lambda: ("key", "secret"))
    return built


class FakeBroker:
    def __init__(self, trading=True):
        self.trading = trading

    def account(self):
        return {"equity": "1010", "last_equity": "1000", "cash": "500", "buying_power": "900"}

    def positions(self):
        return [{"symbol": "NVDA", "qty": "2", "avg_entry_price": "100", "current_price": "103", "unrealized_pl": "6"}]

    def fills_since(self, start):
        return [{"symbol": "NVDA", "side": "buy", "qty": "2", "price": "100",
                 "transaction_time": "2026-11-04T14:50:00Z"},
                {"symbol": "MU", "side": "buy", "qty": "1", "price": "90",
                 "transaction_time": "2026-11-05T14:50:00Z"}]  # another New York day

    def is_trading_day(self, day):
        return self.trading


@pytest.fixture
def archive(tmp_path, monkeypatch):
    root = tmp_path / "archive"
    monkeypatch.setenv(dr.ARCHIVE_ENV, str(root))
    return root


def slot(cfg):
    write_slot(cfg, briefs={"NVDA": ticker_brief("NVDA", 7.0, f"bearish — {SECRET_PROSE}."),
                            "FN": ticker_brief("FN", 2.0, "mixed — m.")}, tickers=[
        entry("NVDA", evaluate="warn", trigger="core", decide=decided(cfg, "NVDA", "BUY", "Overweight")),
        entry("FN", evaluate="skipped-below-threshold", trigger="below-threshold")])


@pytest.mark.unit
def test_the_archive_is_by_category_and_date_and_idempotent(tmp_path, archive):
    cfg = config(tmp_path)
    slot(cfg)
    orders = cfg.ledger_dir / "orders.jsonl"
    orders.write_text(json.dumps({"client_order_id": f"{DAY}-us-NVDA-entry", "kind": "submitted"}) + "\n"
                      + json.dumps({"client_order_id": "2026-11-03-us-MU-entry", "kind": "skip"}) + "\n")
    counts = dr.archive_day(cfg, DAY, prices=prices_for("NVDA", "FN", moves={"FN": 8.0}))
    iso = DAY.isoformat()
    assert (archive / "macro_briefs" / iso / "us.md").exists() and (archive / "funnel" / iso / "us.json").exists()
    assert (archive / "ticker_briefs" / iso / "NVDA.md").exists()
    decisions = (archive / "decisions" / iso / "decisions.jsonl").read_text().splitlines()
    assert [json.loads(r)["ticker"] for r in decisions] == ["NVDA"]
    assert [json.loads(r)["client_order_id"] for r in
            (archive / "orders" / iso / "orders.jsonl").read_text().splitlines()] == [f"{iso}-us-NVDA-entry"]
    csv = (archive / "prices" / iso / "closes.csv").read_text()
    assert f"{iso},FN,108.0000,8.00" in csv
    assert counts["macro_briefs:missing"] >= 1  # cn macro brief absent: skipped, not an error
    before = {p: p.read_bytes() for p in archive.rglob("*") if p.is_file()}
    dr.archive_day(cfg, DAY, prices=prices_for("NVDA", "FN", moves={"FN": 8.0}))
    assert {p: p.read_bytes() for p in archive.rglob("*") if p.is_file()} == before


@pytest.mark.unit
def test_facts_are_structured_and_carry_no_brief_prose(tmp_path):
    cfg = config(tmp_path)
    slot(cfg)
    facts = dr.build_facts(cfg, DAY, prices=prices_for("NVDA", "FN", moves={"NVDA": 2.0, "FN": 8.0}))
    assert SECRET_PROSE not in json.dumps(facts, ensure_ascii=False)
    nvda = next(r for r in facts["us"]["names"] if r["ticker"] == "NVDA")
    assert (nvda["impact"], nvda["trigger"], nvda["decision"], nvda["pm_rating"]) == ("偏空", "core", "BUY", "Overweight")
    assert nvda["move_pct"] == 2.0 and nvda["excess_pct"] == 2.0
    assert [r["ticker"] for r in facts["us"]["missed_moves"]] == ["FN"]  # skipped, moved 8%


@pytest.mark.unit
def test_a_decision_on_an_older_brief_is_named(tmp_path):
    cfg = config(tmp_path)
    write_slot(cfg, briefs={}, tickers=[entry("MSFT", collect="failed", evaluate="skipped-absent", trigger="core",
                                              decide=decided(cfg, "MSFT", "BUY", "Overweight"))])
    path = cfg.ledger_dir / "decisions.jsonl"
    rows = [json.loads(r) for r in path.read_text().splitlines()]
    rows[0]["inputs"]["ticker_brief"] = {"path": "MSFT/2026-11-03.md"}
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))
    msft = dr.build_facts(cfg, DAY, prices=prices_for("MSFT"))["us"]["names"][0]
    assert msft["brief_used"] == "2026-11-03"


def review():
    return {"summary": "今天一笔买入。", "issues": [{"title": "分析出错", "evidence": "4/12 ERROR", "severity": "高"}],
            "verdict": "继续观察", "proposals": [], "watch": ["偏空简报区分度"]}


@pytest.mark.unit
def test_the_review_goes_to_the_trades_group_once_and_is_archived(tmp_path, archive, monkeypatch):
    cfg = config(tmp_path)
    slot(cfg)
    monkeypatch.setattr(dr, "closes", lambda tickers, fetch=None, **k: prices_for("NVDA", "FN"))
    sent, prompts = [], []
    text = dr.review_day(cfg, DAY, send=lambda title, t: sent.append(t),
                         runner=lambda prompt: prompts.append(prompt) or review())
    assert sent == [text] and text.startswith(f"🧭 每日复盘 · {DAY.isoformat()}")
    assert "策略：继续观察" in text and "• [高] 分析出错 — 4/12 ERROR" in text and "需你确认" in text
    assert SECRET_PROSE not in prompts[0] and "只提建议" in prompts[0]
    assert (archive / "reviews" / DAY.isoformat() / "review.md").read_text().startswith("🧭")
    dr.review_day(cfg, DAY, send=lambda title, t: sent.append(t), runner=lambda prompt: review())
    assert len(sent) == 1  # key review:<day>: exactly once


@pytest.mark.unit
def test_without_the_model_the_facts_still_go_out(tmp_path, archive, monkeypatch):
    cfg = config(tmp_path)
    slot(cfg)
    monkeypatch.setattr(dr, "closes", lambda tickers, fetch=None, **k: prices_for("NVDA", "FN", moves={"FN": 8.0}))
    sent = []

    def broken(prompt):
        raise TimeoutError("model")

    text = dr.review_day(cfg, DAY, send=lambda title, t: sent.append(t), runner=broken)
    assert "复盘模型不可用" in text and "FN +8.0%" in text and sent == [text]
    bad = dr.review_day(cfg, date(2026, 11, 5), send=None, runner=lambda prompt: {**review(), "verdict": "全仓买入"})
    assert "复盘模型不可用" in bad  # a verdict outside the schema is no review


@pytest.mark.unit
def test_the_watchdog_starts_the_review_once_after_the_close(tmp_path):
    cfg = config(tmp_path)
    slot(cfg)
    spawned = []
    friday = DAY  # 2026-11-04 (a Wednesday); times below are New York
    at = lambda h, m=0: datetime(friday.year, friday.month, friday.day, h, m, tzinfo=dr.NY)  # noqa: E731
    assert not dr.maybe_start(cfg, at(16, 59), runner=lambda cfg, day: spawned.append(day))  # before 17:00 ET
    assert dr.maybe_start(cfg, at(17, 5), runner=lambda cfg, day: spawned.append(day)) and spawned == [friday]
    assert not dr.maybe_start(cfg, at(18), runner=lambda cfg, day: spawned.append(day))  # started < 2 h ago
    assert dr.maybe_start(cfg, at(19, 30), runner=lambda cfg, day: spawned.append(day))  # stuck: retried
    _started, done = dr._marks(cfg, friday)
    done.write_text("x")
    assert not dr.maybe_start(cfg, at(21), runner=lambda cfg, day: spawned.append(day)) and len(spawned) == 2
    saturday = datetime(2026, 11, 7, 23, tzinfo=timezone.utc)
    assert not dr.maybe_start(cfg, saturday, runner=lambda cfg, day: spawned.append(day))
    other = config(tmp_path / "empty")  # no us snapshot for the day: nothing to review
    assert not dr.maybe_start(other, at(18), runner=lambda cfg, day: spawned.append(day))


@pytest.mark.unit
def test_the_hourly_watchdog_asks_for_the_review(tmp_path, monkeypatch):
    from pipeline import orchestrator, research_digest, trade_notify

    calls = []
    monkeypatch.setattr(trade_notify, "run_hourly", lambda **k: None)
    monkeypatch.setattr(trade_notify, "flush_outbox", lambda *a: None)
    monkeypatch.setattr(research_digest, "retry_pending", lambda *a: None)
    monkeypatch.setattr(dr, "maybe_start", lambda cfg, now: calls.append(now))
    cfg = config(tmp_path, lark_trades_webhook_url="https://open.feishu.cn/open-apis/bot/v2/hook/abc")
    now = datetime(2026, 11, 4, 22, tzinfo=timezone.utc)
    orchestrator._hourly_trade_notifications(cfg, now)
    assert calls == [now]


@pytest.mark.unit
def test_the_digest_as_sent_is_kept_for_the_archive(tmp_path, monkeypatch):
    from pipeline import research_digest, trade_monitor

    monkeypatch.setattr(trade_monitor, "make_trades_sender", lambda c: lambda title, t: None)
    cfg = config(tmp_path, lark_trades_webhook_url="https://open.feishu.cn/open-apis/bot/v2/hook/abc")
    book(cfg)
    write_slot(cfg, briefs={"MU": ticker_brief("MU", 4.0, "m — m.")},
               tickers=[entry("MU", decide=stage("HOLD", arm="brief", pm_rating="Hold"))])
    research_digest.send_digest(cfg, "us", DAY)
    kept = cfg.state_dir / "research_digest" / "us" / f"{DAY.isoformat()}.txt"
    assert kept.read_text().startswith("📊 研究摘要 · 美股")


_ = BUY_PLAN  # the shared fixtures' plan shape


@pytest.mark.unit
def test_the_by_hand_run_loads_the_repo_env(tmp_path, monkeypatch):
    from pipeline import orchestrator

    loaded, ran = [], []
    monkeypatch.setattr(orchestrator, "_load_repo_dotenv", lambda: loaded.append(1))
    monkeypatch.setattr(dr, "run", lambda cfg, day: ran.append(day) or 0)
    assert dr.main(["run", "--date", DAY.isoformat()]) == 0
    assert loaded == [1] and ran == [DAY]


@pytest.mark.unit
def test_a_broken_archive_or_macro_brief_still_sends_the_day(tmp_path, monkeypatch):
    cfg = config(tmp_path)
    slot(cfg)
    blocker = tmp_path / "a-file"
    blocker.write_text("x")
    monkeypatch.setenv(dr.ARCHIVE_ENV, str(blocker / "archive"))  # an unwritable root
    monkeypatch.setattr(dr, "closes", lambda tickers, fetch=None, **k: prices_for("NVDA", "FN"))
    (cfg.macro_brief_dir / f"{DAY}.us.md").write_text("---\nas_of_date: 2026-13-45\n---\n")
    sent = []
    text = dr.review_day(cfg, DAY, send=lambda title, t: sent.append(t), runner=lambda prompt: review())
    assert sent == [text] and "策略：继续观察" in text


@pytest.mark.unit
def test_a_brief_less_name_that_moved_is_a_missed_move(tmp_path):
    cfg = config(tmp_path)
    write_slot(cfg, briefs={}, tickers=[
        entry("MSFT", collect="failed", evaluate="skipped-absent", trigger="core"),
        {"ticker": "ORCL", "layer": "watch", "stages": {"collect": stage("not-collected-watch")}}])
    facts = dr.build_facts(cfg, DAY, prices=prices_for("MSFT", "ORCL", moves={"MSFT": -17.0, "ORCL": 8.0}))
    missed = {r["ticker"]: r for r in facts["us"]["missed_moves"]}
    assert set(missed) == {"MSFT", "ORCL"} and missed["ORCL"]["collect"] == "not-collected-watch"


@pytest.mark.unit
def test_re_archiving_never_replaces_good_files_with_empty_ones(tmp_path, archive):
    cfg = config(tmp_path)
    slot(cfg)
    dr.archive_day(cfg, DAY, prices=prices_for("NVDA", "FN"))
    iso = DAY.isoformat()
    kept = {p: p.read_bytes() for p in (archive / "decisions" / iso / "decisions.jsonl",
                                        archive / "prices" / iso / "closes.csv")}
    (cfg.ledger_dir / "decisions.jsonl").write_bytes(b"\xff\xfe broken")
    counts = dr.archive_day(cfg, DAY, fetch=lambda t: (_ for _ in ()).throw(OSError("down")))
    assert {p: p.read_bytes() for p in kept} == kept
    assert counts["decisions:missing"] == 1 and counts["prices:missing"] == 1


@pytest.mark.unit
@pytest.mark.parametrize("bad", [
    {**review(), "summary": None}, {**review(), "issues": ["x"]}, {**review(), "verdict": "全仓买入"},
    {**review(), "summary": "详见 https://evil.example 。"}, {**review(), "watch": ['<at user_id="all">所有人</at>']},
    {**review(), "summary": "详见 evil.example/x。"}, {**review(), "summary": "ftp://evil.example"},
    {**review(), "summary": "[点击](evil)"}, {**review(), "summary": "见 evil.com。"},
])
def test_a_malformed_or_unsafe_review_falls_back_to_the_facts(tmp_path, archive, monkeypatch, bad):
    cfg = config(tmp_path)
    slot(cfg)
    monkeypatch.setattr(dr, "closes", lambda tickers, fetch=None, **k: prices_for("NVDA", "FN"))
    sent = []
    text = dr.review_day(cfg, DAY, send=lambda title, t: sent.append(t), runner=lambda prompt: bad)
    assert "复盘模型不可用" in text and sent == [text] and "evil" not in text and "<at" not in text


@pytest.mark.unit
def test_a_review_call_that_used_the_web_is_rejected(monkeypatch):
    import subprocess

    from pipeline import claude_cli

    envelope = {"structured_output": review(), "modelUsage": {"m": {"webSearchRequests": 1}}, "_tool_calls": {}}
    monkeypatch.setattr(claude_cli, "claude_command", lambda *a, **k: ["claude"])
    monkeypatch.setattr(claude_cli, "call_env", lambda command: None)
    monkeypatch.setattr(claude_cli, "parse_envelope", lambda stdout: envelope)
    monkeypatch.setattr(claude_cli, "record_usage", lambda *a, **k: None)
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout="{}", stderr=""))
    with pytest.raises(RuntimeError, match="web tools"):
        dr._run_review("prompt")


@pytest.mark.unit
def test_the_scoreboard_counts_decision_day_excess_by_impact_and_rating(tmp_path):
    cfg = config(tmp_path)
    slot(cfg)
    board = dr.build_facts(cfg, DAY, prices=prices_for("NVDA", "FN", moves={"NVDA": 3.0, "FN": -2.0, "SPY": 1.0}))
    sb = board["scoreboard"]
    assert sb["sessions"] == 1
    one = sb["horizons"]["1d"]
    assert one["impact"] == {"偏空": {"n": 1, "mean_excess_pct": 2.0}, "分歧": {"n": 1, "mean_excess_pct": -3.0}}
    assert one["pm_rating"] == {"Overweight": {"n": 1, "mean_excess_pct": 2.0}}
    assert one["decision"] == {"BUY": {"n": 1, "mean_excess_pct": 2.0}}
    assert sb["horizons"]["3d"]["impact"] == {}  # not closed yet: left out, never a partial span


@pytest.mark.unit
def test_the_usage_archive_reads_the_log_the_writer_uses_and_includes_the_review(tmp_path, archive, monkeypatch):
    cfg = config(tmp_path)
    slot(cfg)
    log = tmp_path / "custom-usage.jsonl"
    monkeypatch.setenv("TRADINGAGENTS_CLAUDE_USAGE_LOG", str(log))
    monkeypatch.setattr(dr, "closes", lambda tickers, fetch=None, **k: prices_for("NVDA", "FN"))

    def runner(prompt):
        log.write_text(json.dumps({"ts": "2026-11-04T22:10:00+00:00", "component": "daily_review"}) + "\n")
        return review()

    dr.review_day(cfg, DAY, send=None, runner=runner)
    rows = (archive / "llm_usage" / DAY.isoformat() / "cli.jsonl").read_text().splitlines()
    assert [json.loads(r)["component"] for r in rows] == ["daily_review"]


@pytest.mark.unit
def test_a_comparison_or_an_exchange_symbol_is_plain_text():
    ok = dr.checked_review({**review(), "summary": "超额>2%，n<10 不足以调整；0700.HK 平稳。"})
    assert ok["summary"].startswith("超额>2%")


@pytest.mark.unit
def test_a_missing_ledger_or_an_empty_source_never_empties_the_archive(tmp_path, archive):
    cfg = config(tmp_path)
    slot(cfg)
    dr.archive_day(cfg, DAY, prices=prices_for("NVDA", "FN"))
    iso = DAY.isoformat()
    kept = {p: p.read_bytes() for p in (archive / "decisions" / iso / "decisions.jsonl",
                                        archive / "ticker_briefs" / iso / "NVDA.md")}
    (cfg.ledger_dir / "decisions.jsonl").unlink()
    (cfg.ticker_brief_dir / "NVDA" / f"{iso}.md").write_text("")
    counts = dr.archive_day(cfg, DAY, prices=prices_for("NVDA", "FN"))
    assert {p: p.read_bytes() for p in kept} == kept
    assert counts["decisions:missing"] == 1 and counts["ticker_briefs:missing"] >= 1


@pytest.mark.unit
def test_closes_are_merged_never_dropped(tmp_path, archive):
    cfg = config(tmp_path)
    slot(cfg)
    dr.archive_day(cfg, DAY, prices=prices_for("NVDA", "FN", moves={"FN": 8.0}))
    counts = dr.archive_day(cfg, DAY, prices={"NVDA": prices_for("NVDA")["NVDA"]})  # a partial refetch
    text = (archive / "prices" / DAY.isoformat() / "closes.csv").read_text()
    assert ",FN,108.0000,8.00" in text and ",NVDA," in text and counts["prices:missing"] >= 1


@pytest.mark.unit
def test_an_unwritable_archive_leaves_the_day_for_the_retry(tmp_path, monkeypatch):
    from pipeline import trade_monitor

    cfg = config(tmp_path)
    slot(cfg)
    blocker = tmp_path / "a-file"
    blocker.write_text("x")
    monkeypatch.setenv(dr.ARCHIVE_ENV, str(blocker / "archive"))
    monkeypatch.setattr(dr, "closes", lambda tickers, fetch=None, **k: prices_for("NVDA", "FN"))
    monkeypatch.setattr(dr, "_run_review", lambda prompt: review())
    monkeypatch.setattr(trade_monitor, "make_trades_sender", lambda c: None)
    assert dr.run(cfg, DAY) == 1
    assert not dr._marks(cfg, DAY)[1].exists()  # no done mark: retried after STUCK_AFTER


@pytest.mark.unit
def test_without_a_trades_webhook_the_watchdog_still_reviews_and_archives(tmp_path, monkeypatch):
    from pipeline import orchestrator

    calls = []
    monkeypatch.setattr(dr, "maybe_start", lambda cfg, now: calls.append(now))
    cfg = config(tmp_path)  # no lark_trades_webhook_url
    now = datetime(2026, 11, 4, 22, tzinfo=timezone.utc)
    orchestrator._hourly_trade_notifications(cfg, now)
    assert calls == [now]


@pytest.mark.unit
def test_the_price_reads_share_a_budget():
    import time as _time

    def slow(ticker):
        _time.sleep(0.05)
        return [(DAY, 1.0)]

    out = dr.closes({f"T{i}" for i in range(20)}, fetch=slow, budget_s=0.12)
    assert 0 < len(out) < 20  # the rest skipped, the review goes on


@pytest.mark.unit
def test_an_empty_or_row_less_ledger_never_empties_the_archive(tmp_path, archive):
    cfg = config(tmp_path)
    slot(cfg)
    dr.archive_day(cfg, DAY, prices=prices_for("NVDA", "FN"))
    dest = archive / "decisions" / DAY.isoformat() / "decisions.jsonl"
    kept = dest.read_bytes()
    assert kept
    ledger = cfg.ledger_dir / "decisions.jsonl"
    for truncated in ("", json.dumps({"date": "2026-10-01", "ticker": "X"}) + "\n", "garbage\n"):
        ledger.write_text(truncated)
        counts = dr.archive_day(cfg, DAY, prices=prices_for("NVDA", "FN"))
        assert dest.read_bytes() == kept and counts["decisions:missing"] == 1


@pytest.mark.unit
def test_benchmarks_and_the_days_names_are_read_before_older_ones(tmp_path):
    cfg = config(tmp_path)
    slot(cfg)  # DAY: NVDA, FN
    old = config(tmp_path)
    funnel = old.state_dir / "funnel" / "us"
    older = DAY.replace(day=1)
    (funnel / f"{older.isoformat()}.json").write_text(json.dumps(
        {"tickers": [{"ticker": f"A{i:02d}"} for i in range(30)]}))
    read = []

    def fetch(ticker):
        read.append(ticker)
        return [(DAY, 1.0)]

    stats = {}
    dr.history_prices(cfg, DAY, fetch=fetch, stats=stats)
    assert read[:3] == list(dr.BENCHMARKS.values()) and set(read[3:5]) == {"NVDA", "FN"}
    assert stats == {"skipped": 0, "failed": 0, "quote_filled": 0, "quote_filled_tickers": [], "failed_tickers": {},
                     "unfilled": {}}
    stats = {}
    out = dr.closes(set(read), fetch=lambda t: [], first=read, stats=stats)
    assert out == {} and stats["failed"] == len(read)


@pytest.mark.unit
def test_a_truncated_price_read_is_a_fact_and_in_the_message(tmp_path, archive, monkeypatch):
    cfg = config(tmp_path)
    slot(cfg)

    def partial(tickers, fetch=None, budget_s=0, *, first=(), stats=None, **k):
        stats.update(skipped=7, failed=1)
        return prices_for("NVDA", "FN")

    monkeypatch.setattr(dr, "closes", partial)
    prompts = []
    text = dr.review_day(cfg, DAY, send=None, runner=lambda prompt: prompts.append(prompt) or review())
    assert '"price_reads": {"skipped": 7, "failed": 1, "quote_filled": 0, "quote_filled_tickers": [], "failed_tickers": {}, "unfilled": {}}' in prompts[0]
    assert "行情读取不完整：跳过 7 只、失败 1 只" in text


@pytest.mark.unit
def test_a_retry_after_an_archive_failure_reuses_the_queued_review(tmp_path, monkeypatch):
    cfg = config(tmp_path)
    slot(cfg)
    blocker = tmp_path / "a-file"
    blocker.write_text("x")
    monkeypatch.setenv(dr.ARCHIVE_ENV, str(blocker / "archive"))
    monkeypatch.setattr(dr, "closes", lambda tickers, fetch=None, **k: prices_for("NVDA", "FN"))
    sent = []
    first, archived = dr.review_day(cfg, DAY, send=lambda title, t: sent.append(t), runner=lambda p: review(),
                                    return_archived=True)
    assert not archived and sent == [first]
    root = tmp_path / "archive"
    monkeypatch.setenv(dr.ARCHIVE_ENV, str(root))

    def again(prompt):
        raise AssertionError("no second model call")

    text, archived = dr.review_day(cfg, DAY, send=lambda title, t: sent.append(t), runner=again,
                                   return_archived=True)
    assert archived and text == first and sent == [first]
    assert (root / "reviews" / DAY.isoformat() / "review.md").read_text() == first + "\n"


@pytest.mark.unit
def test_a_past_day_run_takes_no_broker_snapshot(tmp_path, archive, monkeypatch, offline_broker):
    from pipeline import trade_monitor, trade_notify

    cfg = config(tmp_path)
    slot(cfg)
    monkeypatch.setattr(dr, "closes", lambda tickers, fetch=None, **k: prices_for("NVDA", "FN"))
    monkeypatch.setattr(dr, "_run_review", lambda prompt: review())
    monkeypatch.setattr(trade_monitor, "make_trades_sender", lambda c: None)
    broker = FakeBroker()
    monkeypatch.setattr(trade_notify, "AlpacaReadOnly", lambda *a: broker)
    snapshots = []
    monkeypatch.setattr(broker, "account", lambda: snapshots.append(1) or {})
    assert dr.run(cfg, DAY) == 0  # DAY is in the past
    assert snapshots == [] and not (archive / "broker").exists()


@pytest.mark.unit
def test_a_market_holiday_is_archived_not_reviewed(tmp_path, archive, monkeypatch):
    from pipeline import trade_monitor, trade_notify

    cfg = config(tmp_path)
    slot(cfg)
    monkeypatch.setattr(dr, "closes", lambda tickers, fetch=None, **k: prices_for("NVDA", "FN"))
    sent = []
    monkeypatch.setattr(trade_monitor, "make_trades_sender", lambda c: lambda title, t: sent.append(t))
    monkeypatch.setattr(trade_notify, "AlpacaReadOnly", lambda *a: FakeBroker(trading=False))

    def never(prompt):
        raise AssertionError("no model call on a holiday")

    monkeypatch.setattr(dr, "_run_review", never)
    assert dr.run(cfg, DAY) == 0
    assert sent == [] and dr._marks(cfg, DAY)[1].exists()
    assert (archive / "funnel" / DAY.isoformat() / "us.json").exists()
    assert not (archive / "reviews" / DAY.isoformat()).exists()


@pytest.mark.unit
def test_an_undone_day_is_picked_up_after_midnight_within_the_look_back(tmp_path):
    cfg = config(tmp_path)
    slot(cfg)  # Wednesday DAY
    spawned = []
    runner = lambda cfg, day: spawned.append(day)  # noqa: E731
    thursday = datetime(2026, 11, 5, 0, 30, tzinfo=dr.NY)
    assert dr.maybe_start(cfg, thursday, runner=runner) and spawned == [DAY]
    assert not dr.maybe_start(cfg, thursday.replace(hour=1), runner=runner)  # started < 2 h ago
    sunday = datetime(2026, 11, 8, 12, tzinfo=dr.NY)  # 4 days on: past the look-back
    assert not dr.maybe_start(config(tmp_path), sunday, runner=runner) and len(spawned) == 1


@pytest.mark.unit
@pytest.mark.parametrize("bad", ["见 360.hk/x", "见 例子.com/x", "见 1.2.3.4/x", "<at id=1>", "</a>", "<!-- x -->",
                                 "<table>", "</table>", "<h1>", "<object data=x>", "<li>", "<at"])
def test_hosts_and_tags_are_screened(bad):
    with pytest.raises(ValueError):
        dr.checked_review({**review(), "summary": bad})


@pytest.mark.unit
@pytest.mark.parametrize("ok", ["超额<SPY", "NVDA<SPY 2 天", "n<10", "<A股>", "NVDA<SPY 但 MU>SPY"])
def test_comparisons_pass_the_screen(ok):
    assert dr.checked_review({**review(), "summary": ok})["summary"] == ok


@pytest.mark.unit
def test_the_broker_snapshot_is_archived_and_in_the_facts(tmp_path, archive):
    cfg = config(tmp_path)
    slot(cfg)
    broker = FakeBroker()
    counts = dr.archive_day(cfg, DAY, broker=broker, prices=prices_for("NVDA", "FN"))
    folder = archive / "broker" / DAY.isoformat()
    assert json.loads((folder / "account.json").read_text())["equity"] == "1010"
    assert json.loads((folder / "positions.json").read_text())[0]["symbol"] == "NVDA"
    assert [f["symbol"] for f in json.loads((folder / "fills.json").read_text())] == ["NVDA"]  # New York day only
    assert counts["broker"] == 3
    facts = dr.build_facts(cfg, DAY, prices=prices_for("NVDA", "FN"), broker=broker)
    assert facts["account"]["equity"] == "1010" and facts["positions"][0]["qty"] == "2"
    assert [f["symbol"] for f in facts["fills"]] == ["NVDA"]

    def down(*a):
        raise ConnectionError("down")

    broken = FakeBroker()
    broken.account = broken.positions = broken.fills_since = down
    counts = dr.archive_day(cfg, DAY, broker=broken, prices=prices_for("NVDA", "FN"))
    assert counts["broker:missing"] == 3
    assert json.loads((folder / "account.json").read_text())["equity"] == "1010"  # kept


@pytest.mark.unit
def test_a_brief_write_failure_leaves_the_day_undone(tmp_path, archive):
    cfg = config(tmp_path)
    slot(cfg)
    (archive / "ticker_briefs").mkdir(parents=True)
    (archive / "ticker_briefs" / DAY.isoformat()).write_text("a file where the folder goes")
    counts = dr.archive_day(cfg, DAY, prices=prices_for("NVDA", "FN"))
    assert counts.get("error")


@pytest.mark.unit
def test_a_change_never_spans_a_missing_close(tmp_path):
    cfg = config(tmp_path)
    slot(cfg)
    bars = {"FN": [(date(2026, 11, 2), 100.0), (date(2026, 11, 3), float("nan")), (DAY, 110.0)]}
    prices = dr.closes({"FN", "SPY"}, fetch=lambda t: bars.get(t, [(date(2026, 11, 3), 1.0), (DAY, 1.0)]))
    assert dr.day_change(prices["FN"], DAY) is None
    facts = dr.build_facts(cfg, DAY, prices=prices)
    assert not facts["us"]["missed_moves"]


@pytest.mark.unit
def test_closes_hold_only_the_days_names_and_benchmarks(tmp_path, archive):
    cfg = config(tmp_path)
    slot(cfg)
    counts = dr.archive_day(cfg, DAY, prices=prices_for("NVDA", "FN", "OLDNAME"))
    text = (archive / "prices" / DAY.isoformat() / "closes.csv").read_text()
    assert ",NVDA," in text and ",SPY," in text and "OLDNAME" not in text
    counts = dr.archive_day(cfg, DAY, prices=prices_for("NVDA", "FN"))
    assert not counts.get("prices:missing")


@pytest.mark.unit
@pytest.mark.parametrize("big", [
    {**review(), "summary": "长" * (dr.MAX_FIELD_CHARS + 1)},
    {**review(), "watch": ["x"] * (dr.MAX_ITEMS["watch"] + 1)},
])
def test_an_oversized_review_falls_back_to_the_facts(tmp_path, archive, monkeypatch, big):
    cfg = config(tmp_path)
    slot(cfg)
    monkeypatch.setattr(dr, "closes", lambda tickers, fetch=None, **k: prices_for("NVDA", "FN"))
    text = dr.review_day(cfg, DAY, send=None, runner=lambda prompt: big)
    assert "复盘模型不可用" in text and len(text) <= dr.MAX_MESSAGE_CHARS


@pytest.mark.unit
def test_the_message_stays_under_its_cap(tmp_path, archive, monkeypatch):
    cfg = config(tmp_path)
    slot(cfg)
    monkeypatch.setattr(dr, "closes", lambda tickers, fetch=None, **k: prices_for("NVDA", "FN"))
    full = {**review(), "issues": [{"title": "t" * 290, "evidence": "e" * 290, "severity": "中"}] * 8,
            "proposals": [dict.fromkeys(("change", "why", "evidence", "risk"), "p" * 290)] * 5,
            "watch": ["w" * 290] * 6}
    text = dr.review_day(cfg, DAY, send=None, runner=lambda prompt: full)
    assert len(text) <= dr.MAX_MESSAGE_CHARS and "复盘模型不可用" in text


@pytest.mark.unit
def test_a_change_never_spans_a_missing_bar(tmp_path):
    cfg = config(tmp_path)
    slot(cfg)
    spy = [(date(2026, 11, 2), 1.0), (date(2026, 11, 3), 1.0), (DAY, 1.0)]
    prices = {"SPY": spy, "FN": [(date(2026, 11, 2), 100.0), (DAY, 110.0)],
              "NVDA": [(date(2026, 11, 3), 100.0), (DAY, 103.0)]}
    facts = dr.build_facts(cfg, DAY, prices=prices)
    moves = {r["ticker"]: r["move_pct"] for r in facts["us"]["names"]}
    assert moves == {"FN": None, "NVDA": 3.0} and not facts["us"]["missed_moves"]


@pytest.mark.unit
def test_errors_without_a_usage_row_are_counted_as_unrecorded(tmp_path):
    cfg = config(tmp_path)
    write_slot(cfg, briefs={"NVDA": ticker_brief("NVDA", 7.0, "bearish — x.")}, tickers=[
        entry("NVDA", evaluate="warn", trigger="core", decide=decided(cfg, "NVDA", "ERROR", None))])
    (cfg.state_dir / "llm_usage.jsonl").write_text("")
    facts = dr.build_facts(cfg, DAY, prices=prices_for("NVDA"))
    assert facts["us"]["analysis_errors"] == 1 and facts["analysis_error_kinds"] == {"unrecorded": 1}


@pytest.mark.unit
def test_the_review_schema_survives_the_strict_form():
    from pipeline.claude_cli import strict_output_schema

    text = json.dumps(strict_output_schema(dr.REVIEW_SCHEMA))
    assert "maxLength" not in text and "maxItems" not in text


@pytest.mark.unit
def test_the_scoreboard_counts_multi_session_excess_once_closed(tmp_path):
    from datetime import timedelta

    cfg = config(tmp_path)
    slot(cfg)  # DAY: NVDA BUY (偏空 brief), FN not analysed (分歧)
    days = [date(2026, 11, 3), DAY, DAY + timedelta(days=1), DAY + timedelta(days=2), DAY + timedelta(days=5),
            DAY + timedelta(days=6)]
    nvda = [100, 101, 102, 104, 103, 99]
    spy = [100, 100, 101, 101, 102, 102]
    prices = {"NVDA": list(zip(days, map(float, nvda), strict=True)),
              "FN": list(zip(days[:3] + days[4:], (50.0, 50.0, 51.0, 49.0, 48.0), strict=True)),  # no bar on day 3
              **{b: [(d, 1.0) for d in days] for b in dr.BENCHMARKS.values()}}
    prices["SPY"] = list(zip(days, map(float, spy), strict=True))
    board = dr.build_facts(cfg, DAY, prices=prices)["scoreboard"]["horizons"]
    assert board["3d"]["decision"] == {"BUY": {"n": 1, "mean_excess_pct": 3.0}}  # 104/100 vs 101/100
    assert board["5d"]["impact"] == {"偏空": {"n": 1, "mean_excess_pct": -3.0},  # 99/100 vs 102/100
                                     "分歧": {"n": 1, "mean_excess_pct": -6.0}}  # FN 48/50: both ends present
    assert dr.forward_excess(prices, "FN", "us", DAY, 3) is None  # its end close is missing
    assert dr.forward_excess(prices, "FN", "us", DAY, 2) == pytest.approx(1.0)
    no_start = {**prices, "NVDA": prices["NVDA"][1:]}  # the close before the decision day is missing
    assert dr.forward_excess(no_start, "NVDA", "us", DAY, 3) is None


@pytest.mark.unit
def test_the_cn_benchmark_is_the_csi300_etf():
    assert dr.BENCHMARKS["cn"] == "510300.SS"
    assert dr._benchmark("600183.SS", "cn") == "510300.SS" and dr._benchmark("0700.HK", "cn") == "^HSI"


@pytest.mark.unit
def test_the_prompt_judges_on_multi_session_horizons(tmp_path, archive, monkeypatch):
    cfg = config(tmp_path)
    slot(cfg)
    monkeypatch.setattr(dr, "closes", lambda tickers, fetch=None, **k: prices_for("NVDA", "FN"))
    prompts = []
    dr.review_day(cfg, DAY, send=None, runner=lambda prompt: prompts.append(prompt) or review())
    assert "以 3d/5d 为准" in prompts[0] and '"horizons"' in prompts[0]


AFTER_ALL_CLOSES = datetime(2026, 11, 4, 22, tzinfo=timezone.utc)  # 17:00 New York, 06:00 next day in Asia
#: The day's closing trade time per market, in UTC.
CLOSE_TRADE = {"510300.SS": datetime(2026, 11, 4, 7, 0, tzinfo=timezone.utc),   # 15:00 Shanghai
               "^HSI": datetime(2026, 11, 4, 8, 8, tzinfo=timezone.utc),        # 16:08 Hong Kong
               "SPY": datetime(2026, 11, 4, 21, 0, tzinfo=timezone.utc)}        # 16:00 New York


@pytest.mark.unit
@pytest.mark.parametrize(("ticker", "series"), [
    ("510300.SS", [(date(2026, 11, 3), 4.13), (DAY, float("nan"))]),  # listed with a NaN close
    ("^HSI", [(date(2026, 11, 3), 24448.95)]),                         # not listed at all
    ("SPY", [(date(2026, 11, 3), 752.69), (DAY, float("nan"))]),
])
def test_a_late_listed_close_is_filled_from_the_quote_after_the_close(ticker, series):
    prev = series[0][1]
    quote = lambda t: (prev * 1.01, CLOSE_TRADE[t], prev)  # noqa: E731
    stats = {}
    out = dr.closes({ticker}, fetch=lambda t: series, day=DAY, quote=quote, now=AFTER_ALL_CLOSES, stats=stats)
    assert dr.day_change(out[ticker], DAY) == pytest.approx(1.0) and stats["quote_filled"] == 1
    assert stats["quote_filled_tickers"] == [ticker]


@pytest.mark.unit
def test_the_quote_never_fills_a_session_it_is_not_about():
    d1, d2 = date(2026, 11, 3), date(2026, 11, 2)
    series = [(d2, 4.130), (d1, 4.133)]  # no row for DAY
    fill = lambda q: dr.fill_day_close("510300.SS", series, DAY, AFTER_ALL_CLOSES, lambda t: q)  # noqa: E731
    on_day = CLOSE_TRADE["510300.SS"]
    assert fill((4.15, on_day, 4.133)) == (sorted([*series, (DAY, 4.15)]), True)
    # a holiday after a flat session: the last trade is the day before, whatever the prices
    assert fill((4.133, on_day - timedelta(days=1), 4.130)) == (series, False)
    # a suspended name: its last trade is days old, last == previous
    assert fill((4.133, on_day - timedelta(days=6), 4.133)) == (series, False)
    # an intraday last trade (before the close less the slack)
    assert fill((4.15, on_day - timedelta(hours=2), 4.133)) == (series, False)
    # the series misses a session: the previous close is not its last close
    assert fill((4.15, on_day, 4.20)) == (series, False)
    assert fill((float("nan"), on_day, 4.133)) == (series, False)
    before_read = datetime(2026, 11, 4, 7, 10, tzinfo=timezone.utc)  # 15:10 Shanghai: before the margin
    assert dr.fill_day_close("510300.SS", series, DAY, before_read, lambda t: (4.15, on_day, 4.133)) == (series, False)
    broken = lambda t: (_ for _ in ()).throw(ConnectionError("down"))  # noqa: E731
    assert dr.fill_day_close("510300.SS", series, DAY, AFTER_ALL_CLOSES, broken) == (series, False)
    later = [*series, (date(2026, 11, 5), 4.2)]  # a later bar: a past-day run
    assert dr.fill_day_close("510300.SS", later, DAY, AFTER_ALL_CLOSES, lambda t: (4.15, on_day, 4.133))[1] is False
    kept = [*series, (DAY, 4.14)]  # a listed close is never replaced
    assert dr.fill_day_close("510300.SS", kept, DAY, AFTER_ALL_CLOSES, lambda t: (4.15, on_day, 4.133)) == (kept, False)


@pytest.mark.unit
def test_only_the_benchmarks_and_the_days_names_ask_for_a_quote(tmp_path):
    cfg = config(tmp_path)
    slot(cfg)  # DAY: NVDA, FN
    funnel = cfg.state_dir / "funnel" / "us"
    (funnel / f"{DAY.replace(day=1).isoformat()}.json").write_text(json.dumps({"tickers": [{"ticker": "OLD"}]}))
    asked = []

    def quote(t):
        asked.append(t)
        raise RuntimeError("offline")

    import functools
    from unittest import mock

    with mock.patch.object(dr, "_yahoo_quote", quote), \
            mock.patch.object(dr, "closes", functools.partial(dr.closes, now=AFTER_ALL_CLOSES)):
        dr.history_prices(cfg, DAY, fetch=lambda t: [(date(2026, 11, 3), 1.0)])
    assert sorted(asked) == sorted({*dr.BENCHMARKS.values(), "NVDA", "FN"})


@pytest.mark.unit
def test_the_core_off_session_is_marked_as_configuration(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADINGAGENTS_CORE_ANALYSIS_SESSIONS", "us")
    cfg = config(tmp_path)
    slot(cfg)
    facts = dr.build_facts(cfg, DAY, prices=prices_for("NVDA", "FN"))
    assert facts["us"]["core_analysed"] is True and facts["cn"]["core_analysed"] is False
    assert "core-session-off" in dr.REVIEW_PROMPT and "不要当作问题" in dr.REVIEW_PROMPT


@pytest.mark.unit
def test_the_quote_reader_maps_yahoos_chart_metadata(monkeypatch):
    import yfinance

    class FakeTicker:
        history_metadata = {"regularMarketPrice": 749.92, "regularMarketTime": 1794344400, "previousClose": 752.69}

        def __init__(self, ticker):
            pass

        def history(self, **kwargs):
            return None

    monkeypatch.setattr(yfinance, "Ticker", FakeTicker)
    price, traded, previous = dr._yahoo_quote.real("SPY")
    assert (price, previous) == (749.92, 752.69)
    assert traded == datetime(2026, 11, 10, 21, 0, tzinfo=timezone.utc)


@pytest.mark.unit
def test_a_shortened_session_is_not_filled():
    series = [(date(2026, 11, 3), 752.69)]
    early = datetime(2026, 11, 4, 18, 0, tzinfo=timezone.utc)  # 13:00 New York: an early close's last trade
    assert dr.fill_day_close("SPY", series, DAY, AFTER_ALL_CLOSES, lambda t: (760.0, early, 752.69)) == (series, False)


@pytest.mark.unit
def test_a_junk_core_sessions_config_leaves_core_analysed_unknown(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADINGAGENTS_CORE_ANALYSIS_SESSIONS", "jp")
    cfg = config(tmp_path)
    slot(cfg)
    facts = dr.build_facts(cfg, DAY, prices=prices_for("NVDA", "FN"))
    assert facts["us"]["core_analysed"] is None and facts["cn"]["core_analysed"] is None


@pytest.mark.unit
def test_an_archive_that_fetches_for_itself_names_its_quote_fills(tmp_path, archive, monkeypatch):
    import functools

    cfg = config(tmp_path)
    slot(cfg)
    series = {"SPY": [(date(2026, 11, 3), 752.69), (DAY, float("nan"))]}
    monkeypatch.setattr(dr, "closes", functools.partial(dr.closes, now=AFTER_ALL_CLOSES,
                                                        quote=lambda t: (760.0, CLOSE_TRADE["SPY"], 752.69)))
    dr.archive_day(cfg, DAY, fetch=lambda t: series.get(t, [(date(2026, 11, 3), 1.0), (DAY, 1.0)]))
    assert json.loads((archive / "prices" / DAY.isoformat() / "quote_filled.json").read_text()) == ["SPY"]
    marker = archive / "prices" / DAY.isoformat() / "quote_filled.json"
    dr.archive_day(cfg, DAY, fetch=lambda t: (_ for _ in ()).throw(OSError("down")))  # a failed refetch
    assert json.loads(marker.read_text()) == ["SPY"]  # the quote-derived row is still there, so is its name
    series["SPY"] = [(date(2026, 11, 3), 752.69), (DAY, 761.0)]  # Yahoo lists the close now
    dr.archive_day(cfg, DAY, fetch=lambda t: series.get(t, [(date(2026, 11, 3), 1.0), (DAY, 1.0)]))
    assert json.loads(marker.read_text()) == []


@pytest.mark.unit
def test_a_duplicate_date_with_an_unusable_close_never_breaks_the_fill():
    d1, d2 = date(2026, 11, 2), date(2026, 11, 3)
    series = [(d1, None), (d1, 4.10), (d2, 4.13)]  # Yahoo listed 11-02 twice, once without a close
    out, filled = dr.fill_day_close("510300.SS", series, DAY, AFTER_ALL_CLOSES,
                                    lambda t: (4.15, CLOSE_TRADE["510300.SS"], 4.13))
    assert filled and out[-1] == (DAY, 4.15) and [d for d, _c in out] == [d1, d1, d2, DAY]


@pytest.mark.unit
@pytest.mark.parametrize(("ticker", "trade_utc", "market"), [
    ("0700.HK", datetime(2026, 11, 4, 8, 8, tzinfo=timezone.utc), "hk"),     # 16:08 Hong Kong
    ("300750.SZ", datetime(2026, 11, 4, 7, 0, tzinfo=timezone.utc), "cn"),   # 15:00 Shenzhen
    ("600183.SS", datetime(2026, 11, 4, 7, 0, tzinfo=timezone.utc), "cn"),
])
def test_hong_kong_and_shenzhen_names_use_their_own_close(ticker, trade_utc, market):
    assert dr._market(ticker) == market
    series = [(date(2026, 11, 3), 100.0)]
    out, filled = dr.fill_day_close(ticker, series, DAY, AFTER_ALL_CLOSES, lambda t: (101.0, trade_utc, 100.0))
    assert filled and out[-1] == (DAY, 101.0)
    # judged on New York hours, the same trade would be hours before the close: never filled
    assert dr.fill_day_close("SPY", series, DAY, AFTER_ALL_CLOSES, lambda t: (101.0, trade_utc, 100.0))[1] is False


@pytest.mark.unit
def test_the_reviews_own_quote_fills_are_named_in_the_archive(tmp_path, archive, monkeypatch):
    cfg = config(tmp_path)
    slot(cfg)

    def read(tickers, fetch=None, budget_s=0, *, first=(), stats=None, **k):
        if stats is not None:
            stats.update(skipped=0, failed=0, quote_filled=1, quote_filled_tickers=["SPY"])
        return prices_for("NVDA", "FN")

    monkeypatch.setattr(dr, "closes", read)
    dr.review_day(cfg, DAY, send=None, runner=lambda prompt: review())
    assert json.loads((archive / "prices" / DAY.isoformat() / "quote_filled.json").read_text()) == ["SPY"]


@pytest.mark.unit
def test_a_retry_that_reads_a_listed_close_clears_the_saved_quote_fill(tmp_path, archive, monkeypatch):
    cfg = config(tmp_path)
    slot(cfg)
    marker = archive / "prices" / DAY.isoformat() / "quote_filled.json"
    marker.parent.mkdir(parents=True)
    marker.write_text(json.dumps(["SPY"]))
    # the saved review says SPY came from the quote; the retry's own read finds it listed
    monkeypatch.setattr(dr, "closes", lambda tickers, fetch=None, **k: prices_for("NVDA", "FN"))
    dr.archive_day(cfg, DAY, prices=None, quote_filled=["SPY"])
    assert json.loads(marker.read_text()) == []


@pytest.mark.unit
@pytest.mark.parametrize("junk", ['[1, "SPY"]', "[null]", '{"SPY": 1}', "not json"])
def test_a_damaged_marker_never_breaks_the_archive(tmp_path, archive, junk):
    cfg = config(tmp_path)
    slot(cfg)
    marker = archive / "prices" / DAY.isoformat() / "quote_filled.json"
    marker.parent.mkdir(parents=True)
    marker.write_text(junk)
    counts = dr.archive_day(cfg, DAY, prices=prices_for("NVDA", "FN"), quote_filled=["SPY"])
    assert not counts.get("error") and json.loads(marker.read_text()) == ["SPY"]


@pytest.mark.unit
def test_quote_fills_are_named_even_when_the_facts_build_fails(tmp_path, archive, monkeypatch):
    cfg = config(tmp_path)
    slot(cfg)

    def read(tickers, fetch=None, budget_s=0, *, first=(), stats=None, **k):
        stats.update(skipped=0, failed=0, quote_filled=1, quote_filled_tickers=["SPY"])
        return prices_for("NVDA", "FN")

    def broken(*a, **k):
        raise RuntimeError("facts")

    monkeypatch.setattr(dr, "closes", read)
    monkeypatch.setattr(dr, "build_facts", broken)
    dr.review_day(cfg, DAY, send=None, runner=lambda prompt: review())
    assert json.loads((archive / "prices" / DAY.isoformat() / "quote_filled.json").read_text()) == ["SPY"]


@pytest.mark.unit
def test_failed_reads_and_unfilled_closes_are_named_with_their_reason():
    def fetch(ticker):
        if ticker == "NUVL":
            raise LookupError("delisted")
        if ticker == "^HSI":
            return [(date(2026, 11, 2), 24000.0)]  # the session before the day is missing too
        return [(date(2026, 11, 3), 1.0), (DAY, 1.0)]

    stats = {}
    dr.closes({"NUVL", "^HSI", "SPY"}, fetch=fetch, day=DAY, now=AFTER_ALL_CLOSES, stats=stats,
              quote=lambda t: (24100.0, CLOSE_TRADE["^HSI"], 24050.0))
    assert stats["failed_tickers"] == {"NUVL": "LookupError"}
    assert stats["unfilled"] == {"^HSI": "missed-session"}
    reasons = {}
    dr.fill_day_close("^HSI", [(date(2026, 11, 3), 24050.0)], DAY, AFTER_ALL_CLOSES,
                      lambda t: (24100.0, CLOSE_TRADE["^HSI"] - timedelta(days=1), 24050.0), reasons)
    assert reasons == {"^HSI": "quote-trade-date:2026-11-03"}


@pytest.mark.unit
def test_the_message_names_failed_reads_and_unfilled_closes():
    facts = {"date": DAY.isoformat(), "us": {}, "cn": {},
             "price_reads": {"skipped": 0, "failed": 1, "failed_tickers": {"NUVL": "LookupError"},
                             "unfilled": {"^HSI": "missed-session"}}}
    text = dr.render(DAY, facts, None)
    assert "失败：NUVL（LookupError）" in text and "当天收盘未补上：^HSI（missed-session）" in text


@pytest.mark.unit
def test_a_likely_holiday_is_a_note_and_long_lists_are_counted():
    unfilled = {"^HSI": "quote-trade-date:2026-11-03", "510300.SS": "missed-session",
                **{f"T{i}": "quote-unusable" for i in range(6)}}
    facts = {"date": DAY.isoformat(), "us": {}, "cn": {}, "price_reads": {"skipped": 0, "failed": 0, "unfilled": unfilled}}
    text = dr.render(DAY, facts, None)
    assert "ℹ️ 可能休市（报价停在前一交易日）：^HSI（quote-trade-date:2026-11-03）" in text
    assert "⚠️ 当天收盘未补上：510300.SS（missed-session）" in text and "等 7 只" in text


@pytest.mark.unit
def test_a_day_skipped_between_two_listed_sessions_reads_as_no_session():
    stats = {}
    dr.closes({"^HSI"}, fetch=lambda t: [(date(2026, 11, 3), 24000.0), (date(2026, 11, 5), 24100.0)],
              day=DAY, now=AFTER_ALL_CLOSES, stats=stats, quote=lambda t: (_ for _ in ()).throw(AssertionError()))
    assert stats["unfilled"] == {"^HSI": "no-session"}
    facts = {"date": DAY.isoformat(), "us": {}, "cn": {}, "price_reads": {"skipped": 0, "failed": 0, **stats}}
    assert "ℹ️ 可能休市（报价停在前一交易日）：^HSI（no-session）" in dr.render(DAY, facts, None)


@pytest.mark.unit
def test_a_listed_day_with_an_unusable_close_is_a_fault_not_a_holiday():
    stats = {}
    dr.closes({"^HSI"}, fetch=lambda t: [(date(2026, 11, 3), 24000.0), (DAY, float("nan")), (date(2026, 11, 5), 24100.0)],
              day=DAY, now=AFTER_ALL_CLOSES, stats=stats)
    assert stats["unfilled"] == {"^HSI": "later-bar"}
