from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from devtools.verification.models import (
    ClaudeJudgment,
    Finding,
    ReviewReport,
    ReviewVerdict,
    Waiver,
    build_report,
    build_waiver_report,
    compute_verdict,
)

BASE = "a" * 40
HEAD = "b" * 40


def finding(severity: str) -> Finding:
    return Finding(
        severity=severity,
        file="pipeline/example.py",
        line=42,
        title="unsafe fallback",
        evidence="the error path returns success",
        suggested_test="assert a non-zero exit",
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    ("findings", "limitations", "expected"),
    [
        ([], [], ReviewVerdict.PASS),
        ([finding("low")], [], ReviewVerdict.WARN),
        ([finding("medium")], [], ReviewVerdict.WARN),
        ([finding("high")], [], ReviewVerdict.FAIL),
        ([finding("critical")], [], ReviewVerdict.FAIL),
        ([], ["live boundary not exercised"], ReviewVerdict.WARN),
    ],
)
def test_compute_verdict(findings, limitations, expected):
    assert compute_verdict(findings, limitations) is expected


@pytest.mark.unit
def test_build_report_stamps_identity_and_recomputes_verdict():
    judgment = ClaudeJudgment(tests_run=[], findings=[finding("high")], limitations=[])
    report = build_report(
        judgment,
        base_sha=BASE,
        head_sha=HEAD,
        reviewed_at=datetime(2026, 8, 10, tzinfo=timezone.utc),
    )
    assert report.schema_version == 1
    assert report.reviewer == "claude"
    assert report.verdict is ReviewVerdict.FAIL
    assert report.base_sha == BASE
    assert report.head_sha == HEAD


@pytest.mark.unit
def test_report_rejects_bad_sha_and_unknown_fields():
    judgment = ClaudeJudgment(tests_run=[], findings=[], limitations=[])
    with pytest.raises(ValidationError):
        build_report(
            judgment,
            base_sha="not-a-sha",
            head_sha=HEAD,
            reviewed_at=datetime.now(timezone.utc),
        )
    with pytest.raises(ValidationError):
        ClaudeJudgment(tests_run=[], findings=[], limitations=[], invented=True)


@pytest.mark.unit
def test_finding_line_must_be_positive_when_present():
    with pytest.raises(ValidationError):
        Finding(
            severity="low",
            file="x.py",
            line=0,
            title="bad line",
            evidence="line must be positive",
            suggested_test="construct the model",
        )


@pytest.mark.unit
def test_user_waiver_is_visible_and_never_a_pass():
    report = build_waiver_report(
        base_sha=BASE,
        head_sha=HEAD,
        waiver=Waiver(
            approved_by="user",
            approved_at="2026-08-10T12:00:00Z",
            reason="Claude quota unavailable",
            unverified_risk="scheduler behavior was not independently reviewed",
        ),
    )
    assert report.reviewer.value == "user-waiver"
    assert report.verdict is ReviewVerdict.WARN
    assert report.waiver is not None
    assert report.tests_run == [] and report.findings == []


@pytest.mark.unit
def test_claude_report_cannot_carry_a_waiver():
    with pytest.raises(ValidationError, match="waiver"):
        ReviewReport(
            schema_version=1,
            base_sha=BASE,
            head_sha=HEAD,
            reviewed_at="2026-08-10T12:00:00Z",
            reviewer="claude",
            verdict="warn",
            tests_run=[],
            findings=[],
            limitations=[],
            waiver={
                "approved_by": "user",
                "approved_at": "2026-08-10T12:00:00Z",
                "reason": "no",
                "unverified_risk": "unknown",
            },
        )
