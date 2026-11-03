"""Trade notifications to a separate Lark trades group (specs/trade-notifications.md).

- :func:`event_from_update` / :func:`events_from_orders` /
  :func:`events_from_fills` — broker trade updates (the listener's stream),
  order snapshots and fill activities (the fallback reconcile) mapped to the
  same :class:`TradeEvent`s (T2/T5).
- :class:`SentStore` — exactly-once delivery: check-send-record under an
  exclusive file lock shared by the listener and the hourly fallback (T3).
- :func:`deliver` — format and send one event; exits carry realized P&L
  from the recorded entry average (T2).
- :func:`run_hourly` — the watchdog's fallback reconcile + daily report (T5/T6).

Read-only toward the broker: nothing here places, replaces or cancels an
order. Webhook and key values are never logged.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import logging
import math
import os
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from pipeline.common import atomic_write, locked_append

logger = logging.getLogger("pipeline.trade_notify")

STORE_DIRNAME = "trade_notify"
TITLE = "TradingAgents 交易"
NY = ZoneInfo("America/New_York")
#: T6: the daily report goes out at the first watchdog fire after this.
DAILY_REPORT_AFTER = time(16, 15)
#: T5: fills are looked up by *fill* time (account FILL activities), so an
#: exit of a bracket submitted weeks ago is still found; order placements and
#: cancels by submission time over a longer window (stale-entry cancels).
FILL_LOOKBACK = timedelta(days=3)
ORDER_LOOKBACK = timedelta(days=30)
#: Order lifecycle events that are reported (T2); the rest are noise.
TERMINAL_EVENTS = ("canceled", "expired", "rejected")
_EVENT_LABEL = {"canceled": "⚠️ 撤单", "expired": "⚠️ 过期", "rejected": "⚠️ 拒单"}

Sender = Callable[[str, str], None]


# ---------------------------------------------------------------------------
# Normalized broker data
# ---------------------------------------------------------------------------


def _num(value: object) -> float | None:
    """A broker number (the API sends strings); junk / non-finite → None."""
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _text(value: object) -> str:
    """Enum or plain value as its lowercase string (``OrderSide.BUY`` → ``buy``)."""
    value = getattr(value, "value", value)
    return str(value or "").lower()


@dataclass(frozen=True)
class OrderView:
    id: str
    client_order_id: str
    symbol: str
    side: str            # buy | sell
    order_type: str      # market | limit | stop | stop_limit | trailing_stop
    order_class: str     # simple | bracket | oco | oto | ""
    status: str
    qty: float | None
    filled_qty: float | None
    filled_avg_price: float | None
    limit_price: float | None
    stop_price: float | None
    legs: tuple[OrderView, ...] = ()
    #: Broker timestamps (submitted / filled / canceled / expired / failed).
    times: tuple[tuple[str, datetime], ...] = ()

    def time_of(self, *names: str) -> datetime | None:
        stamps = dict(self.times)
        return next((stamps[name] for name in names if name in stamps), None)

    @property
    def kind(self) -> str:
        """``entry`` / ``close`` (pipeline orders, by contract id), ``stop`` /
        ``target`` (bracket legs: sell-side stop / limit), else ``other``."""
        cid = self.client_order_id
        if cid.endswith("-entry"):
            return "entry"
        if "-close-" in cid:
            return "close"
        # Bracket legs carry order_class "bracket" (measured on MU/SNDK legs):
        # a manual sell is never mistaken for a pipeline stop or target.
        if self.order_class == "bracket" and self.side == "sell":
            if self.order_type in ("stop", "stop_limit", "trailing_stop"):
                return "stop"
            if self.order_type == "limit":
                return "target"
        return "other"


def order_view(raw: Any) -> OrderView:
    """An alpaca-py ``Order`` model or a raw stream/REST dict, normalized."""
    data = raw.model_dump() if hasattr(raw, "model_dump") else dict(raw or {})
    legs = data.get("legs") or ()
    return OrderView(
        id=str(data.get("id") or ""),
        client_order_id=str(data.get("client_order_id") or ""),
        symbol=str(data.get("symbol") or "").upper(),
        side=_text(data.get("side")),
        order_type=_text(data.get("order_type") or data.get("type")),
        order_class=_text(data.get("order_class")),
        status=_text(data.get("status")),
        qty=_num(data.get("qty")),
        filled_qty=_num(data.get("filled_qty")),
        filled_avg_price=_num(data.get("filled_avg_price")),
        limit_price=_num(data.get("limit_price")),
        stop_price=_num(data.get("stop_price")),
        legs=tuple(order_view(leg) for leg in legs if leg),
        times=tuple((name, stamp) for name in _TIME_FIELDS
                    if (stamp := _instant(data.get(f"{name}_at"))) is not None),
    )


_TIME_FIELDS = ("submitted", "filled", "canceled", "expired", "failed", "updated")


def _instant(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


@dataclass(frozen=True)
class TradeEvent:
    """One reportable order lifecycle event (T2)."""

    event: str                 # new | fill | canceled | expired | rejected
    order: OrderView           # the order's state; fills report its totals

    @property
    def at(self) -> datetime:
        """When it happened, for ordering a reconcile batch."""
        names = {"new": ("submitted",), "fill": ("filled", "updated"), "canceled": ("canceled", "updated"),
                 "expired": ("expired", "updated"), "rejected": ("failed", "updated")}.get(self.event, ("updated",))
        return self.order.time_of(*names, "updated", "submitted") or datetime.min.replace(tzinfo=timezone.utc)

    @property
    def key(self) -> str:
        """The T3 dedup key — the same whether the stream or a snapshot saw it."""
        return f"{self.order.id}:{self.event}"


def _reportable(event: str, order: OrderView) -> bool:
    # Partial fills are not messages: orders fill in several executions
    # (measured: AAPL 4, MU 2), and the final fill reports the totals.
    if event == "fill":
        return order.kind != "other"
    if event == "new" or event in TERMINAL_EVENTS:
        # A bracket leg canceled because its sibling filled is not news.
        return order.kind in ("entry", "close")
    return False


def event_from_update(update: Any) -> TradeEvent | None:
    """A stream ``trade_updates`` message (raw dict or ``TradeUpdate``) as a
    reportable event, else None. ``accepted`` reads as ``new``."""
    data = update.model_dump() if hasattr(update, "model_dump") else dict(update or {})
    event = _text(data.get("event"))
    if event == "accepted":
        event = "new"
    order = order_view(data.get("order") or {})
    if not order.id or not _reportable(event, order):
        return None
    return TradeEvent(event, order)


def _flatten(orders: Iterable[OrderView]) -> Iterator[OrderView]:
    for order in orders:
        yield order
        yield from _flatten(order.legs)


def events_from_orders(orders: Iterable[Any]) -> list[TradeEvent]:
    """T5: placements and terminal states of entry/close orders implied by
    their *current* state (fills come from :func:`events_from_fills`)."""
    events: list[TradeEvent] = []
    for order in _flatten(order_view(o) for o in orders):
        if not order.id or order.kind not in ("entry", "close"):
            continue
        if order.status in ("new", "accepted", "partially_filled", "filled", "canceled", "expired"):
            events.append(TradeEvent("new", order))  # a rejected order never was "new"
        if order.status in TERMINAL_EVENTS:
            events.append(TradeEvent(order.status, order))
    return events


def events_from_fills(activities: Iterable[Any], get_order: Callable[[str], Any]) -> list[TradeEvent]:
    """T5: one ``fill`` per order whose FILL activities (by fill time, any
    order age) reached ``order_status: filled``, with the order's totals,
    in fill-time order. An order the broker cannot return is skipped (the
    next reconcile retries)."""
    events: list[TradeEvent] = []
    for activity in activities:
        data = activity if isinstance(activity, dict) else {}
        if _text(data.get("order_status")) != "filled" or not data.get("order_id"):
            continue
        try:
            order = order_view(get_order(str(data["order_id"])))
        except Exception as exc:
            logger.warning("order of a fill not readable: %s", type(exc).__name__)
            continue
        if order.id and _reportable("fill", order):
            events.append(TradeEvent("fill", order))
    return events


# ---------------------------------------------------------------------------
# T3 — exactly-once delivery
# ---------------------------------------------------------------------------


class SentStore:
    """Keys of delivered messages in ``sent.jsonl``; :meth:`exclusive` holds
    an flock across check-send-record, so two processes never both send."""

    def __init__(self, state_dir: str | Path) -> None:
        self.dir = Path(state_dir) / STORE_DIRNAME
        self.path = self.dir / "sent.jsonl"
        self._lock_path = self.dir / "sent.lock"

    def last_reconcile(self) -> datetime | None:
        try:
            return _instant((self.dir / "last_reconcile").read_text(encoding="utf-8").strip())
        except OSError:
            return None

    def mark_reconciled(self, now: datetime) -> None:
        atomic_write(self.dir / "last_reconcile", now.astimezone(timezone.utc).isoformat() + "\n")

    def initialized(self) -> bool:
        """False until the first reconcile recorded the history (T3)."""
        return (self.dir / "initialized").exists()

    def mark_initialized(self) -> None:
        atomic_write(self.dir / "initialized", datetime.now(timezone.utc).isoformat() + "\n")

    def keys(self) -> set[str]:
        try:
            text = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return set()
        keys = set()
        for raw in text.splitlines():
            try:
                row = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict) and isinstance(row.get("key"), str):
                keys.add(row["key"])
        return keys

    def record(self, key: str, *, sent: bool) -> None:
        row = {"key": key, "sent": sent, "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        locked_append(self.path, json.dumps(row, separators=(",", ":")))

    @contextlib.contextmanager
    def exclusive(self) -> Iterator[None]:
        self.dir.mkdir(parents=True, exist_ok=True)
        with open(self._lock_path, "a", encoding="utf-8") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)


# ---------------------------------------------------------------------------
# Entry averages for exit P&L
# ---------------------------------------------------------------------------


class Entries:
    """``entries.json``: symbol → {avg, qty} of the open long position, as
    the notified fills built it (seeded from broker positions)."""

    def __init__(self, state_dir: str | Path) -> None:
        self.path = Path(state_dir) / STORE_DIRNAME / "entries.json"

    def load(self) -> dict[str, dict[str, Any]]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        out: dict[str, dict[str, Any]] = {}
        for symbol, row in (data.items() if isinstance(data, dict) else ()):
            row = row if isinstance(row, dict) else {}
            avg, qty = _num(row.get("avg")), _num(row.get("qty"))
            if avg is not None and qty is not None and avg > 0 and qty > 0:
                out[str(symbol)] = {"avg": avg, "qty": qty}
                if _instant(row.get("seeded_at")) is not None:
                    out[str(symbol)]["seeded_at"] = str(row["seeded_at"])
        return out

    def save(self, entries: Mapping[str, Mapping[str, float]]) -> None:
        atomic_write(self.path, json.dumps(entries, sort_keys=True) + "\n")

    def seed(self, positions: Iterable[Any]) -> None:
        """Fill in symbols the store does not know from the broker's open
        long positions (never overwrites what fills recorded)."""
        entries = self.load()
        changed = False
        for position in positions:
            data = position.model_dump() if hasattr(position, "model_dump") else dict(position or {})
            symbol = str(data.get("symbol") or "").upper()
            avg, qty = _num(data.get("avg_entry_price")), _num(data.get("qty"))
            if symbol and symbol not in entries and avg and qty and qty > 0:
                # seeded_at: a fill of an order placed before this snapshot may
                # already be inside it (a partial) — see _apply_fill.
                entries[symbol] = {"avg": avg, "qty": qty,
                                   "seeded_at": datetime.now(timezone.utc).isoformat()}
                changed = True
        if changed:
            self.save(entries)


def _position_row(positions: Iterable[Any], symbol: str) -> dict[str, float] | None:
    for position in positions:
        data = position.model_dump() if hasattr(position, "model_dump") else dict(position or {})
        if str(data.get("symbol") or "").upper() == symbol:
            avg, qty = _num(data.get("avg_entry_price")), _num(data.get("qty"))
            if avg and qty and qty > 0:
                return {"avg": avg, "qty": qty}
    return None


def _apply_fill(
    entries: dict[str, dict[str, float]],
    event: TradeEvent,
    positions: Callable[[], list[Any]] | None,
) -> float | None:
    """Update the entry book for a fill — or an entry that ended partly
    filled — and return the entry average an exit is measured against (None
    when unknown). The broker's position is the truth (adds average in
    exactly); without it, the order's own totals."""
    order, symbol = event.order, event.order.symbol
    filled = (order.filled_qty or 0) > 0
    if not (event.event == "fill" or (event.event in TERMINAL_EVENTS and filled)):
        return None
    current = None
    if positions is not None:
        try:
            current = _position_row(positions(), symbol)
        except Exception as exc:  # P&L is a courtesy; never block the message
            logger.warning("positions unavailable for %s: %s", symbol, type(exc).__name__)
            positions = None
    if order.side == "buy":
        if current is not None:
            entries[symbol] = current
        elif order.filled_avg_price and order.filled_qty:
            held = entries.get(symbol)
            seeded = _instant(held.get("seeded_at")) if held else None
            submitted = order.time_of("submitted")
            if seeded is not None and (submitted is None or submitted < seeded):
                # Placed before the seed snapshot: some of its shares may be in
                # the seeded row already — averaging in could count them twice.
                entries.pop(symbol, None)  # cost basis unknown: no P&L, never a wrong one
            elif held:  # an add: average in
                total = held["qty"] + order.filled_qty
                entries[symbol] = {"avg": (held["avg"] * held["qty"] + order.filled_avg_price * order.filled_qty)
                                   / total, "qty": total}
            else:
                entries[symbol] = {"avg": order.filled_avg_price, "qty": order.filled_qty}
        return None
    held = entries.get(symbol)
    avg = held["avg"] if held else None
    if positions is not None:
        if current is None:
            entries.pop(symbol, None)
        else:
            entries[symbol] = {"avg": avg or current["avg"], "qty": current["qty"]}
    elif held:
        remaining = held["qty"] - (order.filled_qty or 0)
        if remaining > 1e-9:
            entries[symbol] = {"avg": held["avg"], "qty": remaining}
        else:
            entries.pop(symbol, None)
    return avg


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------


def _signed_pct(pct: float) -> str:
    return f"{'+' if pct >= 0 else '−'}{abs(pct):.2f}%"


def _px(value: float | None) -> str:
    return "—" if value is None else f"{value:,.2f}"


def _qty(value: float | None) -> str:
    return "—" if value is None else f"{value:g}"


_SIDE = {"buy": "买入", "sell": "卖出"}
_EXIT_LABEL = {"stop": "🔚 止损平仓", "target": "🔚 止盈平仓", "close": "🔚 信号平仓"}


def _jsonl(path: Path) -> Iterator[dict]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return
    for raw in text.splitlines():
        try:
            row = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            yield row


def plan_line(ledger_dir: str | Path, client_order_id: str) -> str | None:
    """For an entry order: the executed run's plan in one line (conviction,
    catalyst score, invalidation). The run is the one the execution adapter
    submitted it for (``orders.jsonl`` client_order_id → run_id); without
    that link, only an executable row (brief arm, valid plan, BUY) for the
    id's date/session/ticker — never a feeds-arm or invalid plan."""
    if not client_order_id.endswith("-entry"):
        return None
    ledger = Path(ledger_dir)
    # The live attempt that reached the broker: an id is shared by every
    # attempt that day, and skip / dry-run rows name runs never executed.
    live = [row for row in _jsonl(ledger / "orders.jsonl")
            if row.get("client_order_id") == client_order_id and row.get("run_id")
            and row.get("dry_run") is False and row.get("kind") in ("submitted", "intent")]
    run_id = next((row["run_id"] for row in reversed(live) if row.get("kind") == "submitted"),
                  live[-1]["run_id"] if live else None)
    parts = client_order_id[: -len("-entry")].split("-")
    if run_id is None and len(parts) < 5:
        return None
    date_iso, session, ticker = "-".join(parts[:3]), parts[3] if len(parts) > 3 else "", "-".join(parts[4:])
    match = None
    for row in _jsonl(ledger / "decisions.jsonl"):
        if run_id is not None:
            if row.get("run_id") == run_id:
                match = row
        elif (row.get("date") == date_iso and row.get("session") == session and row.get("ticker") == ticker
              and str(row.get("decision")).upper() == "BUY" and row.get("arm", "brief") == "brief"
              and row.get("plan_valid") is True):
            match = row
    if match is None:
        return None
    plan = match.get("plan") if isinstance(match.get("plan"), dict) else {}
    bits = []
    if plan.get("conviction") is not None:
        bits.append(f"信心 {plan['conviction']}")
    if match.get("catalyst_score") is not None:
        bits.append(f"催化剂 {match['catalyst_score']}")
    invalidation = " ".join(str(plan.get("invalidation") or "").split())
    if invalidation:
        bits.append(f"失效条件：{invalidation[:140]}{'…' if len(invalidation) > 140 else ''}")
    return "；".join(bits) or None


def format_event(event: TradeEvent, *, entry_avg: float | None = None, plan: str | None = None) -> str:
    order = event.order
    side = _SIDE.get(order.side, order.side)
    if event.event == "new":
        line = f"📝 下单 {order.symbol} {side} {_qty(order.qty)} 股 @ 限价 {_px(order.limit_price)}"
        stop = next((leg for leg in order.legs if leg.kind == "stop"), None)
        target = next((leg for leg in order.legs if leg.kind == "target"), None)
        if stop or target:
            line += (f"（止损 {_px(stop.stop_price if stop else None)} / "
                     f"止盈 {_px(target.limit_price if target else None)}）")
        if order.kind == "close":
            line = line.replace("📝 下单", "📝 平仓下单", 1)
        return line + (f"\n{plan}" if plan else "")
    qty, price = order.filled_qty, order.filled_avg_price  # the order's totals
    if event.event == "fill":
        if order.side == "buy":
            limit = f"（限价 {_px(order.limit_price)}）" if order.limit_price is not None else ""
            return f"✅ 成交 {order.symbol} 买入 {_qty(qty)} 股 @ 均价 {_px(price)}{limit}"
        line = f"{_EXIT_LABEL.get(order.kind, '🔚 平仓')} {order.symbol} 卖出 {_qty(qty)} 股 @ 均价 {_px(price)}"
        if entry_avg and price is not None and qty:
            pnl = (price - entry_avg) * qty
            line += (f"\n盈亏 {'+' if pnl >= 0 else '−'}${abs(pnl):,.2f}"
                     f"（{_signed_pct((price / entry_avg - 1) * 100)}，成本 {_px(entry_avg)}）")
        return line
    line = (f"{_EVENT_LABEL.get(event.event, event.event)} {order.symbol} {side} {_qty(order.qty)} 股 "
            f"@ 限价 {_px(order.limit_price)}")
    if qty:
        line += f"（已成交 {_qty(qty)} 股，均价 {_px(price)}）"
    return f"{line}\n{order.client_order_id}"


def deliver(
    events: Iterable[TradeEvent],
    *,
    state_dir: str | Path,
    send: Sender | None,
    positions: Callable[[], list[Any]] | None = None,
    reconcile_positions: Callable[[], list[Any]] | None = None,
    ledger_dir: str | Path | None = None,
    rebuild_book: bool = False,
    reconciled_at: datetime | None = None,
) -> int:
    """Send each not-yet-delivered event once, in order (T3); returns the
    messages sent.

    ``positions`` (the listener, at the moment of the fill) lets a fill take
    its cost basis from the broker's position. ``reconcile_positions`` (the
    fallback, events possibly hours old) never does — the position *now* is
    not the position *then* — fills use the orders' own totals, and after
    a complete batch symbols the broker no longer holds are dropped (never
    added or resized). It also decides the first run under
    the lock: no ``initialized`` marker → the book is seeded from today's
    positions and the history recorded without sending.

    A failed send stops the batch (logged, unrecorded): the next reconcile
    resumes there, so an exit is never measured before its entry is booked.
    """
    store, book = SentStore(state_dir), Entries(state_dir)
    sent, stopped = 0, False
    with store.exclusive():
        first_run = reconcile_positions is not None and not store.initialized()
        if first_run:
            book.seed(reconcile_positions())
        seen = store.keys()
        entries = book.load()
        for event in events:
            if event.key in seen:
                continue
            if event.event == "new" and any(key.startswith(f"{event.order.id}:") for key in seen):
                # A later event of this order was already reported: a
                # placement message now would arrive out of order.
                store.record(event.key, sent=False)
                seen.add(event.key)
                continue
            # First run: the book was seeded from the broker's positions —
            # replaying history into it would count those fills twice.
            entry_avg = None if first_run else _apply_fill(entries, event, positions)
            if rebuild_book:
                entry_avg = None  # the book may miss exits from the hole: no guessed P&L
            if not first_run:
                if send is None:
                    stopped = True
                    break
                plan = (plan_line(ledger_dir or Path(state_dir) / "ledger", event.order.client_order_id)
                        if event.event == "new" else None)
                try:
                    send(TITLE, format_event(event, entry_avg=entry_avg, plan=plan))
                except Exception as exc:  # never the URL: LarkError text is scrubbed
                    logger.warning("trade notification %s not sent (%s); the next reconcile resumes there",
                                   event.key, exc if exc.__class__.__name__ == "LarkError" else type(exc).__name__)
                    entries = book.load()  # drop the unsent fill's book change
                    stopped = True
                    break
                sent += 1
            store.record(event.key, sent=not first_run)
            seen.add(event.key)
            book.save(entries)
        if rebuild_book and not first_run and reconcile_positions is not None and not stopped:
            try:  # read first: a failed read leaves the book and the window as they were
                held = reconcile_positions()
            except Exception as exc:
                logger.warning("positions unavailable to rebuild the entry book: %s", type(exc).__name__)
                stopped = True
            else:
                book.save({})
                book.seed(held)  # the broker's own cost basis of what is held now
        if reconciled_at is not None and not stopped:
            store.mark_reconciled(reconciled_at)  # only a complete batch closes the window
        if first_run:
            store.mark_initialized()
        elif reconcile_positions is not None and not stopped and not rebuild_book:
            # Only after a complete batch: aligning before an unsent fill is
            # retried would let it average into today's position.
            _align_book(book, reconcile_positions)
    return sent


def _align_book(book: Entries, positions: Callable[[], list[Any]]) -> None:
    """After a complete fallback batch: drop symbols the broker no longer
    holds. Nothing is added or resized — every fill in the window reaches
    the book as an event (an entry that ended partly filled, as its
    terminal event), so a position read here would count an in-flight or
    partial fill twice."""
    try:
        held = {str((p.model_dump() if hasattr(p, "model_dump") else dict(p or {})).get("symbol") or "").upper()
                for p in positions()}
    except Exception as exc:
        logger.warning("positions unavailable to align the entry book: %s", type(exc).__name__)
        return
    entries = book.load()
    aligned = {symbol: row for symbol, row in entries.items() if symbol in held}
    if aligned != entries:
        book.save(aligned)


# ---------------------------------------------------------------------------
# Broker (read-only) and the hourly fallback
# ---------------------------------------------------------------------------


class ReadOnlyBroker(Protocol):
    def orders_since(self, after: datetime) -> list[Any]: ...
    def fills_since(self, after: datetime) -> list[dict]: ...
    def order(self, order_id: str) -> Any: ...
    def open_orders(self) -> list[Any]: ...
    def positions(self) -> list[Any]: ...
    def account(self) -> Any: ...
    def is_trading_day(self, day: Any) -> bool: ...


class AlpacaReadOnly:
    """Paper-pinned, read-only Alpaca access for notifications (T4/T5)."""

    def __init__(self, api_key: str, secret_key: str) -> None:
        from alpaca.trading.client import TradingClient

        from pipeline.execution_adapter import PAPER_ENDPOINT

        self._client = TradingClient(api_key, secret_key, paper=True, url_override=PAPER_ENDPOINT)
        number = str(getattr(self._client.get_account(), "account_number", ""))
        if not number.startswith("PA"):
            raise RuntimeError("refusing to read a non-paper (PA…) Alpaca account")

    def orders_since(self, after: datetime) -> list[Any]:
        from alpaca.trading.enums import QueryOrderStatus
        from alpaca.trading.requests import GetOrdersRequest

        # Newest first, so a cap drops the oldest, never the recent orders the
        # fallback exists for; returned oldest first.
        request = GetOrdersRequest(status=QueryOrderStatus.ALL, after=after, nested=True,
                                   limit=500, direction="desc")
        return list(reversed(self._client.get_orders(filter=request)))

    def fills_since(self, after: datetime) -> list[dict]:
        """Account FILL activities by fill time (``/v2/account/activities``;
        not wrapped by alpaca-py 0.43), oldest first, all pages."""
        rows: list[dict] = []
        params: dict[str, Any] = {"after": after.astimezone(timezone.utc).isoformat(),
                                  "direction": "asc", "page_size": 100}
        for _ in range(50):
            page = self._client.get("/account/activities/FILL", params) or []
            rows.extend(row for row in page if isinstance(row, dict))
            if len(page) < 100:
                break
            params["page_token"] = page[-1].get("id")
        return rows

    def order(self, order_id: str) -> Any:
        return self._client.get_order_by_id(order_id)

    def open_orders(self) -> list[Any]:
        from alpaca.trading.enums import QueryOrderStatus
        from alpaca.trading.requests import GetOrdersRequest

        # Nested: bracket legs count under their parent, not as orders.
        return list(self._client.get_orders(filter=GetOrdersRequest(status=QueryOrderStatus.OPEN,
                                                                   nested=True, limit=500)))

    def positions(self) -> list[Any]:
        return list(self._client.get_all_positions())

    def account(self) -> Any:
        return self._client.get_account()

    def is_trading_day(self, day: Any) -> bool:
        from alpaca.trading.requests import GetCalendarRequest

        # Fail closed like the adapter: some SDK versions return the NEXT
        # session for a closed day, so the entry's date must be the day.
        from pipeline.execution_adapter import _calendar_date

        days = self._client.get_calendar(GetCalendarRequest(start=day, end=day)) or ()
        return any(_calendar_date(entry) == day for entry in days)


def alpaca_keys() -> tuple[str, str]:
    """``.env`` Alpaca keys, as the execution adapter reads them (values
    never logged)."""
    api_key = os.environ.get("ALPACA_API_KEY") or os.environ.get("ALPACA_API_KEY_ID") or ""
    secret = os.environ.get("ALPACA_SECRET_KEY") or os.environ.get("ALPACA_API_SECRET_KEY") or ""
    if not api_key or not secret:
        raise RuntimeError("Alpaca keys not configured in .env")
    return api_key, secret


def reconcile(broker: ReadOnlyBroker, *, state_dir: str | Path, send: Sender | None,
              now: datetime, ledger_dir: str | Path | None = None) -> int:
    """T5: report anything the listener missed — placements and cancels by
    submission time, fills by fill time. The first run (decided under the
    store lock) records history without sending and marks the store
    initialized, even when empty."""
    store = SentStore(state_dir)
    last = store.last_reconcile()
    # A filled order's placement older than the fill window has no fill
    # message to follow (its fill is out of view): announcing it now would
    # read as a live pending order.
    events = [event for event in events_from_orders(broker.orders_since(now - ORDER_LOOKBACK))
              if not (event.event == "new" and event.order.status == "filled"
                      and event.at < now - FILL_LOOKBACK)]
    events += events_from_fills(broker.fills_since(now - FILL_LOOKBACK), broker.order)
    events.sort(key=lambda event: event.at)  # one timeline: book changes in the order they happened
    # A hole longer than the fill window may have hidden exits: this batch's
    # P&L is not guessed, and the book is rebuilt from today's positions.
    hole = last is not None and now - last > FILL_LOOKBACK
    return deliver(events, state_dir=state_dir, send=send, reconcile_positions=broker.positions,
                   ledger_dir=ledger_dir, rebuild_book=hole, reconciled_at=now)


def daily_report(broker: ReadOnlyBroker) -> str:
    account = broker.account()
    acct = account.model_dump() if hasattr(account, "model_dump") else dict(account)
    equity, last = _num(acct.get("equity")), _num(acct.get("last_equity"))
    lines = [f"📊 日报 权益 ${_px(equity)}"]
    if equity is not None and last:
        change = equity - last
        lines[0] += f"（今日 {'+' if change >= 0 else '−'}${abs(change):,.2f}，{_signed_pct((equity / last - 1) * 100)}）"
    positions = broker.positions()
    if not positions:
        lines.append("无持仓")
    for position in positions:
        p = position.model_dump() if hasattr(position, "model_dump") else dict(position)
        upl, uplpc = _num(p.get("unrealized_pl")), _num(p.get("unrealized_plpc"))
        pl = "" if upl is None else f"，浮动 {'+' if upl >= 0 else '−'}${abs(upl):,.2f}"
        pl += "" if uplpc is None else f"（{_signed_pct(uplpc * 100)}）"
        lines.append(f"• {str(p.get('symbol') or '').upper()} {_qty(_num(p.get('qty')))} 股，成本 "
                     f"{_px(_num(p.get('avg_entry_price')))}，现价 {_px(_num(p.get('current_price')))}{pl}")
    with contextlib.suppress(Exception):  # the count is a courtesy line
        lines.append(f"挂单 {len(broker.open_orders())} 笔")
    return "\n".join(lines)


def run_hourly(
    *,
    state_dir: str | Path,
    send: Sender | None,
    now: datetime,
    broker_factory: Callable[[], ReadOnlyBroker] | None = None,
    ledger_dir: str | Path | None = None,
) -> None:
    """The watchdog's share (T5 + T6); best-effort, never raises. The
    reconcile and the daily report fail independently."""
    if send is None:
        return

    def skipped(what: str, exc: Exception) -> None:
        reason = str(exc) if exc.__class__.__name__ == "LarkError" else type(exc).__name__
        logger.warning("trade notifications: %s skipped: %s", what, reason)

    try:
        broker = (broker_factory or (lambda: AlpacaReadOnly(*alpaca_keys())))()
    except Exception as exc:
        skipped("broker", exc)
        return
    try:
        reconcile(broker, state_dir=state_dir, send=send, now=now, ledger_dir=ledger_dir)
    except Exception as exc:
        skipped("reconcile", exc)
    try:
        local = now.astimezone(NY)
        marker = Path(state_dir) / STORE_DIRNAME / f"daily-{local.date().isoformat()}.sent"
        if local.time() >= DAILY_REPORT_AFTER and not marker.exists() and broker.is_trading_day(local.date()):
            send(TITLE, daily_report(broker))
            atomic_write(marker, local.isoformat() + "\n")
    except Exception as exc:
        skipped("daily report", exc)
