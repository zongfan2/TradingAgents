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

Bar = tuple[date, float]  # (session date, close)
Fetch = Callable[[str], Sequence[Bar]]

#: How a brief cites a figure taken from the tape (no URL: the data itself).
TAPE_CITATION = "[price tape]"

UNAVAILABLE = (
    "Price tape unavailable (market data could not be read). Verify any close or "
    "daily move you state against a primary source."
)


def fetch_daily_closes(ticker: str) -> list[Bar]:
    """Daily closes for the last ~2 months via yfinance (the pool builder's
    source; ``auto_adjust=False`` so closes are the quoted ones)."""
    import yfinance as yf  # deferred: pipeline components stay import-light

    frame = yf.Ticker(ticker).history(period="2mo", interval="1d", auto_adjust=False)
    bars: list[Bar] = []
    for index, row in frame.iterrows():
        moment = index.to_pydatetime() if hasattr(index, "to_pydatetime") else index
        day = moment.date() if isinstance(moment, datetime) else moment
        bars.append((day, float(row["Close"])))
    return bars


def tape_rows(bars: Sequence[Bar], as_of: date, sessions: int = TAPE_SESSIONS) -> list[tuple[date, float, float | None]]:
    """``(date, close, % change vs previous session)`` for the last
    ``sessions`` sessions strictly before ``as_of``, oldest first."""
    clean = sorted((day, close) for day, close in bars if day < as_of and close == close and close > 0)
    rows = []
    for i, (day, close) in enumerate(clean):
        prev = clean[i - 1][1] if i else None
        rows.append((day, close, (close / prev - 1) * 100 if prev else None))
    return rows[-sessions:]


def render_tape(ticker: str, as_of: date, fetch: Fetch | None = None, *, for_writer: bool = True) -> str:
    """The tape block for a prompt: a table of closes and daily moves and the
    big recent moves called out. ``for_writer`` (the collector) adds the
    writer's rules; the evaluator (``for_writer=False``) gets the facts only
    and states its own rules. :data:`UNAVAILABLE` on any failure."""
    try:
        rows = tape_rows((fetch or fetch_daily_closes)(ticker), as_of)
    except Exception as exc:  # never blocks a brief or an evaluation
        logger.warning("price tape for %s unavailable: %s", ticker, type(exc).__name__)
        return UNAVAILABLE
    if len(rows) < 2:
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
