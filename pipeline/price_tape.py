"""Verified price tape for ticker briefs and their evaluation (specs/price-tape.md).

The 2026-11-02/03 briefs got their own recent price action wrong (INTC's
Nov 2 −8.8% read as a "bounce", SNDK's post-earnings −13.5% day missing,
MU's late-October rebound described as a "steady decline"): web search is
an unreliable source for closes. The collector and the evaluator now get
the last sessions' closes from market data — the same yfinance source the
pool builder's gate uses — strictly *before* the brief's ``as_of`` date, so
no partial day and no look-ahead.

Never raises: an unavailable tape renders as such, and the prompts then
fall back to today's behaviour.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from datetime import date, datetime

logger = logging.getLogger("pipeline.price_tape")

#: Sessions shown (before ``as_of``).
TAPE_SESSIONS = 10
#: A daily move at least this large in the last BIG_MOVE_LOOKBACK sessions
#: must be explained by the brief (or marked unverified).
BIG_MOVE_PCT = 5.0
BIG_MOVE_LOOKBACK = 3
#: Without the benchmark's latest session: the newest tape session may be at
#: most this many calendar days before ``as_of`` (a weekend plus a holiday).
MAX_STALE_DAYS = 4

Bar = tuple[date, float]  # (session date, close)
Fetch = Callable[[str], Sequence[Bar]]

#: How a brief cites a figure taken from the tape (no URL: the data itself).
TAPE_CITATION = "[price tape]"

UNAVAILABLE = (
    "Price tape unavailable (market data could not be read). Verify any close or "
    "daily move you state against a primary source and cite its URL."
)


def fetch_daily_closes(ticker: str) -> list[Bar]:
    """Daily closes for the last ~2 months via yfinance (the pool builder's
    source). ``auto_adjust=False`` leaves dividends out, but yfinance still
    split-adjusts every Close before a split: a split on or after a tape
    session — inside the window, or between a past ``as_of`` and today —
    would rescale closes away from the prices sources quote, so any split in
    the fetched history raises (the tape is then unavailable; a split before
    the window costs one tape, a rare and safe miss)."""
    import yfinance as yf  # deferred: pipeline components stay import-light

    return closes_from_history(ticker, yf.Ticker(ticker).history(period="2mo", interval="1d", auto_adjust=False))


def closes_from_history(ticker: str, frame) -> list[Bar]:
    """``(date, close)`` bars from a yfinance history frame; raises on any
    split in it (see :func:`fetch_daily_closes`)."""
    if "Stock Splits" in frame and (frame["Stock Splits"].fillna(0) != 0).any():
        raise ValueError(f"{ticker}: stock split in the fetched history (split-adjusted closes)")
    bars: list[Bar] = []
    for index, row in frame.iterrows():
        moment = index.to_pydatetime() if hasattr(index, "to_pydatetime") else index
        day = moment.date() if isinstance(moment, datetime) else moment
        bars.append((day, float(row["Close"])))
    return bars


#: Benchmarks whose newest bar is the market's latest session. A ticker
#: without a suffix is US (SPY); any other suffix has no benchmark here, so
#: the calendar-day rule applies rather than another exchange's calendar.
_BENCHMARKS = ((".SS", "000001.SS"), (".SZ", "000001.SS"), (".BJ", "000001.SS"), (".HK", "^HSI"))
_SESSION_CACHE: dict[tuple[str, date], date] = {}


def benchmark_for(ticker: str) -> str | None:
    upper = ticker.upper()
    if "." not in upper:
        return "SPY"
    return next((bench for suffix, bench in _BENCHMARKS if upper.endswith(suffix)), None)


def latest_market_session(ticker: str, as_of: date, fetch: Fetch | None = None) -> date | None:
    """The market's latest session before ``as_of``: the newest bar of the
    ticker's market benchmark, with or without a close — yfinance lists a
    finished session with a NaN close until its data is complete, and that
    session still exists. Only a session read is cached (per process): a
    failed read is retried by the next ticker. None when it cannot be read —
    the caller then falls back to the calendar-day rule."""
    bench = benchmark_for(ticker)
    if bench is None or bench.upper() == ticker.upper():
        return None
    key = (bench, as_of)
    if key not in _SESSION_CACHE:
        try:
            days = [day for day, _close in (fetch or fetch_daily_closes)(bench) if day < as_of]
        except Exception as exc:
            logger.warning("benchmark %s unavailable for the staleness check: %s", bench, type(exc).__name__)
            return None
        if not days:
            return None
        _SESSION_CACHE[key] = max(days)
    return _SESSION_CACHE[key]


def tape_rows(bars: Sequence[Bar], as_of: date, sessions: int = TAPE_SESSIONS) -> list[tuple[date, float, float | None]]:
    """``(date, close, % change vs previous session)`` for the last
    ``sessions`` sessions strictly before ``as_of``, oldest first. A session
    listed without a usable close is left out, and the next session's change
    is None: a move across the gap spans two sessions, not one."""
    rows = []
    prev = None
    for day, close in sorted(bar for bar in bars if bar[0] < as_of):
        if not (close == close and close > 0):  # NaN or non-positive
            prev = None
            continue
        rows.append((day, close, (close / prev - 1) * 100 if prev else None))
        prev = close
    return rows[-sessions:]


def render_tape(ticker: str, as_of: date, fetch: Fetch | None = None, *, for_writer: bool = True) -> str:
    """The tape block for a prompt: a table of closes and daily moves and the
    big recent moves called out. ``for_writer`` (the collector) adds the
    writer's rules; the evaluator (``for_writer=False``) gets the facts only
    and states its own rules. :data:`UNAVAILABLE` on any failure."""
    try:
        bars = (fetch or fetch_daily_closes)(ticker)
        rows = tape_rows(bars, as_of)
    except Exception as exc:  # never blocks a brief or an evaluation
        logger.warning("price tape for %s unavailable: %s", ticker, type(exc).__name__)
        return UNAVAILABLE
    if len(rows) < 2:
        return UNAVAILABLE
    # A session listed without a usable close (incomplete data) still happened.
    listed = max(day for day, _close in bars if day < as_of)
    market = latest_market_session(ticker, as_of, fetch)
    expected = max(listed, market) if market else listed
    newest = rows[-1][0]
    if newest < expected or (market is None and (as_of - newest).days > MAX_STALE_DAYS):
        # The latest session is missing (late data, outage, a halt): a tape
        # without it would hide the most relevant move behind "authoritative".
        logger.warning("price tape for %s stale: last session %s, market's %s (before %s)",
                       ticker, newest, expected, as_of)
        return UNAVAILABLE
    lines = [f"Daily closes for {ticker} before {as_of.isoformat()} (market data, authoritative):", "",
             "| session | close | change |", "|---|---|---|"]
    big = []
    for i, (day, close, pct) in enumerate(rows):
        change = "—" if pct is None else f"{pct:+.2f}%"
        recent = i >= len(rows) - BIG_MOVE_LOOKBACK
        mark = " ⚠" if pct is not None and abs(pct) >= BIG_MOVE_PCT and recent else ""
        if mark:
            big.append(f"{day.isoformat()} ({change})")
        lines.append(f"| {day.isoformat()} | {close:,.2f} | {change}{mark} |")
    lines.append("")
    if big:
        lines.append(f"⚠ Large recent moves: {', '.join(big)}.")
    if for_writer:
        lines.append(
            "These closes and moves are verified: never contradict them, and do not spend "
            "searches on past closes or daily % moves. Cite a close or move taken from this "
            f"tape as {TAPE_CITATION} — a valid citation without a URL."
        )
        if big:
            lines.append(
                "Spend your FIRST searches on the ⚠ sessions: one targeted search per ⚠ session "
                f"for its driver (e.g. \"{ticker} stock <date> why\", or the sector/peer news of "
                "that day). Explain each in Company Developments (48h) with a cited driver; write "
                "'driver unverified' only if that search found nothing — never describe a move in "
                "the opposite direction, never omit one."
            )
    return "\n".join(lines)
