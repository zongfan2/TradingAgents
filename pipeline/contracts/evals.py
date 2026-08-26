"""Evaluation report contract models (macro + ticker ``*.eval.json``).

Sources of truth: the evaluation sections of specs/macro-brief-data-contract.md
and specs/ticker-brief-data-contract.md, plus the evaluator's verdict rule
(specs/macro-brief-evaluator.md R4) implemented by :func:`compute_verdict`.
"""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import Field

from pipeline.contracts.base import ContractModel, UtcInstant
from pipeline.contracts.briefs import Session

Severity = Literal["minor", "major", "fabrication"]
Verdict = Literal["pass", "warn", "fail"]

#: Scores every eval carries, each a 0–10 float.
SCORE_DIMENSIONS = (
    "factual_accuracy",
    "citation_support",
    "coverage",
    "timeliness",
    "consistency",
)


class EvalScores(ContractModel):
    factual_accuracy: float = Field(ge=0.0, le=10.0)
    citation_support: float = Field(ge=0.0, le=10.0)
    coverage: float = Field(ge=0.0, le=10.0)
    timeliness: float = Field(ge=0.0, le=10.0)
    consistency: float = Field(ge=0.0, le=10.0)


class FlaggedClaim(ContractModel):
    section: str
    claim: str
    issue: str
    severity: Severity


class _EvalReportBase(ContractModel):
    as_of_date: date
    brief_sha256: str = Field(min_length=1)
    brief_generated_at: UtcInstant
    evaluator: str = Field(min_length=1)
    evaluated_at: UtcInstant
    scores: EvalScores
    flagged_claims: list[FlaggedClaim] = Field(default_factory=list)
    verdict: Verdict
    notes: str = ""


class MacroEvalReport(_EvalReportBase):
    """``YYYY-MM-DD.<session>.eval.json`` next to a macro brief."""

    session: Session


class TickerEvalReport(_EvalReportBase):
    """``<TICKER>/YYYY-MM-DD.eval.json`` next to a ticker brief."""

    ticker: str = Field(min_length=1)
    session: Session


def compute_verdict(scores: EvalScores, flagged_claims: list[FlaggedClaim]) -> Verdict:
    """Evaluator R4 verdict rule.

    ``fail``: any ``fabrication`` flag, or ``factual_accuracy`` < 5.
    ``warn``: any ``major`` flag, or any score < 6.
    Else ``pass``.
    """
    severities = {claim.severity for claim in flagged_claims}
    if "fabrication" in severities or scores.factual_accuracy < 5:
        return "fail"
    all_scores = (getattr(scores, dimension) for dimension in SCORE_DIMENSIONS)
    if "major" in severities or any(score < 6 for score in all_scores):
        return "warn"
    return "pass"
