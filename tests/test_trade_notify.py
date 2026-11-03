"""Trade notifications — specs/trade-notifications.md (offline: fake broker, stream and sender)."""

import json
from datetime import datetime, timezone

import pytest

from pipeline import trade_notify as tn

NOW = datetime(2026, 10, 30, 21, 0, tzinfo=timezone.utc)  # 17:00 ET, after the close


def order(oid, cid, *, side="buy", type_="limit", status="new", qty=5, filled_qty=0, avg=None,
          limit=940.0, stop=None, legs=(), order_class="bracket", symbol="MU"):
    return {"id": oid, "client_order_id": cid, "symbol": symbol, "side": side, "order_type": type_,
            "order_class": order_class, "status": status, "qty": str(qty), "filled_qty": str(filled_qty),
            "filled_avg_price": None if avg is None else str(avg), "limit_price": None if limit is None else str(limit),
            "stop_price": None if stop is None else str(stop), "legs": list(legs)}


def entry(status="new", **kw):
    legs = [order("L-stop", "leg-a", side="sell", type_="stop", status=kw.pop("stop_status", "held"),
                  limit=None, stop=850.0, filled_qty=kw.pop("stop_filled", 0), avg=kw.pop("stop_avg", None)),
            order("L-tgt", "leg-b", side="sell", type_="limit", status=kw.pop("tgt_status", "held"),
                  limit=980.38, filled_qty=kw.pop("tgt_filled", 0), avg=kw.pop("tgt_avg", None))]
    return order("E1", "2026-10-30-us-MU-entry", status=status, legs=legs, **kw)


class Sender:
    def __init__(self, fail=0):
        self.sent, self.fail = [], fail

    def __call__(self, title, message):
        if self.fail:
            self.fail -= 1
            raise RuntimeError("boom https://open.feishu.cn/secret-token")
        self.sent.append((title, message))


class Broker:
    def __init__(self, orders=(), positions=(), trading_day=True, equity=10_100.0, last=10_000.0,
                 fills=(), by_id=None):
        self.orders, self._positions, self.trading_day = list(orders), list(positions), trading_day
        self.acct = {"equity": str(equity), "last_equity": str(last), "account_number": "PA123"}
        self.fills, self.by_id = list(fills), dict(by_id or {})

    def orders_since(self, after):
        return self.orders

    def fills_since(self, after):
        return self.fills

    def order(self, order_id):
        return self.by_id[order_id]

    def open_orders(self):
        return [o for o in self.orders if o["status"] in ("new", "accepted", "held")]

    def positions(self):
        return self._positions

    def account(self):
        return self.acct

    def is_trading_day(self, day):
        return self.trading_day


# ---------------------------------------------------------------------------
# T2 — mapping
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_kinds_and_stream_events():
    view = tn.order_view(entry())
    assert (view.kind, [leg.kind for leg in view.legs]) == ("entry", ["stop", "target"])
    assert tn.order_view(order("C", "2026-11-02-us-MU-close-1", side="sell")).kind == "close"
    manual = order("M", "manual-sell", side="sell", type_="limit", order_class="simple")
    assert tn.order_view(manual).kind == "other"  # never a bracket target
    assert tn.event_from_update({"event": "fill", "order": {**manual, "status": "filled"}}) is None
    new = tn.event_from_update({"event": "accepted", "order": entry()})
    assert (new.event, new.key) == ("new", "E1:new")
    fill = tn.event_from_update({"event": "fill", "order": entry("filled", filled_qty=5, avg=893.92),
                                 "price": "893.92", "qty": "1", "position_qty": "5"})
    assert (fill.event, fill.key, fill.order.filled_qty) == ("fill", "E1:fill", 5.0)
    # Partial fills are not messages (orders fill in several executions).
    assert tn.event_from_update({"event": "partial_fill", "order": entry("partially_filled", filled_qty=4),
                                 "price": "893.92", "qty": "4"}) is None
    leg_cancel = {"event": "canceled", "order": order("L-stop", "leg-a", side="sell", type_="stop", status="canceled")}
    assert tn.event_from_update(leg_cancel) is None  # sibling filled: not news
    assert tn.event_from_update({"event": "replaced", "order": entry()}) is None
    assert tn.event_from_update({"event": "new", "order": order("X", "manual-1")}) is None


@pytest.mark.unit
def test_snapshot_events_match_the_stream_keys():
    snapshot = entry("filled", filled_qty=5, avg=893.92, tgt_status="filled", tgt_filled=5, tgt_avg=980.5,
                     stop_status="canceled")
    assert [e.key for e in tn.events_from_orders([snapshot])] == ["E1:new"]  # fills come from activities
    rejected = tn.events_from_orders([order("E2", "2026-11-02-us-AMD-entry", status="rejected", symbol="AMD")])
    assert [e.key for e in rejected] == ["E2:rejected"]  # a rejected order never was "new"
    activities = [  # as /v2/account/activities/FILL returns them (measured)
        {"order_id": "E1", "type": "partial_fill", "order_status": "partially_filled", "qty": "4"},
        {"order_id": "E1", "type": "fill", "order_status": "filled", "qty": "1"},
        {"order_id": "L-tgt", "type": "fill", "order_status": "filled", "qty": "5"},
        {"order_id": "gone", "type": "fill", "order_status": "filled"},
    ]
    legs = {leg["id"]: leg for leg in snapshot["legs"]}
    by_id = {"E1": snapshot, **legs}
    events = tn.events_from_fills(activities, lambda oid: by_id[oid])
    assert [(e.key, e.order.filled_qty) for e in events] == [("E1:fill", 5.0), ("L-tgt:fill", 5.0)]


# ---------------------------------------------------------------------------
# T3 — exactly once; T2 — messages and P&L
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_stream_then_snapshot_sends_each_event_once_with_pnl(tmp_path):
    send = Sender()
    held = [{"symbol": "MU", "avg_entry_price": "893.92", "qty": "5"}]
    positions = {"now": held}

    def stream(event_dict):  # as the listener's handler: None (not reportable) is dropped
        event = tn.event_from_update(event_dict)
        if event is not None:
            tn.deliver([event], state_dir=tmp_path, send=send, positions=lambda: positions["now"])

    stream({"event": "new", "order": entry()})
    stream({"event": "partial_fill", "order": entry("partially_filled", filled_qty=4, avg=893.92),
            "price": "893.92", "qty": "4"})
    stream({"event": "fill", "order": entry("filled", filled_qty=5, avg=893.92),
            "price": "893.92", "qty": "1", "position_qty": "5"})  # the last execution: 1 share
    positions["now"] = []  # the target leg closed the position
    target = order("L-tgt", "leg-b", side="sell", status="filled", filled_qty=5, avg=980.5, limit=980.38)
    stream({"event": "fill", "order": target, "price": "980.5", "qty": "2", "position_qty": "0"})
    snapshot = entry("filled", filled_qty=5, avg=893.92, tgt_status="filled", tgt_filled=5, tgt_avg=980.5,
                     stop_status="canceled")
    broker = Broker(orders=[snapshot], fills=[{"order_id": "E1", "order_status": "filled"},
                                              {"order_id": "L-tgt", "order_status": "filled"}],
                    by_id={"E1": snapshot, "L-tgt": target})
    tn.SentStore(tmp_path).mark_initialized()
    assert tn.reconcile(broker, state_dir=tmp_path, send=send, now=NOW) == 0  # all seen
    messages = [m for _, m in send.sent]
    assert len(messages) == 3
    assert messages[0].startswith("📝 下单 MU 买入 5 股 @ 限价 940.00（止损 850.00 / 止盈 980.38）")
    assert messages[1] == "✅ 成交 MU 买入 5 股 @ 均价 893.92（限价 940.00）"  # totals, not the last slice
    assert messages[2].startswith("🔚 止盈平仓 MU 卖出 5 股 @ 均价 980.50")
    assert "盈亏 +$432.90（+9.69%，成本 893.92）" in messages[2]
    assert tn.Entries(tmp_path).load() == {}


@pytest.mark.unit
def test_an_exit_without_a_known_entry_has_no_guessed_pnl(tmp_path):
    send = Sender()
    event = tn.event_from_update({"event": "fill", "order": order("L-stop", "leg-a", side="sell", type_="stop",
                                                                  status="filled", filled_qty=5, avg=849.0, limit=None,
                                                                  stop=850.0), "price": "849", "qty": "5"})
    tn.deliver([event], state_dir=tmp_path, send=send)
    assert send.sent[0][1] == "🔚 止损平仓 MU 卖出 5 股 @ 均价 849.00"


@pytest.mark.unit
def test_a_partly_filled_entry_that_expires_says_so_and_books_the_entry(tmp_path):
    send = Sender()
    expired = entry("expired", filled_qty=4, avg=893.92)
    tn.deliver([tn.event_from_update({"event": "expired", "order": expired})], state_dir=tmp_path, send=send,
               positions=lambda: [{"symbol": "MU", "avg_entry_price": "893.92", "qty": "4"}])
    assert send.sent[0][1].splitlines()[0] == "⚠️ 过期 MU 买入 5 股 @ 限价 940.00（已成交 4 股，均价 893.92）"
    assert tn.Entries(tmp_path).load() == {"MU": {"avg": 893.92, "qty": 4.0}}


@pytest.mark.unit
def test_a_failed_send_is_retried_and_never_leaks_the_url(tmp_path, caplog):
    send = Sender(fail=1)
    event = tn.event_from_update({"event": "new", "order": entry()})
    assert tn.deliver([event], state_dir=tmp_path, send=send) == 0
    assert "secret-token" not in caplog.text and "RuntimeError" in caplog.text
    assert tn.deliver([event], state_dir=tmp_path, send=send) == 1  # the next reconcile retries
    assert tn.deliver([event], state_dir=tmp_path, send=send) == 0


@pytest.mark.unit
def test_entry_message_carries_the_executed_runs_plan(tmp_path):
    ledger = tmp_path / "ledger"
    ledger.mkdir()

    def row(run_id, arm, conviction, valid=True):
        return {"run_id": run_id, "date": "2026-10-30", "session": "us", "ticker": "MU", "decision": "BUY",
                "arm": arm, "plan_valid": valid, "catalyst_score": 7.5,
                "plan": {"conviction": conviction, "invalidation": "Close below the 856.49 October low."}}

    rows = [row("2026-10-30-us-MU-brief-1", "brief", "0.7"), row("2026-10-30-us-MU-feeds-1", "feeds", "0.2"),
            row("2026-10-30-us-MU-brief-2", "brief", "0.3", valid=False)]
    (ledger / "decisions.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    send = Sender()
    tn.deliver([tn.event_from_update({"event": "new", "order": entry()})], state_dir=tmp_path, send=send)
    # No order link: the last executable row (brief arm, valid) — never feeds or invalid.
    assert send.sent[0][1].splitlines()[1] == "信心 0.7；催化剂 7.5；失效条件：Close below the 856.49 October low."
    cid = "2026-10-30-us-MU-entry"
    (ledger / "orders.jsonl").write_text("".join(json.dumps(r) + "\n" for r in [
        {"run_id": "2026-10-30-us-MU-brief-2", "client_order_id": cid, "kind": "skip", "dry_run": False},
        {"run_id": "2026-10-30-us-MU-feeds-1", "client_order_id": cid, "kind": "intent", "dry_run": True},
        {"run_id": "2026-10-30-us-MU-brief-1", "client_order_id": cid, "kind": "intent", "dry_run": False},
        {"run_id": "2026-10-30-us-MU-brief-1", "client_order_id": cid, "kind": "submitted", "dry_run": False},
    ]))
    rows.append(row("2026-10-30-us-MU-brief-3", "brief", "0.9"))  # a later run that was not executed
    (ledger / "decisions.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    assert tn.plan_line(ledger, "2026-10-30-us-MU-entry").startswith("信心 0.7；")  # the linked run


@pytest.mark.unit
def test_alpaca_read_only_calendar_and_open_orders(monkeypatch):
    from datetime import date

    class Day:
        def __init__(self, d):
            self.date = d

    class Client:
        def __init__(self):
            self.requests = []

        def get_calendar(self, request):
            return [Day(date(2026, 11, 27))]  # the NEXT session, as some SDKs answer for a holiday

        def get_orders(self, filter):
            self.requests.append(filter)
            return []

    broker = tn.AlpacaReadOnly.__new__(tn.AlpacaReadOnly)
    broker._client = Client()
    assert broker.is_trading_day(date(2026, 11, 26)) is False
    assert broker.is_trading_day(date(2026, 11, 27)) is True
    broker.open_orders()
    assert broker._client.requests[0].nested is True


# ---------------------------------------------------------------------------
# T5 — reconcile; T6 — daily report
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_first_reconcile_records_history_without_sending(tmp_path):
    send = Sender()
    filled = entry("filled", filled_qty=5, avg=893.92)
    broker = Broker(orders=[filled], positions=[{"symbol": "MU", "avg_entry_price": "893.92", "qty": "5"}],
                    fills=[{"order_id": "E1", "order_status": "filled"}], by_id={"E1": filled})
    assert tn.reconcile(broker, state_dir=tmp_path, send=send, now=NOW) == 0
    assert send.sent == [] and tn.Entries(tmp_path).load() == {"MU": {"avg": 893.92, "qty": 5.0}}
    broker.orders.append(order("E2", "2026-11-02-us-AMD-entry", status="new", symbol="AMD", legs=()))
    assert tn.reconcile(broker, state_dir=tmp_path, send=send, now=NOW) == 1
    assert send.sent[0][1].startswith("📝 下单 AMD")


@pytest.mark.unit
def test_an_empty_first_run_still_ends_the_first_run(tmp_path):
    send = Sender()
    broker = Broker()
    assert tn.reconcile(broker, state_dir=tmp_path, send=send, now=NOW) == 0  # nothing to record
    broker.orders.append(order("E2", "2026-11-02-us-AMD-entry", status="new", symbol="AMD", legs=()))
    assert tn.reconcile(broker, state_dir=tmp_path, send=send, now=NOW) == 1  # sent, not swallowed


@pytest.mark.unit
@pytest.mark.parametrize(("now", "trading", "sent"), [
    (datetime(2026, 10, 30, 21, 0, tzinfo=timezone.utc), True, True),    # 17:00 ET
    (datetime(2026, 10, 30, 19, 0, tzinfo=timezone.utc), True, False),   # 15:00 ET: too early
    (datetime(2026, 10, 30, 21, 0, tzinfo=timezone.utc), False, False),  # holiday
])
def test_daily_report_once_after_the_close(tmp_path, now, trading, sent):
    send = Sender()
    tn.SentStore(tmp_path).mark_initialized()  # not a first run
    broker = Broker(positions=[{"symbol": "MU", "qty": "5", "avg_entry_price": "893.92", "current_price": "920",
                                "unrealized_pl": "130.4", "unrealized_plpc": "0.02917"}], trading_day=trading)
    for _ in range(2):
        tn.run_hourly(state_dir=tmp_path, send=send, now=now, broker_factory=lambda: broker)
    reports = [m for _, m in send.sent if m.startswith("📊")]
    assert len(reports) == (1 if sent else 0)
    if sent:
        assert reports[0].splitlines()[0] == "📊 日报 权益 $10,100.00（今日 +$100.00，+1.00%）"
        assert reports[0].splitlines()[1] == "• MU 5 股，成本 893.92，现价 920.00，浮动 +$130.40（+2.92%）"
        assert reports[0].splitlines()[2] == "挂单 0 笔"


@pytest.mark.unit
def test_hourly_without_a_sender_or_with_a_broken_broker_never_raises(tmp_path, caplog):
    tn.run_hourly(state_dir=tmp_path, send=None, now=NOW, broker_factory=lambda: 1 / 0)
    tn.run_hourly(state_dir=tmp_path, send=Sender(), now=NOW, broker_factory=lambda: 1 / 0)
    assert "ZeroDivisionError" in caplog.text


# ---------------------------------------------------------------------------
# T1 config, T4 listener, scheduling, watchdog
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_trades_webhook_config(monkeypatch, caplog):
    from pipeline.config import load_config

    env = {"TRADINGAGENTS_LARK_TRADES_WEBHOOK_URL": "https://open.feishu.cn/open-apis/bot/v2/hook/abc",
           "TRADINGAGENTS_LARK_TRADES_WEBHOOK_SECRET": "s3cret"}
    config = load_config(env=env)
    assert config.lark_trades_webhook_url.endswith("/abc") and "abc" not in repr(config)
    bad = load_config(env={"TRADINGAGENTS_LARK_TRADES_WEBHOOK_URL": "http://insecure"})
    assert bad.lark_trades_webhook_url is None and "trade notifications disabled" in caplog.text


class FakeStream:
    instances = []

    def __init__(self, updates):
        self.updates, self.handler = updates, None
        FakeStream.instances.append(self)

    def subscribe_trade_updates(self, handler):
        self.handler = handler

    def run(self):
        import asyncio

        for update in self.updates:
            asyncio.run(self.handler(update))


@pytest.mark.unit
def test_listener_sends_stream_events_and_runs_single_instance(tmp_path, monkeypatch):
    from pipeline import trade_monitor

    monkeypatch.setenv("TRADINGAGENTS_SKIP_DOTENV", "1")
    monkeypatch.setenv("TRADINGAGENTS_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("ALPACA_API_KEY", "k")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "s")
    assert trade_monitor.main([]) == 0  # no trades webhook: not started
    monkeypatch.setenv("TRADINGAGENTS_LARK_TRADES_WEBHOOK_URL", "https://open.feishu.cn/hook/x")
    monkeypatch.setenv("TRADINGAGENTS_NOTIFICATIONS_ENABLED", "false")
    assert trade_monitor.main([], stream_factory=lambda: pytest.fail("started while muted")) == 0
    monkeypatch.delenv("TRADINGAGENTS_NOTIFICATIONS_ENABLED")
    monkeypatch.setenv("TRADINGAGENTS_LARK_TRADES_WEBHOOK_URL", "https://open.feishu.cn/hook/x")
    send = Sender()
    monkeypatch.setattr(trade_monitor, "make_trades_sender", lambda config: send)
    broker = Broker(positions=[])
    tn.SentStore(tmp_path).mark_initialized()
    updates = [{"event": "new", "order": entry()}, {"event": "new", "order": entry()}]  # duplicate delivery
    code = trade_monitor.main([], stream_factory=lambda: FakeStream(updates), broker_factory=lambda: broker)
    assert code == 1 and [m.split(" @")[0] for _, m in send.sent] == ["📝 下单 MU 买入 5 股"]
    held = trade_monitor.acquire_single_instance(tmp_path)
    assert trade_monitor.main([], stream_factory=lambda: FakeStream([]), broker_factory=lambda: broker) == 0
    held.close()


@pytest.mark.unit
def test_installer_adds_the_listener_task_only_with_the_trades_webhook(tmp_path):
    from pipeline import install_schedule as inst
    from pipeline.config import load_config

    base = load_config(env={"TRADINGAGENTS_STATE_DIR": str(tmp_path)})
    tasks = inst.generate_windows_tasks(base, "Asia/Shanghai", 2026, "Ubuntu", "u")
    assert not any("trade-monitor" in name for name in tasks)
    with_trades = load_config(env={"TRADINGAGENTS_STATE_DIR": str(tmp_path),
                                   "TRADINGAGENTS_LARK_TRADES_WEBHOOK_URL": "https://open.feishu.cn/hook/x"})
    tasks = inst.generate_windows_tasks(with_trades, "Asia/Shanghai", 2026, "Ubuntu", "u")
    name = next(name for name in tasks if "trade-monitor" in name)
    assert "pipeline.trade_monitor" in tasks[name] and "open.feishu.cn" not in tasks[name]


@pytest.mark.unit
def test_watchdog_runs_the_hourly_trade_share_only_with_the_webhook(tmp_path, monkeypatch):
    from pipeline import orchestrator
    from pipeline.config import load_config

    calls = []
    monkeypatch.setattr(tn, "run_hourly", lambda **kw: calls.append(kw))
    config = load_config(env={"TRADINGAGENTS_STATE_DIR": str(tmp_path)})
    orchestrator._hourly_trade_notifications(config, NOW)
    assert calls == []
    config = load_config(env={"TRADINGAGENTS_STATE_DIR": str(tmp_path),
                              "TRADINGAGENTS_LARK_TRADES_WEBHOOK_URL": "https://open.feishu.cn/hook/x"})
    orchestrator._hourly_trade_notifications(config, NOW)
    assert len(calls) == 1 and calls[0]["now"] == NOW


@pytest.mark.unit
def test_fallback_never_takes_a_later_positions_cost_basis(tmp_path):
    """Listener down while MU entered at 893.92, stopped out, re-entered at 950."""
    tn.SentStore(tmp_path).mark_initialized()
    e1 = entry("filled", filled_qty=5, avg=893.92, stop_status="filled", stop_filled=5, stop_avg=849.55,
               tgt_status="canceled")
    stop_leg = next(leg for leg in e1["legs"] if leg["id"] == "L-stop")
    e2 = {**order("E2", "2026-11-03-us-MU-entry", status="filled", filled_qty=5, avg=950.0, legs=())}
    broker = Broker(orders=[e1, e2], positions=[{"symbol": "MU", "avg_entry_price": "950", "qty": "5"}],
                    fills=[{"order_id": "E1", "order_status": "filled"}, {"order_id": "L-stop", "order_status": "filled"},
                           {"order_id": "E2", "order_status": "filled"}],
                    by_id={"E1": e1, "L-stop": stop_leg, "E2": e2})
    send = Sender()
    tn.reconcile(broker, state_dir=tmp_path, send=send, now=NOW)
    exit_msg = next(m for _, m in send.sent if m.startswith("🔚"))
    assert "成本 893.92" in exit_msg and "−$221.85" in exit_msg  # not against today's 950
    assert tn.Entries(tmp_path).load() == {"MU": {"avg": 950.0, "qty": 5.0}}


@pytest.mark.unit
def test_a_failed_send_stops_the_batch_so_order_is_kept(tmp_path):
    tn.SentStore(tmp_path).mark_initialized()
    e1 = entry("filled", filled_qty=5, avg=893.92, stop_status="filled", stop_filled=5, stop_avg=849.55,
               tgt_status="canceled")
    stop_leg = next(leg for leg in e1["legs"] if leg["id"] == "L-stop")
    broker = Broker(orders=[], fills=[{"order_id": "E1", "order_status": "filled"},
                                      {"order_id": "L-stop", "order_status": "filled"}],
                    by_id={"E1": e1, "L-stop": stop_leg})
    send = Sender(fail=1)
    assert tn.reconcile(broker, state_dir=tmp_path, send=send, now=NOW) == 0  # entry failed: exit not sent yet
    assert tn.reconcile(broker, state_dir=tmp_path, send=send, now=NOW) == 2
    assert "成本 893.92" in send.sent[1][1]


@pytest.mark.unit
def test_a_failed_send_does_not_align_the_book_before_the_retry(tmp_path):
    """E1 893.92 → stop 849.55 → E2 re-entry 950 held now; E1's message fails once."""
    tn.SentStore(tmp_path).mark_initialized()
    e1 = entry("filled", filled_qty=5, avg=893.92, stop_status="filled", stop_filled=5, stop_avg=849.55,
               tgt_status="canceled")
    stop_leg = next(leg for leg in e1["legs"] if leg["id"] == "L-stop")
    e2 = order("E2", "2026-11-03-us-MU-entry", status="filled", filled_qty=5, avg=950.0, legs=())
    broker = Broker(positions=[{"symbol": "MU", "avg_entry_price": "950", "qty": "5"}],
                    fills=[{"order_id": "E1", "order_status": "filled"}, {"order_id": "L-stop", "order_status": "filled"},
                           {"order_id": "E2", "order_status": "filled"}],
                    by_id={"E1": e1, "L-stop": stop_leg, "E2": e2})
    send = Sender(fail=1)
    tn.reconcile(broker, state_dir=tmp_path, send=send, now=NOW)
    assert tn.Entries(tmp_path).load() == {}  # not aligned to today's 950 before the retry
    tn.reconcile(broker, state_dir=tmp_path, send=send, now=NOW)
    exit_msg = next(m for _, m in send.sent if m.startswith("🔚"))
    assert "成本 893.92" in exit_msg and "−$221.85" in exit_msg


@pytest.mark.unit
def test_plan_line_follows_the_configured_ledger_dir(tmp_path):
    ledger = tmp_path / "elsewhere"
    ledger.mkdir()
    (ledger / "decisions.jsonl").write_text(json.dumps(
        {"date": "2026-10-30", "session": "us", "ticker": "MU", "decision": "BUY", "arm": "brief",
         "plan_valid": True, "plan": {"conviction": "0.6"}}) + "\n")
    send = Sender()
    tn.deliver([tn.event_from_update({"event": "new", "order": entry()})], state_dir=tmp_path / "state",
               send=send, ledger_dir=ledger)
    assert send.sent[0][1].splitlines()[1] == "信心 0.6"


@pytest.mark.unit
def test_daily_report_survives_a_failing_reconcile(tmp_path):
    class Broken(Broker):
        def fills_since(self, after):
            raise RuntimeError("activities endpoint down")

    tn.SentStore(tmp_path).mark_initialized()
    send = Sender()
    tn.run_hourly(state_dir=tmp_path, send=send, now=NOW, broker_factory=lambda: Broken())
    assert [m.split()[0] for _, m in send.sent] == ["📊"]


@pytest.mark.unit
def test_no_placement_message_after_a_later_event_of_the_order(tmp_path):
    tn.SentStore(tmp_path).mark_initialized()
    send = Sender()
    filled = entry("filled", filled_qty=5, avg=893.92)
    tn.deliver([tn.event_from_update({"event": "fill", "order": filled})], state_dir=tmp_path, send=send)
    tn.reconcile(Broker(orders=[filled]), state_dir=tmp_path, send=send, now=NOW)
    assert [m.split()[0] for _, m in send.sent] == ["✅"]  # the missed "new" is not sent late
