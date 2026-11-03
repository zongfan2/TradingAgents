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
#: Event keys remembered as booked in ``entries.json`` (kill safety).
APPLIED_KEEP = 2000
#: A queued message is dropped (logged) only after this many failed sends
#: AND this long since its first failure, so it never blocks later ones
#: forever yet survives a Lark outage during busy trading.
MAX_SEND_FAILURES = 10
DROP_AFTER = timedelta(hours=2)
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
_NO_TIME = datetime.min.replace(tzinfo=timezone.utc)


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
    #: The order's first execution (fill activities), when known: whether a
    #: fill straddles a seed is decided by it, not by the placement time.
    first_fill: datetime | None = None

    @property
    def time(self) -> datetime | None:
        """When it happened, None when the broker gave no time."""
        at = self.at
        return None if at == _NO_TIME else at

    @property
    def at(self) -> datetime:
        """When it happened, for ordering a reconcile batch."""
        names = {"new": ("submitted",), "fill": ("filled", "updated"), "canceled": ("canceled", "updated"),
                 "expired": ("expired", "updated"), "rejected": ("failed", "updated")}.get(self.event, ("updated",))
        return self.order.time_of(*names, "updated", "submitted") or _NO_TIME

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


def events_from_fills(activities: Iterable[Any], get_order: Callable[[str], Any],
                      skipped: list[str] | None = None) -> list[TradeEvent]:
    """T5: one ``fill`` per order whose FILL activities (by fill time, any
    order age) reached ``order_status: filled``, with the order's totals,
    in fill-time order. An order the broker cannot return is skipped (the
    next reconcile retries)."""
    events: list[TradeEvent] = []
    activities = [a for a in activities if isinstance(a, dict)]
    first: dict[str, datetime] = {}
    for data in activities:
        stamp = _instant(data.get("transaction_time"))
        oid = str(data.get("order_id") or "")
        if oid and stamp is not None and (oid not in first or stamp < first[oid]):
            first[oid] = stamp
    for data in activities:
        if _text(data.get("order_status")) != "filled" or not data.get("order_id"):
            continue
        try:
            order = order_view(get_order(str(data["order_id"])))
        except Exception as exc:
            logger.warning("order of a fill not readable: %s", type(exc).__name__)
            if skipped is not None:
                skipped.append(str(data["order_id"]))
            continue
        if order.id and _reportable("fill", order):
            events.append(TradeEvent("fill", order, first.get(order.id)))
    return events


# ---------------------------------------------------------------------------
# T3 — exactly-once delivery
# ---------------------------------------------------------------------------


def _row_time(raw: str) -> datetime | None:
    try:
        row = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return _instant(row.get("at")) if isinstance(row, dict) else None


#: sent.jsonl keeps keys this long: past both lookbacks (30 d orders, 3 d fills).
SENT_KEEP = timedelta(days=45)


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

    def first_started(self, now: datetime) -> datetime:
        """When trade notifications first ran here (written once). The first
        run records only events before it, so a listener that started
        before the first successful reconcile never has its era swallowed."""
        path = self.dir / "first_started"
        stamp = None
        with contextlib.suppress(OSError):
            stamp = _instant(path.read_text(encoding="utf-8").strip())
        if stamp is None:
            stamp = now.astimezone(timezone.utc)
            atomic_write(path, stamp.isoformat() + "\n")
        return stamp

    def outbox(self) -> list[dict[str, Any]]:
        """Queued messages, oldest first (see :func:`deliver`)."""
        try:
            text = (self.dir / "outbox.jsonl").read_text(encoding="utf-8")
        except OSError:
            return []
        items = []
        for raw in text.splitlines():
            try:
                item = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                items.append(item)
        return items

    def save_outbox(self, items: list[dict[str, Any]]) -> None:
        atomic_write(self.dir / "outbox.jsonl",
                     "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in items))

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

    def prune(self, now: datetime, keep: timedelta) -> None:
        """Drop keys older than ``keep`` (longer than every lookback, so no
        event in a window can be re-sent); call under :meth:`exclusive`."""
        try:
            rows = self.path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return
        cutoff = now - keep
        kept = [raw for raw in rows if (_row_time(raw) or cutoff) >= cutoff]  # undated rows stay
        if len(kept) < len(rows):
            atomic_write(self.path, "".join(raw + "\n" for raw in kept))

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
            if str(symbol).startswith("_"):
                continue  # bookkeeping (``_applied``), not a symbol
            row = row if isinstance(row, dict) else {}
            if row.get("unknown") is True:  # held, but no trustworthy cost basis
                out[str(symbol)] = {"unknown": True}
                if _instant(row.get("as_of")) is not None:
                    out[str(symbol)]["as_of"] = str(row["as_of"])
                continue
            avg, qty = _num(row.get("avg")), _num(row.get("qty"))
            if avg is not None and qty is not None and avg > 0 and qty > 0:
                out[str(symbol)] = {"avg": avg, "qty": qty}
                for stamp in ("seeded_at", "as_of", "seed_from"):
                    if _instant(row.get(stamp)) is not None:
                        out[str(symbol)][stamp] = str(row[stamp])
        return out

    def save(self, entries: Mapping[str, Mapping[str, Any]],
             applied: dict[str, float | None] | None = None) -> None:
        """Rows plus ``_applied``: event key → the entry average it was
        measured against, for the events already booked — a run killed
        before it recorded them neither books one twice nor loses its P&L."""
        keep = self.applied() if applied is None else applied
        # A list of [key, avg] pairs: sort_keys would reorder a mapping, and
        # the cap must keep the newest, not the alphabetically last.
        recent = [[key, avg] for key, avg in list(keep.items())[-APPLIED_KEEP:]]
        atomic_write(self.path, json.dumps({**entries, "_applied": recent}, sort_keys=True) + "\n")

    def applied(self) -> dict[str, float | None]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        pairs = data.get("_applied") if isinstance(data, dict) else None
        if not isinstance(pairs, list):
            return {}
        return {pair[0]: _num(pair[1]) for pair in pairs
                if isinstance(pair, list) and len(pair) == 2 and isinstance(pair[0], str)}  # oldest first

    def seed(self, positions: Iterable[Any], at: datetime | None = None,
             window_from: datetime | None = None) -> None:
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
                seeded = (at or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()
                entries[symbol] = {"avg": avg, "qty": qty, "seeded_at": seeded, "as_of": seeded}
                if window_from is not None:  # the read's start: fills inside the read are ambiguous
                    entries[symbol]["seed_from"] = window_from.astimezone(timezone.utc).isoformat()
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


def _straddles_seed(event: TradeEvent, seeded_at: object) -> bool:
    """Whether an order's fills may straddle the row's seed: it started
    executing before the seed. Its first execution decides when known (a
    bracket leg is *placed* with its entry, long before it fills); else the
    placement time does (unknown → assume it may)."""
    seeded = _instant(seeded_at) if seeded_at else None
    if seeded is None:
        return False
    started = event.first_fill or event.order.time_of("submitted")
    return started is None or started < seeded


def _apply_fill(
    entries: dict[str, dict[str, Any]],
    event: TradeEvent,
    positions: Callable[[], list[Any]] | None,
    broker_now: Callable[[], datetime | None] | None = None,
) -> float | None:
    """Update the entry book for a fill — or an entry that ended partly
    filled — and return the entry average an exit is measured against (None
    when unknown, never guessed).

    Every row carries ``as_of``: the time its figure is valid for (an
    event's time, or — for a broker position read — the read time). An event
    older than its symbol's ``as_of`` is already reflected in the row (the
    listener moved the book ahead while this one was missed), so it changes
    nothing and its exit carries no P&L. The broker's position is the truth
    (adds average in exactly); without it, the order's own totals."""
    order, symbol = event.order, event.order.symbol
    filled = (order.filled_qty or 0) > 0
    if not (event.event == "fill" or (event.event in TERMINAL_EVENTS and filled)):
        return None
    when = event.time
    held = entries.get(symbol)
    window = (_instant((held or {}).get("seed_from")), _instant((held or {}).get("seeded_at")))
    # Fallback replays only: the listener reads the broker's own figure.
    if (positions is None and held and window[0] and window[1] and when is not None
            and window[0] <= when <= window[1]):
        # Filled while the seed's positions were being read: maybe inside the
        # seeded figure, maybe not — unknown rather than counted 0 or 2 times.
        entries[symbol] = {"unknown": True, "as_of": when.isoformat(),
                           "seeded_at": held["seeded_at"], "seed_from": held["seed_from"]}
        return None
    as_of = _instant(held.get("as_of")) if held else None
    if positions is None and as_of is not None and when is not None and when < as_of:
        # Fallback replay: the book is ahead of this event (already in the
        # figure). The listener instead reads the broker's position below —
        # the truth whatever the book's age; a sell keeps the average.
        return None
    current = None
    read_at = None
    if positions is not None:
        try:
            current = _position_row(positions(), symbol)
        except Exception as exc:  # P&L is a courtesy; never block the message
            logger.warning("positions unavailable for %s: %s", symbol, type(exc).__name__)
            positions = None
        else:
            # An upper bound on the broker's clock, taken after the read: the
            # figure may include any fill up to here (the event time is only a
            # lower bound). No broker clock → the read is not used at all (the
            # book stamps stay broker-clock only); the fallback rules apply.
            read_at = _broker_time(broker_now) if broker_now is not None else datetime.now(timezone.utc)
            if read_at is None:
                current, positions = None, None
    stamp = (held or {}).get("seeded_at")
    seed_from = (held or {}).get("seed_from")

    def row(figure: dict[str, Any], valid_at: datetime | None) -> dict[str, Any]:
        out = {**figure, "as_of": (valid_at or read_at or datetime.now(timezone.utc)).isoformat()}
        if stamp:  # a seeded row stays seeded: a pre-seed order's fill may still come
            out["seeded_at"] = stamp
            if seed_from:
                out["seed_from"] = seed_from
        return out

    def from_broker(figure: dict[str, Any]) -> dict[str, Any]:
        """A row from a listener position read is a fresh seed: an order
        placed before the read may straddle it (partly inside the figure),
        and a fill between the event and the read may or may not be in it."""
        at = read_at or datetime.now(timezone.utc)
        return {**figure, "as_of": at.isoformat(), "seeded_at": at.isoformat(),
                "seed_from": (when or at).isoformat()}



    if (held or {}).get("unknown") and current is None:
        # Unknown stays unknown until the broker's own figure replaces it
        # (listener) or the position is gone (sell to zero, alignment).
        if order.side == "sell" and positions is not None:
            entries.pop(symbol, None)
        return None
    if order.side == "buy":
        if current is not None:
            entries[symbol] = from_broker(current)
        elif order.filled_avg_price and order.filled_qty:
            if _straddles_seed(event, stamp):
                # Placed before the seed snapshot but filled after it: some of
                # its shares may be in the seeded row — averaging in could
                # count them twice.
                entries[symbol] = row({"unknown": True}, when)
            elif held and not held.get("unknown"):  # an add: average in
                total = held["qty"] + order.filled_qty
                entries[symbol] = row({"avg": (held["avg"] * held["qty"]
                                               + order.filled_avg_price * order.filled_qty) / total,
                                       "qty": total}, when)
            else:
                entries[symbol] = row({"avg": order.filled_avg_price, "qty": order.filled_qty}, when)
        return None
    avg = held["avg"] if held and not held.get("unknown") else None  # unknown: no P&L
    if positions is not None:
        if current is None:
            entries.pop(symbol, None)
        else:
            entries[symbol] = from_broker({"avg": avg or current["avg"], "qty": current["qty"]})
    elif held and not held.get("unknown") and _straddles_seed(event, stamp):
        # A leg placed before the seed snapshot may have partly filled before
        # it — the seed already excludes that slice: subtracting the order's
        # total would count it twice.
        entries[symbol] = row({"unknown": True}, when)
    elif held and not held.get("unknown"):
        remaining = held["qty"] - (order.filled_qty or 0)
        if remaining > 1e-9:
            entries[symbol] = row({"avg": held["avg"], "qty": remaining}, when)
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
    broker_now: Callable[[], datetime | None] | None = None,
    snapshot_at: datetime | None = None,
    snapshot_from: datetime | None = None,
    complete: bool = True,
) -> int:
    """Handle each new event once, in order, through a persistent outbox
    (T3); returns the messages sent.

    Each event is booked at once (the book changes in the order things
    happened) and its message — P&L computed now, with the right cost
    basis — appended to ``outbox.jsonl``; the outbox is then sent head
    first. A failed send leaves its message at the head, so later ones —
    from this batch, the listener or the next reconcile — queue behind it
    and nothing goes out of order; after MAX_SEND_FAILURES the message (not
    the trade) is dropped with an error log.

    ``positions`` (the listener, at the moment of the fill) lets a fill take
    its cost basis from the broker's position. ``reconcile_positions`` (the
    fallback, events possibly hours old) never does — the position *now* is
    not the position *then* — fills use the orders' own totals, and after
    the batch symbols the broker no longer holds are dropped (never added
    or resized). It also decides the first run under the lock: no
    ``initialized`` marker → the book is seeded from today's positions and
    the history before the first start recorded without a message.
    """
    store, book = SentStore(state_dir), Entries(state_dir)
    rebuild_failed = False
    with store.exclusive():
        first_run = reconcile_positions is not None and not store.initialized()
        # The snapshot's own broker time: fills after it are not in the seed,
        # so they must be neither history nor "already in the figure".
        cutoff = (store.first_started(snapshot_from or snapshot_at or _broker_time(broker_now) or reconciled_at
                                      or datetime.now(timezone.utc))
                  if first_run else None)
        if first_run:
            book.seed(reconcile_positions(), snapshot_at or _broker_time(broker_now), snapshot_from)
        seen = store.keys()
        seen_orders = {key.split(":", 1)[0] for key in seen}
        entries = book.load()
        applied = book.applied()
        outbox = store.outbox()
        in_outbox = {item.get("key") for item in outbox}
        queued: list[str] = []
        for event in events:
            if event.key in seen:
                continue
            if event.event == "new" and event.order.id in seen_orders:
                # A later event of this order was already handled: a
                # placement message now would arrive out of order.
                store.record(event.key, sent=False)
                seen.add(event.key)
                continue
            # First run: history before the first start is recorded, not sent
            # (and not booked — the book was seeded from today's positions).
            if first_run and cutoff is not None and event.at < cutoff:
                store.record(event.key, sent=False)
                seen.add(event.key)
                continue
            if event.key in applied:
                entry_avg = applied[event.key]  # booked by a run killed before it recorded the key
            else:
                entry_avg = _apply_fill(entries, event, positions, broker_now)
                applied[event.key] = entry_avg
            if rebuild_book:
                entry_avg = None  # the book may miss exits from the hole: no guessed P&L
            plan = (plan_line(ledger_dir or Path(state_dir) / "ledger", event.order.client_order_id)
                    if event.event == "new" else None)
            if event.key not in in_outbox:  # already queued by a run killed before recording it
                outbox.append({"key": event.key, "text": format_event(event, entry_avg=entry_avg, plan=plan),
                               "failures": 0})
                in_outbox.add(event.key)
            queued.append(event.key)
            seen.add(event.key)
            seen_orders.add(event.order.id)
        # Kill-safe order, each step idempotent on a rerun: the book (with
        # the applied keys) → the outbox (deduplicated by key) → the handled
        # keys. A kill can at worst resend one message, never lose one, and
        # never book an event twice.
        book.save(entries, applied)
        store.save_outbox(outbox)
        for key in queued:
            store.record(key, sent=True)  # handled: queued in the outbox
        sent = _flush(outbox, send, persist=store.save_outbox)
        if rebuild_book and not first_run and reconcile_positions is not None:
            try:  # read first: a failed read leaves the book and the window as they were
                held = reconcile_positions()
            except Exception as exc:
                logger.warning("positions unavailable to rebuild the entry book: %s", type(exc).__name__)
                rebuild_failed = True
            else:
                # Rows booked after the snapshot are newer than it: kept.
                book.save({sym: row for sym, row in book.load().items() if _newer(row, snapshot_at)}, applied)
                book.seed(held, snapshot_at or _broker_time(broker_now), snapshot_from)  # the broker's figure
        # An incomplete batch (a fill's order unreadable) neither closes the
        # window nor aligns: the next reconcile replays it with the book intact.
        if reconciled_at is not None and not rebuild_failed and complete:
            store.mark_reconciled(reconciled_at)
            store.prune(reconciled_at, SENT_KEEP)
        if first_run and complete:  # an incomplete first run stays the first run
            store.mark_initialized()
        elif reconcile_positions is not None and not rebuild_book and complete:
            _align_book(book, reconcile_positions, snapshot_at)
    return sent


def enqueue(state_dir: str | Path, key: str, text: str, send: Sender | None) -> int:
    """Queue one non-trade message (the daily report) behind the trade
    messages, once per key, and flush; returns messages sent."""
    store = SentStore(state_dir)
    with store.exclusive():
        outbox, handled = store.outbox(), store.keys()
        if key not in handled and all(item.get("key") != key for item in outbox):
            outbox.append({"key": key, "text": text, "failures": 0})
            store.save_outbox(outbox)
            store.record(key, sent=True)
        return _flush(outbox, send, persist=store.save_outbox)


def _flush(
    outbox: list[dict[str, Any]],
    send: Sender | None,
    persist: Callable[[list[dict[str, Any]]], None] | None = None,
    now: Callable[[], datetime] | None = None,
) -> int:
    """Send the outbox head first; each sent or dropped message is persisted
    off the outbox at once. Stop at the first failure — unless that message
    has failed MAX_SEND_FAILURES times *and* for DROP_AFTER since its first
    failure (attempts alone would drop it within minutes of a busy trading
    outage): then only the message is dropped (logged); the trade is booked."""
    clock = now or (lambda: datetime.now(timezone.utc))
    sent = 0
    while outbox and send is not None:
        item = outbox[0]
        try:
            send(TITLE, str(item.get("text") or ""))
        except Exception as exc:  # never the URL: LarkError text is scrubbed
            reason = exc if exc.__class__.__name__ == "LarkError" else type(exc).__name__
            item["failures"] = int(item.get("failures") or 0) + 1
            first = _instant(item.get("first_failed_at")) or clock()
            item["first_failed_at"] = first.isoformat()
            if item["failures"] < MAX_SEND_FAILURES or clock() - first < DROP_AFTER:
                logger.warning("trade notification %s not sent (%s, attempt %d); %d queued behind it",
                               item.get("key"), reason, item["failures"], len(outbox) - 1)
                if persist is not None:
                    persist(outbox)
                return sent
            logger.error("trade notification %s dropped after %d failed sends over %s (%s)",
                          item.get("key"), item["failures"], clock() - first, reason)
            outbox.pop(0)
        else:
            outbox.pop(0)
            sent += 1
        if persist is not None:
            persist(outbox)
    return sent


def _broker_time(broker_now: Callable[[], datetime | None] | None) -> datetime | None:
    """The broker's clock for seed stamps (compared with broker event times);
    None → the host clock."""
    if broker_now is None:
        return None
    try:
        return broker_now()
    except Exception as exc:
        logger.warning("broker clock unavailable: %s", type(exc).__name__)
        return None


def _newer(row: Mapping[str, Any], snapshot_at: datetime | None) -> bool:
    """A row booked after the positions snapshot (its ``as_of`` is later)."""
    as_of = _instant(row.get("as_of"))
    return snapshot_at is not None and as_of is not None and as_of >= snapshot_at


def _align_book(book: Entries, positions: Callable[[], list[Any]], snapshot_at: datetime | None = None) -> None:
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
    # A row booked after the snapshot (an entry the listener or this batch
    # booked meanwhile) is newer than the snapshot: never dropped by it.
    aligned = {symbol: row for symbol, row in entries.items() if symbol in held or _newer(row, snapshot_at)}
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

    def now(self) -> datetime | None:
        """The broker's clock (seed stamps are compared with broker times)."""
        stamp = getattr(self._client.get_clock(), "timestamp", None)
        return _instant(stamp)

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
    # Positions first, before the events: alignment and rebuild then only
    # reflect fills the snapshot below includes — a stop filling during the
    # sends can never drop its row before its own event is booked.
    clock = getattr(broker, "now", None)
    snapshot_from = _broker_time(clock)  # broker clock around the read: [from, at]
    try:
        snapshot, snapshot_error = list(broker.positions()), None
    except Exception as exc:
        snapshot, snapshot_error = None, exc
    snapshot_at = _broker_time(clock)
    if clock is not None and (snapshot_from is None or snapshot_at is None):
        # Book stamps must be on the broker's clock: without it, skip this
        # reconcile (logged); the next one retries.
        raise RuntimeError("broker clock unavailable")

    def positions_snapshot() -> list[Any]:
        if snapshot is None:
            raise snapshot_error
        return snapshot

    # A filled order's placement older than the fill window has no fill
    # message to follow (its fill is out of view): announcing it now would
    # read as a live pending order.
    events = [event for event in events_from_orders(broker.orders_since(now - ORDER_LOOKBACK))
              if not (event.event == "new" and event.order.status == "filled"
                      and event.at < now - FILL_LOOKBACK)]
    skipped: list[str] = []
    events += events_from_fills(broker.fills_since(now - FILL_LOOKBACK), broker.order, skipped)
    events.sort(key=lambda event: event.at)  # one timeline: book changes in the order they happened
    # A hole longer than the fill window may have hidden exits: this batch's
    # P&L is not guessed, and the book is rebuilt from today's positions.
    hole = last is not None and now - last > FILL_LOOKBACK
    return deliver(events, state_dir=state_dir, send=send, reconcile_positions=positions_snapshot,
                   ledger_dir=ledger_dir, rebuild_book=hole, reconciled_at=now,
                   broker_now=clock, snapshot_at=snapshot_at, snapshot_from=snapshot_from,
                   complete=not skipped)


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
            # Through the outbox, behind any queued trade message (never ahead).
            enqueue(state_dir, f"daily:{local.date().isoformat()}", daily_report(broker), send)
            atomic_write(marker, local.isoformat() + "\n")
    except Exception as exc:
        skipped("daily report", exc)
