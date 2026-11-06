"""Daily post-close review and archive — specs/daily-review.md."""

import json
from datetime import date, datetime, timezone

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
    for t in (*tickers, "SPY", "000300.SS", "^HSI"):
        m = moves.get(t, 0.0)
        out[t] = [(date(2026, 11, 3), 100.0), (DAY, 100.0 * (1 + m / 100))]
    return out


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
    monkeypatch.setattr(dr, "closes", lambda tickers, fetch=None: prices_for("NVDA", "FN"))
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
    monkeypatch.setattr(dr, "closes", lambda tickers, fetch=None: prices_for("NVDA", "FN", moves={"FN": 8.0}))
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
    assert not dr.maybe_start(cfg, at(16, 59), spawn=spawned.append)  # before 17:00 ET
    assert dr.maybe_start(cfg, at(17, 5), spawn=spawned.append) and spawned == [friday]
    assert not dr.maybe_start(cfg, at(18), spawn=spawned.append)  # started < 2 h ago
    assert dr.maybe_start(cfg, at(19, 30), spawn=spawned.append)  # stuck: retried
    _started, done = dr._marks(cfg, friday)
    done.write_text("x")
    assert not dr.maybe_start(cfg, at(21), spawn=spawned.append) and len(spawned) == 2
    saturday = datetime(2026, 11, 7, 23, tzinfo=timezone.utc)
    assert not dr.maybe_start(cfg, saturday, spawn=spawned.append)
    other = config(tmp_path / "empty")  # no us snapshot for the day: nothing to review
    assert not dr.maybe_start(other, at(18), spawn=spawned.append)


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
