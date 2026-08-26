"""Eval verdict surfacing (specs/pipeline-consumption-v2.md change 5).

The reader exposes a brief's eval verdict for runner gating and the report
header. Revision binding is normative: an eval whose ``brief_sha256`` does not
hash-match the brief being served reports ``missing`` — a re-collected brief
must never inherit its predecessor's ``pass``.
"""
import hashlib
import json

import pytest

from tradingagents.dataflows.brief_evals import get_eval_verdict, report_header_verdicts
from tradingagents.dataflows.config import set_config


def _write_pair(tmp_path, name, body, verdict="pass", sha=None, **extra):
    brief = tmp_path / f"{name}.md"
    brief.write_text(body, encoding="utf-8")
    payload = {
        "brief_sha256": sha if sha is not None else hashlib.sha256(
            body.encode("utf-8")
        ).hexdigest(),
        "verdict": verdict,
        **extra,
    }
    (tmp_path / f"{name}.eval.json").write_text(json.dumps(payload), encoding="utf-8")
    return brief


@pytest.mark.unit
@pytest.mark.parametrize("verdict", ["pass", "warn", "fail"])
def test_valid_eval_returns_verdict(tmp_path, verdict):
    brief = _write_pair(tmp_path, "2026-08-03.us", "macro body", verdict=verdict)
    assert get_eval_verdict(str(brief)) == verdict


@pytest.mark.unit
def test_ticker_style_sibling_name(tmp_path):
    # Ticker briefs have no session component: 2026-08-03.md -> 2026-08-03.eval.json.
    brief = _write_pair(tmp_path, "2026-08-03", "ticker body", verdict="warn")
    assert get_eval_verdict(str(brief)) == "warn"


@pytest.mark.unit
def test_hash_mismatch_is_missing(tmp_path):
    # A re-collected brief must not inherit the old revision's verdict.
    brief = _write_pair(
        tmp_path, "2026-08-03.us", "recollected body",
        sha=hashlib.sha256(b"previous revision").hexdigest(),
    )
    assert get_eval_verdict(str(brief)) == "missing"


@pytest.mark.unit
def test_absent_eval_is_missing(tmp_path):
    brief = tmp_path / "2026-08-03.us.md"
    brief.write_text("macro body", encoding="utf-8")
    assert get_eval_verdict(str(brief)) == "missing"


@pytest.mark.unit
def test_absent_brief_is_missing(tmp_path):
    assert get_eval_verdict(str(tmp_path / "2026-08-03.us.md")) == "missing"


@pytest.mark.unit
def test_corrupt_eval_json_is_missing(tmp_path):
    brief = tmp_path / "2026-08-03.us.md"
    brief.write_text("macro body", encoding="utf-8")
    (tmp_path / "2026-08-03.us.eval.json").write_text("{not json", encoding="utf-8")
    assert get_eval_verdict(str(brief)) == "missing"


@pytest.mark.unit
def test_non_object_eval_json_is_missing(tmp_path):
    brief = tmp_path / "2026-08-03.us.md"
    brief.write_text("macro body", encoding="utf-8")
    (tmp_path / "2026-08-03.us.eval.json").write_text('["pass"]', encoding="utf-8")
    assert get_eval_verdict(str(brief)) == "missing"


@pytest.mark.unit
def test_unknown_verdict_is_missing(tmp_path):
    brief = _write_pair(tmp_path, "2026-08-03.us", "macro body", verdict="excellent")
    assert get_eval_verdict(str(brief)) == "missing"


@pytest.mark.unit
def test_verdict_is_normalized(tmp_path):
    brief = _write_pair(tmp_path, "2026-08-03.us", "macro body", verdict=" PASS ")
    assert get_eval_verdict(str(brief)) == "pass"


# ---------------------------------------------------------------------------
# Report-header surfacing (rendered by tradingagents.reporting, spec §5)
# ---------------------------------------------------------------------------


def _configure_brief_dirs(tmp_path, **arms):
    macro_dir = tmp_path / "macro"
    ticker_dir = tmp_path / "ticker"
    macro_dir.mkdir(exist_ok=True)
    ticker_dir.mkdir(exist_ok=True)
    set_config({
        "macro_brief_dir": str(macro_dir),
        "ticker_brief_dir": str(ticker_dir),
        **arms,
    })
    return macro_dir, ticker_dir


@pytest.mark.unit
def test_report_header_feeds_arms_add_nothing(tmp_path):
    # Default feeds/feeds run: no brief was consumed, so the report header
    # stays byte-identical to the pre-change output.
    _configure_brief_dirs(tmp_path)
    assert report_header_verdicts("NVDA", "2026-08-03") == []


@pytest.mark.unit
def test_report_header_surfaces_both_verdicts(tmp_path):
    macro_dir, ticker_dir = _configure_brief_dirs(
        tmp_path, macro_source="brief", ticker_source="brief"
    )
    _write_pair(macro_dir, "2026-08-03.us", "macro body", verdict="pass")
    nvda_dir = ticker_dir / "NVDA"
    nvda_dir.mkdir()
    _write_pair(nvda_dir, "2026-08-03", "ticker body", verdict="warn")
    assert report_header_verdicts("NVDA", "2026-08-03") == [
        "Macro brief eval: pass (2026-08-03.us.md)",
        "Ticker brief eval: warn (2026-08-03.md)",
    ]


@pytest.mark.unit
def test_report_header_uses_ticker_session(tmp_path):
    # The macro verdict binds to the session-matching brief for the
    # instrument under analysis, not a fixed session.
    macro_dir, _ = _configure_brief_dirs(tmp_path, macro_source="brief")
    _write_pair(macro_dir, "2026-08-03.cn", "cn macro", verdict="pass")
    _write_pair(macro_dir, "2026-08-03.us", "us macro", verdict="warn")
    assert report_header_verdicts("0700.HK", "2026-08-03") == [
        "Macro brief eval: pass (2026-08-03.cn.md)"
    ]
    assert report_header_verdicts("NVDA", "2026-08-03") == [
        "Macro brief eval: warn (2026-08-03.us.md)"
    ]


@pytest.mark.unit
def test_report_header_absent_briefs_are_missing(tmp_path):
    # DATA_UNAVAILABLE serves have no revision to bind an eval to.
    _configure_brief_dirs(tmp_path, macro_source="brief", ticker_source="brief")
    assert report_header_verdicts("NVDA", "2026-08-03") == [
        "Macro brief eval: missing (no brief served)",
        "Ticker brief eval: missing (no brief served)",
    ]


@pytest.mark.unit
def test_report_header_hash_mismatch_is_missing(tmp_path):
    # Revision binding holds through the header path: a re-collected brief
    # must not surface its predecessor's verdict.
    macro_dir, _ = _configure_brief_dirs(tmp_path, macro_source="brief")
    _write_pair(
        macro_dir, "2026-08-03.us", "recollected body",
        sha=hashlib.sha256(b"previous revision").hexdigest(),
    )
    assert report_header_verdicts("NVDA", "2026-08-03") == [
        "Macro brief eval: missing (2026-08-03.us.md)"
    ]


@pytest.mark.unit
def test_report_header_never_raises(tmp_path):
    _configure_brief_dirs(tmp_path, ticker_source="brief")
    # A hostile ticker fails path validation inside the resolver; surfacing
    # swallows it (report saving must not break) and omits the lines.
    assert report_header_verdicts("../evil", "2026-08-03") == []
    # No trade date recorded in the state: nothing to resolve against.
    assert report_header_verdicts("NVDA", None) == []
