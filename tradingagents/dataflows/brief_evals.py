"""Reader for brief evaluation verdicts (specs/pipeline-consumption-v2.md §5).

The evaluator writes a sibling ``<brief>.eval.json`` next to each brief
(macro: ``YYYY-MM-DD.<session>.eval.json``, ticker: ``YYYY-MM-DD.eval.json``).
This util surfaces its verdict to the runner (``get_eval_verdict``, gating)
and to the report header (``report_header_verdicts``, rendered by the report
saving path in ``tradingagents.reporting``).

Revision binding is normative (both brief contracts): an eval is valid only
for the exact brief revision whose content hash equals ``brief_sha256``.
A mismatch — e.g. a re-collected brief silently inheriting its predecessor's
``pass`` — is reported as ``missing``, as is any absent or corrupt file.
The pipeline itself never blocks on eval; gating policy lives in the runner.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from collections.abc import Callable

from .config import get_config
from .errors import VendorNotConfiguredError
from .macro_brief import resolve_macro_brief_path
from .symbol_utils import session_for_ticker
from .ticker_brief import resolve_ticker_brief_path

logger = logging.getLogger(__name__)

_VERDICTS = ("pass", "warn", "fail")


def _eval_path_for(brief_path: str) -> str:
    """Sibling eval path: ``…/X.md`` → ``…/X.eval.json``."""
    root, ext = os.path.splitext(brief_path)
    if ext.lower() != ".md":
        root = brief_path
    return root + ".eval.json"


def get_eval_verdict(brief_path: str) -> str:
    """Return the eval verdict for the brief at ``brief_path``.

    ``'pass'`` / ``'warn'`` / ``'fail'`` when a valid, revision-bound eval
    exists; ``'missing'`` otherwise — brief or eval file absent/unreadable,
    eval JSON corrupt, verdict outside the contract vocabulary, or
    ``brief_sha256`` not matching the sha256 of the brief content being
    served. Never raises: eval surfacing must not break a run.
    """
    try:
        with open(brief_path, "rb") as f:
            brief_sha256 = hashlib.sha256(f.read()).hexdigest()
    except OSError:
        return "missing"

    eval_path = _eval_path_for(brief_path)
    try:
        with open(eval_path, encoding="utf-8") as f:
            evaluation = json.load(f)
    except (OSError, ValueError):
        return "missing"
    if not isinstance(evaluation, dict):
        return "missing"

    if evaluation.get("brief_sha256") != brief_sha256:
        logger.warning(
            "Eval %s does not match the brief revision at %s (hash mismatch); "
            "treating verdict as missing (revision binding).",
            eval_path, brief_path,
        )
        return "missing"

    verdict = evaluation.get("verdict")
    if isinstance(verdict, str) and verdict.strip().lower() in _VERDICTS:
        return verdict.strip().lower()
    return "missing"


def _verdict_line(resolve_path: Callable[[], str]) -> str:
    try:
        brief_path = resolve_path()
    except VendorNotConfiguredError:
        # The reader would have degraded to DATA_UNAVAILABLE: nothing was
        # served, so there is no revision to bind an eval to.
        return "missing (no brief served)"
    return f"{get_eval_verdict(brief_path)} ({os.path.basename(brief_path)})"


def report_header_verdicts(ticker: str, curr_date: str | None) -> list[str]:
    """Eval-verdict lines for the report header (spec §5 surfacing).

    One ``<Macro|Ticker> brief eval: <verdict>`` line per news arm configured
    to ``brief``; ``feeds`` arms consumed no brief and contribute nothing, so
    the default configuration's reports are byte-identical to before. Each
    verdict is looked up against the exact file the reader's selection serves
    (the ``resolve_*_brief_path`` helpers share the readers' selection core),
    preserving the contracts' revision binding. Never raises — report saving
    must not break on eval surfacing.
    """
    if not curr_date:
        return []
    lines: list[str] = []
    try:
        config = get_config()
        if str(config.get("macro_source") or "feeds").strip().lower() == "brief":
            session = session_for_ticker(ticker)
            lines.append(
                "Macro brief eval: "
                + _verdict_line(lambda: resolve_macro_brief_path(curr_date, session))
            )
        if str(config.get("ticker_source") or "feeds").strip().lower() == "brief":
            lines.append(
                "Ticker brief eval: "
                + _verdict_line(lambda: resolve_ticker_brief_path(ticker, curr_date))
            )
    except Exception:
        logger.exception(
            "Eval-verdict surfacing failed; omitting verdict lines from the report header"
        )
        return []
    return lines
