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

import hashlib
import logging
import re
from collections.abc import Callable, Sequence
from datetime import date, datetime, timedelta

logger = logging.getLogger("pipeline.price_tape")

#: Sessions shown (before ``as_of``).
TAPE_SESSIONS = 10
#: A daily move at least this large in the last BIG_MOVE_LOOKBACK sessions
#: must be explained by the brief (or marked unverified).
BIG_MOVE_PCT = 5.0
BIG_MOVE_LOOKBACK = 3
def previous_weekday(as_of: date) -> date:
    """The last weekday before ``as_of``: the tape must reach it. Market data
    can lag for a ticker and its whole market together (no bar listed at
    all), which no comparison with the market's own data can see; a holiday
    then makes the tape unavailable too — a missed tape (about ten days a
    year per market), never a tape a session short."""
    day = as_of - timedelta(days=1)
    while day.weekday() >= 5:
        day -= timedelta(days=1)
    return day

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


def tape_rows(bars: Sequence[Bar], as_of: date, sessions: int = TAPE_SESSIONS) -> list[tuple[date, float, float | None]]:
    """``(date, close, % change vs previous session)`` for the last
    ``sessions`` sessions strictly before ``as_of``, oldest first. A session
    listed without a usable close is left out, and the next session's change
    is None: a move across the gap spans two sessions, not one. So is the
    change after a weekday with no bar at all (a suspension, or a holiday —
    the weekday calendar cannot tell them apart: one change figure lost)."""
    rows = []
    prev: tuple[date, float] | None = None
    for day, close in sorted(bar for bar in bars if bar[0] < as_of):
        if not (close == close and close > 0):  # NaN or non-positive
            prev = None
            continue
        contiguous = prev is not None and previous_weekday(day) == prev[0]
        rows.append((day, close, (close / prev[1] - 1) * 100 if contiguous else None))
        prev = (day, close)
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
    newest = rows[-1][0]
    expected = previous_weekday(as_of)
    if newest < expected:
        # The latest session is missing (late data, outage, a halt, or a
        # holiday): a tape without it would hide the most relevant move
        # behind "authoritative". yfinance lists a finished session with a NaN
        # close until its data is complete — that session is missing too.
        logger.warning("price tape for %s stale: last session %s, expected %s (before %s)",
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


_ROW_DATE = re.compile(r"^\| (\d{4}-\d{2}-\d{2}) \|", re.MULTILINE)


def newest_session(tape: str) -> date | None:
    """The newest session in a rendered tape (None for :data:`UNAVAILABLE`):
    the eval report records it, so a verdict reached with or without a tape
    can be told apart afterwards."""
    days = _ROW_DATE.findall(tape)
    return date.fromisoformat(days[-1]) if days else None


_ROW = re.compile(r"^\| \d{4}-\d{2}-\d{2} \|.*\|$", re.MULTILINE)


def tape_digest(tape: str) -> str | None:
    """sha256 of the tape's table rows only — the same for the writer's and
    the evaluator's block (their rules differ), so the collector's logged
    digest and the eval report's tell whether both saw the same closes.
    None for :data:`UNAVAILABLE`."""
    rows = _ROW.findall(tape)
    return hashlib.sha256("\n".join(rows).encode()).hexdigest() if rows else None
