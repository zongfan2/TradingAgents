"""Trade notifications to a separate Lark trades group (specs/trade-notifications.md).

- :func:`event_from_update` / :func:`events_from_orders` — broker trade
  updates (the listener's stream) and order snapshots (the fallback
  reconcile) mapped to the same :class:`TradeEvent`s (T2/T5).
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
#: T5: how far back the fallback reconcile looks.
RECONCILE_LOOKBACK = timedelta(days=3)
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

    @property
    def kind(self) -> str:
        """``entry`` / ``close`` (pipeline orders, by contract id), ``stop`` /
        ``target`` (bracket legs: sell-side stop / limit), else ``other``."""
        cid = self.client_order_id
        if cid.endswith("-entry"):
            return "entry"
        if "-close-" in cid:
            return "close"
        if self.side == "sell" and self.order_type in ("stop", "stop_limit", "trailing_stop"):
            return "stop"
        if self.side == "sell" and self.order_type == "limit":
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
    )


@dataclass(frozen=True)
class TradeEvent:
    """One reportable order lifecycle event (T2)."""

    event: str                 # new | fill | partial_fill | canceled | expired | rejected
    order: OrderView
    price: float | None = None  # this execution's price (fills)
    qty: float | None = None    # this execution's qty (fills)
    position_qty: float | None = None

    @property
    def key(self) -> str:
        """The T3 dedup key — the same whether the stream or a snapshot saw it."""
        if self.event == "partial_fill":
            return f"{self.order.id}:partial:{self.order.filled_qty:g}"
        return f"{self.order.id}:{self.event}"


def _reportable(event: str, order: OrderView) -> bool:
    if event in ("fill", "partial_fill"):
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
    return TradeEvent(event, order, _num(data.get("price")), _num(data.get("qty")),
                      _num(data.get("position_qty")))


def _flatten(orders: Iterable[OrderView]) -> Iterator[OrderView]:
    for order in orders:
        yield order
        yield from _flatten(order.legs)


def events_from_orders(orders: Iterable[Any]) -> list[TradeEvent]:
    """T5: the events each order's *current* state implies, oldest first by
    the broker's listing (entries before their legs' exits)."""
    events: list[TradeEvent] = []
    for order in _flatten(order_view(o) for o in orders):
        if not order.id:
            continue
        if order.kind in ("entry", "close") and order.status in (
            "new", "accepted", "partially_filled", "filled", *TERMINAL_EVENTS
        ):
            events.append(TradeEvent("new", order))
        if order.status == "filled" and _reportable("fill", order):
            events.append(TradeEvent("fill", order, order.filled_avg_price, order.filled_qty))
        elif order.status in TERMINAL_EVENTS and _reportable(order.status, order):
            events.append(TradeEvent(order.status, order))
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

    def exists(self) -> bool:
        return self.path.exists()

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

    def load(self) -> dict[str, dict[str, float]]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        out = {}
        for symbol, row in (data.items() if isinstance(data, dict) else ()):
            avg, qty = _num((row or {}).get("avg")), _num((row or {}).get("qty"))
            if avg is not None and qty is not None and avg > 0 and qty > 0:
                out[str(symbol)] = {"avg": avg, "qty": qty}
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
                entries[symbol] = {"avg": avg, "qty": qty}
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
    """Update the entry book for a (final) fill; return the entry average an
    exit is measured against, None when unknown. The broker's position is
    the truth (adds average in exactly); without it, the order's own fill."""
    order, symbol = event.order, event.order.symbol
    if event.event != "fill":
        return None  # partial fills: the final fill settles the book
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
        remaining = event.position_qty if event.position_qty is not None else held["qty"] - (event.qty or 0)
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


def plan_line(state_dir: str | Path, client_order_id: str) -> str | None:
    """For an entry ``<date>-<session>-<TICKER>-entry``: the run's plan in one
    line (conviction, catalyst score, invalidation) from ``decisions.jsonl``."""
    if not client_order_id.endswith("-entry"):
        return None
    parts = client_order_id[: -len("-entry")].split("-")
    if len(parts) < 5:
        return None
    date_iso, session, ticker = "-".join(parts[:3]), parts[3], "-".join(parts[4:])
    path = Path(state_dir) / "ledger" / "decisions.jsonl"
    match = None
    try:
        for raw in path.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if (isinstance(row, dict) and row.get("date") == date_iso and row.get("session") == session
                    and row.get("ticker") == ticker and str(row.get("decision")).upper() == "BUY"):
                match = row
    except OSError:
        return None
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
    if event.event == "partial_fill":
        return (f"◐ 部分成交 {order.symbol} {side} {_qty(event.qty)} 股 @ {_px(event.price)}"
                f"（累计 {_qty(order.filled_qty)}/{_qty(order.qty)}）")
    if event.event == "fill":
        if order.side == "buy":
            limit = f"（限价 {_px(order.limit_price)}）" if order.limit_price is not None else ""
            return f"✅ 成交 {order.symbol} 买入 {_qty(event.qty)} 股 @ {_px(event.price)}{limit}"
        label = _EXIT_LABEL.get(order.kind, "🔚 平仓")
        line = f"{label} {order.symbol} 卖出 {_qty(event.qty)} 股 @ {_px(event.price)}"
        if entry_avg and event.price is not None and event.qty:
            pnl = (event.price - entry_avg) * event.qty
            pct = (event.price / entry_avg - 1) * 100
            line += f"\n盈亏 {'+' if pnl >= 0 else '−'}${abs(pnl):,.2f}（{_signed_pct(pct)}，成本 {_px(entry_avg)}）"
        return line
    label = _EVENT_LABEL.get(event.event, event.event)
    return f"{label} {order.symbol} {side} {_qty(order.qty)} 股 @ 限价 {_px(order.limit_price)}（{order.client_order_id}）"


def deliver(
    events: Iterable[TradeEvent],
    *,
    state_dir: str | Path,
    send: Sender | None,
    record_only: bool = False,
    positions: Callable[[], list[Any]] | None = None,
) -> int:
    """Send each not-yet-delivered event once (T3). ``record_only`` (the
    first run) records the history without sending. Returns messages sent.
    A failed send is logged and left unrecorded for the next reconcile."""
    store, book = SentStore(state_dir), Entries(state_dir)
    sent = 0
    with store.exclusive():
        seen = store.keys()
        entries = book.load()
        for event in events:
            if event.key in seen:
                continue
            entry_avg = _apply_fill(entries, event, None if record_only else positions)
            if not record_only:
                if send is None:
                    return sent
                plan = plan_line(state_dir, event.order.client_order_id) if event.event == "new" else None
                try:
                    send(TITLE, format_event(event, entry_avg=entry_avg, plan=plan))
                except Exception as exc:  # never the URL: LarkError text is scrubbed
                    logger.warning("trade notification %s not sent (%s); the next reconcile retries",
                                   event.key, exc if exc.__class__.__name__ == "LarkError" else type(exc).__name__)
                    entries = book.load()  # do not keep the unsent fill's book change
                    continue
                sent += 1
            store.record(event.key, sent=not record_only)
            seen.add(event.key)
            book.save(entries)
    return sent


# ---------------------------------------------------------------------------
# Broker (read-only) and the hourly fallback
# ---------------------------------------------------------------------------


class ReadOnlyBroker(Protocol):
    def orders_since(self, after: datetime) -> list[Any]: ...
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

        request = GetOrdersRequest(status=QueryOrderStatus.ALL, after=after, nested=True,
                                   limit=500, direction="asc")
        return list(self._client.get_orders(filter=request))

    def positions(self) -> list[Any]:
        return list(self._client.get_all_positions())

    def account(self) -> Any:
        return self._client.get_account()

    def is_trading_day(self, day: Any) -> bool:
        from alpaca.trading.requests import GetCalendarRequest

        return bool(self._client.get_calendar(GetCalendarRequest(start=day, end=day)))


def alpaca_keys() -> tuple[str, str]:
    """``.env`` Alpaca keys, as the execution adapter reads them (values
    never logged)."""
    api_key = os.environ.get("ALPACA_API_KEY") or os.environ.get("ALPACA_API_KEY_ID") or ""
    secret = os.environ.get("ALPACA_SECRET_KEY") or os.environ.get("ALPACA_API_SECRET_KEY") or ""
    if not api_key or not secret:
        raise RuntimeError("Alpaca keys not configured in .env")
    return api_key, secret


def reconcile(broker: ReadOnlyBroker, *, state_dir: str | Path, send: Sender | None,
              now: datetime) -> int:
    """T5: report anything the listener missed (first run: record only)."""
    store = SentStore(state_dir)
    first_run = not store.exists()
    if first_run:
        Entries(state_dir).seed(broker.positions())
    events = events_from_orders(broker.orders_since(now - RECONCILE_LOOKBACK))
    return deliver(events, state_dir=state_dir, send=send, record_only=first_run,
                   positions=broker.positions)


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
    try:
        open_orders = sum(1 for o in broker.orders_since(datetime.now(timezone.utc) - RECONCILE_LOOKBACK)
                          if order_view(o).status in ("new", "accepted", "held", "partially_filled"))
        lines.append(f"挂单 {open_orders} 笔")
    except Exception:  # the count is a courtesy line
        pass
    return "\n".join(lines)


def run_hourly(
    *,
    state_dir: str | Path,
    send: Sender | None,
    now: datetime,
    broker_factory: Callable[[], ReadOnlyBroker] | None = None,
) -> None:
    """The watchdog's share (T5 + T6); best-effort, never raises."""
    if send is None:
        return
    try:
        broker = (broker_factory or (lambda: AlpacaReadOnly(*alpaca_keys())))()
        reconcile(broker, state_dir=state_dir, send=send, now=now)
        local = now.astimezone(NY)
        marker = Path(state_dir) / STORE_DIRNAME / f"daily-{local.date().isoformat()}.sent"
        if local.time() >= DAILY_REPORT_AFTER and not marker.exists() and broker.is_trading_day(local.date()):
            send(TITLE, daily_report(broker))
            atomic_write(marker, local.isoformat() + "\n")
    except Exception as exc:
        reason = str(exc) if exc.__class__.__name__ == "LarkError" else type(exc).__name__
        logger.warning("trade notifications (hourly) skipped: %s", reason)
