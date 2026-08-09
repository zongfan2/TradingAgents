"""Local ticker-brief vendor: reads the per-ticker deep-search brief from disk.

Briefs are produced offline by the ticker collector and consumed here
read-only. File layout, format, and staleness rules are defined in
specs/ticker-brief-data-contract.md; this module never writes to the brief
directory.

Staleness differs from the macro brief on purpose: ticker news decays faster,
so a 1-calendar-day gap warns and a gap of 2 or more days is treated as absent
(``VendorNotConfiguredError`` → standard DATA_UNAVAILABLE degrade) — with a
fresh weekday collection, a 2-day-old company brief is a failure signal, not a
weekend artifact.
"""

import logging
import os
import re
from datetime import datetime

from .config import get_config
from .errors import VendorNotConfiguredError
from .utils import safe_ticker_component

logger = logging.getLogger(__name__)

_BRIEF_NAME = re.compile(r"^(\d{4}-\d{2}-\d{2})\.md$")

# Gap thresholds in calendar days between the brief date and the analysis date
# (specs/ticker-brief-data-contract.md): 0 serves as-is, exactly WARN_GAP_DAYS
# warns, ABSENT_AFTER_DAYS or more is treated as absent.
WARN_GAP_DAYS = 1
ABSENT_AFTER_DAYS = 2


def _select_brief(ticker: str, curr_date: str):
    """Selection core shared by the reader and the path resolver.

    Returns ``(symbol, brief_dir, brief_dt, name, gap_days)`` for the winning
    candidate, applying the contract's absent rule (a gap of
    ``ABSENT_AFTER_DAYS`` or more raises); raises
    ``VendorNotConfiguredError`` when nothing qualifies.
    """
    # The ticker arrives from an LLM tool call; validate before it touches a
    # filesystem path (same guard as the cache/results paths). Uppercased to
    # match the collector's directory convention (`NVDA`, `0700.HK`).
    symbol = safe_ticker_component(str(ticker).strip().upper())
    brief_dir = os.path.join(get_config()["ticker_brief_dir"], symbol)
    curr_dt = datetime.strptime(curr_date, "%Y-%m-%d")

    candidates = []
    if os.path.isdir(brief_dir):
        for name in os.listdir(brief_dir):
            match = _BRIEF_NAME.match(name)
            if not match:
                continue
            try:
                brief_dt = datetime.strptime(match.group(1), "%Y-%m-%d")
            except ValueError:
                continue
            if brief_dt <= curr_dt:
                candidates.append((brief_dt, name))

    if not candidates:
        raise VendorNotConfiguredError(
            f"No ticker brief found for {symbol} in {brief_dir} dated on or "
            f"before {curr_date}. Generate one with the ticker-brief collector "
            "(specs/ticker-brief-collector.md)."
        )

    brief_dt, name = max(candidates)
    gap_days = (curr_dt - brief_dt).days
    if gap_days >= ABSENT_AFTER_DAYS:
        raise VendorNotConfiguredError(
            f"Newest ticker brief for {symbol} is {name}, {gap_days} days before "
            f"analysis date {curr_date}; briefs {ABSENT_AFTER_DAYS}+ days old are "
            "treated as absent (specs/ticker-brief-data-contract.md). Re-run the "
            "ticker-brief collector to refresh it."
        )

    return symbol, brief_dir, brief_dt, name, gap_days


def get_ticker_brief_local(ticker: str, curr_date: str) -> str:
    """Return the newest ticker brief for ``ticker`` dated on or before
    ``curr_date``, applying the contract's staleness rules.

    Raises ``VendorNotConfiguredError`` when no brief exists within the
    freshness window — the ``ticker_brief`` category is optional, so this
    degrades to the standard DATA_UNAVAILABLE sentinel instead of aborting
    the run.
    """
    symbol, brief_dir, brief_dt, name, gap_days = _select_brief(ticker, curr_date)
    path = os.path.join(brief_dir, name)
    with open(path, encoding="utf-8") as f:
        content = f.read()

    header = (
        f"[Ticker brief for {symbol} as of {brief_dt:%Y-%m-%d}, "
        f"read for analysis date {curr_date}]\n\n"
    )
    if gap_days >= WARN_GAP_DAYS:
        header = (
            f"[WARNING: this ticker brief is {gap_days} day(s) older than the "
            f"analysis date; developments since {brief_dt:%Y-%m-%d} are not "
            "covered — treat time-sensitive claims with caution]\n" + header
        )
        logger.warning(
            "Serving %s-day-old ticker brief %s/%s for analysis date %s",
            gap_days, symbol, name, curr_date,
        )

    return header + content


def resolve_ticker_brief_path(ticker: str, curr_date: str) -> str:
    """Filesystem path of the exact brief ``get_ticker_brief_local`` would serve.

    Shares the reader's selection core — including the ≥ 2-day absent rule —
    so callers that need the served file itself (runner gating, the report
    header's eval-verdict lookup) bind to the same revision the reader serves
    and never see a path the reader would refuse (revision binding,
    specs/ticker-brief-data-contract.md). Raises ``VendorNotConfiguredError``
    when no brief exists within the freshness window.
    """
    _symbol, brief_dir, _brief_dt, name, _gap_days = _select_brief(ticker, curr_date)
    return os.path.join(brief_dir, name)
