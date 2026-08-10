"""Execution adapter (``pipeline/execution_adapter.py``) — spec AC1-AC4, offline.

The broker boundary is faked at the ``AlpacaBroker`` seam (alpaca-py is never
imported). Every safety invariant S1-S9 is tested individually: paper-only
refusal on both entrypoints, opt-in dry-run, the mid-queue kill switch,
ticker/day dedupe across run_ids + intent-row crash recovery, eligibility
recomputation from the DecisionRecord (manual exclusion, short-block, close
clamp, the gating-table verdict matrix), the caps incl. pending-entry gross
exposure, secret hygiene, the fail-closed market-state guards, and the add-on
tranche rule matrix plus buying-power/total-risk arithmetic with pinned
numbers. Order mapping (AC2): GTC bracket construction, stale-entry cancels
that never touch a filled entry, SELL cancel-then-close and the once-only
close re-submission under ``-close-2``. AC3 pins a mixed fixture slot to its
exact dry-run ``orders.jsonl``; AC4 greps the codebase for the single paper
endpoint pin. Every ledger row a test reads is validated against the
``OrderRecord`` contract.
"""

from __future__ import annotations

import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from pipeline import execution_adapter as ea
from pipeline.config import PipelineConfig
from pipeline.contracts.ledger import OrderRecord, PositionsSnapshot

SLOT = date(2026, 8, 7)  # a Friday
DATE_ISO = "2026-08-07"
NOW = datetime(2026, 8, 7, 13, 40, tzinfo=timezone.utc)  # 09:40 New York


def clock():
    return NOW


BUY_PLAN = {
    "action": "BUY",
    "conviction": 0.7,
    "add_intent": False,
    "add_rationale": None,
    "entry_zone": [99.0, 100.0],
    "stop": 90.0,
    "targets": [110.0, 120.0],
    "horizon_days": 10,
    "invalidation": "daily close below the weekly mid-band",
    "sizing": {"risk_pct": 1.0},
    "source_levels": "entry = daily BOLL mid, stop = below daily lower band",
}
ADDON_PLAN = {**BUY_PLAN, "add_intent": True, "add_rationale": "new breakout on volume"}
SELL_PLAN = {
    "action": "SELL",
    "conviction": 0.6,
    "add_intent": False,
    "add_rationale": None,
    "entry_zone": [95.0, 97.0],
    "stop": None,
    "targets": None,
    "horizon_days": None,
    "invalidation": None,
    "sizing": None,
    "source_levels": None,
}
SELL_PLAN_NO_ZONE = {**SELL_PLAN, "entry_zone": None}


def make_config(tmp_path) -> PipelineConfig:
    return PipelineConfig(state_dir=tmp_path / "state")


def write_decision(
    config,
    *,
    ticker="NVDA",
    n=1,
    arm="brief",
    trigger="core",
    plan=BUY_PLAN,
    plan_valid=True,
    decision=None,
    macro="pass",
    ticker_v="pass",
    session="us",
    date_iso=DATE_ISO,
):
    """Append one contract-shaped decisions.jsonl row."""
    action = None if plan is None else plan["action"]
    row = {
        "run_id": f"{date_iso}-{session}-{ticker}-{arm}-{n}",
        "pair_id": None,
        "date": date_iso,
        "session": session,
        "ticker": ticker,
        "arm": arm,
        "preset": "test",
        "trigger": trigger,
        "catalyst_score": None,
        "macro_eval_verdict": macro,
        "ticker_eval_verdict": ticker_v,
        "inputs": {"macro_brief": None, "ticker_brief": None, "pool": None, "config_digest": "d"},
        "decided_at": "2026-08-07T13:00:00Z",
        "decision": decision or (action or "HOLD"),
        "plan": plan,
        "plan_valid": plan_valid,
    }
    path = config.ledger_dir / "decisions.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(row) + "\n")
    return row


def append_order_row(config, **fields):
    """Raw orders.jsonl fixture row (prior-history seeding)."""
    row = {
        "run_id": f"{DATE_ISO}-us-NVDA-brief-1",
        "written_at": "2026-08-07T12:00:00Z",
        "session": "us",
        "ticker": "NVDA",
        "kind": "submitted",
        "dry_run": False,
        "reason": None,
        "client_order_id": f"{DATE_ISO}-us-NVDA-entry",
        "order_kind": "bracket",
        "tif": "gtc",
        "qty": 10,
        "limit_price": 100.0,
        "stop_price": 90.0,
        "target_price": 110.0,
        "broker_status": "accepted",
        "filled_avg_price": None,
        "filled_qty": None,
    }
    row.update(fields)
    path = config.ledger_dir / "orders.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(row) + "\n")
    return row


def seed_entry_fill(config, ticker, day_iso, price, qty=50):
    """A prior live entry bracket + its filled refresh row (one tranche)."""
    cid = f"{day_iso}-us-{ticker}-entry"
    run_id = f"{day_iso}-us-{ticker}-brief-1"
    append_order_row(
        config, run_id=run_id, written_at=f"{day_iso}T13:00:00Z", ticker=ticker,
        kind="submitted", client_order_id=cid, order_kind="bracket", qty=qty,
        limit_price=price, stop_price=round(price * 0.9, 2), target_price=round(price * 1.2, 2),
    )
    append_order_row(
        config, run_id=run_id, written_at=f"{day_iso}T14:30:00Z", ticker=ticker,
        kind="refresh", client_order_id=cid, order_kind=None, tif=None, qty=qty,
        limit_price=price, stop_price=None, target_price=None,
        broker_status="filled", filled_avg_price=price, filled_qty=qty,
    )


def read_orders(config):
    """All orders.jsonl rows — each one validated against the contract."""
    path = config.ledger_dir / "orders.jsonl"
    if not path.exists():
        return []
    rows = [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    for row in rows:
        OrderRecord.parse_lenient(row)
    return rows


FRESH_QUOTE = ea.Quote(bid=99.5, ask=100.0, timestamp=NOW - timedelta(minutes=1))


def position(ticker="NVDA", qty=100.0, available=None, held=0.0, avg=95.0, mv=None, upl=0.0):
    available = qty - held if available is None else available
    return ea.BrokerPosition(
        ticker=ticker,
        qty=qty,
        qty_available=available,
        qty_held_for_orders=held,
        avg_entry=avg,
        market_value=mv if mv is not None else qty * avg,
        unrealized_pl=upl,
    )


def broker_order(
    cid,
    *,
    ticker="NVDA",
    side="buy",
    qty=10.0,
    limit=100.0,
    stop=None,
    status="accepted",
    tif="gtc",
    filled=0.0,
    favg=None,
    legs=(),
    oid=None,
):
    return ea.BrokerOrder(
        order_id=oid or f"oid-{cid}",
        client_order_id=cid,
        ticker=ticker,
        side=side,
        qty=qty,
        limit_price=limit,
        stop_price=stop,
        status=status,
        tif=tif,
        filled_qty=filled,
        filled_avg_price=favg,
        submitted_at=None,
        legs=tuple(legs),
    )


class FakeBroker:
    """AlpacaBroker seam fake with per-method error injection."""

    def __init__(
        self,
        *,
        account_number="PA37TEST01",
        equity=100_000.0,
        buying_power=200_000.0,
        trading_day=True,
        now=NOW,
        quotes=None,
        assets=None,
        positions=(),
        open_orders=(),
        broker_orders=None,
        errors=(),
        submit_status="accepted",
    ):
        self.account_number = account_number
        self.equity = equity
        self.buying_power = buying_power
        self.trading_day = trading_day
        self.now = now
        self.quotes = dict(quotes or {})
        self.assets = dict(assets or {})
        self.positions = list(positions)
        self.open_orders = list(open_orders)
        self.broker_orders = dict(broker_orders or {})
        self.errors = set(errors)
        self.submit_status = submit_status
        self.submit_hook = None
        self.calls: list[str] = []
        self.client_queries: list[str] = []
        self.submitted: list[ea.OrderSpec] = []
        self.canceled: list[str] = []

    def _touch(self, name):
        self.calls.append(name)
        if name in self.errors:
            raise RuntimeError(f"{name} unavailable")

    def get_account(self):
        self._touch("get_account")
        return ea.AccountState(self.account_number, self.equity, self.buying_power)

    def get_clock(self):
        self._touch("get_clock")
        return self.now

    def get_calendar_today(self):
        self._touch("get_calendar_today")
        return ea.CalendarDay(date=self.now.date()) if self.trading_day else None

    def get_asset(self, ticker):
        self._touch("get_asset")
        return self.assets.get(ticker, ea.AssetInfo(symbol=ticker, tradable=True))

    def latest_quote(self, ticker):
        self._touch("latest_quote")
        return self.quotes.get(ticker, FRESH_QUOTE)

    def list_positions(self):
        self._touch("list_positions")
        return list(self.positions)

    def list_open_orders(self):
        self._touch("list_open_orders")
        return list(self.open_orders)

    def get_order_by_client_id(self, client_order_id):
        self._touch("get_order_by_client_id")
        self.client_queries.append(client_order_id)
        return self.broker_orders.get(client_order_id)

    def submit_order(self, spec):
        self._touch("submit_order")
        if self.submit_hook is not None:
            self.submit_hook(spec)
        self.submitted.append(spec)
        return broker_order(
            spec.client_order_id,
            ticker=spec.ticker,
            side=spec.side,
            qty=spec.qty,
            limit=spec.limit_price,
            stop=spec.stop_loss,
            status=self.submit_status,
            tif=spec.tif,
            oid=f"oid-{len(self.submitted)}",
        )

    def cancel_order(self, order_id):
        self._touch("cancel_order")
        if "cancel_order_fail" in self.errors:
            raise RuntimeError("cancel refused")
        self.canceled.append(order_id)


def live_settings(**overrides) -> ea.AdapterSettings:
    return ea.AdapterSettings(execution_enabled=True, **overrides)


def submit_slot(config, broker, *, settings=None, **kwargs):
    return ea.run_submit(
        config=config,
        settings=settings if settings is not None else ea.AdapterSettings(),
        broker=broker,
        slot_date=SLOT,
        clock=clock,
        **kwargs,
    )


def refresh(config, broker, *, settings=None, today=SLOT):
    return ea.run_refresh(
        config=config,
        settings=settings if settings is not None else ea.AdapterSettings(),
        broker=broker,
        today=today,
        clock=clock,
    )


def touch_halt(config):
    path = ea.halt_file_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("halt\n", encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# S1 — paper-only, fail-closed (both entrypoints, before anything)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_s1_submit_refuses_non_paper_account_before_anything(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("TRADINGAGENTS_STATE_DIR", str(tmp_path / "state"))
    config = make_config(tmp_path)
    write_decision(config)
    broker = FakeBroker(account_number="12345LIVE9")
    exit_code = ea.main(
        ["submit", "--date", DATE_ISO], broker_factory=lambda: broker, clock=clock
    )
    assert exit_code == 1
    err = capsys.readouterr().err
    assert "S1" in err and "PAPER" in err
    # The account number is never echoed (S7-adjacent hygiene).
    assert "12345LIVE9" not in err
    # Refused BEFORE anything: only the account fetch happened, no row written.
    assert broker.calls == ["get_account"]
    assert read_orders(config) == []


@pytest.mark.unit
def test_s1_refresh_refuses_non_paper_account(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("TRADINGAGENTS_STATE_DIR", str(tmp_path / "state"))
    config = make_config(tmp_path)
    broker = FakeBroker(account_number="LV0001")
    assert ea.main(["refresh", "--date", DATE_ISO], broker_factory=lambda: broker) == 1
    assert broker.calls == ["get_account"]
    assert not (config.ledger_dir / "positions.json").exists()
    assert "S1" in capsys.readouterr().err


@pytest.mark.unit
def test_s1_account_fetch_error_aborts_both_entrypoints(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADINGAGENTS_STATE_DIR", str(tmp_path / "state"))
    config = make_config(tmp_path)
    write_decision(config)
    for argv in (["submit", "--date", DATE_ISO], ["refresh", "--date", DATE_ISO]):
        broker = FakeBroker(errors={"get_account"})
        assert ea.main(argv, broker_factory=lambda b=broker: b) == 1
    assert read_orders(config) == []


# ---------------------------------------------------------------------------
# S2 — opt-in (default false ⇒ dry-run rows) + R5 --dry-run
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_s2_default_disabled_appends_dry_run_rows(tmp_path):
    config = make_config(tmp_path)
    write_decision(config)
    broker = FakeBroker()
    summary = submit_slot(config, broker)  # default AdapterSettings: disabled
    assert broker.submitted == []
    rows = read_orders(config)
    assert [(r["kind"], r["dry_run"]) for r in rows] == [("submitted", True)]
    row = rows[0]
    assert row["client_order_id"] == "2026-08-07-us-NVDA-entry"
    assert row["order_kind"] == "bracket"
    assert (row["qty"], row["limit_price"], row["stop_price"], row["target_price"]) == (
        100.0, 100.0, 90.0, 110.0,
    )
    assert row["tif"] == "gtc"
    assert summary["dry_run"] == 1 and summary["submitted"] == 0 and summary["live"] is False


@pytest.mark.unit
def test_s2_enabled_submits_live_gtc_bracket(tmp_path):
    """Also AC2: the GTC bracket construction — limit at entry_zone[1], stop
    leg at stop, take-profit at targets[0], deterministic client id."""
    config = make_config(tmp_path)
    write_decision(config)
    broker = FakeBroker()
    summary = submit_slot(config, broker, settings=live_settings())
    assert len(broker.submitted) == 1
    spec = broker.submitted[0]
    assert spec.side == "buy"
    assert spec.tif == "gtc"
    assert spec.order_class == "bracket"
    assert spec.qty == 100  # floor(100000 × 1% / (100 − 90))
    assert spec.limit_price == 100.0  # entry_zone[1] — worst acceptable fill
    assert spec.stop_loss == 90.0
    assert spec.take_profit == 110.0  # targets[0]
    assert spec.client_order_id == "2026-08-07-us-NVDA-entry"
    rows = read_orders(config)
    # Intent row appended immediately BEFORE the live submit, then submitted.
    assert [(r["kind"], r["dry_run"]) for r in rows] == [
        ("intent", False), ("submitted", False),
    ]
    assert rows[1]["broker_status"] == "accepted"
    assert summary["submitted"] == 1 and summary["dry_run"] == 0


@pytest.mark.unit
def test_r5_dry_run_flag_forces_dry_despite_enabled_config(tmp_path):
    config = make_config(tmp_path)
    write_decision(config)
    broker = FakeBroker()
    summary = submit_slot(config, broker, settings=live_settings(), dry_run=True)
    assert broker.submitted == []
    assert [(r["kind"], r["dry_run"]) for r in read_orders(config)] == [("submitted", True)]
    assert summary["dry_run"] == 1


# ---------------------------------------------------------------------------
# S3 — kill switch (startup + immediately before every submission)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_s3_halt_at_startup_forces_dry_run(tmp_path):
    config = make_config(tmp_path)
    write_decision(config)
    touch_halt(config)
    broker = FakeBroker()
    summary = submit_slot(config, broker, settings=live_settings())
    assert broker.submitted == []
    assert [(r["kind"], r["dry_run"]) for r in read_orders(config)] == [("submitted", True)]
    assert summary["halted"] is True and summary["live"] is False


@pytest.mark.unit
def test_s3_mid_queue_halt_stops_the_remaining_queue(tmp_path):
    config = make_config(tmp_path)
    write_decision(config, ticker="NVDA")
    write_decision(config, ticker="AAPL")
    broker = FakeBroker()
    # The halt file appears WHILE the first order is being submitted.
    broker.submit_hook = lambda spec: touch_halt(config)
    summary = submit_slot(config, broker, settings=live_settings())
    assert [spec.ticker for spec in broker.submitted] == ["NVDA"]
    rows = read_orders(config)
    assert [(r["ticker"], r["kind"], r["dry_run"]) for r in rows] == [
        ("NVDA", "intent", False),
        ("NVDA", "submitted", False),
        ("AAPL", "submitted", True),  # would-place row — never reached the broker
    ]
    assert summary == {**summary, "submitted": 1, "dry_run": 1, "halted": True}


@pytest.mark.unit
def test_s3_mid_queue_halt_skips_sell_cancel_close_as_a_unit(tmp_path):
    """A halt that appears mid-queue must not half-execute a later SELL: the
    cancel+close pair is destructive, so a halted SELL runs entirely dry —
    the held position keeps its protective stop leg at the broker instead of
    being stripped naked with the close suppressed."""
    config = make_config(tmp_path)
    write_decision(config, ticker="NVDA", plan=BUY_PLAN)
    write_decision(config, ticker="AAPL", plan=SELL_PLAN)
    aapl_stop_leg = broker_order(
        "2026-08-04-us-AAPL-entry", ticker="AAPL", side="sell", qty=20, limit=None,
        stop=85.0, oid="aapl-stop-leg",
    )
    broker = FakeBroker(
        positions=[position(ticker="AAPL", qty=20, avg=90.0, mv=1_800.0)],
        open_orders=[aapl_stop_leg],
    )
    # The halt file appears WHILE the NVDA bracket is being submitted.
    broker.submit_hook = lambda spec: touch_halt(config)
    summary = submit_slot(config, broker, settings=live_settings())
    assert [spec.ticker for spec in broker.submitted] == ["NVDA"]
    assert broker.canceled == []  # the stop leg was never touched
    rows = read_orders(config)
    assert [(r["ticker"], r["kind"], r["dry_run"]) for r in rows] == [
        ("NVDA", "intent", False),
        ("NVDA", "submitted", False),
        ("AAPL", "cancel", True),  # would-cancel — recorded, not executed
        ("AAPL", "submitted", True),  # would-close — never reached the broker
    ]
    assert summary["halted"] is True and summary["submitted"] == 1


# ---------------------------------------------------------------------------
# S4 — one entry per ticker per day + intent-row crash recovery
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_s4_dedupe_across_distinct_run_ids(tmp_path):
    """The dedupe key is the ticker/day, NOT run_id: a rerun's n=2 record must
    not double-buy after n=1's live submission."""
    config = make_config(tmp_path)
    append_order_row(config)  # prior LIVE submitted bracket for NVDA today (run n=1)
    write_decision(config, n=2)
    broker = FakeBroker()
    summary = submit_slot(config, broker, settings=live_settings())
    assert broker.submitted == []
    rows = read_orders(config)
    assert (rows[-1]["kind"], rows[-1]["reason"]) == ("skip", "dedupe")
    assert summary["skipped"] == 1


@pytest.mark.unit
def test_s4_dry_run_rows_never_dedupe(tmp_path):
    config = make_config(tmp_path)
    append_order_row(config, dry_run=True)  # yesterday's dry rehearsal row
    write_decision(config)
    broker = FakeBroker()
    summary = submit_slot(config, broker, settings=live_settings())
    assert [spec.ticker for spec in broker.submitted] == ["NVDA"]
    assert summary["submitted"] == 1


@pytest.mark.unit
def test_s4_same_invocation_rerun_executes_only_the_latest_attempt(tmp_path):
    """Ledger current-state rule: the greatest attempt per (ticker, arm) IS
    the decision — the superseded n=1 row produces no orders.jsonl rows and
    the single submission is attributed to n=2 (so the contract's derived
    submitted/filled states join to the current attempt, not a stale one)."""
    config = make_config(tmp_path)
    write_decision(config, n=1)
    write_decision(config, n=2)
    broker = FakeBroker()
    summary = submit_slot(config, broker, settings=live_settings())
    assert len(broker.submitted) == 1
    rows = read_orders(config)
    assert [(r["kind"], r["run_id"]) for r in rows] == [
        ("intent", "2026-08-07-us-NVDA-brief-2"),
        ("submitted", "2026-08-07-us-NVDA-brief-2"),
    ]
    assert summary["submitted"] == 1 and summary["skipped"] == 0


@pytest.mark.unit
def test_flipped_rerun_executes_only_the_current_side(tmp_path):
    """A rerun that flips the decision must never run BOTH plans in one
    invocation: BUY(n=1)→SELL(n=2) executes only the close; SELL(n=1)→
    BUY(n=2) executes only the bracket."""
    config = make_config(tmp_path)
    write_decision(config, n=1, plan=ADDON_PLAN)  # superseded add-on BUY
    write_decision(config, n=2, plan=SELL_PLAN)  # the current decision
    stop_leg = broker_order(
        "2026-08-03-us-NVDA-entry", ticker="NVDA", side="sell", qty=50, limit=None,
        stop=85.0, oid="nvda-stop",
    )
    broker = FakeBroker(positions=[position(qty=50, avg=90.0)], open_orders=[stop_leg])
    summary = submit_slot(config, broker, settings=live_settings())
    assert [(s.side, s.client_order_id) for s in broker.submitted] == [
        ("sell", "2026-08-07-us-NVDA-close-1"),
    ]
    assert broker.canceled == ["nvda-stop"]
    assert summary["submitted"] == 1

    # Reverse flip on a flat ticker: only the current BUY runs.
    config2 = make_config(tmp_path / "reverse")
    write_decision(config2, n=1, plan=SELL_PLAN)
    write_decision(config2, n=2, plan=BUY_PLAN)
    broker2 = FakeBroker()
    submit_slot(config2, broker2, settings=live_settings())
    assert [(s.side, s.client_order_id) for s in broker2.submitted] == [
        ("buy", "2026-08-07-us-NVDA-entry"),
    ]
    assert broker2.canceled == []


@pytest.mark.unit
def test_same_run_live_bracket_is_visible_to_the_sell_cancel_pass(tmp_path):
    """Defense-in-depth behind the latest-attempt reduction: a bracket this
    invocation just placed joins the run's open-orders view, so a later SELL
    cancel pass ('cancel ALL open orders for the ticker') would include it —
    never only the __init__-time broker snapshot."""
    config = make_config(tmp_path)
    write_decision(config)
    broker = FakeBroker()
    run = ea._SubmitRun(
        config=config,
        settings=live_settings(),
        broker=broker,
        account=broker.get_account(),
        session="us",
        slot_date=SLOT,
        dry_run=False,
        clock=clock,
    )
    assert run.open_orders == []
    run.handle_buy(ea.load_decision_records(config.ledger_dir)[0])
    assert [o.client_order_id for o in run.open_orders] == ["2026-08-07-us-NVDA-entry"]


@pytest.mark.unit
def test_s4_dangling_intent_with_order_at_broker_recovers_without_resubmit(tmp_path):
    config = make_config(tmp_path)
    cid = "2026-08-07-us-NVDA-entry"
    append_order_row(config, kind="intent", client_order_id=cid)  # crash before result
    write_decision(config)
    broker = FakeBroker(
        broker_orders={cid: broker_order(cid, status="accepted", qty=100, limit=100.0, stop=90.0)}
    )
    summary = submit_slot(config, broker, settings=live_settings())
    assert broker.submitted == []  # the broker already has it — never resubmitted
    assert broker.client_queries == [cid]
    rows = read_orders(config)
    recovered = rows[-1]
    assert (recovered["kind"], recovered["dry_run"]) == ("submitted", False)
    assert recovered["reason"] == "recovered-intent"
    assert recovered["broker_status"] == "accepted"
    assert summary["recovered"] == 1 and summary["submitted"] == 0


@pytest.mark.unit
def test_s4_dangling_intent_without_broker_order_proceeds(tmp_path):
    config = make_config(tmp_path)
    cid = "2026-08-07-us-NVDA-entry"
    append_order_row(config, kind="intent", client_order_id=cid)
    write_decision(config)
    broker = FakeBroker()  # broker knows nothing about the id
    summary = submit_slot(config, broker, settings=live_settings())
    assert broker.client_queries == [cid]
    assert [spec.client_order_id for spec in broker.submitted] == [cid]
    assert summary["submitted"] == 1


@pytest.mark.unit
def test_s4_recovery_query_error_fails_closed(tmp_path):
    config = make_config(tmp_path)
    append_order_row(config, kind="intent")
    write_decision(config)
    broker = FakeBroker(errors={"get_order_by_client_id"})
    summary = submit_slot(config, broker, settings=live_settings())
    assert broker.submitted == []
    rows = read_orders(config)
    assert (rows[-1]["kind"], rows[-1]["reason"]) == ("skip", "recovery-unavailable")
    assert summary["skipped"] == 1


# ---------------------------------------------------------------------------
# S5 — eligibility recomputed from the DecisionRecord itself
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_s5_non_candidates_produce_no_rows(tmp_path):
    """Rows that are not executable plans at all (HOLD, ERROR, feeds shadow,
    invalid plans, cn session) never reach orders.jsonl."""
    config = make_config(tmp_path)
    write_decision(config, ticker="META", arm="feeds", plan=BUY_PLAN)  # shadow arm
    write_decision(config, ticker="AMZN", plan=None, decision="HOLD", plan_valid=True)
    write_decision(config, ticker="INTC", plan=BUY_PLAN, plan_valid=False)
    write_decision(config, ticker="IBM", plan=None, decision="ERROR", plan_valid=False)
    write_decision(config, ticker="0700.HK", session="cn", plan=BUY_PLAN)
    broker = FakeBroker()
    summary = submit_slot(config, broker, settings=live_settings())
    assert read_orders(config) == []
    assert broker.submitted == []
    assert summary["submitted"] == summary["skipped"] == 0


@pytest.mark.unit
def test_s5_manual_trigger_never_auto_executed(tmp_path):
    config = make_config(tmp_path)
    write_decision(config, trigger="manual")
    broker = FakeBroker()
    submit_slot(config, broker, settings=live_settings())
    rows = read_orders(config)
    assert [(r["kind"], r["reason"]) for r in rows] == [("skip", "manual-trigger")]
    assert broker.submitted == []


@pytest.mark.unit
def test_s5_manual_run_id_with_execute_submits(tmp_path):
    config = make_config(tmp_path)
    row = write_decision(config, trigger="manual")
    broker = FakeBroker()
    summary = submit_slot(
        config, broker, settings=live_settings(), run_id=row["run_id"], execute=True
    )
    assert [spec.ticker for spec in broker.submitted] == ["NVDA"]
    assert summary["submitted"] == 1


@pytest.mark.unit
def test_s5_manual_execute_still_respects_execution_disabled(tmp_path):
    """--execute lifts only the manual-trigger exclusion — S2/S3 still apply."""
    config = make_config(tmp_path)
    row = write_decision(config, trigger="manual")
    broker = FakeBroker()
    summary = submit_slot(config, broker, run_id=row["run_id"], execute=True)  # disabled
    assert broker.submitted == []
    assert [(r["kind"], r["dry_run"]) for r in read_orders(config)] == [("submitted", True)]
    assert summary["dry_run"] == 1


@pytest.mark.unit
@pytest.mark.parametrize(
    ("macro", "ticker_v", "on_missing", "expected_reason"),
    [
        ("pass", "pass", False, None),
        ("warn", "warn", False, None),
        ("fail", "pass", False, "macro-eval-fail"),
        ("pass", "fail", False, "ticker-eval-fail"),
        ("warn", "fail", True, "ticker-eval-fail"),  # fail always blocks
        ("missing", "pass", False, "eval-missing"),
        ("pass", "missing", False, "eval-missing"),
        ("missing", "warn", True, None),  # execute_on_missing_eval opt-in
    ],
)
def test_s5_eligibility_recompute_matrix(tmp_path, macro, ticker_v, on_missing, expected_reason):
    config = make_config(tmp_path)
    write_decision(config, macro=macro, ticker_v=ticker_v)
    broker = FakeBroker()
    submit_slot(
        config, broker, settings=live_settings(execute_on_missing_eval=on_missing)
    )
    rows = read_orders(config)
    if expected_reason is None:
        assert [spec.ticker for spec in broker.submitted] == ["NVDA"]
        assert rows[-1]["kind"] == "submitted"
    else:
        assert broker.submitted == []
        assert [(r["kind"], r["reason"]) for r in rows] == [("skip", expected_reason)]


@pytest.mark.unit
def test_s5_sell_without_position_is_short_blocked(tmp_path):
    config = make_config(tmp_path)
    write_decision(config, plan=SELL_PLAN)
    broker = FakeBroker()  # flat account
    submit_slot(config, broker, settings=live_settings())
    rows = read_orders(config)
    assert [(r["kind"], r["reason"]) for r in rows] == [("skip", "no-position")]
    assert broker.submitted == []


@pytest.mark.unit
def test_s5_close_qty_clamped_to_available_plus_held(tmp_path):
    config = make_config(tmp_path)
    write_decision(config, plan=SELL_PLAN)
    broker = FakeBroker(positions=[position(qty=100, available=40, held=10)])
    submit_slot(config, broker, settings=live_settings())
    assert len(broker.submitted) == 1
    assert broker.submitted[0].qty == 50  # min(100, 40 available + 10 held)
    assert broker.submitted[0].side == "sell"


# ---------------------------------------------------------------------------
# S6 — caps (pinned arithmetic against the fake broker)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_s6_notional_cap_reduces_qty(tmp_path):
    """risk 2% of 100k = 2000, stop distance 2 ⇒ raw qty 1000 (100k notional);
    the 15% cap (15k) reduces it to floor(15000/100) = 150."""
    config = make_config(tmp_path)
    plan = {**BUY_PLAN, "stop": 98.0, "sizing": {"risk_pct": 2.0}}
    write_decision(config, plan=plan)
    broker = FakeBroker()
    submit_slot(config, broker, settings=live_settings())
    assert [spec.qty for spec in broker.submitted] == [150]
    assert read_orders(config)[-1]["qty"] == 150.0


@pytest.mark.unit
def test_s6_qty_zero_after_caps_skips(tmp_path):
    config = make_config(tmp_path)
    write_decision(config)
    broker = FakeBroker(equity=100.0, buying_power=200.0)  # cap 15 < one share
    submit_slot(config, broker, settings=live_settings())
    rows = read_orders(config)
    assert [(r["kind"], r["reason"]) for r in rows] == [("skip", "zero-qty")]


@pytest.mark.unit
def test_s6_gross_exposure_counts_pending_entries(tmp_path):
    """positions MV 60k + open entry notional 31k + new 10k = 101k > 100% of
    100k equity ⇒ skip; at exactly 100k the order goes through."""
    config = make_config(tmp_path)
    write_decision(config)
    pending = broker_order(
        "2026-08-06-us-AAPL-entry", ticker="AAPL", side="buy", qty=310, limit=100.0
    )
    broker = FakeBroker(
        positions=[position(ticker="MSFT", qty=600, avg=100.0, mv=60_000.0)],
        open_orders=[pending],
    )
    submit_slot(config, broker, settings=live_settings())
    rows = read_orders(config)
    assert [(r["kind"], r["reason"]) for r in rows] == [("skip", "gross-exposure")]
    assert broker.submitted == []

    # Boundary: pending 30k ⇒ 60k + 30k + 10k = 100k ≤ 100% — submits. The
    # position and the pending entry carry stop legs so the (fail-closed)
    # total-risk sum stays inside the 5% budget: (100−99)×600 + (100−99)×300
    # + the new (100−90)×100 = 1900 ≤ 5000.
    config2 = make_config(tmp_path / "boundary")
    write_decision(config2)
    pending2_leg = broker_order(
        "leg-aapl", ticker="AAPL", side="sell", qty=300, limit=None, stop=99.0
    )
    pending2 = broker_order(
        "2026-08-06-us-AAPL-entry", ticker="AAPL", side="buy", qty=300, limit=100.0,
        legs=[pending2_leg],
    )
    msft_stop = broker_order(
        "stop-msft", ticker="MSFT", side="sell", qty=600, limit=None, stop=99.0, tif="gtc"
    )
    broker2 = FakeBroker(
        positions=[position(ticker="MSFT", qty=600, avg=100.0, mv=60_000.0)],
        open_orders=[msft_stop, pending2],
    )
    submit_slot(config2, broker2, settings=live_settings())
    assert [spec.ticker for spec in broker2.submitted] == ["NVDA"]


@pytest.mark.unit
def test_s6_max_positions_counts_positions_and_pending_entries(tmp_path):
    config = make_config(tmp_path)
    write_decision(config)  # NVDA would be the 3rd name
    broker = FakeBroker(
        positions=[position(ticker="MSFT", qty=10, avg=100.0, mv=1000.0)],
        open_orders=[
            broker_order("2026-08-06-us-AAPL-entry", ticker="AAPL", side="buy", qty=5, limit=100.0)
        ],
    )
    submit_slot(config, broker, settings=live_settings(max_open_positions=2))
    rows = read_orders(config)
    assert [(r["kind"], r["reason"]) for r in rows] == [("skip", "max-positions")]


@pytest.mark.unit
def test_s6_live_submissions_per_slot_cap(tmp_path):
    config = make_config(tmp_path)
    write_decision(config, ticker="AAPL")
    write_decision(config, ticker="NVDA")
    broker = FakeBroker()
    summary = submit_slot(config, broker, settings=live_settings(max_orders_per_slot=1))
    assert [spec.ticker for spec in broker.submitted] == ["AAPL"]
    rows = read_orders(config)
    assert (rows[-1]["ticker"], rows[-1]["reason"]) == ("NVDA", "max-orders-per-slot")
    assert summary["submitted"] == 1 and summary["skipped"] == 1

    # Prior live submissions recorded in the ledger count against the budget
    # too (a recovery rerun cannot double the slot's order budget).
    config2 = make_config(tmp_path / "prior")
    append_order_row(
        config2, ticker="MSFT", client_order_id=f"{DATE_ISO}-us-MSFT-entry",
        run_id=f"{DATE_ISO}-us-MSFT-brief-1",
    )
    write_decision(config2, ticker="NVDA")
    broker2 = FakeBroker()
    submit_slot(config2, broker2, settings=live_settings(max_orders_per_slot=1))
    assert broker2.submitted == []
    assert read_orders(config2)[-1]["reason"] == "max-orders-per-slot"


# ---------------------------------------------------------------------------
# S7 — secrets never reach ledger, logs, status, or stdout
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_s7_secrets_never_written_anywhere(tmp_path, monkeypatch, capsys):
    api_secret = "AK-SUPER-SECRET-KEY-42XYZ"
    monkeypatch.setenv("TRADINGAGENTS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("TRADINGAGENTS_EXECUTION_ENABLED", "1")
    monkeypatch.setenv("ALPACA_API_KEY", api_secret)
    monkeypatch.setenv("ALPACA_SECRET_KEY", api_secret + "-SS")
    config = make_config(tmp_path)
    write_decision(config)
    broker = FakeBroker()
    assert (
        ea.main(["submit", "--date", DATE_ISO], broker_factory=lambda: broker, clock=clock) == 0
    )
    captured = capsys.readouterr()
    assert api_secret not in captured.out + captured.err
    for path in (tmp_path / "state").rglob("*"):
        if path.is_file():
            assert api_secret not in path.read_text(encoding="utf-8"), path


@pytest.mark.unit
def test_s7_real_broker_factory_error_paths_never_echo_secrets(tmp_path, monkeypatch, capsys):
    """S7 on the REAL credential path: the fake-factory test above never
    routes the secrets through the adapter's own code, so here main() runs
    the default broker factory itself (the alpaca import is forced to fail
    deterministically — offline either way) and every surfaced error must be
    free of the key material."""
    api_secret = "AK-REAL-PATH-SECRET-77XYZ"
    monkeypatch.chdir(tmp_path)  # load_dotenv must not pick up the repo .env
    monkeypatch.setenv("TRADINGAGENTS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("ALPACA_API_KEY", api_secret)
    monkeypatch.setenv("ALPACA_SECRET_KEY", api_secret + "-SS")
    monkeypatch.setitem(sys.modules, "alpaca", None)  # lazy import ⇒ ImportError
    config = make_config(tmp_path)
    write_decision(config)
    assert ea.main(["submit", "--date", DATE_ISO], clock=clock) == 1
    captured = capsys.readouterr()
    assert api_secret not in captured.out + captured.err
    assert read_orders(config) == []

    # Missing-credentials BrokerError: the message names the variables, never
    # values, and nothing else leaks either.
    monkeypatch.delenv("ALPACA_API_KEY")
    monkeypatch.delenv("ALPACA_SECRET_KEY")
    assert ea.main(["submit", "--date", DATE_ISO], clock=clock) == 1
    err = capsys.readouterr().err
    assert "ALPACA_API_KEY" in err and api_secret not in err
    for path in (tmp_path / "state").rglob("*"):
        if path.is_file():
            assert api_secret not in path.read_text(encoding="utf-8"), path


# ---------------------------------------------------------------------------
# S8 — market-state guards, fail-closed
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_s8_market_closed_skips_all(tmp_path):
    config = make_config(tmp_path)
    write_decision(config, ticker="NVDA")
    write_decision(config, ticker="AAPL", plan=SELL_PLAN)
    broker = FakeBroker(trading_day=False, positions=[position(ticker="AAPL", qty=10)])
    submit_slot(config, broker, settings=live_settings())
    rows = read_orders(config)
    assert [(r["ticker"], r["kind"], r["reason"]) for r in rows] == [
        ("NVDA", "skip", "market-closed"),
        ("AAPL", "skip", "market-closed"),
    ]
    assert broker.submitted == [] and broker.canceled == []


@pytest.mark.unit
@pytest.mark.parametrize(
    "failing",
    ["get_clock", "get_calendar_today", "get_asset", "latest_quote",
     "list_positions", "list_open_orders"],
)
def test_s8_any_guard_source_error_fails_closed(tmp_path, failing):
    config = make_config(tmp_path)
    write_decision(config)
    broker = FakeBroker(errors={failing})
    submit_slot(config, broker, settings=live_settings())
    rows = read_orders(config)
    assert [(r["kind"], r["reason"]) for r in rows] == [("skip", "guard-unavailable")]
    assert broker.submitted == []


@pytest.mark.unit
def test_s8_not_tradable_asset_skips(tmp_path):
    config = make_config(tmp_path)
    write_decision(config)
    broker = FakeBroker(assets={"NVDA": ea.AssetInfo(symbol="NVDA", tradable=False)})
    submit_slot(config, broker, settings=live_settings())
    assert [(r["kind"], r["reason"]) for r in read_orders(config)] == [("skip", "not-tradable")]


@pytest.mark.unit
def test_s8_stale_quote_skips_and_fresh_quote_passes(tmp_path):
    config = make_config(tmp_path)
    write_decision(config)
    stale = ea.Quote(bid=99.5, ask=100.0, timestamp=NOW - timedelta(minutes=16))
    broker = FakeBroker(quotes={"NVDA": stale})
    submit_slot(config, broker, settings=live_settings())
    assert [(r["kind"], r["reason"]) for r in read_orders(config)] == [("skip", "stale-quote")]

    config2 = make_config(tmp_path / "fresh")
    write_decision(config2)
    fresh_14m = ea.Quote(bid=99.5, ask=100.0, timestamp=NOW - timedelta(minutes=14))
    broker2 = FakeBroker(quotes={"NVDA": fresh_14m})
    submit_slot(config2, broker2, settings=live_settings())
    assert [spec.ticker for spec in broker2.submitted] == ["NVDA"]


@pytest.mark.unit
def test_s8_quote_age_measured_at_check_time_not_slot_start(tmp_path):
    """The quote-age guard is the operational halt proxy: as the queue drags,
    staleness must be measured against the clock at the moment of the check
    — a quote 10 min old at slot start but 30 min old by the time its
    candidate is reached is stale, not fresh."""
    config = make_config(tmp_path)
    write_decision(config, ticker="AAPL")
    write_decision(config, ticker="NVDA")
    broker = FakeBroker(
        quotes={"NVDA": ea.Quote(bid=99.5, ask=100.0, timestamp=NOW - timedelta(minutes=10))}
    )
    # The AAPL submission takes 20 minutes of broker time (slow queue).
    broker.submit_hook = lambda spec: setattr(
        broker, "now", NOW + timedelta(minutes=20)
    )
    submit_slot(config, broker, settings=live_settings())
    assert [spec.ticker for spec in broker.submitted] == ["AAPL"]
    rows = read_orders(config)
    assert (rows[-1]["ticker"], rows[-1]["kind"], rows[-1]["reason"]) == (
        "NVDA", "skip", "stale-quote",
    )


# ---------------------------------------------------------------------------
# S9 — portfolio constraints (maintain, add-on matrix, buying power, risk)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_s9_buy_on_held_ticker_without_add_intent_is_maintain(tmp_path):
    config = make_config(tmp_path)
    write_decision(config)  # add_intent False
    broker = FakeBroker(positions=[position(qty=50, avg=95.0)])
    submit_slot(config, broker, settings=live_settings())
    assert [(r["kind"], r["reason"]) for r in read_orders(config)] == [("skip", "maintain")]
    assert broker.submitted == []


def addon_scenario(config, *, fill_day="2026-08-03", fill_price=95.0, tranches=1, mv=4500.0):
    """A held NVDA position with ledger fill history for the add-on rules.

    Defaults satisfy EVERY add-on condition for ``ADDON_PLAN`` (entry zone
    [99, 100]): 1 tranche < 2; last fill 95 < 99 (pyramid-up); Monday 08-03 →
    Friday 08-07 = 4 trading days ≥ 3; MV 4500 + notional 10000 ≤ 20% of 100k.
    The position carries its GTC stop leg at the broker (risk (90−85)×50 =
    250), keeping the fail-closed total-risk sum inside the 5% budget.
    """
    seed_entry_fill(config, "NVDA", fill_day, fill_price)
    if tranches >= 2:
        seed_entry_fill(config, "NVDA", "2026-08-05", fill_price)
    write_decision(config, plan=ADDON_PLAN, n=9)
    stop_leg = broker_order(
        f"{fill_day}-us-NVDA-entry", ticker="NVDA", side="sell", qty=50, limit=None,
        stop=85.0, oid="nvda-stop-leg",
    )
    return FakeBroker(positions=[position(qty=50, avg=90.0, mv=mv)], open_orders=[stop_leg])


@pytest.mark.unit
def test_s9_addon_all_conditions_met_submits_second_tranche(tmp_path):
    config = make_config(tmp_path)
    broker = addon_scenario(config)
    summary = submit_slot(config, broker, settings=live_settings())
    assert [spec.client_order_id for spec in broker.submitted] == ["2026-08-07-us-NVDA-entry"]
    assert broker.submitted[0].order_class == "bracket"  # every tranche its own bracket
    assert summary["submitted"] == 1


@pytest.mark.unit
def test_s9_addon_tranche_cap_violation(tmp_path):
    config = make_config(tmp_path)
    broker = addon_scenario(config, tranches=2)
    submit_slot(config, broker, settings=live_settings())
    assert read_orders(config)[-1]["reason"] == "max-tranches"
    assert broker.submitted == []


@pytest.mark.unit
def test_s9_addon_pyramid_up_violation(tmp_path):
    """entry_zone[0] = 99 not above the last fill (101) — averaging down and
    flat adds stay excluded."""
    config = make_config(tmp_path)
    broker = addon_scenario(config, fill_price=101.0)
    submit_slot(config, broker, settings=live_settings())
    assert read_orders(config)[-1]["reason"] == "pyramid-up-only"
    assert broker.submitted == []


@pytest.mark.unit
def test_s9_addon_spacing_violation(tmp_path):
    """Last fill Thursday 08-06 → Friday 08-07 is 1 trading day < 3."""
    config = make_config(tmp_path)
    broker = addon_scenario(config, fill_day="2026-08-06")
    submit_slot(config, broker, settings=live_settings())
    assert read_orders(config)[-1]["reason"] == "add-spacing"
    assert broker.submitted == []


@pytest.mark.unit
def test_s9_addon_per_ticker_notional_violation(tmp_path):
    """position MV 12000 + new notional 10000 = 22000 > 20% of 100k equity."""
    config = make_config(tmp_path)
    broker = addon_scenario(config, mv=12_000.0)
    submit_slot(config, broker, settings=live_settings())
    assert read_orders(config)[-1]["reason"] == "per-ticker-notional-cap"
    assert broker.submitted == []


@pytest.mark.unit
def test_s9_buying_power_checked_before_submit(tmp_path):
    config = make_config(tmp_path)
    write_decision(config)
    broker = FakeBroker(buying_power=5_000.0)  # notional 10000 > 5000
    submit_slot(config, broker, settings=live_settings())
    assert [(r["kind"], r["reason"]) for r in read_orders(config)] == [("skip", "buying-power")]
    assert broker.submitted == []


def total_risk_fixture():
    """Pinned open-risk state: position risk (50−40)×100 = 1000; pending
    entry (100−90)×10 = 100; the new NVDA order adds (100−90)×100 = 1000 —
    total 2100."""
    msft_stop = broker_order(
        "stop-msft", ticker="MSFT", side="sell", qty=100, limit=None, stop=40.0, tif="gtc"
    )
    pending_leg = broker_order(
        "leg-aapl", ticker="AAPL", side="sell", qty=10, limit=None, stop=90.0
    )
    pending = broker_order(
        "2026-08-06-us-AAPL-entry", ticker="AAPL", side="buy", qty=10, limit=100.0,
        legs=[pending_leg],
    )
    return FakeBroker(
        positions=[position(ticker="MSFT", qty=100, avg=50.0, mv=5_000.0)],
        open_orders=[msft_stop, pending],
    )


@pytest.mark.unit
def test_s9_total_open_risk_cap_arithmetic(tmp_path):
    # Budget 3% of 100k = 3000 ≥ 2100 — submits.
    config = make_config(tmp_path)
    write_decision(config)
    broker = total_risk_fixture()
    submit_slot(config, broker, settings=live_settings(max_total_risk=0.03))
    assert [spec.ticker for spec in broker.submitted] == ["NVDA"]

    # Budget 2% of 100k = 2000 < 2100 — the order is skipped.
    config2 = make_config(tmp_path / "tight")
    write_decision(config2)
    broker2 = total_risk_fixture()
    submit_slot(config2, broker2, settings=live_settings(max_total_risk=0.02))
    assert [(r["kind"], r["reason"]) for r in read_orders(config2)] == [("skip", "total-risk")]
    assert broker2.submitted == []


@pytest.mark.unit
def test_s9_position_without_stop_leg_counts_full_value_in_open_risk(tmp_path):
    """Fail-closed: a position whose protective stop is gone (e.g. canceled
    by hand in the broker UI) contributes its FULL entry value to the
    total-risk sum — it must block new BUYs, never loosen the cap to zero."""
    config = make_config(tmp_path)
    write_decision(config)
    broker = FakeBroker(
        positions=[position(ticker="MSFT", qty=100, avg=100.0, mv=10_000.0)],  # no stop leg
    )
    submit_slot(config, broker, settings=live_settings())
    # 10 000 (full MSFT value) + 1 000 (new) > 5% of 100k = 5 000 ⇒ skip.
    assert [(r["kind"], r["reason"]) for r in read_orders(config)] == [("skip", "total-risk")]
    assert broker.submitted == []

    # Same posture for a pending entry with no resolvable stop leg: its full
    # notional (100×100) is the open risk.
    config2 = make_config(tmp_path / "pending")
    write_decision(config2)
    naked_pending = broker_order(
        "2026-08-06-us-AAPL-entry", ticker="AAPL", side="buy", qty=100, limit=100.0
    )
    broker2 = FakeBroker(open_orders=[naked_pending])
    submit_slot(config2, broker2, settings=live_settings())
    assert [(r["kind"], r["reason"]) for r in read_orders(config2)] == [("skip", "total-risk")]
    assert broker2.submitted == []


# ---------------------------------------------------------------------------
# Order mapping (AC2) — SELL cancel-then-close, close price fallback
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_sell_cancels_all_ticker_orders_then_submits_day_limit_close(tmp_path):
    config = make_config(tmp_path)
    write_decision(config, plan=SELL_PLAN)
    nvda_leg = broker_order(
        "2026-08-03-us-NVDA-entry", ticker="NVDA", side="sell", qty=20, limit=None,
        stop=85.0, oid="nvda-leg",
    )
    aapl_order = broker_order(
        "2026-08-06-us-AAPL-entry", ticker="AAPL", side="buy", qty=5, limit=100.0,
        oid="aapl-order",
    )
    broker = FakeBroker(
        positions=[position(qty=20, avg=90.0, mv=1_800.0)],
        open_orders=[nvda_leg, aapl_order],
    )
    submit_slot(config, broker, settings=live_settings())
    # Step 1: ALL open NVDA orders canceled (legs incl.) — never AAPL's.
    assert broker.canceled == ["nvda-leg"]
    # Step 2: DAY limit close for the full position at entry_zone[0].
    assert len(broker.submitted) == 1
    spec = broker.submitted[0]
    assert (spec.side, spec.tif, spec.qty, spec.limit_price) == ("sell", "day", 20, 95.0)
    assert spec.client_order_id == "2026-08-07-us-NVDA-close-1"
    assert spec.order_class is None
    rows = read_orders(config)
    assert [(r["kind"], r["client_order_id"]) for r in rows] == [
        ("cancel", "2026-08-03-us-NVDA-entry"),
        ("intent", "2026-08-07-us-NVDA-close-1"),
        ("submitted", "2026-08-07-us-NVDA-close-1"),
    ]
    assert rows[0]["order_kind"] == "bracket"
    assert rows[2]["order_kind"] == "close"


@pytest.mark.unit
def test_sell_close_price_falls_back_to_live_bid_discount(tmp_path):
    config = make_config(tmp_path)
    write_decision(config, plan=SELL_PLAN_NO_ZONE)
    broker = FakeBroker(positions=[position(qty=10, avg=90.0, mv=900.0)])
    submit_slot(config, broker, settings=live_settings())
    # bid 99.5 × (1 − 0.002) = 99.301 → 99.30 (marketable limit, live quote).
    assert broker.submitted[0].limit_price == pytest.approx(99.3)


@pytest.mark.unit
def test_sell_cancel_failure_fails_closed_without_a_close(tmp_path):
    """A leg that cannot be canceled would make the close double-sell into a
    short — the close is skipped instead."""
    config = make_config(tmp_path)
    write_decision(config, plan=SELL_PLAN)
    leg = broker_order(
        "2026-08-03-us-NVDA-entry", ticker="NVDA", side="sell", qty=20, limit=None,
        stop=85.0, oid="nvda-leg",
    )
    broker = FakeBroker(
        positions=[position(qty=20, avg=90.0)],
        open_orders=[leg],
        errors={"cancel_order_fail"},
    )
    summary = submit_slot(config, broker, settings=live_settings())
    assert broker.submitted == []
    assert read_orders(config)[-1]["reason"] == "cancel-failed"
    assert summary["errors"] == 1


@pytest.mark.unit
def test_trading_days_since_weekday_arithmetic():
    friday, monday = date(2026, 8, 7), date(2026, 8, 3)
    assert ea.trading_days_since(monday, friday) == 4  # Tue..Fri
    assert ea.trading_days_since(date(2026, 8, 6), friday) == 1
    assert ea.trading_days_since(friday, friday) == 0
    assert ea.trading_days_since(friday, date(2026, 8, 10)) == 1  # Fri → Mon


# ---------------------------------------------------------------------------
# refresh — status rows, stale-entry cancels, close re-submission, snapshot
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_refresh_appends_rows_for_non_terminal_orders_only(tmp_path):
    config = make_config(tmp_path)
    live_cid = f"{DATE_ISO}-us-NVDA-entry"
    append_order_row(config, client_order_id=live_cid)  # accepted — non-terminal
    # A second order already terminal in the ledger — never re-queried.
    done_cid = "2026-08-05-us-MSFT-entry"
    append_order_row(
        config, ticker="MSFT", client_order_id=done_cid,
        run_id="2026-08-05-us-MSFT-brief-1", written_at="2026-08-05T13:00:00Z",
    )
    append_order_row(
        config, ticker="MSFT", client_order_id=done_cid, kind="refresh", order_kind=None,
        run_id="2026-08-05-us-MSFT-brief-1", written_at="2026-08-05T14:00:00Z",
        broker_status="filled", filled_qty=10, filled_avg_price=99.0,
    )
    broker = FakeBroker(
        broker_orders={
            live_cid: broker_order(
                live_cid, status="partially_filled", qty=10, filled=5, favg=99.5
            )
        }
    )
    summary = refresh(config, broker)
    assert broker.client_queries == [live_cid]  # the terminal order stayed untouched
    row = read_orders(config)[-1]
    assert (row["kind"], row["client_order_id"], row["order_kind"]) == (
        "refresh", live_cid, None,
    )
    assert (row["broker_status"], row["filled_qty"], row["filled_avg_price"]) == (
        "partially_filled", 5.0, 99.5,
    )
    assert summary["refreshed"] == 1
    assert broker.submitted == []  # refresh never submits new entries


@pytest.mark.unit
def test_refresh_cancels_stale_unfilled_entry_but_never_a_filled_entry(tmp_path):
    """AC2: a 2-day-old unfilled entry is canceled; a partially/fully filled
    entry (whose GTC legs protect a position) is never touched; today's entry
    is left to fill."""
    config = make_config(tmp_path)
    stale_cid = "2026-08-05-us-NVDA-entry"
    filled_cid = "2026-08-05-us-AAPL-entry"
    fresh_cid = f"{DATE_ISO}-us-MSFT-entry"
    append_order_row(
        config, client_order_id=stale_cid, written_at="2026-08-05T13:00:00Z",
        run_id="2026-08-05-us-NVDA-brief-1",
    )
    append_order_row(
        config, ticker="AAPL", client_order_id=filled_cid, written_at="2026-08-05T13:00:00Z",
        run_id="2026-08-05-us-AAPL-brief-1",
    )
    append_order_row(
        config, ticker="MSFT", client_order_id=fresh_cid,
        run_id=f"{DATE_ISO}-us-MSFT-brief-1",
    )
    aapl_leg = broker_order(
        "leg-aapl", ticker="AAPL", side="sell", qty=8, limit=None, stop=85.0, oid="aapl-leg-oid"
    )
    broker = FakeBroker(
        broker_orders={
            stale_cid: broker_order(stale_cid, status="accepted", oid="oid-stale"),
            filled_cid: broker_order(
                filled_cid, ticker="AAPL", status="partially_filled", qty=8, filled=8,
                favg=98.0, oid="oid-filled",
            ),
            fresh_cid: broker_order(fresh_cid, ticker="MSFT", status="new", oid="oid-fresh"),
        },
        open_orders=[aapl_leg],
    )
    summary = refresh(config, broker)
    assert broker.canceled == ["oid-stale"]  # never the filled entry, never legs
    cancel_rows = [r for r in read_orders(config) if r["kind"] == "cancel"]
    assert [(r["client_order_id"], r["reason"]) for r in cancel_rows] == [
        (stale_cid, "stale-entry"),
    ]
    assert summary["canceled_stale"] == 1


@pytest.mark.unit
def test_refresh_resubmits_unfilled_close_exactly_once_under_close_2(tmp_path):
    config = make_config(tmp_path)
    close_1 = "2026-08-06-us-NVDA-close-1"
    append_order_row(
        config, client_order_id=close_1, order_kind="close", tif="day", qty=50,
        limit_price=95.0, stop_price=None, target_price=None,
        run_id="2026-08-06-us-NVDA-brief-1", written_at="2026-08-06T13:30:00Z",
    )
    broker = FakeBroker(
        broker_orders={
            close_1: broker_order(close_1, side="sell", qty=50, limit=95.0,
                                  status="expired", tif="day")
        },
        positions=[position(qty=50, avg=90.0)],
    )
    summary = refresh(config, broker, settings=live_settings())
    assert [spec.client_order_id for spec in broker.submitted] == ["2026-08-06-us-NVDA-close-2"]
    spec = broker.submitted[0]
    assert (spec.side, spec.tif, spec.qty) == ("sell", "day", 50)
    assert spec.limit_price == pytest.approx(99.3)  # fresh marketable limit
    tail = read_orders(config)[-2:]
    assert [(r["kind"], r["client_order_id"]) for r in tail] == [
        ("intent", "2026-08-06-us-NVDA-close-2"),
        ("submitted", "2026-08-06-us-NVDA-close-2"),
    ]
    assert summary["close_resubmitted"] == 1


@pytest.mark.unit
def test_refresh_escalates_after_second_unfilled_close_attempt(tmp_path):
    config = make_config(tmp_path)
    close_1 = "2026-08-06-us-NVDA-close-1"
    close_2 = "2026-08-06-us-NVDA-close-2"
    append_order_row(
        config, client_order_id=close_1, order_kind="close", tif="day", qty=50,
        limit_price=95.0, run_id="2026-08-06-us-NVDA-brief-1",
        written_at="2026-08-06T13:30:00Z",
    )
    append_order_row(
        config, client_order_id=close_1, kind="refresh", order_kind=None, qty=50,
        broker_status="expired", run_id="2026-08-06-us-NVDA-brief-1",
        written_at="2026-08-06T21:30:00Z",
    )
    append_order_row(
        config, client_order_id=close_2, order_kind="close", tif="day", qty=50,
        limit_price=94.0, run_id="2026-08-06-us-NVDA-brief-1",
        written_at="2026-08-06T22:00:00Z",
    )
    broker = FakeBroker(
        broker_orders={
            close_2: broker_order(close_2, side="sell", qty=50, limit=94.0,
                                  status="expired", tif="day")
        },
        positions=[position(qty=50, avg=90.0)],
    )
    summary = refresh(config, broker, settings=live_settings())
    assert broker.submitted == []  # once-only: no -close-3 ever
    assert any(alert["kind"] == "close-unfilled" for alert in summary["alerts"])
    assert summary["close_resubmitted"] == 0


@pytest.mark.unit
@pytest.mark.parametrize(
    ("halted", "reason"),
    [
        (True, "halt"),  # halt file present, execution otherwise enabled
        (False, "execution-disabled"),  # default settings: S2 opt-in is off
    ],
)
def test_refresh_close_resubmit_gated_by_halt_and_enablement(tmp_path, halted, reason):
    config = make_config(tmp_path)
    close_1 = "2026-08-06-us-NVDA-close-1"
    append_order_row(
        config, client_order_id=close_1, order_kind="close", tif="day", qty=50,
        limit_price=95.0, run_id="2026-08-06-us-NVDA-brief-1",
        written_at="2026-08-06T13:30:00Z",
    )
    if halted:
        touch_halt(config)
    broker = FakeBroker(
        broker_orders={
            close_1: broker_order(close_1, side="sell", qty=50, limit=95.0,
                                  status="expired", tif="day")
        },
        positions=[position(qty=50, avg=90.0)],
    )
    summary = refresh(
        config, broker, settings=live_settings() if halted else ea.AdapterSettings()
    )
    assert broker.submitted == []
    skip_rows = [r for r in read_orders(config) if r["kind"] == "skip"]
    assert [(r["client_order_id"], r["reason"]) for r in skip_rows] == [
        ("2026-08-06-us-NVDA-close-2", reason),
    ]
    assert summary["skipped"] == 1


def seed_expired_close(config):
    """An expired-unfilled close attempt 1 with its terminal refresh row."""
    close_1 = "2026-08-06-us-NVDA-close-1"
    append_order_row(
        config, client_order_id=close_1, order_kind="close", tif="day", qty=50,
        limit_price=95.0, stop_price=None, target_price=None,
        run_id="2026-08-06-us-NVDA-brief-1", written_at="2026-08-06T13:30:00Z",
    )
    append_order_row(
        config, client_order_id=close_1, kind="refresh", order_kind=None, qty=50,
        broker_status="expired", run_id="2026-08-06-us-NVDA-brief-1",
        written_at="2026-08-06T21:30:00Z",
    )
    return close_1


@pytest.mark.unit
def test_refresh_transient_skip_does_not_consume_the_close_resubmission(tmp_path):
    """A guard-unavailable (or halt/disabled) skip is NOT the once-only
    attempt: the next healthy refresh still re-submits under -close-2 — a
    transient outage must not leave the position open forever in silence."""
    config = make_config(tmp_path)
    seed_expired_close(config)
    # Refresh 1: the quote source is down ⇒ skip guard-unavailable + alert.
    broker1 = FakeBroker(positions=[position(qty=50, avg=90.0)], errors={"latest_quote"})
    summary1 = refresh(config, broker1, settings=live_settings())
    assert broker1.submitted == []
    assert [a["kind"] for a in summary1["alerts"]] == ["close-unfilled"]
    assert read_orders(config)[-1]["reason"] == "guard-unavailable"
    # Refresh 2 (next day, healthy): the retry actually happens.
    broker2 = FakeBroker(positions=[position(qty=50, avg=90.0)])
    summary2 = refresh(config, broker2, settings=live_settings())
    assert [s.client_order_id for s in broker2.submitted] == ["2026-08-06-us-NVDA-close-2"]
    assert summary2["close_resubmitted"] == 1


@pytest.mark.unit
def test_refresh_real_resubmit_attempt_consumes_and_keeps_escalating(tmp_path):
    """A -close-2 attempt that reached the broker seam (intent written, the
    submit call errored) IS consumed — never a third order — but every later
    refresh keeps alerting while the close remains unfilled."""
    config = make_config(tmp_path)
    seed_expired_close(config)
    broker1 = FakeBroker(positions=[position(qty=50, avg=90.0)], errors={"submit_order"})
    summary1 = refresh(config, broker1, settings=live_settings())
    assert summary1["close_resubmitted"] == 0 and summary1["errors"] == 1
    tail = read_orders(config)[-2:]
    assert [(r["kind"], r["client_order_id"]) for r in tail] == [
        ("intent", "2026-08-06-us-NVDA-close-2"),
        ("skip", "2026-08-06-us-NVDA-close-2"),
    ]
    # Later refreshes: no resubmission (the intent may live at the broker),
    # but the unfilled close keeps surfacing as an alert.
    broker2 = FakeBroker(positions=[position(qty=50, avg=90.0)])
    summary2 = refresh(config, broker2, settings=live_settings())
    assert broker2.submitted == []
    assert [a["kind"] for a in summary2["alerts"]] == ["close-unfilled"]
    assert summary2["close_resubmitted"] == 0


@pytest.mark.unit
def test_refresh_close_resubmit_quote_age_uses_check_time_clock(tmp_path):
    """Same S8 posture as submit: the re-submission's quote-age check reads
    the clock at the check, not the refresh-start snapshot."""
    config = make_config(tmp_path)
    seed_expired_close(config)
    broker = FakeBroker(
        positions=[position(qty=50, avg=90.0)],
        quotes={"NVDA": ea.Quote(bid=99.5, ask=100.0, timestamp=NOW - timedelta(minutes=10))},
    )
    original_quote = broker.latest_quote

    def quote_then_slow_clock(ticker):
        result = original_quote(ticker)
        broker.now = NOW + timedelta(minutes=20)  # 30 min have truly passed
        return result

    broker.latest_quote = quote_then_slow_clock
    summary = refresh(config, broker, settings=live_settings())
    assert broker.submitted == []
    assert read_orders(config)[-1]["reason"] == "stale-quote"
    assert summary["skipped"] == 1


@pytest.mark.unit
def test_refresh_alerts_on_external_reject_and_cancel_only(tmp_path):
    config = make_config(tmp_path)
    rejected_cid = f"{DATE_ISO}-us-NVDA-entry"
    ours_cid = "2026-08-06-us-AAPL-entry"
    append_order_row(config, client_order_id=rejected_cid)
    append_order_row(
        config, ticker="AAPL", client_order_id=ours_cid, written_at="2026-08-06T13:00:00Z",
        run_id="2026-08-06-us-AAPL-brief-1",
    )
    # We canceled AAPL ourselves — a cancel row exists, so no alert for it.
    append_order_row(
        config, ticker="AAPL", client_order_id=ours_cid, kind="cancel", order_kind="bracket",
        written_at="2026-08-06T14:00:00Z", run_id="2026-08-06-us-AAPL-brief-1",
        broker_status=None,
    )
    broker = FakeBroker(
        broker_orders={
            rejected_cid: broker_order(rejected_cid, status="rejected"),
            ours_cid: broker_order(ours_cid, ticker="AAPL", status="canceled"),
        }
    )
    summary = refresh(config, broker)
    assert summary["rejected"] == 1
    assert summary["canceled"] == 0
    assert [alert["kind"] for alert in summary["alerts"]] == ["rejected"]
    assert summary["alerts"][0]["client_order_id"] == rejected_cid


@pytest.mark.unit
def test_refresh_rewrites_positions_snapshot_from_live_broker_state(tmp_path):
    config = make_config(tmp_path)
    seed_entry_fill(config, "NVDA", "2026-08-03", 283.6, qty=12)
    stop_leg = broker_order(
        "2026-08-03-us-NVDA-entry", ticker="NVDA", side="sell", qty=12, limit=None,
        stop=268.0, oid="leg-stop",
    )
    target_leg = broker_order(
        "2026-08-03-us-NVDA-entry", ticker="NVDA", side="sell", qty=12, limit=301.0,
        oid="leg-target",
    )
    broker = FakeBroker(
        positions=[
            position(qty=12, avg=283.6, mv=3_520.0, upl=114.7),
        ],
        open_orders=[stop_leg, target_leg],
    )
    refresh(config, broker)
    snapshot = PositionsSnapshot.parse_lenient(
        json.loads((config.ledger_dir / "positions.json").read_text(encoding="utf-8"))
    )
    assert snapshot.equity == 100_000.0
    assert snapshot.as_of == NOW
    assert len(snapshot.positions) == 1
    entry = snapshot.positions[0]
    assert (entry.ticker, entry.qty, entry.avg_entry) == ("NVDA", 12.0, 283.6)
    assert entry.tranches == 1  # counted from filled entry brackets
    assert entry.last_fill_at == datetime(2026, 8, 3, 14, 30, tzinfo=timezone.utc)
    assert [(leg.leg, leg.price) for leg in entry.open_orders] == [
        ("stop", 268.0), ("target", 301.0),
    ]
    assert broker.submitted == []  # never a new entry


@pytest.mark.unit
def test_refresh_snapshot_last_fill_fallback_is_stable_not_now(tmp_path):
    """A position whose fill was never captured in the ledger (fills landed
    while refresh queries were erroring) falls back to the entry's stable
    submission instant — never the refresh time, which would present a stale
    position as freshly acquired on every refresh. Only a fully out-of-band
    position (no ledger rows at all) gets the refresh instant."""
    config = make_config(tmp_path)
    submitted_at = "2026-08-05T13:00:00Z"
    append_order_row(
        config, client_order_id="2026-08-05-us-NVDA-entry", written_at=submitted_at,
        run_id="2026-08-05-us-NVDA-brief-1", broker_status="accepted",
    )
    broker = FakeBroker(
        positions=[
            position(qty=10, avg=95.0, mv=950.0),
            position(ticker="GME", qty=5, avg=20.0, mv=100.0),  # out-of-band
        ]
    )
    refresh(config, broker)
    snapshot = PositionsSnapshot.parse_lenient(
        json.loads((config.ledger_dir / "positions.json").read_text(encoding="utf-8"))
    )
    by_ticker = {entry.ticker: entry for entry in snapshot.positions}
    assert by_ticker["NVDA"].last_fill_at == datetime(2026, 8, 5, 13, 0, tzinfo=timezone.utc)
    assert by_ticker["GME"].last_fill_at == NOW  # last resort only


# ---------------------------------------------------------------------------
# AC3 — a fixture slot of mixed plans ⇒ the exact expected orders.jsonl
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_ac3_mixed_fixture_slot_produces_exact_dry_run_ledger(tmp_path):
    config = make_config(tmp_path)
    write_decision(config, ticker="NVDA", plan=BUY_PLAN)  # eligible BUY
    write_decision(config, ticker="AAPL", plan=SELL_PLAN)  # close of a held position
    write_decision(config, ticker="MSFT", plan=BUY_PLAN, trigger="manual")
    write_decision(config, ticker="TSLA", plan=BUY_PLAN, ticker_v="fail")
    write_decision(config, ticker="AMZN", plan=None, decision="HOLD", plan_valid=True)
    write_decision(config, ticker="META", plan=BUY_PLAN, arm="feeds")  # shadow
    write_decision(config, ticker="INTC", plan=BUY_PLAN, plan_valid=False)
    write_decision(config, ticker="ORCL", plan=BUY_PLAN)  # held, no add_intent
    aapl_leg = broker_order(
        "2026-08-04-us-AAPL-entry", ticker="AAPL", side="sell", qty=20, limit=None,
        stop=85.0, oid="aapl-leg",
    )
    broker = FakeBroker(
        positions=[
            position(ticker="AAPL", qty=20, avg=90.0, mv=1_800.0),
            position(ticker="ORCL", qty=10, avg=90.0, mv=900.0),
        ],
        open_orders=[aapl_leg],
    )
    summary = submit_slot(config, broker)  # default settings ⇒ dry-run (S2)

    rows = read_orders(config)
    assert [
        (r["ticker"], r["kind"], r["reason"], r["dry_run"], r["order_kind"],
         r["client_order_id"])
        for r in rows
    ] == [
        ("NVDA", "submitted", None, True, "bracket", "2026-08-07-us-NVDA-entry"),
        ("AAPL", "cancel", None, True, "bracket", "2026-08-04-us-AAPL-entry"),
        ("AAPL", "submitted", None, True, "close", "2026-08-07-us-AAPL-close-1"),
        ("MSFT", "skip", "manual-trigger", True, None, "2026-08-07-us-MSFT-entry"),
        ("TSLA", "skip", "ticker-eval-fail", True, None, "2026-08-07-us-TSLA-entry"),
        ("ORCL", "skip", "maintain", True, None, "2026-08-07-us-ORCL-entry"),
    ]
    # The would-place rows carry the full order shape.
    nvda = rows[0]
    assert (nvda["qty"], nvda["limit_price"], nvda["stop_price"], nvda["target_price"]) == (
        100.0, 100.0, 90.0, 110.0,
    )
    assert nvda["tif"] == "gtc"
    aapl_close = rows[2]
    assert (aapl_close["qty"], aapl_close["limit_price"], aapl_close["tif"]) == (
        20.0, 95.0, "day",
    )
    # Nothing ever reached the broker's write surface.
    assert broker.submitted == [] and broker.canceled == []
    assert summary == {
        "entrypoint": "submit",
        "date": DATE_ISO,
        "session": "us",
        "live": False,
        "halted": False,
        "submitted": 0,
        "dry_run": 2,
        "skipped": 3,
        "canceled": 1,
        "recovered": 0,
        "errors": 0,
        "rejected": 0,
    }


# ---------------------------------------------------------------------------
# AC4 — the paper URL literal appears exactly once; no live-endpoint literal
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_ac4_paper_url_pinned_exactly_once_and_no_live_endpoint_literal():
    repo = Path(ea.__file__).resolve().parents[1]
    # Both needles are assembled so this test file itself contributes 0 hits.
    paper_host = "paper-api." + "alpaca.markets"
    live_url = "https://" + "api.alpaca.markets"
    paper_hits: list[Path] = []
    live_hits: list[Path] = []
    for rel in ("pipeline", "tradingagents", "compare", "webui", "cli", "tests"):
        base = repo / rel
        if not base.exists():
            continue
        for path in base.rglob("*.py"):
            text = path.read_text(encoding="utf-8", errors="ignore")
            paper_hits.extend([path.resolve()] * text.count(paper_host))
            live_hits.extend([path.resolve()] * text.count(live_url))
    assert paper_hits == [Path(ea.__file__).resolve()], (
        "the paper endpoint literal must appear exactly once — the S1 pin in "
        "pipeline/execution_adapter.py"
    )
    assert live_hits == [], "a live-endpoint literal must not exist anywhere in the codebase"


# ---------------------------------------------------------------------------
# CLI + R4 isolation
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_cli_submit_prints_one_json_summary_line(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("TRADINGAGENTS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.delenv("TRADINGAGENTS_EXECUTION_ENABLED", raising=False)
    config = make_config(tmp_path)
    write_decision(config)
    broker = FakeBroker()
    exit_code = ea.main(
        ["submit", "--date", DATE_ISO], broker_factory=lambda: broker, clock=clock
    )
    assert exit_code == 0
    out_lines = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert len(out_lines) == 1  # exactly one stdout line: the JSON summary
    summary = json.loads(out_lines[0])
    assert summary["entrypoint"] == "submit"
    assert summary["dry_run"] == 1  # env default: execution disabled (S2)


@pytest.mark.unit
def test_cli_refresh_prints_summary(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("TRADINGAGENTS_STATE_DIR", str(tmp_path / "state"))
    config = make_config(tmp_path)
    broker = FakeBroker()
    assert ea.main(["refresh", "--date", DATE_ISO], broker_factory=lambda: broker) == 0
    summary = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert summary["entrypoint"] == "refresh"
    # The snapshot rewrite happened even on an empty book (empty positions list).
    assert (config.ledger_dir / "positions.json").exists()


@pytest.mark.unit
def test_cli_usage_errors(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("TRADINGAGENTS_STATE_DIR", str(tmp_path / "state"))
    # --execute without --run-id is a usage error (S5 manual path).
    assert ea.main(["submit", "--execute"], broker_factory=FakeBroker) == 2
    assert "--run-id" in capsys.readouterr().err
    # Unknown run-id.
    assert (
        ea.main(
            ["submit", "--run-id", "2026-08-07-us-NVDA-brief-9", "--execute"],
            broker_factory=FakeBroker,
        )
        == 2
    )
    assert "not found" in capsys.readouterr().err
    # A cn record can never be executed (Alpaca is US-only).
    config = make_config(tmp_path)
    row = write_decision(config, ticker="0700.HK", session="cn")
    assert (
        ea.main(
            ["submit", "--run-id", row["run_id"], "--execute"], broker_factory=FakeBroker
        )
        == 2
    )
    assert "cn" in capsys.readouterr().err


@pytest.mark.unit
def test_r4_broker_error_on_one_order_never_aborts_the_slot(tmp_path):
    config = make_config(tmp_path)
    write_decision(config, ticker="AAPL")
    write_decision(config, ticker="NVDA")
    broker = FakeBroker()

    def hook(spec):
        if spec.ticker == "AAPL":
            raise RuntimeError("api down")

    broker.submit_hook = hook
    summary = submit_slot(config, broker, settings=live_settings())
    # AAPL's intent is resolved by a submit-error skip; NVDA still submitted.
    assert [spec.ticker for spec in broker.submitted] == ["NVDA"]
    rows = read_orders(config)
    assert [(r["ticker"], r["kind"]) for r in rows] == [
        ("AAPL", "intent"),
        ("AAPL", "skip"),
        ("NVDA", "intent"),
        ("NVDA", "submitted"),
    ]
    assert rows[1]["reason"].startswith("submit-error: ")
    assert summary["errors"] == 1 and summary["submitted"] == 1
