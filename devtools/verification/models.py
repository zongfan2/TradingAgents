"""Strict models for persisted code-review reports."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

SHA_PATTERN = r"^[0-9a-f]{40,64}$"


class StrictModel(BaseModel):
    """Base model that rejects fields outside the persisted contract."""

    model_config = ConfigDict(extra="forbid")


class Severity(str, Enum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class ReviewVerdict(str, Enum):
    PASS = "pass"
    WARN = "warn"
    FAIL = "fail"


class Reviewer(str, Enum):
    CLAUDE = "claude"
    USER_WAIVER = "user-waiver"


class TestStatus(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    NOT_RUN = "not_run"


class TestRun(StrictModel):
    command: str = Field(min_length=1)
    status: TestStatus
    summary: str = Field(min_length=1)


class Finding(StrictModel):
    severity: Severity
    file: str | None = None
    line: int | None = Field(default=None, ge=1)
    title: str = Field(min_length=1)
    evidence: str = Field(min_length=1)
    suggested_test: str = Field(min_length=1)


class ClaudeJudgment(StrictModel):
    tests_run: list[TestRun]
    findings: list[Finding]
    limitations: list[str]


class Waiver(StrictModel):
    approved_by: Literal["user"]
    approved_at: datetime
    reason: str = Field(min_length=1)
    unverified_risk: str = Field(min_length=1)


class ReviewReport(StrictModel):
    schema_version: Literal[1] = 1
    base_sha: str = Field(pattern=SHA_PATTERN)
    head_sha: str = Field(pattern=SHA_PATTERN)
    reviewed_at: datetime
    reviewer: Reviewer = Reviewer.CLAUDE
    verdict: ReviewVerdict
    tests_run: list[TestRun]
    findings: list[Finding]
    limitations: list[str]
    waiver: Waiver | None = None

    @model_validator(mode="after")
    def validate_reviewer_shape(self) -> ReviewReport:
        if self.reviewer is Reviewer.CLAUDE and self.waiver is not None:
            raise ValueError("claude report cannot carry a waiver")
        if self.reviewer is Reviewer.USER_WAIVER:
            if self.waiver is None or self.verdict is not ReviewVerdict.WARN:
                raise ValueError("user-waiver report requires waiver and warn verdict")
            if self.tests_run or self.findings:
                raise ValueError("user-waiver report cannot fabricate tests or findings")
        return self


def compute_verdict(
    findings: Sequence[Finding], limitations: Sequence[str]
) -> ReviewVerdict:
    severities = {item.severity for item in findings}
    if severities & {Severity.CRITICAL, Severity.HIGH}:
        return ReviewVerdict.FAIL
    if severities or limitations:
        return ReviewVerdict.WARN
    return ReviewVerdict.PASS


def build_report(
    judgment: ClaudeJudgment,
    *,
    base_sha: str,
    head_sha: str,
    reviewed_at: datetime,
) -> ReviewReport:
    return ReviewReport(
        base_sha=base_sha,
        head_sha=head_sha,
        reviewed_at=reviewed_at,
        verdict=compute_verdict(judgment.findings, judgment.limitations),
        tests_run=judgment.tests_run,
        findings=judgment.findings,
        limitations=judgment.limitations,
    )


def build_waiver_report(
    *, base_sha: str, head_sha: str, waiver: Waiver
) -> ReviewReport:
    return ReviewReport(
        base_sha=base_sha,
        head_sha=head_sha,
        reviewed_at=waiver.approved_at,
        reviewer=Reviewer.USER_WAIVER,
        verdict=ReviewVerdict.WARN,
        tests_run=[],
        findings=[],
        limitations=[f"Claude review waived: {waiver.unverified_risk}"],
        waiver=waiver,
    )
