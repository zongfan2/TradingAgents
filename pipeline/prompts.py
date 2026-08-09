"""Prompt-template rendering for the collectors (macro collector R1).

The macro template gains a session-specific research block: the ``cn`` render
runs pre-Asia-open and the ``us`` render pre-US-open, so the two renders of
the same date MUST differ (collector acceptance criterion 5).
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from pipeline.common import render_template

#: Repo-level prompts directory (templates live in the repo, not runtime data).
PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"

MACRO_PROMPT_TEMPLATE = PROMPTS_DIR / "macro_deep_search.md"

CN_SESSION_BLOCK = (
    "Session focus — cn (pre-Asia-open, 08:30 Asia/Shanghai): lead with a recap of "
    "the overnight US close (index moves, Treasury yields, USD, notable single-name "
    "movers) and what it sets up for today's Asia session; then check today's Asia "
    "calendars — China/Japan/Korea/India data releases, PBoC operations and fixings, "
    "HK/A-share corporate events — before writing."
)

US_SESSION_BLOCK = (
    "Session focus — us (pre-US-open, 08:30 America/New_York): recap the Asia session "
    "that just closed (A-shares/HK/Japan moves and their drivers) and what it carries "
    "into the US open; then check today's US pre-market calendar — key economic "
    "releases land at 08:30 ET, at or after this run, so list them as scheduled with "
    "consensus expectations instead of asserting outcomes."
)

SESSION_BLOCKS = {
    "cn": CN_SESSION_BLOCK,
    "us": US_SESSION_BLOCK,
}


def render_macro_prompt(
    as_of_date: date | str,
    session: str,
    generator: str,
    template_path: str | Path | None = None,
) -> str:
    """Render the macro deep-search prompt for one session slot.

    Substitutes ``{{DATE}}``, ``{{SESSION}}``, ``{{GENERATOR}}`` and the
    session-specific ``{{SESSION_BLOCK}}``; any placeholder left unresolved
    raises (never send a partial render to the backend).
    """
    if session not in SESSION_BLOCKS:
        raise ValueError(f"unknown session {session!r} — expected one of {sorted(SESSION_BLOCKS)}")
    path = Path(template_path) if template_path is not None else MACRO_PROMPT_TEMPLATE
    text = path.read_text(encoding="utf-8")
    return render_template(
        text,
        {
            "DATE": as_of_date.isoformat() if isinstance(as_of_date, date) else str(as_of_date),
            "SESSION": session,
            "GENERATOR": generator,
            "SESSION_BLOCK": SESSION_BLOCKS[session],
        },
    )
