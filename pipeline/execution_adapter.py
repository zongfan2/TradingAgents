"""Execution adapter — Alpaca paper broker gateway (specs/execution-adapter.md).

The ONLY broker gateway in the system: ``submit`` (slot step 7, us only) turns
the slot's eligible ``brief``-arm decisions into paper bracket/close orders;
``refresh`` (invoked by the settle step) refreshes order state, performs the
two hygiene duties (stale-entry cancels, once-only close re-submission) and
rewrites ``positions.json`` from live broker state. Nothing else constructs a
broker client.

Safety invariants (spec S1-S9, each individually tested):

- S1  paper-only fail-closed: endpoint pinned to :data:`PAPER_ENDPOINT` (the
      only endpoint literal in the codebase, AC4); both entrypoints fetch the
      account FIRST and refuse to run unless ``account_number`` starts ``PA``.
- S2  opt-in: ``execution_enabled`` (env ``TRADINGAGENTS_EXECUTION_ENABLED``)
      defaults false — submit appends the rows it *would* place with
      ``dry_run: true`` and submits nothing.
- S3  kill switch: ``<state_dir>/EXECUTION_HALT`` existing ⇒ dry-run
      behavior; checked at startup AND immediately before every individual
      submission, so a mid-slot halt stops the remaining queue.
- S4  one entry per ticker/day (dedupe key is the ticker/day, never run_id)
      plus intent-row crash recovery: a dangling ``intent`` row means the
      broker is queried by the deterministic ``client_order_id`` before any
      resubmission decision.
- S5  eligibility recomputed from the DecisionRecord itself (never a stdout
      hint): session us, arm brief, plan_valid, action BUY|SELL, trigger
      core|catalyst, plus the gating table's execution column over the
      record's stored verdicts. Manual-trigger records execute only via
      ``submit --run-id <id> --execute`` (execution_enabled/halt still apply).
- S6  caps: per-order notional (BUY only — closes are risk-reducing and
      exempt), gross exposure counting pending entries, open positions +
      pending entries, live submissions per slot.
- S7  secrets (``.env`` ``ALPACA_API_KEY``/``ALPACA_SECRET_KEY``) never
      logged, never echoed into ledger or status files.
- S8  market-state guards, fail-closed: broker calendar/clock trading day,
      asset tradability, quote freshness (``max_quote_age_minutes``); ANY
      guard source error ⇒ skip ``guard-unavailable`` — never submit on
      missing information.
- S9  portfolio constraints: BUY on a held ticker without ``add_intent`` is a
      ``maintain`` skip; add-on tranches gated by tranche cap / pyramid-up /
      trading-day spacing / per-ticker notional (each violation its own skip
      reason); buying power; total open risk.

Every attempt/outcome appends one contract-valid ``orders.jsonl`` row via
``locked_append`` (``intent`` immediately before each live submit, then
``submitted``, or ``skip`` with a reason; ``cancel``/``refresh`` for the
lifecycle duties); stdout carries exactly one JSON summary line for the
orchestrator; any broker error on one order logs and continues to the next
(R4). ``--dry-run`` forces S2 behavior regardless of config (R5).

Config surface: ``pipeline/config.py`` is frozen this change set, so the
spec-named keys are resolved privately with the design-doc defaults —
``execution_enabled`` / ``execute_on_missing_eval`` from env, the caps as
:class:`AdapterSettings` fields. All are hoist candidates for the shared
config.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import re
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Protocol

from pipeline.common import (
    append_log_line,
    atomic_write,
    locked_append,
    session_date,
    to_utc_iso,
)
from pipeline.config import PipelineConfig, load_config
from pipeline.contracts.ledger import (
    DecisionRecord,
    OpenOrderLeg,
    OrderRecord,
    PositionEntry,
    PositionsSnapshot,
    TradePlan,
)

logger = logging.getLogger("pipeline.execution_adapter")

#: S1 pin — the ONLY broker endpoint literal in the codebase (AC4: the paper
#: URL appears exactly once; no live-endpoint literal exists anywhere). There
#: is deliberately no env/config override for it.
PAPER_ENDPOINT = "https://paper-api.alpaca.markets"

#: S3 kill switch: this file existing under ``state_dir`` (~/.tradingagents)
#: makes submit behave as if ``execution_enabled=false``. Touchable by hand or
#: from the webui.
HALT_FILENAME = "EXECUTION_HALT"

DECISIONS_NAME = "decisions.jsonl"
ORDERS_NAME = "orders.jsonl"
POSITIONS_NAME = "positions.json"
ADAPTER_LOG_NAME = "execution_adapter.log"

EXECUTION_ENABLED_ENV = "TRADINGAGENTS_EXECUTION_ENABLED"
EXECUTE_ON_MISSING_EVAL_ENV = "TRADINGAGENTS_EXECUTE_ON_MISSING_EVAL"

#: Broker order states that need no further refresh.
TERMINAL_STATUSES = frozenset({"filled", "canceled", "rejected", "expired"})

#: SELL close fallback price: broker bid − 0.2% (marketable limit from a live
#: quote — never from a stale close; the S8 quote guard enforces freshness).
CLOSE_BID_DISCOUNT = 0.002

_EPS = 1e-9

_CLOSE_CID_RE = re.compile(r"^(?P<base>.+-close-)(?P<n>[1-9][0-9]*)$")

Clock = Callable[[], datetime]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _one_line(exc: BaseException) -> str:
    return " ".join(str(exc).split()) or exc.__class__.__name__


# ---------------------------------------------------------------------------
# Settings (config.py is frozen — spec keys resolved privately, hoist candidates)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AdapterSettings:
    """Spec/design-doc defaulted knobs (config appendix, v2 keys)."""

    execution_enabled: bool = False  # S2 — default OFF
    execute_on_missing_eval: bool = False  # S5 gating table
    per_order_notional_cap: float = 0.15  # S6, share of equity, BUY only
    max_gross_exposure: float = 1.0  # S6, share of equity, no margin use
    max_total_risk: float = 0.05  # S9, share of equity
    max_open_positions: int = 10  # S6, positions + pending entries
    max_orders_per_slot: int = 10  # S6, live submissions per slot
    max_tranches_per_ticker: int = 2  # S9 add-on rules
    add_spacing_days: int = 3  # S9, trading days since last fill
    per_ticker_notional_cap: float = 0.20  # S9, share of equity
    pyramid_up_only: bool = True  # S9 — averaging down stays excluded
    max_quote_age_minutes: float = 15.0  # S8 operational halt proxy


def load_settings(env: Mapping[str, str] | None = None) -> AdapterSettings:
    """Resolve the env-overridable keys (the caps are file-config-only per the
    design appendix; tests construct :class:`AdapterSettings` directly)."""
    if env is None:
        env = os.environ

    def flag(key: str, default: bool) -> bool:
        raw = (env.get(key) or "").strip().lower()
        if not raw:
            return default
        return raw in {"1", "true", "yes", "on"}

    return AdapterSettings(
        execution_enabled=flag(EXECUTION_ENABLED_ENV, False),
        execute_on_missing_eval=flag(EXECUTE_ON_MISSING_EVAL_ENV, False),
    )


def halt_file_path(config: PipelineConfig) -> Path:
    return config.state_dir / HALT_FILENAME


# ---------------------------------------------------------------------------
# Broker seam (narrow, injectable — tests fake this; alpaca-py stays lazy)
# ---------------------------------------------------------------------------


class BrokerError(RuntimeError):
    """A broker interaction failed (credentials, network, API)."""


class PaperAccountError(RuntimeError):
    """S1 refusal: the connected account is not an Alpaca PAPER account."""


@dataclass(frozen=True)
class AccountState:
    account_number: str
    equity: float
    buying_power: float


@dataclass(frozen=True)
class CalendarDay:
    """Today's trading session per the broker calendar (None ⇒ closed)."""

    date: date


@dataclass(frozen=True)
class AssetInfo:
    symbol: str
    tradable: bool


@dataclass(frozen=True)
class Quote:
    bid: float
    ask: float
    timestamp: datetime | None


@dataclass(frozen=True)
class BrokerPosition:
    ticker: str
    qty: float
    qty_available: float
    qty_held_for_orders: float
    avg_entry: float
    market_value: float
    unrealized_pl: float


@dataclass(frozen=True)
class BrokerOrder:
    order_id: str
    client_order_id: str
    ticker: str
    side: str  # "buy" | "sell"
    qty: float
    limit_price: float | None
    stop_price: float | None
    status: str
    tif: str
    filled_qty: float = 0.0
    filled_avg_price: float | None = None
    submitted_at: datetime | None = None
    legs: tuple[BrokerOrder, ...] = ()


@dataclass(frozen=True)
class OrderSpec:
    """What the adapter asks the broker to place — always a LIMIT (S5: no
    market orders); ``order_class='bracket'`` adds the protective legs."""

    ticker: str
    side: str  # "buy" | "sell"
    qty: float
    limit_price: float
    tif: str  # "gtc" | "day"
    client_order_id: str
    order_class: str | None = None  # "bracket" for entries
    stop_loss: float | None = None
    take_profit: float | None = None


class AlpacaBroker(Protocol):
    """The narrow injectable broker boundary (spec R2)."""

    def get_account(self) -> AccountState: ...

    def get_clock(self) -> datetime: ...

    def get_calendar_today(self) -> CalendarDay | None: ...

    def get_asset(self, ticker: str) -> AssetInfo: ...

    def latest_quote(self, ticker: str) -> Quote: ...

    def list_positions(self) -> list[BrokerPosition]: ...

    def list_open_orders(self) -> list[BrokerOrder]: ...

    def get_order_by_client_id(self, client_order_id: str) -> BrokerOrder | None: ...

    def submit_order(self, spec: OrderSpec) -> BrokerOrder: ...

    def cancel_order(self, order_id: str) -> None: ...


BrokerFactory = Callable[[], AlpacaBroker]


class AlpacaPaperBroker:
    """Default implementation over ``alpaca-py`` (lazy import — the optional
    ``execution`` dependency group; tests never construct this)."""

    def __init__(self, api_key: str, secret_key: str) -> None:
        from alpaca.data.historical import StockHistoricalDataClient
        from alpaca.trading.client import TradingClient

        # The paper pin is structural: paper=True AND an explicit override to
        # the module's single endpoint literal — no env/config can retarget it.
        self._trading = TradingClient(
            api_key, secret_key, paper=True, url_override=PAPER_ENDPOINT
        )
        self._data = StockHistoricalDataClient(api_key, secret_key)

    def get_account(self) -> AccountState:
        acct = self._trading.get_account()
        return AccountState(
            account_number=str(acct.account_number),
            equity=float(acct.equity),
            buying_power=float(acct.buying_power),
        )

    def get_clock(self) -> datetime:
        return self._trading.get_clock().timestamp

    def get_calendar_today(self) -> CalendarDay | None:
        from alpaca.trading.requests import GetCalendarRequest

        today = self.get_clock().date()
        days = self._trading.get_calendar(GetCalendarRequest(start=today, end=today))
        for day in days or ():
            day_date = getattr(day, "date", None)
            if isinstance(day_date, datetime):
                day_date = day_date.date()
            if day_date == today:
                return CalendarDay(date=today)
        # Some SDK versions return the NEXT session for a closed day — the
        # equality check above keeps that fail-closed (closed ⇒ None).
        return None

    def get_asset(self, ticker: str) -> AssetInfo:
        asset = self._trading.get_asset(ticker)
        return AssetInfo(symbol=ticker, tradable=bool(asset.tradable))

    def latest_quote(self, ticker: str) -> Quote:
        from alpaca.data.requests import StockLatestQuoteRequest

        quotes = self._data.get_stock_latest_quote(
            StockLatestQuoteRequest(symbol_or_symbols=ticker)
        )
        quote = quotes[ticker]
        return Quote(
            bid=float(quote.bid_price),
            ask=float(quote.ask_price),
            timestamp=quote.timestamp,
        )

    def list_positions(self) -> list[BrokerPosition]:
        out: list[BrokerPosition] = []
        for pos in self._trading.get_all_positions():
            qty = float(pos.qty)
            available = float(getattr(pos, "qty_available", None) or qty)
            out.append(
                BrokerPosition(
                    ticker=str(pos.symbol),
                    qty=qty,
                    qty_available=available,
                    qty_held_for_orders=max(0.0, qty - available),
                    avg_entry=float(pos.avg_entry_price),
                    market_value=float(pos.market_value or 0.0),
                    unrealized_pl=float(pos.unrealized_pl or 0.0),
                )
            )
        return out

    def list_open_orders(self) -> list[BrokerOrder]:
        from alpaca.trading.enums import QueryOrderStatus
        from alpaca.trading.requests import GetOrdersRequest

        orders = self._trading.get_orders(
            GetOrdersRequest(status=QueryOrderStatus.OPEN, nested=True, limit=500)
        )
        return [self._convert_order(order) for order in orders]

    def get_order_by_client_id(self, client_order_id: str) -> BrokerOrder | None:
        try:
            order = self._trading.get_order_by_client_id(client_order_id)
        except Exception as exc:
            if getattr(exc, "status_code", None) == 404:
                return None
            raise
        return self._convert_order(order)

    def submit_order(self, spec: OrderSpec) -> BrokerOrder:
        from alpaca.trading.enums import OrderClass, OrderSide, TimeInForce
        from alpaca.trading.requests import (
            LimitOrderRequest,
            StopLossRequest,
            TakeProfitRequest,
        )

        kwargs: dict = {
            "symbol": spec.ticker,
            "qty": spec.qty,
            "side": OrderSide.BUY if spec.side == "buy" else OrderSide.SELL,
            "time_in_force": TimeInForce.GTC if spec.tif == "gtc" else TimeInForce.DAY,
            "limit_price": spec.limit_price,
            "client_order_id": spec.client_order_id,
        }
        if spec.order_class == "bracket":
            kwargs.update(
                order_class=OrderClass.BRACKET,
                stop_loss=StopLossRequest(stop_price=spec.stop_loss),
                take_profit=TakeProfitRequest(limit_price=spec.take_profit),
            )
        return self._convert_order(
            self._trading.submit_order(order_data=LimitOrderRequest(**kwargs))
        )

    def cancel_order(self, order_id: str) -> None:
        self._trading.cancel_order_by_id(order_id)

    def _convert_order(self, order) -> BrokerOrder:
        def _float(value) -> float | None:
            return float(value) if value is not None else None

        def _name(value) -> str:
            return str(getattr(value, "value", value)).lower()

        legs = tuple(
            self._convert_order(leg) for leg in (getattr(order, "legs", None) or ())
        )
        return BrokerOrder(
            order_id=str(order.id),
            client_order_id=str(order.client_order_id),
            ticker=str(order.symbol),
            side="buy" if "buy" in _name(order.side) else "sell",
            qty=float(order.qty or 0.0),
            limit_price=_float(order.limit_price),
            stop_price=_float(order.stop_price),
            status=_name(order.status),
            tif=_name(order.time_in_force),
            filled_qty=float(order.filled_qty or 0.0),
            filled_avg_price=_float(order.filled_avg_price),
            submitted_at=getattr(order, "submitted_at", None),
            legs=legs,
        )


def default_broker_factory() -> AlpacaBroker:
    """Production broker: keys from ``.env`` (S7 — values are never logged,
    never echoed; only their *absence* is reported)."""
    from dotenv import load_dotenv

    load_dotenv()
    api_key = os.environ.get("ALPACA_API_KEY") or ""
    secret_key = os.environ.get("ALPACA_SECRET_KEY") or ""
    if not api_key or not secret_key:
        raise BrokerError("ALPACA_API_KEY / ALPACA_SECRET_KEY not configured in .env")
    return AlpacaPaperBroker(api_key, secret_key)


def _require_paper(account: AccountState) -> None:
    """S1: refuse to run unless the account number starts with ``PA``. The
    account number itself is never echoed (only the refusal)."""
    if not str(account.account_number).startswith("PA"):
        raise PaperAccountError(
            "refusing to run — connected Alpaca account is not a PAPER (PA…) "
            "account (S1 paper-only invariant)"
        )


# ---------------------------------------------------------------------------
# Ledger IO
# ---------------------------------------------------------------------------


def load_decision_records(ledger_dir: str | Path) -> list[DecisionRecord]:
    """All decision rows, contract-validated; unparseable rows are skipped
    loudly — a row the contract cannot vouch for is never executed."""
    path = Path(ledger_dir) / DECISIONS_NAME
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    records: list[DecisionRecord] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
            records.append(DecisionRecord.parse_lenient(row))
        except Exception as exc:
            logger.warning("decisions.jsonl: skipping unparseable row (%s)", _one_line(exc))
    return records


def read_order_rows(ledger_dir: str | Path) -> list[dict]:
    path = Path(ledger_dir) / ORDERS_NAME
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    rows: list[dict] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            logger.warning("orders.jsonl: skipping unparseable line")
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _row_instant(row: Mapping) -> datetime:
    raw = row.get("written_at")
    if isinstance(raw, str):
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            pass
        else:
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed
    return datetime.min.replace(tzinfo=timezone.utc)


def latest_live_states(rows: list[dict]) -> dict[str, dict]:
    """Per ``client_order_id`` state over LIVE rows only (``dry_run: false`` —
    dry rows never describe broker state). ``latest`` is the greatest
    ``written_at`` among submitted/refresh/cancel rows (ledger current-state
    rule); ties resolve to physical order."""
    states: dict[str, dict] = {}
    for row in rows:
        if row.get("dry_run") is not False:
            continue
        cid = row.get("client_order_id")
        if not isinstance(cid, str) or not cid:
            continue
        info = states.setdefault(
            cid, {"submitted": None, "intent": None, "latest": None, "cancel": False, "skip": False}
        )
        kind = row.get("kind")
        if kind == "submitted" and info["submitted"] is None:
            info["submitted"] = row
        elif kind == "intent":
            info["intent"] = row
        elif kind == "cancel":
            info["cancel"] = True
        elif kind == "skip":
            info["skip"] = True
        if kind in ("submitted", "refresh", "cancel") and (
            info["latest"] is None or _row_instant(row) >= _row_instant(info["latest"])
        ):
            info["latest"] = row
    return states


def has_dangling_intent(states: Mapping[str, dict], client_order_id: str) -> bool:
    """S4 crash marker: a live ``intent`` row with no result row after it."""
    info = states.get(client_order_id)
    return bool(
        info
        and info["intent"] is not None
        and info["submitted"] is None
        and not info["skip"]
        and not info["cancel"]
    )


def entry_client_id(date_iso: str, session: str, ticker: str) -> str:
    """Contract-verbatim entry id: ``<date>-<session>-<ticker>-entry``."""
    return f"{date_iso}-{session}-{ticker}-entry"


def close_client_id(date_iso: str, session: str, ticker: str, attempt: int) -> str:
    """Contract-verbatim close id: ``<date>-<session>-<ticker>-close-<attempt>``."""
    return f"{date_iso}-{session}-{ticker}-close-{attempt}"


def _append_order_row(config: PipelineConfig, fields: Mapping[str, object], clock: Clock) -> dict:
    """Validate against the contract and append one line under flock."""
    record = OrderRecord.model_validate({"written_at": to_utc_iso(clock()), **fields})
    line = json.dumps(record.model_dump(mode="json"), ensure_ascii=False)
    locked_append(Path(config.ledger_dir) / ORDERS_NAME, line)
    append_log_line(
        config.state_dir / ADAPTER_LOG_NAME,
        to_utc_iso(clock()),
        record.kind,
        record.ticker,
        record.client_order_id,
        record.reason or "-",
        "dry" if record.dry_run else "live",
    )
    return record.model_dump(mode="json")


def _slot_live_submissions(rows: list[dict], date_iso: str, session: str) -> int:
    """Prior live submissions charged against this slot's S6 order budget."""
    prefix = f"{date_iso}-{session}-"
    return sum(
        1
        for row in rows
        if row.get("kind") == "submitted"
        and row.get("dry_run") is False
        and str(row.get("client_order_id") or "").startswith(prefix)
    )


# ---------------------------------------------------------------------------
# Eligibility (S5 — recomputed from the DecisionRecord, never a stdout hint)
# ---------------------------------------------------------------------------


def _buy_plan_shape_ok(plan: TradePlan) -> bool:
    return (
        plan.entry_zone is not None
        and len(plan.entry_zone) == 2
        and plan.stop is not None
        and plan.targets is not None
        and len(plan.targets) >= 1
        and plan.sizing is not None
        and math.isfinite(plan.sizing.risk_pct)
    )


def is_execution_candidate(record: DecisionRecord) -> bool:
    """The shape filter: rows that are not executable plans at all produce no
    ``orders.jsonl`` rows (HOLD, ERROR, feeds shadow, invalid plans)."""
    if (
        record.session != "us"
        or record.arm != "brief"
        or record.plan is None
        or not record.plan_valid
        or record.decision not in ("BUY", "SELL")
        or record.plan.action != record.decision
    ):
        return False
    if record.plan.action == "BUY" and not _buy_plan_shape_ok(record.plan):
        logger.warning(
            "%s: plan_valid row with an incomplete BUY plan — not executable", record.run_id
        )
        return False
    return True


def blocking_reason(
    record: DecisionRecord, settings: AdapterSettings, *, allow_manual: bool = False
) -> str | None:
    """S5 gate over the record's own trigger + stored verdicts (the analysis
    runner's gating-table execution column, recomputed here)."""
    if record.trigger == "manual" and not allow_manual:
        return "manual-trigger"
    if record.macro_eval_verdict == "fail":
        return "macro-eval-fail"
    if record.ticker_eval_verdict == "fail":
        return "ticker-eval-fail"
    if "missing" in (record.macro_eval_verdict, record.ticker_eval_verdict):
        return None if settings.execute_on_missing_eval else "eval-missing"
    return None


# ---------------------------------------------------------------------------
# Sizing + portfolio helpers
# ---------------------------------------------------------------------------


def sized_qty(plan: TradePlan, equity: float, settings: AdapterSettings) -> int:
    """``floor(equity × risk_pct/100 / (entry_zone[1] − stop))``, reduced to
    honor the per-order notional cap (S6). 0 after caps ⇒ the caller skips."""
    limit = plan.entry_zone[1]
    distance = limit - plan.stop
    if distance <= 0 or limit <= 0:
        return 0
    qty = math.floor(equity * plan.sizing.risk_pct / 100.0 / distance)
    cap = settings.per_order_notional_cap * equity
    if qty * limit > cap:
        qty = math.floor(cap / limit)
    return max(0, qty)


def trading_days_since(start: date, end: date) -> int:
    """Weekdays strictly after ``start`` up to and including ``end``.

    Weekday approximation of "trading days": a market holiday inside the
    window counts as a trading day and inflates the count by one. The error
    direction is use-dependent — for the refresh stale-entry cancel the
    inflation is fail-safe (a stale entry is canceled no later than a true
    count would), but for the S9 add-on spacing gate it is PERMISSIVE:
    around a holiday, ``add_spacing_days`` can be satisfied one true trading
    day early. Bounded at one day per holiday in the window; an exact count
    needs a broker calendar range query (post-v2 — the broker seam exposes
    only today's session).
    """
    if end <= start:
        return 0
    return sum(
        1
        for offset in range(1, (end - start).days + 1)
        if (start + timedelta(days=offset)).weekday() < 5
    )


def count_tranches(rows: list[dict], ticker: str) -> int:
    """Filled entry brackets building the CURRENT position: filled entries
    after the last filled close (a SELL close closes all tranches)."""
    states = latest_live_states(rows)
    entry_fills: list[datetime] = []
    last_close_fill: datetime | None = None
    for info in states.values():
        sub = info["submitted"]
        if sub is None or sub.get("ticker") != ticker:
            continue
        latest = info["latest"] or sub
        status = str(latest.get("broker_status") or "").lower()
        filledish = status in ("filled", "partially_filled") or bool(latest.get("filled_qty"))
        if not filledish:
            continue
        moment = _row_instant(latest)
        if sub.get("order_kind") == "close" and status == "filled":
            if last_close_fill is None or moment > last_close_fill:
                last_close_fill = moment
        elif sub.get("order_kind") == "bracket":
            entry_fills.append(moment)
    return sum(1 for t in entry_fills if last_close_fill is None or t > last_close_fill)


def last_entry_fill(rows: list[dict], ticker: str) -> tuple[float | None, datetime | None]:
    """Latest recorded entry fill for the ticker: ``(price, instant)`` from
    live rows carrying fill evidence; ``(None, None)`` when unknown."""
    best_time: datetime | None = None
    best_price: float | None = None
    for row in rows:
        if row.get("dry_run") is not False or row.get("ticker") != ticker:
            continue
        if not str(row.get("client_order_id") or "").endswith("-entry"):
            continue
        price = row.get("filled_avg_price")
        status = str(row.get("broker_status") or "").lower()
        if price is None or status not in ("filled", "partially_filled"):
            continue
        moment = _row_instant(row)
        if best_time is None or moment >= best_time:
            best_time, best_price = moment, float(price)
    return best_price, best_time


def last_entry_submit_time(rows: list[dict], ticker: str) -> datetime | None:
    """Latest live entry *submission* instant for the ticker — the stable
    fallback for ``positions.json`` ``last_fill_at`` when the ledger holds no
    fill evidence (e.g. fills that landed while refresh queries were
    erroring). Deliberately never the refresh instant: stamping "now" would
    present a stale position as freshly acquired on every refresh."""
    best: datetime | None = None
    for row in rows:
        if row.get("dry_run") is not False or row.get("ticker") != ticker:
            continue
        if row.get("kind") != "submitted":
            continue
        if not str(row.get("client_order_id") or "").endswith("-entry"):
            continue
        moment = _row_instant(row)
        if best is None or moment > best:
            best = moment
    return best


def _attempt_number(run_id: str) -> int:
    try:
        return int(run_id.rsplit("-", 1)[1])
    except (IndexError, ValueError):
        return 0


def _latest_attempts(records: list[DecisionRecord]) -> list[DecisionRecord]:
    """Reduce a slot's decision rows to the greatest attempt ``<n>`` per
    ``(ticker, arm)`` — the ledger contract's current-state rule. Executing a
    superseded attempt could run BOTH sides of a flipped rerun in one
    invocation (the stale BUY's bracket and the current SELL, or a stale
    live close under the current BUY); only the latest attempt is the
    decision. Original append order is preserved for the survivors."""
    latest: dict[tuple[str, str], DecisionRecord] = {}
    for record in records:
        key = (record.ticker, record.arm)
        current = latest.get(key)
        if current is None or _attempt_number(record.run_id) >= _attempt_number(current.run_id):
            latest[key] = record
    keep = {id(record) for record in latest.values()}
    return [record for record in records if id(record) in keep]


def _order_stop_price(order: BrokerOrder) -> float | None:
    if order.stop_price is not None:
        return order.stop_price
    for leg in order.legs:
        if leg.stop_price is not None:
            return leg.stop_price
    return None


def _cid_order_kind(client_order_id: str) -> str | None:
    if client_order_id.endswith("-entry"):
        return "bracket"
    if _CLOSE_CID_RE.match(client_order_id):
        return "close"
    return None


# ---------------------------------------------------------------------------
# submit entrypoint
# ---------------------------------------------------------------------------


class _SubmitRun:
    """One submit invocation: broker snapshot + running S6/S9 state.

    Rule order per candidate (spec R1 "applies S1–S9 in order"): S5 blocking →
    S8 market day → S4 dedupe/recovery → S8 per-ticker guards → S9 position
    rules → sizing → S6 caps → S9 buying power/total risk → S3-checked
    submission.
    """

    def __init__(
        self,
        *,
        config: PipelineConfig,
        settings: AdapterSettings,
        broker: AlpacaBroker,
        account: AccountState,
        session: str,
        slot_date: date,
        dry_run: bool,
        clock: Clock,
    ) -> None:
        self.config = config
        self.settings = settings
        self.broker = broker
        self.session = session
        self.slot_date = slot_date
        self.date_iso = slot_date.isoformat()
        self.clock = clock
        self.halted = halt_file_path(config).exists()
        self.live = settings.execution_enabled and not dry_run and not self.halted
        self.equity = account.equity
        self.buying_power_left = account.buying_power
        self.counters = {
            "submitted": 0,
            "dry_run": 0,
            "skipped": 0,
            "canceled": 0,
            "recovered": 0,
            "errors": 0,
            "rejected": 0,
        }
        self.order_rows = read_order_rows(config.ledger_dir)
        self.states = latest_live_states(self.order_rows)
        self.entry_done: set[str] = set()
        self.close_done: set[str] = set()

        # S8 market-level guard sources — fail-closed on any error.
        self.market_reason: str | None = None
        try:
            self.now = broker.get_clock()
            open_day = broker.get_calendar_today()
        except Exception as exc:
            logger.warning("market guard sources unavailable: %s", _one_line(exc))
            self.market_reason = "guard-unavailable"
            self.now = clock()
        else:
            if open_day is None:
                self.market_reason = "market-closed"
        if self.now.tzinfo is None:
            self.now = self.now.replace(tzinfo=timezone.utc)

        # Portfolio snapshot (S6/S9 inputs) — fail-closed on any error.
        self.snapshot_reason: str | None = None
        try:
            self.positions = broker.list_positions()
            self.open_orders = broker.list_open_orders()
        except Exception as exc:
            logger.warning("portfolio snapshot unavailable: %s", _one_line(exc))
            self.positions, self.open_orders = [], []
            self.snapshot_reason = "guard-unavailable"

        pending = [order for order in self.open_orders if order.side == "buy"]
        self.positions_mv = sum(p.market_value for p in self.positions)
        self.pending_entry_notional = sum(
            (order.limit_price or 0.0) * max(0.0, order.qty - order.filled_qty)
            for order in pending
        )
        self.exposure_tickers = {p.ticker for p in self.positions} | {
            order.ticker for order in pending
        }
        self.open_risk = self._initial_open_risk(pending)
        self.slot_submissions = _slot_live_submissions(self.order_rows, self.date_iso, session)

    def _initial_open_risk(self, pending: list[BrokerOrder]) -> float:
        """Σ (entry − stop) × qty over open positions and pending entries.

        Fail-closed: a position (or pending entry) whose protective stop leg
        cannot be resolved at the broker counts its FULL entry value as open
        risk — an un-stopped position's worst case is the whole stake, and
        counting it as zero would let a new BUY through an already-breached
        ``max_total_risk`` cap (every other unknown in this adapter blocks)."""
        risk = 0.0
        for position in self.positions:
            stops = [
                order.stop_price
                for order in self.open_orders
                if order.ticker == position.ticker
                and order.side == "sell"
                and order.stop_price is not None
            ]
            if not stops:
                logger.warning(
                    "position %s has no open stop leg — counting its full entry "
                    "value as open risk (fail-closed)",
                    position.ticker,
                )
                risk += max(0.0, position.avg_entry * position.qty)
                continue
            risk += max(0.0, (position.avg_entry - max(stops)) * position.qty)
        for order in pending:
            if order.limit_price is None:
                logger.warning(
                    "pending entry %s has no limit price — cannot price its risk; "
                    "excluded from open-risk sum",
                    order.client_order_id,
                )
                continue
            remaining = max(0.0, order.qty - order.filled_qty)
            stop = _order_stop_price(order)
            if stop is None:
                logger.warning(
                    "pending entry %s has no stop leg — counting its full notional "
                    "as open risk (fail-closed)",
                    order.client_order_id,
                )
                risk += max(0.0, order.limit_price * remaining)
                continue
            risk += max(0.0, (order.limit_price - stop) * remaining)
        return risk

    # -- row plumbing -------------------------------------------------------

    def _append(self, record: DecisionRecord, kind: str, **fields) -> None:
        payload = {
            "run_id": record.run_id,
            "session": record.session,
            "ticker": record.ticker,
            "kind": kind,
            "dry_run": not self.live,
            "reason": None,
            "order_kind": None,
            "tif": None,
            "qty": None,
            "limit_price": None,
            "stop_price": None,
            "target_price": None,
            "broker_status": None,
            "filled_avg_price": None,
            "filled_qty": None,
            **fields,
        }
        _append_order_row(self.config, payload, self.clock)

    def skip(self, record: DecisionRecord, reason: str, client_order_id: str) -> None:
        self.counters["skipped"] += 1
        logger.info("skip %s (%s): %s", record.ticker, record.run_id, reason)
        self._append(record, "skip", reason=reason, client_order_id=client_order_id)

    def _recheck_halt(self) -> None:
        """S3 mid-queue re-check: the halt file appearing after startup flips
        the rest of the run to dry. Called immediately before every individual
        submission AND at the top of :meth:`handle_sell`, so a halted SELL
        skips its cancel+close sequence as a unit — a halt must never strip a
        position's protective legs live and then suppress the close that was
        to replace them (a naked position exactly when nothing else acts)."""
        if self.live and halt_file_path(self.config).exists():
            logger.warning("EXECUTION_HALT appeared mid-queue — remaining queue runs dry (S3)")
            self.live = False
            self.halted = True

    def place(
        self,
        record: DecisionRecord,
        spec: OrderSpec,
        order_kind: str,
        stop_price: float | None,
        target_price: float | None,
    ) -> BrokerOrder | bool:
        """S3-checked submission: intent → broker → submitted (live) or one
        dry-run 'would place' row. Returns the live :class:`BrokerOrder`
        (truthy) so callers can track same-run submissions, ``True`` for a
        dry-run row, ``False`` on a broker error — which resolves the intent
        with a ``submit-error`` skip and never aborts the slot (R4)."""
        self._recheck_halt()
        common = {
            "client_order_id": spec.client_order_id,
            "order_kind": order_kind,
            "tif": spec.tif,
            "qty": float(spec.qty),
            "limit_price": spec.limit_price,
            "stop_price": stop_price,
            "target_price": target_price,
        }
        self.slot_submissions += 1  # dry runs mirror the live per-slot budget
        if not self.live:
            self._append(record, "submitted", dry_run=True, **common)
            self.counters["dry_run"] += 1
            return True
        self._append(record, "intent", dry_run=False, **common)
        try:
            order = self.broker.submit_order(spec)
        except Exception as exc:
            reason = f"submit-error: {_one_line(exc)}"[:300]
            logger.warning("%s: %s", record.ticker, reason)
            self._append(
                record, "skip", dry_run=False, reason=reason, client_order_id=spec.client_order_id
            )
            self.counters["errors"] += 1
            return False
        status = str(order.status or "").lower()
        self._append(record, "submitted", dry_run=False, broker_status=status or None, **common)
        self.counters["submitted"] += 1
        if status == "rejected":
            self.counters["rejected"] += 1
        return order

    # -- shared checks ------------------------------------------------------

    def _position(self, ticker: str) -> BrokerPosition | None:
        return next(
            (p for p in self.positions if p.ticker.upper() == ticker.upper() and p.qty > 0),
            None,
        )

    def _ticker_guard(self, ticker: str) -> tuple[str | None, Quote | None]:
        """S8 per-ticker guards: tradability then quote freshness."""
        try:
            asset = self.broker.get_asset(ticker)
        except Exception as exc:
            logger.warning("asset guard unavailable for %s: %s", ticker, _one_line(exc))
            return "guard-unavailable", None
        if not asset.tradable:
            return "not-tradable", None
        try:
            quote = self.broker.latest_quote(ticker)
        except Exception as exc:
            logger.warning("quote guard unavailable for %s: %s", ticker, _one_line(exc))
            return "guard-unavailable", None
        timestamp = quote.timestamp if quote else None
        if timestamp is None:
            return "guard-unavailable", None
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        # S8: staleness is measured against the clock at the time of THIS
        # check, never the slot-start snapshot — a slow queue must not
        # under-state quote age (the quote-age guard is the operational halt
        # proxy). A clock error here fails closed like every guard source.
        try:
            check_now = self.broker.get_clock()
        except Exception as exc:
            logger.warning("clock guard unavailable for %s: %s", ticker, _one_line(exc))
            return "guard-unavailable", None
        if check_now.tzinfo is None:
            check_now = check_now.replace(tzinfo=timezone.utc)
        age_seconds = (check_now - timestamp).total_seconds()
        if age_seconds > self.settings.max_quote_age_minutes * 60.0:
            return "stale-quote", quote
        return None, quote

    def _recover(self, record: DecisionRecord, cid: str, order_kind: str) -> str:
        """S4 crash recovery: a dangling intent means the broker decides —
        ``recovered`` (order exists; recorded, never resubmitted), ``proceed``
        (broker has nothing), or ``unavailable`` (query failed ⇒ fail closed)."""
        if not has_dangling_intent(self.states, cid):
            return "proceed"
        try:
            existing = self.broker.get_order_by_client_id(cid)
        except Exception as exc:
            logger.warning("recovery query failed for %s: %s", cid, _one_line(exc))
            return "unavailable"
        if existing is None:
            return "proceed"
        logger.warning("dangling intent %s resolved: order exists at the broker", cid)
        self._append(
            record,
            "submitted",
            dry_run=False,
            reason="recovered-intent",
            client_order_id=cid,
            order_kind=order_kind,
            tif=existing.tif or None,
            qty=existing.qty,
            limit_price=existing.limit_price,
            stop_price=existing.stop_price,
            broker_status=existing.status or None,
            filled_qty=existing.filled_qty or None,
            filled_avg_price=existing.filled_avg_price,
        )
        self.counters["recovered"] += 1
        return "recovered"

    def _addon_pre_reason(self, plan: TradePlan, position: BrokerPosition) -> str | None:
        """S9 add-on tranche rules (each violation its own reason)."""
        ticker = position.ticker
        if count_tranches(self.order_rows, ticker) >= self.settings.max_tranches_per_ticker:
            return "max-tranches"
        last_price, last_time = last_entry_fill(self.order_rows, ticker)
        if last_price is None:
            last_price = position.avg_entry
        if self.settings.pyramid_up_only and plan.entry_zone[0] <= last_price:
            return "pyramid-up-only"
        if last_time is None:
            # Spacing cannot be established from the ledger — fail closed.
            return "add-spacing"
        if trading_days_since(last_time.date(), self.slot_date) < self.settings.add_spacing_days:
            return "add-spacing"
        return None

    # -- BUY ----------------------------------------------------------------

    def handle_buy(self, record: DecisionRecord) -> None:
        plan = record.plan
        ticker = record.ticker
        cid = entry_client_id(self.date_iso, record.session, ticker)
        if self.market_reason:
            self.skip(record, self.market_reason, cid)
            return
        # S4 — ticker/day dedupe (never run_id): any prior LIVE submitted
        # bracket for this ticker/day blocks, dry-run rows never do.
        prior = self.states.get(cid)
        if ticker in self.entry_done or (
            prior is not None
            and prior["submitted"] is not None
            and prior["submitted"].get("order_kind") == "bracket"
        ):
            self.skip(record, "dedupe", cid)
            return
        outcome = self._recover(record, cid, "bracket")
        if outcome == "recovered":
            self.entry_done.add(ticker)
            return
        if outcome == "unavailable":
            self.skip(record, "recovery-unavailable", cid)
            return
        if self.snapshot_reason:
            self.skip(record, self.snapshot_reason, cid)
            return
        guard_reason, _quote = self._ticker_guard(ticker)
        if guard_reason:
            self.skip(record, guard_reason, cid)
            return
        position = self._position(ticker)
        if position is not None:
            if not plan.add_intent:
                self.skip(record, "maintain", cid)
                return
            addon_reason = self._addon_pre_reason(plan, position)
            if addon_reason:
                self.skip(record, addon_reason, cid)
                return
        qty = sized_qty(plan, self.equity, self.settings)
        if qty <= 0:
            self.skip(record, "zero-qty", cid)
            return
        limit = plan.entry_zone[1]
        notional = qty * limit
        if (
            position is not None
            and position.market_value + notional
            > self.settings.per_ticker_notional_cap * self.equity + _EPS
        ):
            self.skip(record, "per-ticker-notional-cap", cid)
            return
        if (
            self.positions_mv + self.pending_entry_notional + notional
            > self.settings.max_gross_exposure * self.equity + _EPS
        ):
            self.skip(record, "gross-exposure", cid)
            return
        if (
            ticker not in self.exposure_tickers
            and len(self.exposure_tickers) >= self.settings.max_open_positions
        ):
            self.skip(record, "max-positions", cid)
            return
        if self.slot_submissions >= self.settings.max_orders_per_slot:
            self.skip(record, "max-orders-per-slot", cid)
            return
        if notional > self.buying_power_left + _EPS:
            self.skip(record, "buying-power", cid)
            return
        new_risk = (limit - plan.stop) * qty
        if self.open_risk + new_risk > self.settings.max_total_risk * self.equity + _EPS:
            self.skip(record, "total-risk", cid)
            return
        spec = OrderSpec(
            ticker=ticker,
            side="buy",
            qty=qty,
            limit_price=limit,
            tif="gtc",  # GTC keeps the protective legs alive multi-day
            client_order_id=cid,
            order_class="bracket",
            stop_loss=plan.stop,
            take_profit=plan.targets[0],
        )
        placed = self.place(record, spec, "bracket", plan.stop, plan.targets[0])
        if placed:
            self.entry_done.add(ticker)
            self.pending_entry_notional += notional
            self.exposure_tickers.add(ticker)
            self.buying_power_left -= notional
            self.open_risk += new_risk
            if isinstance(placed, BrokerOrder):
                # Same-run visibility: a later SELL's cancel pass must see the
                # bracket THIS invocation just placed, not only the
                # __init__-time open-orders snapshot — "cancel ALL open
                # orders for the ticker" includes seconds-old ones.
                self.open_orders.append(placed)

    # -- SELL (close) -------------------------------------------------------

    def _has_live_close(self, ticker: str) -> bool:
        prefix = f"{self.date_iso}-{self.session}-{ticker}-close-"
        return any(
            cid.startswith(prefix) and info["submitted"] is not None
            for cid, info in self.states.items()
        )

    def handle_sell(self, record: DecisionRecord) -> None:
        # S3 — re-check the halt BEFORE the cancel pass, not only inside
        # place(): cancel+close is a destructive pair, and a mid-queue halt
        # arriving between candidates must skip the whole SELL as a unit.
        # Otherwise the stale ``self.live`` would strip the position's
        # protective legs live while the halt then suppresses the close.
        self._recheck_halt()
        plan = record.plan
        ticker = record.ticker
        cid = close_client_id(self.date_iso, record.session, ticker, 1)
        if self.market_reason:
            self.skip(record, self.market_reason, cid)
            return
        if ticker in self.close_done or self._has_live_close(ticker):
            self.skip(record, "dedupe", cid)
            return
        outcome = self._recover(record, cid, "close")
        if outcome == "recovered":
            self.close_done.add(ticker)
            return
        if outcome == "unavailable":
            self.skip(record, "recovery-unavailable", cid)
            return
        if self.snapshot_reason:
            self.skip(record, self.snapshot_reason, cid)
            return
        guard_reason, quote = self._ticker_guard(ticker)
        if guard_reason:
            self.skip(record, guard_reason, cid)
            return
        position = self._position(ticker)
        if position is None:
            self.skip(record, "no-position", cid)  # S5: no short opening
            return
        if self.slot_submissions >= self.settings.max_orders_per_slot:
            # Checked BEFORE the cancels: legs must not be stripped for a
            # close that cannot be submitted.
            self.skip(record, "max-orders-per-slot", cid)
            return
        # Step 1 — cancel ALL open orders for the ticker (bracket legs incl.),
        # else the close is rejected for held qty or double-sells into a short.
        cancel_failed = False
        for order in [o for o in self.open_orders if o.ticker.upper() == ticker.upper()]:
            if self.live:
                try:
                    self.broker.cancel_order(order.order_id)
                except Exception as exc:
                    logger.warning(
                        "cancel failed for %s (%s): %s",
                        order.client_order_id,
                        ticker,
                        _one_line(exc),
                    )
                    self.counters["errors"] += 1
                    cancel_failed = True
                    continue
            self._append(
                record,
                "cancel",
                client_order_id=order.client_order_id,
                order_kind=_cid_order_kind(order.client_order_id),
                tif=order.tif or None,
                qty=order.qty,
                limit_price=order.limit_price,
                stop_price=order.stop_price,
            )
            self.counters["canceled"] += 1
        if cancel_failed:
            self.skip(record, "cancel-failed", cid)
            return
        # Step 2 — DAY limit close for the ENTIRE position, clamped to the
        # broker's reported available+held qty (S5).
        close_qty = min(position.qty, position.qty_available + position.qty_held_for_orders)
        if close_qty <= 0:
            self.skip(record, "zero-qty", cid)
            return
        if plan.entry_zone:
            price = plan.entry_zone[0]
        else:
            price = round(quote.bid * (1.0 - CLOSE_BID_DISCOUNT), 2)
        spec = OrderSpec(
            ticker=ticker,
            side="sell",
            qty=close_qty,
            limit_price=price,
            tif="day",
            client_order_id=cid,
        )
        if self.place(record, spec, "close", None, None):
            self.close_done.add(ticker)


def _find_record(ledger_dir: str | Path, run_id: str) -> DecisionRecord:
    for record in load_decision_records(ledger_dir):
        if record.run_id == run_id:
            return record
    raise ValueError(f"run-id {run_id!r} not found in {DECISIONS_NAME}")


def run_submit(
    *,
    config: PipelineConfig,
    settings: AdapterSettings,
    broker: AlpacaBroker,
    slot_date: date | None = None,
    session: str = "us",
    run_id: str | None = None,
    execute: bool = False,
    dry_run: bool = False,
    clock: Clock = _utc_now,
) -> dict:
    """The ``submit`` entrypoint (R1): read the slot's decisions, apply S1-S9
    in order, append one row per attempt/outcome, return the stdout summary."""
    if session != "us":
        raise ValueError("the execution adapter serves the us session only (Alpaca is US-only)")
    account = broker.get_account()  # S1 — before any ledger read or order
    _require_paper(account)

    if run_id is not None:
        record = _find_record(config.ledger_dir, run_id)
        if record.session != "us":
            raise ValueError(
                f"run {run_id} belongs to session {record.session!r} — "
                "cn/hk execution is a non-goal (future adapter)"
            )
        records = [record]
        slot_date = record.date
    else:
        if slot_date is None:
            raise ValueError("slot_date is required without --run-id")
        # Ledger current-state rule: only the greatest attempt per
        # (ticker, arm) is the slot's decision — a rerun before step 7 must
        # never execute both the superseded and the current plan.
        records = _latest_attempts(
            [
                r
                for r in load_decision_records(config.ledger_dir)
                if r.date == slot_date and r.session == session
            ]
        )

    run = _SubmitRun(
        config=config,
        settings=settings,
        broker=broker,
        account=account,
        session=session,
        slot_date=slot_date,
        dry_run=dry_run,
        clock=clock,
    )
    allow_manual = run_id is not None and execute
    for record in records:
        if not is_execution_candidate(record):
            logger.info(
                "not an execution candidate: %s (arm=%s decision=%s plan_valid=%s)",
                record.run_id,
                record.arm,
                record.decision,
                record.plan_valid,
            )
            continue
        cid = (
            entry_client_id(run.date_iso, record.session, record.ticker)
            if record.plan.action == "BUY"
            else close_client_id(run.date_iso, record.session, record.ticker, 1)
        )
        reason = blocking_reason(record, settings, allow_manual=allow_manual)
        if reason:
            run.skip(record, reason, cid)
            continue
        try:
            if record.plan.action == "BUY":
                run.handle_buy(record)
            else:
                run.handle_sell(record)
        except Exception as exc:  # R4: per-order isolation — never abort the slot
            logger.exception("adapter error on %s", record.run_id)
            run.counters["errors"] += 1
            run.skip(record, f"adapter-error: {_one_line(exc)}"[:300], cid)
    return {
        "entrypoint": "submit",
        "date": run.date_iso,
        "session": session,
        "live": run.live,
        "halted": run.halted,
        **run.counters,
    }


# ---------------------------------------------------------------------------
# refresh entrypoint
# ---------------------------------------------------------------------------


def run_refresh(
    *,
    config: PipelineConfig,
    settings: AdapterSettings,
    broker: AlpacaBroker,
    today: date | None = None,
    clock: Clock = _utc_now,
) -> dict:
    """The ``refresh`` entrypoint (R3): refresh rows for every non-terminal
    order, stale-entry cancels, once-only close re-submission, and the
    ``positions.json`` atomic rewrite from live broker state. Never submits
    new entries."""
    account = broker.get_account()  # S1 — both entrypoints
    _require_paper(account)
    if today is None:
        today = session_date("us")
    date_iso = today.isoformat()
    rows = read_order_rows(config.ledger_dir)
    states = latest_live_states(rows)
    halted = halt_file_path(config).exists()
    counters = {
        "refreshed": 0,
        "canceled_stale": 0,
        "close_resubmitted": 0,
        "rejected": 0,
        "canceled": 0,
        "skipped": 0,
        "errors": 0,
    }
    alerts: list[dict] = []
    try:
        now = broker.get_clock()
    except Exception:
        now = clock()
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    snapshot_ok = True
    try:
        positions = broker.list_positions()
        open_orders = broker.list_open_orders()
    except Exception as exc:
        logger.warning("broker positions/orders unavailable: %s", _one_line(exc))
        positions, open_orders = [], []
        snapshot_ok = False
        counters["errors"] += 1

    # -- refresh rows for every non-terminal order --------------------------
    fresh: dict[str, BrokerOrder] = {}
    for cid in sorted(states):
        info = states[cid]
        sub = info["submitted"]
        if sub is None:
            continue  # dangling intents are submit's recovery duty (S4)
        last_status = str((info["latest"] or sub).get("broker_status") or "").lower()
        if last_status in TERMINAL_STATUSES:
            continue
        try:
            order = broker.get_order_by_client_id(cid)
        except Exception as exc:  # R4: one order never aborts the pass
            logger.warning("refresh query failed for %s: %s", cid, _one_line(exc))
            counters["errors"] += 1
            continue
        if order is None:
            logger.warning("order %s unknown at the broker — no refresh row", cid)
            continue
        fresh[cid] = order
        status = str(order.status or "").lower()
        try:
            _append_order_row(
                config,
                {
                    "run_id": sub.get("run_id"),
                    "session": sub.get("session"),
                    "ticker": sub.get("ticker"),
                    "kind": "refresh",
                    "dry_run": False,
                    "reason": None,
                    "client_order_id": cid,
                    "order_kind": None,  # contract: null for refresh rows
                    "tif": order.tif or None,
                    "qty": order.qty,
                    "limit_price": order.limit_price,
                    "stop_price": order.stop_price,
                    "target_price": None,
                    "broker_status": status or None,
                    "filled_qty": order.filled_qty,
                    "filled_avg_price": order.filled_avg_price,
                },
                clock,
            )
        except Exception as exc:
            logger.warning("refresh row rejected by contract for %s: %s", cid, _one_line(exc))
            counters["errors"] += 1
            continue
        counters["refreshed"] += 1
        # Rejected/canceled statuses we did not initiate are notification-worthy.
        if status in ("rejected", "canceled") and not info["cancel"]:
            counters["rejected" if status == "rejected" else "canceled"] += 1
            alerts.append(
                {"kind": status, "ticker": sub.get("ticker"), "client_order_id": cid}
            )

    # -- hygiene 1: cancel stale unfilled entries (never a filled entry) ----
    for cid, order in fresh.items():
        sub = states[cid]["submitted"]
        if sub.get("order_kind") != "bracket":
            continue
        status = str(order.status or "").lower()
        if status in TERMINAL_STATUSES:
            continue
        if (order.filled_qty or 0) > 0:
            continue  # a filled/partial position keeps its GTC protective legs
        submitted_on = _row_instant(sub).date()
        if trading_days_since(submitted_on, today) < 1:
            continue
        try:
            broker.cancel_order(order.order_id)
        except Exception as exc:
            logger.warning("stale-entry cancel failed for %s: %s", cid, _one_line(exc))
            counters["errors"] += 1
            continue
        try:
            _append_order_row(
                config,
                {
                    "run_id": sub.get("run_id"),
                    "session": sub.get("session"),
                    "ticker": sub.get("ticker"),
                    "kind": "cancel",
                    "dry_run": False,
                    "reason": "stale-entry",
                    "client_order_id": cid,
                    "order_kind": "bracket",
                    "tif": order.tif or None,
                    "qty": order.qty,
                    "limit_price": order.limit_price,
                    "stop_price": order.stop_price,
                },
                clock,
            )
        except Exception as exc:
            logger.warning("cancel row rejected by contract for %s: %s", cid, _one_line(exc))
            counters["errors"] += 1
            continue
        counters["canceled_stale"] += 1

    # -- hygiene 2: once-only close re-submission under -close-<n+1> --------
    groups: dict[str, dict[int, str]] = {}
    for cid, info in states.items():
        if info["submitted"] is None:
            continue
        match = _CLOSE_CID_RE.match(cid)
        if match:
            groups.setdefault(match.group("base"), {})[int(match.group("n"))] = cid
    def _skip_resubmit(sub: Mapping, new_cid: str, orig_cid: str, reason: str) -> None:
        try:
            _append_order_row(
                config,
                {
                    "run_id": sub.get("run_id"),
                    "session": sub.get("session"),
                    "ticker": sub.get("ticker"),
                    "kind": "skip",
                    "dry_run": False,
                    "reason": reason,
                    "client_order_id": new_cid,
                },
                clock,
            )
        except Exception as exc:
            logger.warning("skip row rejected for %s: %s", new_cid, _one_line(exc))
        counters["skipped"] += 1
        alerts.append(
            {"kind": "close-unfilled", "ticker": sub.get("ticker"),
             "client_order_id": orig_cid, "reason": reason}
        )

    for base in sorted(groups):
        attempts = groups[base]
        attempt = max(attempts)
        cid = attempts[attempt]
        info = states[cid]
        sub = info["submitted"]
        order = fresh.get(cid)
        status = str(
            (order.status if order else (info["latest"] or sub).get("broker_status")) or ""
        ).lower()
        qty = float((order.qty if order else sub.get("qty")) or 0.0)
        filled = float(
            (order.filled_qty if order else (info["latest"] or {}).get("filled_qty")) or 0.0
        )
        # "Unfilled at day end" ⇒ the DAY limit expired. A rejected or
        # externally canceled close alerts (above) but is never auto-retried.
        if status != "expired" or filled >= qty:
            continue
        ticker = str(sub.get("ticker"))
        run_id = str(sub.get("run_id"))
        if attempt >= 2:
            alerts.append({"kind": "close-unfilled", "ticker": ticker, "client_order_id": cid})
            continue
        new_cid = f"{base}{attempt + 1}"
        new_info = states.get(new_cid)
        if new_info is not None and (
            new_info["submitted"] is not None or new_info["intent"] is not None
        ):
            # Once-only is structural — but only a REAL broker attempt
            # (a submitted row, or an intent the broker may have acted on)
            # consumes it. Guard/halt/disabled skips do not: a transient
            # outage must not convert the one retry into never. With the
            # retry spent and the close still unfilled, keep escalating.
            alerts.append({"kind": "close-unfilled", "ticker": ticker, "client_order_id": cid})
            continue
        if not snapshot_ok:
            _skip_resubmit(sub, new_cid, cid, "guard-unavailable")
            continue
        position = next(
            (p for p in positions if p.ticker.upper() == ticker.upper() and p.qty > 0), None
        )
        if position is None:
            logger.info("close %s: no position left — nothing to re-submit", base)
            continue
        if halted or not settings.execution_enabled:
            _skip_resubmit(sub, new_cid, cid, "halt" if halted else "execution-disabled")
            continue
        try:
            quote = broker.latest_quote(ticker)
        except Exception as exc:
            logger.warning("close re-submit quote unavailable for %s: %s", ticker, _one_line(exc))
            _skip_resubmit(sub, new_cid, cid, "guard-unavailable")
            continue
        timestamp = quote.timestamp
        if timestamp is not None and timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        # Quote age against the clock at the time of THIS check, never the
        # refresh-start snapshot (same S8 posture as submit); clock errors
        # fail closed.
        try:
            check_now = broker.get_clock()
        except Exception as exc:
            logger.warning(
                "clock unavailable for close re-submit %s: %s", ticker, _one_line(exc)
            )
            _skip_resubmit(sub, new_cid, cid, "guard-unavailable")
            continue
        if check_now.tzinfo is None:
            check_now = check_now.replace(tzinfo=timezone.utc)
        if (
            timestamp is None
            or (check_now - timestamp).total_seconds() > settings.max_quote_age_minutes * 60.0
        ):
            _skip_resubmit(
                sub, new_cid, cid, "stale-quote" if timestamp is not None else "guard-unavailable"
            )
            continue
        remaining = min(
            qty - filled, position.qty, position.qty_available + position.qty_held_for_orders
        )
        if remaining <= 0:
            continue
        price = round(quote.bid * (1.0 - CLOSE_BID_DISCOUNT), 2)
        common = {
            "run_id": run_id,
            "session": sub.get("session"),
            "ticker": ticker,
            "dry_run": False,
            "client_order_id": new_cid,
            "order_kind": "close",
            "tif": "day",
            "qty": remaining,
            "limit_price": price,
        }
        _append_order_row(config, {**common, "kind": "intent"}, clock)
        spec = OrderSpec(
            ticker=ticker,
            side="sell",
            qty=remaining,
            limit_price=price,
            tif="day",
            client_order_id=new_cid,
        )
        try:
            order = broker.submit_order(spec)
        except Exception as exc:
            reason = f"submit-error: {_one_line(exc)}"[:300]
            logger.warning("close re-submit failed for %s: %s", new_cid, reason)
            _append_order_row(
                config,
                {**common, "kind": "skip", "reason": reason, "order_kind": None,
                 "tif": None, "qty": None, "limit_price": None},
                clock,
            )
            counters["errors"] += 1
            continue
        _append_order_row(
            config,
            {**common, "kind": "submitted", "broker_status": str(order.status or "").lower()},
            clock,
        )
        counters["close_resubmitted"] += 1

    # -- positions.json: atomic rewrite from live broker state --------------
    if snapshot_ok:
        entries: list[PositionEntry] = []
        for position in positions:
            legs = [
                OpenOrderLeg(
                    client_order_id=order.client_order_id,
                    leg="stop" if order.stop_price is not None else "target",
                    price=order.stop_price
                    if order.stop_price is not None
                    else order.limit_price,
                )
                for order in open_orders
                if order.ticker.upper() == position.ticker.upper()
                and order.side == "sell"
                and (order.stop_price is not None or order.limit_price is not None)
            ]
            _price, fill_time = last_entry_fill(rows, position.ticker)
            if fill_time is None:
                # No fill evidence in the ledger: fall back to the entry's
                # submission instant — stable across refreshes — rather than
                # fabricating the refresh time (which would present a stale
                # position as freshly acquired every day). A fully
                # out-of-band position (no ledger rows at all) still gets
                # ``now`` as a last resort; the contract requires a value.
                fill_time = last_entry_submit_time(rows, position.ticker)
            entries.append(
                PositionEntry(
                    ticker=position.ticker,
                    qty=position.qty,
                    avg_entry=position.avg_entry,
                    last_fill_at=fill_time or now,
                    market_value=position.market_value,
                    unrealized_pl=position.unrealized_pl,
                    tranches=max(1, count_tranches(rows, position.ticker)),
                    open_orders=legs,
                )
            )
        snapshot = PositionsSnapshot(as_of=clock(), equity=account.equity, positions=entries)
        atomic_write(
            Path(config.ledger_dir) / POSITIONS_NAME,
            json.dumps(snapshot.model_dump(mode="json"), indent=2, ensure_ascii=False) + "\n",
        )

    return {
        "entrypoint": "refresh",
        "date": date_iso,
        "session": "us",
        **counters,
        "alerts": alerts,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"invalid date {value!r} — expected YYYY-MM-DD") from None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m pipeline.execution_adapter",
        description="Alpaca PAPER execution adapter — the system's only broker "
        "gateway (specs/execution-adapter.md).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    submit = sub.add_parser(
        "submit", help="slot step 7: turn the slot's eligible decisions into paper orders"
    )
    submit.add_argument(
        "--session",
        choices=["us"],
        default="us",
        help="us only — Alpaca is US-only (runner R5)",
    )
    submit.add_argument(
        "--date",
        type=_parse_date,
        default=None,
        metavar="YYYY-MM-DD",
        help="slot date (default: today in the us session timezone)",
    )
    submit.add_argument(
        "--run-id",
        dest="run_id",
        default=None,
        help="process exactly one decision record (the only manual path, S5)",
    )
    submit.add_argument(
        "--execute",
        action="store_true",
        help="with --run-id: lift the manual-trigger exclusion "
        "(execution_enabled, the halt file, and every guard still apply)",
    )
    submit.add_argument(
        "--dry-run",
        action="store_true",
        help="force dry-run rows regardless of config (R5)",
    )

    refresh = sub.add_parser(
        "refresh",
        help="settle step: order-status refresh, stale-entry cancels, close "
        "re-submission, positions.json rewrite",
    )
    refresh.add_argument(
        "--date",
        type=_parse_date,
        default=None,
        metavar="YYYY-MM-DD",
        help="reference trading date (default: today in the us session timezone)",
    )
    return parser


def main(
    argv: list[str] | None = None,
    *,
    broker_factory: BrokerFactory | None = None,
    clock: Clock = _utc_now,
) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.INFO, format="%(levelname)s %(name)s: %(message)s", stream=sys.stderr
    )
    if getattr(args, "execute", False) and not getattr(args, "run_id", None):
        print(
            "execution-adapter: --execute requires --run-id (the S5 manual path)",
            file=sys.stderr,
        )
        return 2
    try:
        config = load_config()
        settings = load_settings()
        broker = (broker_factory or default_broker_factory)()
        if args.command == "submit":
            summary = run_submit(
                config=config,
                settings=settings,
                broker=broker,
                slot_date=args.date or session_date("us"),
                session=args.session,
                run_id=args.run_id,
                execute=args.execute,
                dry_run=args.dry_run,
                clock=clock,
            )
        else:
            summary = run_refresh(
                config=config,
                settings=settings,
                broker=broker,
                today=args.date or session_date("us"),
                clock=clock,
            )
    except KeyboardInterrupt:
        print("execution-adapter: interrupted", file=sys.stderr)
        return 130
    except PaperAccountError as exc:
        print(f"execution-adapter: {exc}", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(f"execution-adapter: {_one_line(exc)}", file=sys.stderr)
        return 2
    except Exception as exc:
        logger.debug("adapter failure detail", exc_info=True)
        print(f"execution-adapter: {_one_line(exc)}", file=sys.stderr)
        return 1
    # The final stdout line is the JSON summary the orchestrator parses (R4).
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
