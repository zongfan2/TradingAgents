"""Settle step — slot step 0: adapter-refresh trigger + outcome backfill.

Spec: specs/orchestrator.md "Settle step (0)" + the outcomes schema in
specs/trade-plan-ledger-contract.md. The settle step **never talks to the
broker itself**: it first invokes ``execution_adapter refresh`` as a
subprocess (the adapter is the system's sole broker gateway), and an absent
or failing adapter degrades to a warning — outcome computation always
proceeds.

Outcome rules implemented here (ledger contract, verbatim):

- **Return anchor (no lookahead, no off-by-one)**: ``d0`` = the first
  regular-session close at or after ``decided_at`` on the ticker's own price
  calendar — a date with a bar means it traded (no holiday tables) — and a
  bar whose close instant is still in the future of the injectable clock is
  never used. ``returns.dh`` = close(d0 + h trading days) / close(d0) − 1,
  null until computable; the benchmark is computed identically on the
  benchmark's own bar calendar. The benchmark resolves per exchange suffix
  via :data:`BENCHMARK_MAP` — a verbatim mirror of ``tradingagents``'
  ``benchmark_map`` (reimplemented because pipeline components never import
  the heavy package at module scope; parity is pinned by a test).
- **Plan replay** (BUY with ``plan_valid`` only; SELL/HOLD/invalid ⇒ null) on
  daily OHLC bars **strictly after** ``decided_at`` — a bar qualifies only
  when its session *open* is at/after the decision, so the decision day's bar
  replays only for pre-open decisions (while the same day's close can still
  anchor the returns): ``entry_hit`` = low ≤ ``entry_zone[1]`` within
  ``horizon_days`` of the anchor; then first-touch ordering of ``stop`` vs
  ``targets[0]`` on the bars after the entry bar; both touched in one bar ⇒
  ``ambiguous`` (excluded from R:R). ``realized_rr`` = (exit − entry) /
  (entry − stop) with entry = ``entry_zone[1]`` and exit = the first-touched
  level.
- **Paper block** from the ``orders.jsonl`` join (latest ``written_at`` per
  ``client_order_id``, live rows only): realized paper P&L over filled closes
  matched against the RECORD's own filled entry — the contract entry id is
  ``<date>-<session>-<ticker>-entry`` (entries are never re-submitted; a
  re-entry is a new date), so a later round trip on the same ticker never
  rewrites an older run's P&L, and the close window is bounded by the
  ticker's next filled entry. Null when the run never executed live or no
  realized P&L is computable yet.
- One :class:`OutcomeRecord` per ``run_id`` via ``locked_append`` — the
  current record is the one with the greatest ``settled_at``. A re-run
  appends only when something new became computable (idempotent otherwise),
  and a transient bars-fetch failure can never regress an already-settled
  horizon: computed nulls merge with, never overwrite, the stored record.

The clock, the daily-bars fetcher (default: yfinance), and the refresh
subprocess runner are all injectable so tests run fully offline.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import re
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from pipeline.common import SESSIONS, append_log_line, locked_append, session_date, to_utc_iso
from pipeline.config import PipelineConfig, load_config
from pipeline.contracts.ledger import (
    DecisionRecord,
    HorizonReturns,
    OutcomeRecord,
    PaperOutcome,
    PlanReplay,
    TradePlan,
)

# Ledger IO is shared with the adapter module (reading only — importing it
# constructs no broker client; alpaca-py stays a lazy import over there).
from pipeline.execution_adapter import latest_live_states, load_decision_records, read_order_rows

logger = logging.getLogger("pipeline.settle")

OUTCOMES_NAME = "outcomes.jsonl"
SETTLE_LOG_NAME = "settle.log"

#: Budget for the adapter-refresh subprocess — well inside the orchestrator's
#: 15-minute settle-step budget, leaving room for the outcomes computation.
REFRESH_TIMEOUT_S = 600.0

_HORIZONS = (("d1", 1), ("d5", 5), ("d20", 20))
_FILLEDISH = ("filled", "partially_filled")
_CLOSE_CID_RE = re.compile(r"-close-[1-9][0-9]*$")
_EPS = 1e-9

Clock = Callable[[], datetime]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _one_line(exc: BaseException) -> str:
    return " ".join(str(exc).split()) or exc.__class__.__name__


# ---------------------------------------------------------------------------
# Market hours (anchor rule inputs)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MarketHours:
    """Regular-session open/close a symbol's daily bar maps to."""

    tz: str
    open_time: tuple[int, int]
    close_time: tuple[int, int]


#: Suffix → market hours for every dotted suffix in :data:`BENCHMARK_MAP`;
#: everything else (plain US listings, unrecognised suffixes) uses New York —
#: the same fallback posture as the benchmark suffix map. Covering all mapped
#: suffixes matters for the no-lookahead anchor rule: with the New York
#: fallback a post-close decision on e.g. a Tokyo listing would map that day's
#: already-elapsed close to a future instant and anchor d0 BEFORE decided_at.
_MARKET_HOURS: dict[str, MarketHours] = {
    ".SS": MarketHours("Asia/Shanghai", (9, 30), (15, 0)),
    ".SZ": MarketHours("Asia/Shanghai", (9, 30), (15, 0)),
    ".HK": MarketHours("Asia/Hong_Kong", (9, 30), (16, 0)),
    ".T": MarketHours("Asia/Tokyo", (9, 0), (15, 30)),
    ".L": MarketHours("Europe/London", (8, 0), (16, 30)),
    ".NS": MarketHours("Asia/Kolkata", (9, 15), (15, 30)),
    ".BO": MarketHours("Asia/Kolkata", (9, 15), (15, 30)),
    ".TO": MarketHours("America/Toronto", (9, 30), (16, 0)),
    ".AX": MarketHours("Australia/Sydney", (10, 0), (16, 0)),
}
_DEFAULT_MARKET_HOURS = MarketHours("America/New_York", (9, 30), (16, 0))


def market_hours(symbol: str) -> MarketHours:
    upper = symbol.upper()
    for suffix, hours in _MARKET_HOURS.items():
        if upper.endswith(suffix):
            return hours
    return _DEFAULT_MARKET_HOURS


def close_instant(day: date, hours: MarketHours) -> datetime:
    hour, minute = hours.close_time
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=ZoneInfo(hours.tz))


def open_instant(day: date, hours: MarketHours) -> datetime:
    hour, minute = hours.open_time
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=ZoneInfo(hours.tz))


# ---------------------------------------------------------------------------
# Benchmark resolution (suffix map — mirror of tradingagents' benchmark_map)
# ---------------------------------------------------------------------------

#: Verbatim mirror of ``tradingagents.default_config.DEFAULT_CONFIG["benchmark_map"]``
#: (test-pinned parity). Each map entry's benchmark trades in the ticker's own
#: market, so the ticker's market hours drive the benchmark anchor too, while
#: the benchmark's *own* bar dates define its calendar.
BENCHMARK_MAP: dict[str, str] = {
    ".NS": "^NSEI",  # NSE India (Nifty 50)
    ".BO": "^BSESN",  # BSE India (Sensex)
    ".T": "^N225",  # Tokyo (Nikkei 225)
    ".HK": "^HSI",  # Hong Kong (Hang Seng)
    ".L": "^FTSE",  # London (FTSE 100)
    ".TO": "^GSPTSE",  # Toronto (TSX Composite)
    ".AX": "^AXJO",  # Australia (ASX 200)
    ".SS": "000001.SS",  # Shanghai (SSE Composite)
    ".SZ": "399001.SZ",  # Shenzhen (SZSE Component)
    "": "SPY",  # default for US-listed tickers (no suffix)
}


def resolve_benchmark(ticker: str) -> str:
    """Suffix-based benchmark resolution (``tradingagents``' rule): first
    matching suffix wins; no dotted suffix (or an unrecognised one, e.g.
    ``BRK.B``) falls back to the empty-suffix entry."""
    upper = ticker.upper()
    for suffix, benchmark in BENCHMARK_MAP.items():
        if suffix and upper.endswith(suffix):
            return benchmark
    return BENCHMARK_MAP[""]


# ---------------------------------------------------------------------------
# Daily bars (injectable fetcher; default yfinance)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DailyBar:
    date: date
    open: float
    high: float
    low: float
    close: float


#: ``(symbol, start, end) -> daily bars`` (dates inclusive; order irrelevant —
#: the settle step sorts and applies the no-lookahead close filter itself).
BarsFetcher = Callable[[str, date, date], "list[DailyBar]"]


def default_bars_fetcher(symbol: str, start: date, end: date) -> list[DailyBar]:
    """Daily OHLC from the same yfinance series the rest of the pipeline uses
    (symbol normalized identically; yfinance's default dividend adjustment
    kept). Any failure returns no bars — the caller then simply computes
    nothing new this run."""
    try:
        import yfinance as yf

        from tradingagents.dataflows.symbol_utils import normalize_symbol

        history = yf.Ticker(normalize_symbol(symbol)).history(
            start=start.isoformat(),
            end=(end + timedelta(days=1)).isoformat(),
            interval="1d",
        )
        if history is None or history.empty:
            return []
        bars: list[DailyBar] = []
        for idx, row in history.iterrows():
            values = [row.get(field) for field in ("Open", "High", "Low", "Close")]
            if any(value is None or not math.isfinite(float(value)) for value in values):
                continue
            bars.append(DailyBar(idx.date(), *(float(value) for value in values)))
        return bars
    except Exception as exc:
        logger.warning("daily bars fetch failed for %s: %s", symbol, _one_line(exc))
        return []


def usable_bars(bars: Sequence[DailyBar], hours: MarketHours, now: datetime) -> list[DailyBar]:
    """Date-sorted bars whose regular-session close has already happened —
    an in-progress (or lookahead) bar is never treated as a close."""
    ordered = sorted(bars, key=lambda bar: bar.date)
    return [bar for bar in ordered if close_instant(bar.date, hours) <= now]


def anchor_index(
    bars: Sequence[DailyBar], decided_at: datetime, hours: MarketHours
) -> int | None:
    """Index of ``d0``: the first bar whose close is at/after ``decided_at``
    (contract anchor rule — a run finishing after the open still anchors to
    that same day's close, which is in its future). ``None`` until that close
    exists in the usable series."""
    for index, bar in enumerate(bars):
        if close_instant(bar.date, hours) >= decided_at:
            return index
    return None


def horizon_returns(bars: Sequence[DailyBar], i0: int | None) -> HorizonReturns:
    """close(d0 + h) / close(d0) − 1 per horizon; null until computable."""
    if i0 is None:
        return HorizonReturns()
    base = bars[i0].close
    values: dict[str, float | None] = {}
    for label, horizon in _HORIZONS:
        j = i0 + horizon
        values[label] = bars[j].close / base - 1.0 if base > 0 and j < len(bars) else None
    return HorizonReturns(**values)


# ---------------------------------------------------------------------------
# Plan replay (BUY + plan_valid only)
# ---------------------------------------------------------------------------


def replay_plan(
    plan: TradePlan,
    bars: Sequence[DailyBar],
    decided_at: datetime,
    hours: MarketHours,
) -> PlanReplay | None:
    """Contract plan replay on bars strictly after ``decided_at``.

    ``None`` while no eligible bar exists yet (nothing to replay). A bar is
    eligible only when its session open is at/after ``decided_at`` — the bar
    containing the decision instant would replay prices from before the
    decision. The entry window is ``horizon_days`` trading days from the
    return anchor; ``entry_hit: false`` is asserted only once that whole
    window has printed — while bars inside it are still pending the outcome
    is undetermined and stays null, exactly like an uncomputable horizon
    return. The exit scan (bars after the entry bar) is unbounded, so a
    pending exit resolves on a later settle run.
    """
    if (
        plan.entry_zone is None
        or len(plan.entry_zone) != 2
        or plan.stop is None
        or not plan.targets
        or plan.horizon_days is None
    ):
        return None  # plan_valid should preclude this; stay defensive
    entry = plan.entry_zone[1]
    stop = plan.stop
    target = plan.targets[0]
    if entry - stop <= 0:
        return None
    i0 = anchor_index(bars, decided_at, hours)
    eligible = [
        (index, bar)
        for index, bar in enumerate(bars)
        if open_instant(bar.date, hours) >= decided_at
    ]
    if i0 is None or not eligible:
        return None
    entry_index = next(
        (index for index, bar in eligible if index <= i0 + plan.horizon_days and bar.low <= entry),
        None,
    )
    if entry_index is None:
        if len(bars) <= i0 + plan.horizon_days:
            # The entry window has not fully elapsed and no bar has touched
            # the zone — entry_hit is still undetermined, not false.
            return None
        return PlanReplay(
            entry_hit=False, stop_hit_first=False, target_hit_first=False, ambiguous=False
        )
    for bar in bars[entry_index + 1 :]:
        stop_touch = bar.low <= stop
        target_touch = bar.high >= target
        if stop_touch and target_touch:
            # Both levels inside one daily bar: intraday order unknowable.
            return PlanReplay(
                entry_hit=True, stop_hit_first=False, target_hit_first=False, ambiguous=True
            )
        if stop_touch:
            return PlanReplay(
                entry_hit=True,
                stop_hit_first=True,
                target_hit_first=False,
                ambiguous=False,
                realized_rr=(stop - entry) / (entry - stop),
            )
        if target_touch:
            return PlanReplay(
                entry_hit=True,
                stop_hit_first=False,
                target_hit_first=True,
                ambiguous=False,
                realized_rr=(target - entry) / (entry - stop),
            )
    # Entered, no exit level touched yet — rr stays null until one is.
    return PlanReplay(
        entry_hit=True, stop_hit_first=False, target_hit_first=False, ambiguous=False
    )


def _wants_replay(record: DecisionRecord) -> bool:
    return (
        record.decision == "BUY"
        and record.plan_valid
        and record.plan is not None
        and record.plan.action == "BUY"
    )


# ---------------------------------------------------------------------------
# Paper block (orders.jsonl join)
# ---------------------------------------------------------------------------


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


def _run_executed_live(record: DecisionRecord, order_rows: list[dict]) -> bool:
    """A live ``submitted`` row for the run's ``run_id`` — dry-run rows never
    count (ledger contract: paper is null when the run never executed)."""
    return any(
        row.get("kind") == "submitted"
        and row.get("dry_run") is False
        and row.get("run_id") == record.run_id
        for row in order_rows
    )


def compute_paper(record: DecisionRecord, order_rows: list[dict]) -> PaperOutcome | None:
    """Realized paper P&L for the run's position, or ``None``.

    ``None`` when the run never executed live (no live ``submitted`` row for
    its ``run_id`` — dry-run rows never count) or when no realized P&L is
    computable yet (entry not filled, or nothing closed). Current order state
    is the greatest ``written_at`` per ``client_order_id`` over live rows
    (ledger current-state rule). The entry is the RECORD's own: its client id
    is deterministic — ``<date>-<session>-<ticker>-entry``, entries are never
    re-submitted and a re-entry is a new date — so a later round trip on the
    same ticker can never rewrite this run's realized P&L (nor is one
    position's P&L ever attributed to a second run_id). Closes are still
    matched at the ticker level (the close of a BUY run's position may have
    been submitted under a later SELL run's id, or a refresh re-submission),
    but only fills inside this entry's position window: at/after its fill and
    before the ticker's next filled entry.
    """
    if not _run_executed_live(record, order_rows):
        return None
    states = latest_live_states(order_rows)
    own_entry_cid = f"{record.date.isoformat()}-{record.session}-{record.ticker}-entry"
    entry_time: datetime | None = None
    entry_price: float | None = None
    entry_qty: float | None = None
    other_entry_times: list[datetime] = []
    closes: list[tuple[datetime, float, float]] = []
    for cid, info in states.items():
        sub = info["submitted"]
        if sub is None or sub.get("ticker") != record.ticker:
            continue
        latest = info["latest"] or sub
        price = latest.get("filled_avg_price")
        qty = latest.get("filled_qty")
        if price is None or not qty:
            continue
        moment = _row_instant(latest)
        status = str(latest.get("broker_status") or "").lower()
        if cid.endswith("-entry") and status in _FILLEDISH:
            if cid == own_entry_cid:
                entry_time, entry_price, entry_qty = moment, float(price), float(qty)
            else:
                other_entry_times.append(moment)
        elif _CLOSE_CID_RE.search(cid):
            # Any close with fill evidence realizes P&L — an expired DAY limit
            # can still carry a partial fill.
            closes.append((moment, float(price), float(qty)))
    if entry_time is None or entry_price is None or not entry_qty:
        return None
    # Closes at/after the NEXT round trip's entry belong to that position.
    window_end = min((t for t in other_entry_times if t > entry_time), default=None)
    fills = sorted(
        (
            c
            for c in closes
            if c[0] >= entry_time and (window_end is None or c[0] < window_end)
        ),
        key=lambda c: c[0],
    )
    remaining = entry_qty
    realized = 0.0
    closed_qty = 0.0
    for _moment, price, qty in fills:
        take = min(qty, remaining)
        if take <= 0:
            break
        realized += (price - entry_price) * take
        closed_qty += take
        remaining -= take
    if closed_qty <= 0:
        return None
    return PaperOutcome(realized_pnl=realized, closed=remaining <= _EPS)


# ---------------------------------------------------------------------------
# outcomes.jsonl state (current record = greatest settled_at per run_id)
# ---------------------------------------------------------------------------


def read_latest_outcomes(ledger_dir: str | Path) -> dict[str, OutcomeRecord]:
    path = Path(ledger_dir) / OUTCOMES_NAME
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    latest: dict[str, OutcomeRecord] = {}
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            record = OutcomeRecord.parse_lenient(json.loads(line))
        except Exception as exc:
            logger.warning("outcomes.jsonl: skipping unparseable row (%s)", _one_line(exc))
            continue
        current = latest.get(record.run_id)
        if current is None or record.settled_at >= current.settled_at:
            latest[record.run_id] = record
    return latest


_PAYLOAD_KEYS = ("returns", "benchmark_returns", "plan_replay", "paper")


def _outcome_payload(record: OutcomeRecord) -> dict:
    dumped = record.model_dump(mode="json")
    return {key: dumped[key] for key in _PAYLOAD_KEYS}


def _replay_rank(replay: Mapping | None) -> int:
    """How far a replay has progressed: 0 = no entry, 1 = entered (exit
    pending), 2 = exit resolved. With a monotonically growing bar history a
    legitimate re-replay can only advance."""
    if replay is None:
        return -1
    if replay["stop_hit_first"] or replay["target_hit_first"] or replay["ambiguous"]:
        return 2
    return 1 if replay["entry_hit"] else 0


def _merge_payloads(computed: dict, existing: dict | None) -> dict:
    """Computed values win where present; a null never overwrites a stored
    value, and a replay never moves backwards — a transient/partial bars
    fetch must not regress a settled horizon or demote a resolved replay."""
    if existing is None:
        return computed
    merged: dict = {}
    for key in ("returns", "benchmark_returns"):
        merged[key] = {
            label: computed[key][label] if computed[key][label] is not None else existing[key][label]
            for label, _ in _HORIZONS
        }
    replay = computed["plan_replay"]
    if _replay_rank(replay) < _replay_rank(existing["plan_replay"]):
        replay = existing["plan_replay"]
    merged["plan_replay"] = replay
    merged["paper"] = computed["paper"] if computed["paper"] is not None else existing["paper"]
    return merged


def _has_content(payload: dict) -> bool:
    return (
        any(value is not None for value in payload["returns"].values())
        or any(value is not None for value in payload["benchmark_returns"].values())
        or payload["plan_replay"] is not None
        or payload["paper"] is not None
    )


def _fully_settled(record: DecisionRecord, payload: dict, order_rows: list[dict]) -> bool:
    """True when the stored record can never change again: every horizon
    present on both return series, the replay terminal (exit resolved or
    ambiguous — rank 2 — or the entry window elapsed without entry — rank 0 —
    or no replay wanted), and the paper block final (closed, or impossible
    because the run never executed live; an open position keeps settling).
    Such records skip the bars fetch entirely — without this cutoff every
    historical decision row would refetch its ticker AND benchmark daily
    series on every slot, growing settle's yfinance call volume without bound
    against the orchestrator's fixed settle-step budget."""
    for key in ("returns", "benchmark_returns"):
        if any(payload[key][label] is None for label, _ in _HORIZONS):
            return False
    if _wants_replay(record) and _replay_rank(payload["plan_replay"]) not in (0, 2):
        return False
    paper = payload["paper"]
    if paper is not None:
        return bool(paper["closed"])
    return not _run_executed_live(record, order_rows)


def _describe(payload: dict) -> str:
    def horizons(block: Mapping) -> str:
        present = ",".join(label for label, _ in _HORIZONS if block[label] is not None)
        return present or "-"

    replay = payload["plan_replay"]
    if replay is None:
        replay_text = "-"
    elif replay["ambiguous"]:
        replay_text = "ambiguous"
    elif replay["stop_hit_first"]:
        replay_text = "stop-first"
    elif replay["target_hit_first"]:
        replay_text = "target-first"
    elif replay["entry_hit"]:
        replay_text = "entry-hit"
    else:
        replay_text = "no-entry"
    paper = payload["paper"]
    if paper is None:
        paper_text = "-"
    else:
        paper_text = f"pnl={paper['realized_pnl']:.2f}" + ("/closed" if paper["closed"] else "")
    return (
        f"returns={horizons(payload['returns'])} | "
        f"benchmark={horizons(payload['benchmark_returns'])} | "
        f"replay={replay_text} | paper={paper_text}"
    )


# ---------------------------------------------------------------------------
# Adapter refresh (subprocess — settle never constructs a broker client)
# ---------------------------------------------------------------------------

#: ``(argv, timeout_s) -> (returncode, stdout, stderr)``.
RefreshRunner = Callable[[Sequence[str], float], "tuple[int, str, str]"]


def refresh_argv(config: PipelineConfig) -> list[str]:
    return [str(config.python_executable), "-m", "pipeline.execution_adapter", "refresh"]


def default_refresh_runner(argv: Sequence[str], timeout: float) -> tuple[int, str, str]:
    proc = subprocess.run(list(argv), capture_output=True, text=True, timeout=timeout)
    return proc.returncode, proc.stdout, proc.stderr


def _last_nonempty_line(text: str) -> str:
    for line in reversed(text.splitlines()):
        if line.strip():
            return line.strip()
    return ""


def run_adapter_refresh(
    config: PipelineConfig, runner: RefreshRunner, timeout: float
) -> tuple[bool, str | None]:
    """Invoke ``execution_adapter refresh``; failures degrade to a warning
    (``(False, reason)``) — the outcomes computation proceeds regardless."""
    argv = refresh_argv(config)
    try:
        returncode, _stdout, stderr = runner(argv, timeout)
    except Exception as exc:  # adapter absent, spawn failure, timeout
        reason = f"adapter refresh could not run: {_one_line(exc)}"[:300]
        logger.warning("%s — outcome computation proceeds without it", reason)
        return False, reason
    if returncode != 0:
        detail = _last_nonempty_line(stderr) or f"exit {returncode}"
        reason = f"adapter refresh exited {returncode}: {detail}"[:300]
        logger.warning("%s — outcome computation proceeds without it", reason)
        return False, reason
    return True, None


# ---------------------------------------------------------------------------
# The settle run
# ---------------------------------------------------------------------------


def run_settle(
    *,
    config: PipelineConfig,
    session: str,
    slot_date: date | None = None,
    bars_fetcher: BarsFetcher | None = None,
    refresh_runner: RefreshRunner | None = None,
    clock: Clock = _utc_now,
    refresh_timeout: float = REFRESH_TIMEOUT_S,
) -> dict:
    """Step 0: adapter refresh, then emit/refresh outcome records.

    Only this session's decision rows are settled (cn/us data stays disjoint —
    concurrent slots can never double-append the same run). Returns the stdout
    summary dict the orchestrator's outcome mapper parses.
    """
    if session not in SESSIONS:
        raise ValueError(f"unknown session {session!r} — expected one of {sorted(SESSIONS)}")
    now = clock()
    slot_date = slot_date or session_date(session, now)
    fetch = bars_fetcher or default_bars_fetcher

    refresh_ok, refresh_error = run_adapter_refresh(
        config, refresh_runner or default_refresh_runner, refresh_timeout
    )

    records = [r for r in load_decision_records(config.ledger_dir) if r.session == session]
    order_rows = read_order_rows(config.ledger_dir)
    existing = read_latest_outcomes(config.ledger_dir)
    outcomes_path = Path(config.ledger_dir) / OUTCOMES_NAME
    log_path = Path(config.ledger_dir) / SETTLE_LOG_NAME
    fetch_cache: dict[tuple[str, str], list[DailyBar]] = {}

    def bars_for(symbol: str, start: date, hours: MarketHours) -> list[DailyBar]:
        key = (symbol, start.isoformat())
        if key not in fetch_cache:
            fetch_cache[key] = fetch(symbol, start, now.date() + timedelta(days=1))
        return usable_bars(fetch_cache[key], hours, now)

    settled = 0
    skipped = 0
    for record in records:
        if record.decision == "ERROR":
            skipped += 1
            continue
        prior = existing.get(record.run_id)
        prior_payload = _outcome_payload(prior) if prior is not None else None
        if prior_payload is not None and _fully_settled(record, prior_payload, order_rows):
            skipped += 1  # terminal record — nothing can change; skip the fetches
            continue
        hours = market_hours(record.ticker)
        # A few calendar days of slack before the decision date keeps the
        # anchor scan robust; d20 needs bars well past it, up to today.
        start = record.date - timedelta(days=7)
        bars = bars_for(record.ticker, start, hours)
        i0 = anchor_index(bars, record.decided_at, hours)
        returns = horizon_returns(bars, i0)
        # The benchmark's own bar dates are its calendar; its session hours are
        # the ticker's market's (every BENCHMARK_MAP entry is that market's
        # own index).
        bench_bars = bars_for(resolve_benchmark(record.ticker), start, hours)
        benchmark_returns = horizon_returns(bench_bars, anchor_index(bench_bars, record.decided_at, hours))
        replay = (
            replay_plan(record.plan, bars, record.decided_at, hours)
            if _wants_replay(record)
            else None
        )
        paper = compute_paper(record, order_rows)
        computed = {
            "returns": returns.model_dump(mode="json"),
            "benchmark_returns": benchmark_returns.model_dump(mode="json"),
            "plan_replay": replay.model_dump(mode="json") if replay is not None else None,
            "paper": paper.model_dump(mode="json") if paper is not None else None,
        }
        merged = _merge_payloads(computed, prior_payload)
        if merged == prior_payload or (prior_payload is None and not _has_content(merged)):
            skipped += 1  # nothing newly computable — idempotent re-run appends nothing
            continue
        outcome = OutcomeRecord.model_validate(
            {"run_id": record.run_id, "settled_at": to_utc_iso(now), **merged}
        )
        locked_append(
            outcomes_path, json.dumps(outcome.model_dump(mode="json"), ensure_ascii=False)
        )
        settled += 1
        append_log_line(
            log_path, to_utc_iso(now), session, record.run_id, "settled", _describe(merged)
        )

    return {
        "entrypoint": "settle",
        "date": slot_date.isoformat(),
        "session": session,
        "settled": settled,
        "skipped": skipped,
        "refresh_ok": refresh_ok,
        "refresh_error": refresh_error,
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
        prog="python -m pipeline.settle",
        description="Settle step (slot step 0): adapter refresh + outcome backfill "
        "(specs/orchestrator.md).",
    )
    parser.add_argument("--session", required=True, choices=sorted(SESSIONS))
    parser.add_argument(
        "--date",
        type=_parse_date,
        default=None,
        metavar="YYYY-MM-DD",
        help="slot date (default: today in the session timezone)",
    )
    return parser


def main(
    argv: list[str] | None = None,
    *,
    bars_fetcher: BarsFetcher | None = None,
    refresh_runner: RefreshRunner | None = None,
    clock: Clock = _utc_now,
) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.INFO, format="%(levelname)s %(name)s: %(message)s", stream=sys.stderr
    )
    try:
        config = load_config()
        summary = run_settle(
            config=config,
            session=args.session,
            slot_date=args.date,
            bars_fetcher=bars_fetcher,
            refresh_runner=refresh_runner,
            clock=clock,
        )
    except KeyboardInterrupt:
        print("settle: interrupted", file=sys.stderr)
        return 130
    except Exception as exc:
        logger.debug("settle failure detail", exc_info=True)
        print(f"settle: {_one_line(exc)}", file=sys.stderr)
        return 1
    # The final stdout line is the JSON summary the orchestrator parses.
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
