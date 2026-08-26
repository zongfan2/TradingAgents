"""Macro & ticker brief contract models and structural validators.

Sources of truth: specs/macro-brief-data-contract.md (v2) and
specs/ticker-brief-data-contract.md (v1). ``parse_macro_brief`` /
``parse_ticker_brief`` validate in two modes:

- default (collector mode): the validation the collector runs before writing
  (macro collector R3) — the contract's hard structural requirements *plus*
  the collector-only quality gates (citation floor, ``sources_count``
  verification, word-count hard bounds, optional ``expected_generator``).
- ``structural_only=True`` (evaluator mode): only the contract's hard
  structural requirements — what the evaluator refuses on (evaluator R1).
  Per the macro contract's carve-out, word count and the citation floor are
  collector quality gates, never grounds for evaluator refusal.

Failures raise :class:`ContractError` carrying the *complete* error list so
the collector can feed it back into the retry prompt.
"""

from __future__ import annotations

import logging
import re
from datetime import date
from pathlib import Path
from typing import Literal

import yaml
from pydantic import Field, ValidationError, field_validator

from pipeline.contracts.base import (
    ContractError,
    ContractModel,
    UtcInstant,
    validation_error_messages,
)

logger = logging.getLogger("pipeline.contracts.briefs")

# ---------------------------------------------------------------------------
# Contract constants
# ---------------------------------------------------------------------------

MACRO_SECTIONS = (
    "Monetary Policy & Rates",
    "Growth & Earnings",
    "Geopolitics & Trade",
    "Global Liquidity & FX",
    "Commodities & Supply Chains",
    "China & Asia",
    "Surprises & Watchlist",
)
#: Macro sections that must end with an ``**Impact**:`` line (1–6; the
#: Watchlist section is exempt).
MACRO_IMPACT_SECTIONS = MACRO_SECTIONS[:6]

TICKER_SECTIONS = (
    "Company Developments (48h)",
    "Catalysts & Calendar",
    "Supply Chain & Competitors",
    "Institutional Views & Positioning",
    "Risks",
)

CATALYST_TYPES = (
    "earnings",
    "product",
    "regulatory",
    "M&A",
    "guidance",
    "flow",
    "macro-exposure",
    "other",
)
CatalystType = Literal[
    "earnings", "product", "regulatory", "M&A", "guidance", "flow", "macro-exposure", "other"
]

Session = Literal["cn", "us"]

MACRO_CITATION_FLOOR = 8
TICKER_CITATION_FLOOR = 5
MACRO_WORD_BOUNDS = (500, 2500)
TICKER_WORD_BOUNDS = (250, 2000)
#: Soft quality targets (collector R3: warn — never fail — when the word count
#: is inside the hard bounds but outside the target).
MACRO_WORD_TARGET = (800, 1500)
TICKER_WORD_TARGET = (400, 1200)

_HEADING_RE = re.compile(r"^##\s+(.*?)\s*$", re.MULTILINE)
#: Hard requirement: direction word *plus* a one-line rationale after a dash
#: separator ('**Impact**: bearish — one-line rationale.').
_IMPACT_RE = re.compile(r"^\*\*Impact\*\*:\s*(bullish|bearish|neutral|mixed)\s*[—–-]\s*\S")
_CITATION_RE = re.compile(r"\[[^\]]*\]\(\s*(https?://[^)\s]+)\s*\)")
_FRONTMATTER_RE = re.compile(r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*\r?\n?(.*)\Z", re.DOTALL)

# ---------------------------------------------------------------------------
# Frontmatter models
# ---------------------------------------------------------------------------


class _BriefMetaBase(ContractModel):
    as_of_date: date
    session: Session
    generated_at: UtcInstant
    generator: str = Field(min_length=1)
    sources_count: int = Field(ge=0)


class MacroBriefMeta(_BriefMetaBase):
    """Frontmatter of a v2 macro brief (``YYYY-MM-DD.<session>.md``)."""


class LegacyMacroBriefMeta(MacroBriefMeta):
    """Frontmatter of a legacy v1 macro brief (``YYYY-MM-DD.md``).

    The macro contract keeps legacy files readable as session-agnostic
    candidates and exempts them from ``session`` (hard requirement 4).
    """

    session: Session | None = None  # type: ignore[assignment]


class TickerBriefMeta(_BriefMetaBase):
    """Frontmatter of a v1 ticker brief (``<TICKER>/YYYY-MM-DD.md``)."""

    ticker: str = Field(min_length=1)
    catalyst_score: float = Field(ge=0.0, le=10.0)
    catalyst_type: CatalystType
    catalyst_window: date | str

    @field_validator("catalyst_window")
    @classmethod
    def _catalyst_window_nonempty(cls, value: date | str) -> date | str:
        if isinstance(value, str) and not value.strip():
            raise ValueError("must be a date, a date range, or 'none'")
        return value


# ---------------------------------------------------------------------------
# Structural parsing / validation
# ---------------------------------------------------------------------------


def split_frontmatter(text: str) -> tuple[dict | None, str, list[str]]:
    """Split a brief into (frontmatter mapping, body, errors).

    Never raises: the caller accumulates the errors so a brief with broken
    frontmatter still gets its body checked (complete error list for the
    retry prompt).
    """
    if not text.startswith("---"):
        return None, text, ["missing YAML frontmatter (document must start with '---')"]
    match = _FRONTMATTER_RE.match(text)
    if not match:
        return None, text, ["unterminated YAML frontmatter (no closing '---' line)"]
    raw, body = match.group(1), match.group(2)
    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        return None, body, [f"frontmatter is not valid YAML: {exc}"]
    if not isinstance(data, dict):
        return None, body, ["frontmatter is not a YAML mapping"]
    return data, body, []


def count_body_citations(body: str) -> int:
    """Distinct cited URLs, counted from inline markdown links in the body.

    Consumers never trust the self-reported ``sources_count``.
    """
    return len(set(_CITATION_RE.findall(body)))


def _section_spans(body: str) -> list[tuple[str, int, int]]:
    """``(title, content_start, content_end)`` for each ``##`` heading."""
    matches = list(_HEADING_RE.finditer(body))
    spans = []
    for i, match in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(body)
        spans.append((match.group(1), match.end(), end))
    return spans


def _check_sections(body: str, expected: tuple[str, ...], errors: list[str]) -> dict[str, str]:
    """Validate exact section titles and order; return {title: content}."""
    spans = _section_spans(body)
    found = [title for title, _, _ in spans]
    if found != list(expected):
        missing = [t for t in expected if t not in found]
        unexpected = [t for t in found if t not in expected]
        errors.extend(f"missing section '## {t}'" for t in missing)
        errors.extend(f"unexpected section '## {t}'" for t in unexpected)
        duplicated = sorted({t for t in found if found.count(t) > 1})
        errors.extend(f"duplicated section '## {t}'" for t in duplicated)
        if not missing and not unexpected and not duplicated:
            errors.append(
                "sections out of order — required order: "
                + ", ".join(f"'## {t}'" for t in expected)
            )
    return {title: body[start:end] for title, start, end in spans}


def _last_nonblank_line(text: str) -> str:
    for line in reversed(text.splitlines()):
        if line.strip():
            return line.strip()
    return ""


def _check_word_count(
    body: str,
    bounds: tuple[int, int],
    errors: list[str],
    target: tuple[int, int] | None = None,
) -> None:
    lo, hi = bounds
    words = len(body.split())
    if not lo <= words <= hi:
        errors.append(f"word count {words} outside hard bounds [{lo}, {hi}]")
    elif target is not None and not target[0] <= words <= target[1]:
        # Collector R3: soft quality target — warn, never fail.
        logger.warning(
            "word count %d inside hard bounds but outside the %d-%d target (soft quality gate)",
            words,
            target[0],
            target[1],
        )


def _check_generator(
    meta: _BriefMetaBase | None, expected_generator: str | None, errors: list[str]
) -> None:
    """Collector R3: ``generator`` must match the invoked backend (opt-in)."""
    if (
        expected_generator is not None
        and meta is not None
        and meta.generator != expected_generator
    ):
        errors.append(
            f"frontmatter generator '{meta.generator}' does not match the invoked "
            f"backend '{expected_generator}'"
        )


def _check_citations(
    body: str, floor: int, meta: _BriefMetaBase | None, errors: list[str]
) -> None:
    distinct = count_body_citations(body)
    if distinct < floor:
        errors.append(
            f"only {distinct} distinct citation URLs in the body — at least {floor} required"
        )
    if meta is not None and meta.sources_count != distinct:
        errors.append(
            f"frontmatter sources_count={meta.sources_count} does not equal the "
            f"{distinct} distinct URLs counted from the body"
        )


def _validate_meta(
    data: dict | None, model: type[_BriefMetaBase], errors: list[str]
) -> _BriefMetaBase | None:
    if data is None:
        return None
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        errors.extend(validation_error_messages(exc, prefix="frontmatter: "))
        return None


def parse_macro_brief(
    text: str,
    *,
    structural_only: bool = False,
    expected_generator: str | None = None,
    legacy: bool = False,
) -> tuple[MacroBriefMeta, str]:
    """Validate a macro brief; returns (meta, body).

    Structural checks (macro contract hard requirements 1–4 — the evaluator's
    refusal set): frontmatter parses with all fields; the 7 exact section
    titles in order; ``**Impact**:`` lines (direction + rationale) ending
    sections 1–6. With ``structural_only=False`` (collector R3) the
    collector-only quality gates also apply: ≥ 8 distinct citation URLs
    counted from the body, ``sources_count`` equal to that count, word count
    within 500–2500 (warn outside the 800–1500 target), and — when
    ``expected_generator`` is given — ``generator`` matching the invoked
    backend. ``legacy=True`` parses a v1 file (``YYYY-MM-DD.md``), which the
    contract exempts from ``session``. Raises :class:`ContractError` with the
    complete error list on any failure.
    """
    errors: list[str] = []
    data, body, fm_errors = split_frontmatter(text)
    errors.extend(fm_errors)
    model = LegacyMacroBriefMeta if legacy else MacroBriefMeta
    meta = _validate_meta(data, model, errors)

    sections = _check_sections(body, MACRO_SECTIONS, errors)
    for title in MACRO_IMPACT_SECTIONS:
        content = sections.get(title)
        if content is not None and not _IMPACT_RE.match(_last_nonblank_line(content)):
            errors.append(
                f"section '## {title}' must end with an "
                "'**Impact**: bullish|bearish|neutral|mixed — <rationale>' line"
            )
    if not structural_only:
        # Collector quality gates — per the macro contract's carve-out these
        # (citation floor, sources_count verification, word bounds) are NOT
        # part of structural validity, so the evaluator never refuses on them.
        _check_citations(body, MACRO_CITATION_FLOOR, meta, errors)
        _check_word_count(body, MACRO_WORD_BOUNDS, errors, target=MACRO_WORD_TARGET)
        _check_generator(meta, expected_generator, errors)

    if errors or meta is None:
        raise ContractError(errors)
    return meta, body


def parse_ticker_brief(
    text: str,
    *,
    structural_only: bool = False,
    expected_generator: str | None = None,
) -> tuple[TickerBriefMeta, str]:
    """Validate a v1 ticker brief; returns (meta, body).

    Structural checks (ticker contract hard requirements 1–3): frontmatter
    parses with all fields (incl. catalyst fields); the 5 exact section
    titles in order followed by a final ``**Impact**:`` line (direction +
    rationale); ≥ 5 distinct citation URLs counted from the body with
    ``sources_count`` equal to that count (part of hard requirement 2, unlike
    the macro contract); frontmatter ``session`` consistent with the ticker's
    suffix (session uniqueness invariant). With ``structural_only=False``
    (collector mode) the word-count quality gate also applies: within 250–2000
    (warn outside the 400–1200 target), plus the optional
    ``expected_generator`` match.
    """
    from pipeline.common import session_for_ticker

    errors: list[str] = []
    data, body, fm_errors = split_frontmatter(text)
    errors.extend(fm_errors)
    meta = _validate_meta(data, TickerBriefMeta, errors)

    _check_sections(body, TICKER_SECTIONS, errors)
    if not _IMPACT_RE.match(_last_nonblank_line(body)):
        errors.append(
            "brief must end with a final "
            "'**Impact**: bullish|bearish|neutral|mixed — <rationale>' line"
        )
    _check_citations(body, TICKER_CITATION_FLOOR, meta, errors)
    if not structural_only:
        _check_word_count(body, TICKER_WORD_BOUNDS, errors, target=TICKER_WORD_TARGET)
        _check_generator(meta, expected_generator, errors)

    if meta is not None and meta.session != session_for_ticker(meta.ticker):
        errors.append(
            f"session '{meta.session}' does not match ticker '{meta.ticker}' "
            f"(suffix derives session '{session_for_ticker(meta.ticker)}')"
        )

    if errors or meta is None:
        raise ContractError(errors)
    return meta, body


# ---------------------------------------------------------------------------
# Filename cross-checks (separate helpers — they need the path, not the text)
# ---------------------------------------------------------------------------


def validate_macro_brief_path(path: str | Path, meta: MacroBriefMeta) -> None:
    """``as_of_date`` and ``session`` must match the ``YYYY-MM-DD.<session>.md``
    name (legacy v1 metas — ``session`` ``None`` — match ``YYYY-MM-DD.md``)."""
    path = Path(path)
    if meta.session is None:
        expected = f"{meta.as_of_date.isoformat()}.md"
    else:
        expected = f"{meta.as_of_date.isoformat()}.{meta.session}.md"
    if path.name != expected:
        raise ContractError(
            [
                f"filename '{path.name}' does not match frontmatter "
                f"(as_of_date={meta.as_of_date.isoformat()}, session={meta.session} "
                f"requires '{expected}')"
            ]
        )


def validate_ticker_brief_path(path: str | Path, meta: TickerBriefMeta) -> None:
    """``as_of_date`` must match the ``YYYY-MM-DD.md`` name; ``ticker`` must
    match the parent directory name (verbatim pipeline symbol)."""
    path = Path(path)
    errors: list[str] = []
    expected = f"{meta.as_of_date.isoformat()}.md"
    if path.name != expected:
        errors.append(
            f"filename '{path.name}' does not match frontmatter "
            f"as_of_date={meta.as_of_date.isoformat()} (requires '{expected}')"
        )
    if path.parent.name != meta.ticker:
        errors.append(
            f"directory '{path.parent.name}' does not match frontmatter ticker '{meta.ticker}'"
        )
    if errors:
        raise ContractError(errors)
