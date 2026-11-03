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


def book(state_dir):
    """The entry book's cost basis and size (bookkeeping fields aside)."""
    return {sym: "unknown" if row.get("unknown") else {"avg": row["avg"], "qty": row["qty"]}
            for sym, row in tn.Entries(state_dir).load().items()}


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
    assert book(tmp_path) == {}


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
    assert book(tmp_path) == {"MU": {"avg": 893.92, "qty": 4.0}}


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
    assert send.sent == [] and book(tmp_path) == {"MU": {"avg": 893.92, "qty": 5.0}}
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
    assert book(tmp_path) == {"MU": {"avg": 950.0, "qty": 5.0}}  # E2 booked from its own fill


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
def test_a_failed_send_keeps_the_messages_queued_and_the_book_in_order(tmp_path):
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
    assert send.sent == [] and len(tn.SentStore(tmp_path).outbox()) == 3  # queued behind the failure
    assert book(tmp_path) == {"MU": {"avg": 950.0, "qty": 5.0}}  # booked in order, E2 from its own fill
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


@pytest.mark.unit
def test_alignment_never_double_counts_a_partly_filled_add(tmp_path):
    """5 MU @800 held; an add of 5 @900 is 4 filled at R1, filled at R2."""
    tn.SentStore(tmp_path).mark_initialized()
    tn.Entries(tmp_path).save({"MU": {"avg": 800.0, "qty": 5.0}})
    add = order("A1", "2026-11-03-us-MU-entry", status="partially_filled", filled_qty=4, avg=900.0, legs=())
    broker = Broker(orders=[add], positions=[{"symbol": "MU", "avg_entry_price": "844.44", "qty": "9"}])
    send = Sender()
    tn.reconcile(broker, state_dir=tmp_path, send=send, now=NOW)  # R1: partial — no fill event yet
    assert book(tmp_path) == {"MU": {"avg": 800.0, "qty": 5.0}}
    filled = {**add, "status": "filled", "filled_qty": "5"}
    broker.orders, broker.fills, broker.by_id = [filled], [{"order_id": "A1", "order_status": "filled"}], {"A1": filled}
    broker._positions = [{"symbol": "MU", "avg_entry_price": "850", "qty": "10"}]
    tn.reconcile(broker, state_dir=tmp_path, send=send, now=NOW)  # R2
    assert book(tmp_path) == {"MU": {"avg": 850.0, "qty": 10.0}}


@pytest.mark.unit
def test_listener_starts_the_stream_even_if_the_startup_reconcile_fails(tmp_path, monkeypatch):
    from pipeline import trade_monitor

    monkeypatch.setenv("TRADINGAGENTS_SKIP_DOTENV", "1")
    monkeypatch.setenv("TRADINGAGENTS_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("ALPACA_API_KEY", "k")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "s")
    monkeypatch.setenv("TRADINGAGENTS_LARK_TRADES_WEBHOOK_URL", "https://open.feishu.cn/hook/x")
    send = Sender()
    monkeypatch.setattr(trade_monitor, "make_trades_sender", lambda config: send)
    monkeypatch.setattr(tn, "reconcile", lambda *a, **k: 1 / 0)
    tn.SentStore(tmp_path).mark_initialized()
    stream = []
    code = trade_monitor.main([], stream_factory=lambda: stream.append(FakeStream([])) or stream[0],
                              broker_factory=lambda: Broker())
    assert code == 1 and stream  # the stream ran


@pytest.mark.unit
def test_orders_listing_keeps_the_newest_when_capped():
    class Client:
        def get_orders(self, filter):
            assert filter.direction == "desc"
            return ["newest", "older"]

    broker = tn.AlpacaReadOnly.__new__(tn.AlpacaReadOnly)
    broker._client = Client()
    assert broker.orders_since(NOW) == ["older", "newest"]  # returned oldest first


def at(order_dict, **stamps):
    return {**order_dict, **{f"{name}_at": value for name, value in stamps.items()}}


@pytest.mark.unit
def test_reconcile_applies_events_in_the_order_they_happened(tmp_path):
    """MU 5@800 held; its stop fills at 849 (10:00), later a new entry 3@900 expires (15:00)."""
    tn.SentStore(tmp_path).mark_initialized()
    tn.Entries(tmp_path).save({"MU": {"avg": 800.0, "qty": 5.0}})
    stop_leg = at(order("L-stop", "leg-a", side="sell", type_="stop", status="filled", filled_qty=5, avg=849.0,
                        limit=None, stop=850.0), filled="2026-11-03T15:00:00Z")
    expired = at(order("E3", "2026-11-03-us-MU-entry", status="expired", filled_qty=3, avg=900.0, legs=()),
                 submitted="2026-11-03T14:45:00Z", expired="2026-11-03T20:00:00Z")
    broker = Broker(orders=[expired], positions=[{"symbol": "MU", "avg_entry_price": "900", "qty": "3"}],
                    fills=[{"order_id": "L-stop", "order_status": "filled"}], by_id={"L-stop": stop_leg})
    send = Sender()
    tn.reconcile(broker, state_dir=tmp_path, send=send, now=NOW)
    exit_msg = next(m for _, m in send.sent if m.startswith("🔚"))
    assert "成本 800.00" in exit_msg  # measured before the later partial entry was booked
    assert book(tmp_path) == {"MU": {"avg": 900.0, "qty": 3.0}}


@pytest.mark.unit
def test_a_hole_longer_than_the_fill_window_never_guesses_pnl(tmp_path):
    from datetime import timedelta

    store = tn.SentStore(tmp_path)
    store.mark_initialized()
    store.mark_reconciled(NOW - timedelta(days=5))  # host off over a long weekend
    tn.Entries(tmp_path).save({"MU": {"avg": 800.0, "qty": 5.0}})  # its exit fell in the hole
    stop_leg = order("L-stop", "leg-a", side="sell", type_="stop", status="filled", filled_qty=5, avg=849.0,
                     limit=None, stop=850.0)
    broker = Broker(positions=[{"symbol": "AMD", "avg_entry_price": "150", "qty": "10"}],
                    fills=[{"order_id": "L-stop", "order_status": "filled"}], by_id={"L-stop": stop_leg})
    send = Sender()
    tn.reconcile(broker, state_dir=tmp_path, send=send, now=NOW)
    assert send.sent[0][1] == "🔚 止损平仓 MU 卖出 5 股 @ 均价 849.00"  # no P&L in the hole batch
    assert book(tmp_path) == {"AMD": {"avg": 150.0, "qty": 10.0}}  # rebuilt from the broker
    assert store.last_reconcile() == NOW
    tn.reconcile(broker, state_dir=tmp_path, send=send, now=NOW)  # no hole any more


@pytest.mark.unit
def test_no_stale_placement_for_an_order_filled_outside_the_fill_window(tmp_path):
    tn.SentStore(tmp_path).mark_initialized()
    old_filled = at(order("E9", "2026-10-20-us-AMD-entry", status="filled", filled_qty=5, avg=150.0, symbol="AMD",
                          legs=()), submitted="2026-10-20T13:45:00Z", filled="2026-10-20T13:46:00Z")
    still_open = at(order("E8", "2026-10-29-us-AMD-entry", status="new", symbol="AMD", legs=()),
                    submitted="2026-10-29T13:45:00Z")
    send = Sender()
    tn.reconcile(Broker(orders=[old_filled, still_open]), state_dir=tmp_path, send=send, now=NOW)
    assert [m.splitlines()[0].startswith("📝 下单 AMD 买入 5 股") for _, m in send.sent] == [True]  # only the open one


@pytest.mark.unit
def test_a_failed_positions_read_never_empties_the_book_on_a_hole_rebuild(tmp_path):
    from datetime import timedelta

    store = tn.SentStore(tmp_path)
    store.mark_initialized()
    store.mark_reconciled(NOW - timedelta(days=5))
    tn.Entries(tmp_path).save({"MU": {"avg": 800.0, "qty": 5.0}})

    class Flaky(Broker):
        def positions(self):
            raise RuntimeError("alpaca 503")

    tn.reconcile(Flaky(), state_dir=tmp_path, send=Sender(), now=NOW)
    assert book(tmp_path) == {"MU": {"avg": 800.0, "qty": 5.0}}  # untouched
    assert store.last_reconcile() == NOW - timedelta(days=5)      # the hole is retried next time


@pytest.mark.unit
def test_a_fill_of_an_order_placed_before_the_seed_never_double_counts(tmp_path):
    """First run seeds MU from a position that already holds 4 of an add's 5 shares."""
    send = Sender()
    add = at(order("A1", "2026-11-02-us-MU-entry", status="partially_filled", filled_qty=4, avg=900.0, legs=()),
             submitted="2026-11-02T14:45:00Z")  # before the seed snapshot (taken at the wall clock)
    broker = Broker(orders=[add], positions=[{"symbol": "MU", "avg_entry_price": "844.44", "qty": "9"}])
    tn.reconcile(broker, state_dir=tmp_path, send=send, now=NOW)  # first run: seeded 9 @ 844.44
    from datetime import timedelta

    after_seed = (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat()  # the seed used the wall clock
    filled = {**add, "status": "filled", "filled_qty": "5", "filled_at": after_seed}
    broker.orders, broker.fills, broker.by_id = [filled], [{"order_id": "A1", "order_status": "filled"}], {"A1": filled}
    tn.reconcile(broker, state_dir=tmp_path, send=send, now=NOW)
    assert book(tmp_path) == {"MU": "unknown"}  # rather than (844.44*9 + 900*5)/14


@pytest.mark.unit
def test_a_seeded_row_stays_seeded_through_an_average_in(tmp_path):
    from datetime import timedelta

    seed_time = NOW - timedelta(hours=2)
    tn.SentStore(tmp_path).mark_initialized()
    tn.Entries(tmp_path).save({"MU": {"avg": 844.44, "qty": 9.0, "seeded_at": seed_time.isoformat()}})
    b = at(order("B1", "2026-10-30-us-MU-entry", status="filled", filled_qty=2, avg=910.0, legs=()),
           submitted=(seed_time + timedelta(minutes=5)).isoformat())   # placed after the seed: averages in
    a1 = at(order("A1", "2026-10-29-us-MU-entry", status="filled", filled_qty=5, avg=900.0, legs=()),
            submitted=(seed_time - timedelta(hours=1)).isoformat())    # placed before: maybe inside the seed
    broker = Broker(fills=[{"order_id": "B1", "order_status": "filled"}, {"order_id": "A1", "order_status": "filled"}],
                    by_id={"B1": b, "A1": a1}, positions=[{"symbol": "MU", "avg_entry_price": "1", "qty": "1"}])
    tn.reconcile(broker, state_dir=tmp_path, send=Sender(), now=NOW)
    assert book(tmp_path) == {"MU": "unknown"}  # stays unknown; B1 never restarts it as the whole position
    stop = order("L-stop", "leg-a", side="sell", type_="stop", status="filled", filled_qty=16, avg=849.0,
                 limit=None, stop=850.0)
    broker.fills, broker.by_id, broker._positions = [{"order_id": "L-stop", "order_status": "filled"}], \
        {"L-stop": stop}, []
    send = Sender()
    tn.reconcile(broker, state_dir=tmp_path, send=send, now=NOW)
    assert "盈亏" not in send.sent[-1][1] and book(tmp_path) == {}  # no guessed P&L; closed position dropped


@pytest.mark.unit
def test_events_after_the_first_start_are_sent_even_by_a_late_first_run(tmp_path):
    from datetime import timedelta

    store = tn.SentStore(tmp_path)
    store.first_started(NOW - timedelta(hours=1))  # the listener started; its startup reconcile failed
    old = at(order("E0", "2026-10-20-us-AMD-entry", status="new", symbol="AMD", legs=()),
             submitted=(NOW - timedelta(days=3)).isoformat())
    missed = at(order("E1", "2026-10-30-us-MU-entry", status="new", legs=()),
                submitted=(NOW - timedelta(minutes=30)).isoformat())   # during a stream reconnect
    send = Sender()
    tn.reconcile(Broker(orders=[old, missed]), state_dir=tmp_path, send=send, now=NOW)  # still the first run
    assert [m.split(" @")[0] for _, m in send.sent] == ["📝 下单 MU 买入 5 股"]  # history recorded, MU sent


@pytest.mark.unit
def test_listener_partial_exit_of_an_unknown_cost_basis(tmp_path):
    tn.SentStore(tmp_path).mark_initialized()
    tn.Entries(tmp_path).save({"MU": {"unknown": True}})
    target = order("L-tgt", "leg-b", side="sell", status="filled", filled_qty=5, avg=980.0, limit=980.0)
    send = Sender()
    sent = tn.deliver([tn.event_from_update({"event": "fill", "order": target})], state_dir=tmp_path, send=send,
                      positions=lambda: [{"symbol": "MU", "avg_entry_price": "850", "qty": "4"}])
    assert sent == 1 and "盈亏" not in send.sent[0][1]  # real time, no guessed P&L
    assert book(tmp_path) == {"MU": {"avg": 850.0, "qty": 4.0}}  # the broker's figure ends "unknown"


@pytest.mark.unit
def test_a_missed_fill_older_than_the_book_never_uses_a_later_cost_basis(tmp_path):
    """Stop fill at T1 missed by the stream; the listener then booked re-entry E2 from the position (T2)."""
    from datetime import timedelta

    tn.SentStore(tmp_path).mark_initialized()
    t1 = datetime.now(timezone.utc) - timedelta(minutes=10)
    send = Sender()
    e2 = at(order("E2", "2026-11-03-us-MU-entry", status="filled", filled_qty=5, avg=950.0, legs=()),
            filled=(t1 + timedelta(minutes=5)).isoformat())
    tn.deliver([tn.event_from_update({"event": "fill", "order": e2})], state_dir=tmp_path, send=send,
               positions=lambda: [{"symbol": "MU", "avg_entry_price": "950", "qty": "5"}])  # listener, live
    stop = at(order("L-stop", "leg-a", side="sell", type_="stop", status="filled", filled_qty=5, avg=849.55,
                    limit=None, stop=850.0), filled=t1.isoformat())
    broker = Broker(positions=[{"symbol": "MU", "avg_entry_price": "950", "qty": "5"}],
                    fills=[{"order_id": "L-stop", "order_status": "filled"}], by_id={"L-stop": stop})
    tn.reconcile(broker, state_dir=tmp_path, send=send, now=datetime.now(timezone.utc))
    exit_msg = send.sent[-1][1]
    assert exit_msg.startswith("🔚 止损平仓 MU") and "盈亏" not in exit_msg  # not measured against 950
    assert book(tmp_path) == {"MU": {"avg": 950.0, "qty": 5.0}}  # the newer figure is untouched


@pytest.mark.unit
def test_seed_and_listener_stamps_use_broker_time(tmp_path):
    from datetime import timedelta

    broker_clock = NOW - timedelta(minutes=7)  # the host runs ahead of the broker
    broker = Broker(positions=[{"symbol": "MU", "avg_entry_price": "893.92", "qty": "5"}])
    broker.now = lambda: broker_clock
    tn.reconcile(broker, state_dir=tmp_path, send=Sender(), now=NOW)  # first run seeds
    assert tn.Entries(tmp_path).load()["MU"]["as_of"] == broker_clock.isoformat()
    # A stop that fills after the broker-time seed is applied with P&L.
    stop = at(order("L-stop", "leg-a", side="sell", type_="stop", status="filled", filled_qty=5, avg=849.55,
                    limit=None, stop=850.0), filled=(broker_clock + timedelta(minutes=1)).isoformat())
    send = Sender()
    tn.deliver([tn.event_from_update({"event": "fill", "order": stop})], state_dir=tmp_path, send=send,
               positions=lambda: [])
    assert "盈亏 −$221.85" in send.sent[0][1]


@pytest.mark.unit
def test_listener_position_read_is_stamped_with_the_broker_clock_after_the_read(tmp_path):
    from datetime import timedelta

    tn.SentStore(tmp_path).mark_initialized()
    event_time = NOW - timedelta(minutes=3)
    read_time = NOW - timedelta(minutes=1)  # a second fill may land in between
    fill = at(entry("filled", filled_qty=5, avg=893.92), filled=event_time.isoformat())
    tn.deliver([tn.event_from_update({"event": "fill", "order": fill})], state_dir=tmp_path, send=Sender(),
               positions=lambda: [{"symbol": "MU", "avg_entry_price": "893.92", "qty": "5"}],
               broker_now=lambda: read_time)
    assert tn.Entries(tmp_path).load()["MU"]["as_of"] == read_time.isoformat()


@pytest.mark.unit
def test_a_message_that_never_sends_is_dropped_after_max_tries_and_time(tmp_path, caplog, monkeypatch):
    from datetime import timedelta

    monkeypatch.setattr(tn, "DROP_AFTER", timedelta(0))  # time elapsed: attempts decide
    tn.SentStore(tmp_path).mark_initialized()
    first = order("E1", "2026-11-03-us-MU-entry", status="new", legs=())
    second = order("E2", "2026-11-03-us-AMD-entry", status="new", symbol="AMD", legs=())

    class Poison(Sender):
        def __call__(self, title, message):
            if "MU" in message:
                raise RuntimeError("keyword rejected")
            self.sent.append((title, message))

    send, broker = Poison(), Broker(orders=[first, second])
    for _ in range(tn.MAX_SEND_FAILURES - 1):
        tn.reconcile(broker, state_dir=tmp_path, send=send, now=NOW)
    assert send.sent == []  # still retried in order
    tn.reconcile(broker, state_dir=tmp_path, send=send, now=NOW)
    assert [m.split(" @")[0] for _, m in send.sent] == ["📝 下单 AMD 买入 5 股"]
    assert f"dropped after {tn.MAX_SEND_FAILURES} failed sends" in caplog.text


@pytest.mark.unit
def test_many_quick_failures_never_drop_a_message_before_drop_after(tmp_path):
    tn.SentStore(tmp_path).mark_initialized()
    send = Sender(fail=50)
    event = tn.event_from_update({"event": "new", "order": entry()})
    for _ in range(3 * tn.MAX_SEND_FAILURES):  # a busy outage: many listener tries in minutes
        tn.deliver([event], state_dir=tmp_path, send=send)
    assert [item["key"] for item in tn.SentStore(tmp_path).outbox()] == ["E1:new"]  # still queued


@pytest.mark.unit
def test_the_outbox_is_durable_before_sending(tmp_path):
    tn.SentStore(tmp_path).mark_initialized()

    def killed(title, message):  # the process dies mid-send
        assert [item["key"] for item in tn.SentStore(tmp_path).outbox()] == ["E1:new"]
        raise SystemExit("killed")

    with pytest.raises(SystemExit):
        tn.deliver([tn.event_from_update({"event": "new", "order": entry()})], state_dir=tmp_path, send=killed)
    send = Sender()
    tn.deliver([], state_dir=tmp_path, send=send)  # a later run delivers it
    assert [m.split(" @")[0] for _, m in send.sent] == ["📝 下单 MU 买入 5 股"]


@pytest.mark.unit
def test_fill_activities_are_read_from_v2_with_pagination():
    calls = []

    class Client:
        def get(self, path, params):
            calls.append((path, dict(params)))
            if "page_token" not in params:
                return [{"id": f"a{i}", "order_status": "filled"} for i in range(100)]
            return [{"id": "last", "order_status": "filled"}]

    broker = tn.AlpacaReadOnly.__new__(tn.AlpacaReadOnly)
    broker._client = Client()
    rows = broker.fills_since(NOW)
    assert len(rows) == 101 and calls[0][0] == "/account/activities/FILL"  # the client adds /v2
    assert calls[0][1]["direction"] == "asc" and calls[1][1]["page_token"] == "a99"


@pytest.mark.unit
def test_a_dropped_fill_message_keeps_its_book_change(tmp_path, monkeypatch):
    from datetime import timedelta

    monkeypatch.setattr(tn, "MAX_SEND_FAILURES", 1)
    monkeypatch.setattr(tn, "DROP_AFTER", timedelta(0))
    tn.SentStore(tmp_path).mark_initialized()
    tn.Entries(tmp_path).save({"MU": {"avg": 800.0, "qty": 5.0}})
    stop = order("L-stop", "leg-a", side="sell", type_="stop", status="filled", filled_qty=5, avg=849.0,
                 limit=None, stop=850.0)
    tn.deliver([tn.event_from_update({"event": "fill", "order": stop})], state_dir=tmp_path, send=Sender(fail=1))
    assert book(tmp_path) == {}  # the exit happened: MU is out of the book though its message was dropped
    reentry = order("E2", "2026-11-04-us-MU-entry", status="filled", filled_qty=5, avg=950.0, legs=())
    tn.deliver([tn.event_from_update({"event": "fill", "order": reentry})], state_dir=tmp_path, send=Sender())
    assert book(tmp_path) == {"MU": {"avg": 950.0, "qty": 5.0}}  # not averaged with the stale 800


@pytest.mark.unit
def test_listener_exit_queues_behind_a_failed_entry_message_with_its_pnl(tmp_path):
    tn.SentStore(tmp_path).mark_initialized()
    send = Sender(fail=1)
    held = {"now": [{"symbol": "MU", "avg_entry_price": "893.92", "qty": "5"}]}

    def stream(update):
        tn.deliver([tn.event_from_update(update)], state_dir=tmp_path, send=send, positions=lambda: held["now"])

    stream({"event": "fill", "order": entry("filled", filled_qty=5, avg=893.92)})  # Lark times out
    held["now"] = []
    stream({"event": "fill", "order": order("L-stop", "leg-a", side="sell", type_="stop", status="filled",
                                            filled_qty=5, avg=849.55, limit=None, stop=850.0)})
    messages = [m for _, m in send.sent]
    assert messages[0].startswith("✅ 成交 MU") and messages[1].startswith("🔚 止损平仓 MU")  # in order
    assert "盈亏 −$221.85（−4.96%，成本 893.92）" in messages[1]
    assert tn.SentStore(tmp_path).outbox() == []


@pytest.mark.unit
def test_alignment_uses_the_positions_snapshot_taken_before_the_events(tmp_path):
    tn.SentStore(tmp_path).mark_initialized()
    tn.Entries(tmp_path).save({"MU": {"avg": 893.92, "qty": 5.0}})

    class Racing(Broker):
        calls = 0

        def positions(self):  # MU's stop fills right after the snapshot, during the sends
            Racing.calls += 1
            return [{"symbol": "MU", "avg_entry_price": "893.92", "qty": "5"}] if Racing.calls == 1 else []

    broker = Racing(orders=[order("E2", "2026-11-03-us-AMD-entry", status="new", symbol="AMD", legs=())])
    tn.reconcile(broker, state_dir=tmp_path, send=Sender(), now=NOW)
    assert book(tmp_path) == {"MU": {"avg": 893.92, "qty": 5.0}}  # kept for the listener's exit event
    send = Sender()
    stop = order("L-stop", "leg-a", side="sell", type_="stop", status="filled", filled_qty=5, avg=849.55,
                 limit=None, stop=850.0)
    tn.deliver([tn.event_from_update({"event": "fill", "order": stop})], state_dir=tmp_path, send=send,
               positions=lambda: [])
    assert "盈亏 −$221.85" in send.sent[0][1]


@pytest.mark.unit
def test_a_kill_after_booking_never_books_twice_nor_loses_the_message(tmp_path, monkeypatch):
    tn.SentStore(tmp_path).mark_initialized()
    tn.Entries(tmp_path).save({"MU": {"avg": 800.0, "qty": 5.0}})
    add = order("A1", "2026-11-03-us-MU-entry", status="filled", filled_qty=5, avg=900.0, legs=())
    event = tn.event_from_update({"event": "fill", "order": add})
    real = tn.SentStore.save_outbox

    def killed(self, items):
        raise SystemExit("killed between the book and the outbox")

    monkeypatch.setattr(tn.SentStore, "save_outbox", killed)
    with pytest.raises(SystemExit):
        tn.deliver([event], state_dir=tmp_path, send=Sender())
    monkeypatch.setattr(tn.SentStore, "save_outbox", real)
    send = Sender()
    tn.deliver([event], state_dir=tmp_path, send=send)  # the rerun (the fallback replays it)
    assert book(tmp_path) == {"MU": {"avg": 850.0, "qty": 10.0}}  # booked once
    assert [m.split(" @")[0] for _, m in send.sent] == ["✅ 成交 MU 买入 5 股"]  # not lost, not doubled


@pytest.mark.unit
def test_an_entry_booked_after_the_snapshot_survives_the_alignment(tmp_path):
    from datetime import timedelta

    tn.SentStore(tmp_path).mark_initialized()
    snapshot_time = NOW - timedelta(minutes=2)
    fill = at(entry("filled", filled_qty=5, avg=893.92), filled=(snapshot_time + timedelta(seconds=30)).isoformat())

    class Late(Broker):  # MU's entry fills between the snapshot and the fills read
        def now(self):
            return snapshot_time

    broker = Late(positions=[], fills=[{"order_id": "E1", "order_status": "filled"}], by_id={"E1": fill})
    tn.reconcile(broker, state_dir=tmp_path, send=Sender(), now=NOW)
    assert book(tmp_path) == {"MU": {"avg": 893.92, "qty": 5.0}}  # newer than the snapshot: kept


@pytest.mark.unit
def test_a_kill_after_booking_an_exit_keeps_its_pnl(tmp_path, monkeypatch):
    tn.SentStore(tmp_path).mark_initialized()
    tn.Entries(tmp_path).save({"MU": {"avg": 893.92, "qty": 5.0}})
    stop = order("L-stop", "leg-a", side="sell", type_="stop", status="filled", filled_qty=5, avg=849.55,
                 limit=None, stop=850.0)
    event = tn.event_from_update({"event": "fill", "order": stop})
    real = tn.SentStore.save_outbox
    monkeypatch.setattr(tn.SentStore, "save_outbox", lambda self, items: (_ for _ in ()).throw(SystemExit("x")))
    with pytest.raises(SystemExit):
        tn.deliver([event], state_dir=tmp_path, send=Sender())
    monkeypatch.setattr(tn.SentStore, "save_outbox", real)
    send = Sender()
    tn.deliver([event], state_dir=tmp_path, send=send)
    assert "盈亏 −$221.85" in send.sent[0][1]  # the killed run's computed P&L is reused


@pytest.mark.unit
def test_sent_keys_older_than_the_lookbacks_are_pruned(tmp_path):
    from datetime import timedelta

    store = tn.SentStore(tmp_path)
    store.dir.mkdir(parents=True)
    old = (NOW - tn.SENT_KEEP - timedelta(days=1)).isoformat()
    store.path.write_text(json.dumps({"key": "OLD:fill", "sent": True, "at": old}) + "\n"
                          + json.dumps({"key": "NEW:fill", "sent": True, "at": NOW.isoformat()}) + "\n")
    store.prune(NOW, tn.SENT_KEEP)
    assert store.keys() == {"NEW:fill"}


@pytest.mark.unit
def test_the_applied_cap_keeps_the_newest_keys(tmp_path, monkeypatch):
    monkeypatch.setattr(tn, "APPLIED_KEEP", 2)
    book_ = tn.Entries(tmp_path)
    applied = {"zzz:fill": 1.0, "aaa:fill": 2.0, "mmm:fill": None}  # insertion = chronological
    book_.save({}, applied)
    assert list(book_.applied()) == ["aaa:fill", "mmm:fill"]  # the two newest, in order


@pytest.mark.unit
def test_the_daily_report_queues_behind_a_stuck_trade_message(tmp_path):
    tn.SentStore(tmp_path).mark_initialized()
    stuck = Sender(fail=1)
    tn.deliver([tn.event_from_update({"event": "new", "order": entry()})], state_dir=tmp_path, send=stuck)
    send = Sender(fail=2)  # the hourly run: the head keeps failing, the report waits behind it
    tn.run_hourly(state_dir=tmp_path, send=send, now=NOW, broker_factory=lambda: Broker())
    assert send.sent == [] and [i["key"] for i in tn.SentStore(tmp_path).outbox()][-1].startswith("daily:")
    tn.deliver([], state_dir=tmp_path, send=send)
    assert [m.split()[0] for _, m in send.sent] == ["📝", "📊"]  # in order, the report never first


@pytest.mark.unit
def test_first_run_seed_and_cutoff_use_the_snapshot_time(tmp_path):
    from datetime import timedelta

    snap = NOW - timedelta(minutes=5)

    class Clocked(Broker):
        def now(self):
            return snap

    add = at(order("A1", "2026-11-03-us-MU-entry", status="filled", filled_qty=5, avg=900.0, legs=()),
             submitted=(snap + timedelta(seconds=10)).isoformat(), filled=(snap + timedelta(seconds=20)).isoformat())
    broker = Clocked(positions=[{"symbol": "MU", "avg_entry_price": "800", "qty": "5"}],
                     fills=[{"order_id": "A1", "order_status": "filled"}], by_id={"A1": add})
    send = Sender()
    tn.reconcile(broker, state_dir=tmp_path, send=send, now=NOW)  # first run
    assert tn.SentStore(tmp_path).first_started(NOW) == snap
    assert book(tmp_path) == {"MU": {"avg": 850.0, "qty": 10.0}}  # the add after the snapshot is booked
    assert [m.split(" @")[0] for _, m in send.sent] == ["✅ 成交 MU 买入 5 股"]  # after the cutoff: sent


@pytest.mark.unit
def test_a_pre_seed_sell_leg_never_subtracts_twice(tmp_path):
    from datetime import timedelta

    seed = NOW - timedelta(hours=1)
    tn.SentStore(tmp_path).mark_initialized()
    tn.Entries(tmp_path).save({"MU": {"avg": 850.0, "qty": 8.0, "seeded_at": seed.isoformat(),
                                      "as_of": seed.isoformat()}})
    leg = at(order("L-tgt", "leg-b", side="sell", status="filled", filled_qty=5, avg=980.0, limit=980.0),
             submitted=(seed - timedelta(days=2)).isoformat(), filled=(seed + timedelta(minutes=10)).isoformat())
    broker = Broker(positions=[{"symbol": "MU", "avg_entry_price": "850", "qty": "5"}],
                    fills=[{"order_id": "L-tgt", "order_status": "filled"}], by_id={"L-tgt": leg})
    tn.reconcile(broker, state_dir=tmp_path, send=Sender(), now=NOW)
    assert book(tmp_path) == {"MU": "unknown"}  # not 8 - 5 = 3 when 2 were sold before the seed


@pytest.mark.unit
def test_a_fill_during_the_seed_read_makes_the_cost_basis_unknown(tmp_path):
    from datetime import timedelta

    t0 = NOW - timedelta(minutes=5)
    clock = iter([t0, t0 + timedelta(seconds=2)])  # before / after the positions read

    class Clocked(Broker):
        def now(self):
            return next(clock)

    add = at(order("A1", "2026-11-03-us-MU-entry", status="filled", filled_qty=5, avg=900.0, legs=()),
             submitted=(t0 + timedelta(seconds=0.5)).isoformat(), filled=(t0 + timedelta(seconds=1)).isoformat())
    broker = Clocked(positions=[{"symbol": "MU", "avg_entry_price": "850", "qty": "10"}],  # maybe includes A1
                     fills=[{"order_id": "A1", "order_status": "filled"}], by_id={"A1": add})
    tn.reconcile(broker, state_dir=tmp_path, send=Sender(), now=NOW)  # first run
    assert book(tmp_path) == {"MU": "unknown"}  # neither 10 @ 850 nor 15 @ 866.67


@pytest.mark.unit
def test_a_failing_broker_clock_skips_the_reconcile(tmp_path, caplog):
    class NoClock(Broker):
        def now(self):
            raise RuntimeError("clock endpoint down")

    send = Sender()
    tn.run_hourly(state_dir=tmp_path, send=send, now=NOW, broker_factory=lambda: NoClock())
    assert not tn.SentStore(tmp_path).initialized() and "reconcile skipped" in caplog.text


@pytest.mark.unit
def test_an_order_straddling_a_listener_read_never_double_counts(tmp_path):
    """MU 5 @800; add A1 (5 @900) 4 filled when the listener reads 9 @844.44; A1's final fill is missed."""
    from datetime import timedelta

    tn.SentStore(tmp_path).mark_initialized()
    tn.Entries(tmp_path).save({"MU": {"avg": 800.0, "qty": 5.0}})
    t = datetime.now(timezone.utc) - timedelta(minutes=10)
    other = at(order("B1", "2026-11-03-us-MU-entry", status="filled", filled_qty=0.0001, avg=844.44, legs=()),
               submitted=(t - timedelta(minutes=1)).isoformat(), filled=t.isoformat())
    tn.deliver([tn.event_from_update({"event": "fill", "order": other})], state_dir=tmp_path, send=Sender(),
               positions=lambda: [{"symbol": "MU", "avg_entry_price": "844.44", "qty": "9"}],
               broker_now=lambda: t + timedelta(seconds=1))  # listener read: 9 @844.44 incl. 4 of A1
    a1 = at(order("A1", "2026-11-03-us-MU-entry", status="filled", filled_qty=5, avg=900.0, legs=()),
            submitted=(t - timedelta(minutes=5)).isoformat(), filled=(t + timedelta(minutes=2)).isoformat())
    tn.deliver([tn.event_from_update({"event": "fill", "order": a1})], state_dir=tmp_path, send=Sender())
    assert book(tmp_path) == {"MU": "unknown"}  # not 14 shares


@pytest.mark.unit
def test_a_bracket_exit_replayed_after_a_listener_booked_entry_keeps_its_pnl(tmp_path):
    """The leg was placed with the entry (before the read) but first executes long after it."""
    from datetime import timedelta

    tn.SentStore(tmp_path).mark_initialized()
    t = datetime.now(timezone.utc) - timedelta(hours=3)
    fill = at(entry("filled", filled_qty=5, avg=893.92), filled=t.isoformat())
    tn.deliver([tn.event_from_update({"event": "fill", "order": fill})], state_dir=tmp_path, send=Sender(),
               positions=lambda: [{"symbol": "MU", "avg_entry_price": "893.92", "qty": "5"}],
               broker_now=lambda: t + timedelta(seconds=1))
    stop_time = t + timedelta(hours=2)
    stop = at(order("L-stop", "leg-a", side="sell", type_="stop", status="filled", filled_qty=5, avg=849.55,
                    limit=None, stop=850.0), submitted=(t - timedelta(seconds=5)).isoformat(),
              filled=stop_time.isoformat())
    broker = Broker(positions=[], fills=[{"order_id": "L-stop", "order_status": "filled",
                                          "transaction_time": stop_time.isoformat()}], by_id={"L-stop": stop})
    send = Sender()
    tn.reconcile(broker, state_dir=tmp_path, send=send, now=datetime.now(timezone.utc))  # listener missed it
    assert "盈亏 −$221.85" in send.sent[-1][1]
