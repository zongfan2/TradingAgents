"""Local macro-brief vendor: reads the daily deep-search brief from disk.

Briefs are produced offline by the collector (specs/macro-brief-collector.md)
and consumed here read-only. File layout and format are defined in
specs/macro-brief-data-contract.md; this module never writes to the brief
directory.
"""

import logging
import os
import re
from datetime import datetime

from .config import get_config
from .errors import VendorNotConfiguredError

logger = logging.getLogger(__name__)

_BRIEF_NAME = re.compile(r"^(\d{4}-\d{2}-\d{2})\.md$")

# A brief older than this many calendar days relative to the analysis date gets
# a WARNING header (weekends make a 2-3 day gap normal; beyond that it's stale).
STALE_AFTER_DAYS = 3


def get_macro_brief_local(curr_date: str) -> str:
    """Return the newest macro brief dated on or before ``curr_date``.

    Raises ``VendorNotConfiguredError`` when no qualifying brief exists — the
    ``macro_brief`` category is optional, so a missing brief degrades to the
    standard DATA_UNAVAILABLE sentinel instead of aborting the run. Callers
    that need loud failure (e.g. the A/B harness pre-flight) call this
    directly.
    """
    brief_dir = get_config()["macro_brief_dir"]
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
            f"No macro brief found in {brief_dir} dated on or before {curr_date}. "
            "Generate one with the collector (specs/macro-brief-collector.md) or "
            "the bootstrap one-liner documented there."
        )

    brief_dt, name = max(candidates)
    path = os.path.join(brief_dir, name)
    with open(path, encoding="utf-8") as f:
        content = f.read()

    age_days = (curr_dt - brief_dt).days
    header = (
        f"[Macro brief as of {brief_dt:%Y-%m-%d}, read for analysis date {curr_date}]\n\n"
    )
    if age_days > STALE_AFTER_DAYS:
        header = (
            f"[WARNING: this macro brief is {age_days} days older than the analysis "
            "date; treat time-sensitive claims with caution]\n" + header
        )
        logger.warning("Serving stale macro brief %s for analysis date %s", name, curr_date)

    return header + content
