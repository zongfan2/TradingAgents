"""Brief structural validation, eval models/verdict rule, prompt rendering.

Contracts under test: specs/macro-brief-data-contract.md (v2),
specs/ticker-brief-data-contract.md (v1), the evaluator's R4 verdict rule,
and the macro collector's R1 renderer (session blocks, AC5).
"""

from datetime import date

import pytest
from pydantic import ValidationError

from pipeline.contracts import ContractError, briefs, evals
from pipeline.prompts import SESSION_BLOCKS, render_macro_prompt

# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------


def _frontmatter(fields, omit=()):
    lines = [f"{key}: {value}" for key, value in fields.items() if key not in omit]
    return "---\n" + "\n".join(lines) + "\n---\n\n"


def _body(titles, impact_titles, n_urls, words_per_section, dup_first_url=False):
    urls = [f"https://example.com/src{i}" for i in range(n_urls)]
    citations = " ".join(f"[Src]({url})" for url in urls)
    if dup_first_url and urls:
        citations += f" [Again]({urls[0]})"
    blocks = []
    for i, title in enumerate(titles):
        content = ("filler " * words_per_section).strip()
        if i == 0 and citations:
            content += " " + citations
        block = f"## {title}\n\n{content}\n"
        if title in impact_titles:
            block += "\n**Impact**: neutral — no marginal change.\n"
        blocks.append(block)
    return "\n".join(blocks)


def macro_text(
    *,
    date_str="2026-08-03",
    session="us",
    n_urls=8,
    sources_count=None,
    words_per_section=80,
    titles=briefs.MACRO_SECTIONS,
    impact_titles=None,
    omit_fields=(),
    with_frontmatter=True,
    dup_first_url=False,
):
    if sources_count is None:
        sources_count = n_urls
    if impact_titles is None:
        impact_titles = briefs.MACRO_IMPACT_SECTIONS
    fields = {
        "as_of_date": date_str,
        "session": session,
        "generated_at": "2026-08-03T12:35:00Z",
        "generator": "claude-deep-search",
        "sources_count": sources_count,
    }
    frontmatter = _frontmatter(fields, omit_fields) if with_frontmatter else ""
    return frontmatter + _body(titles, impact_titles, n_urls, words_per_section, dup_first_url)


def ticker_text(
    *,
    date_str="2026-08-03",
    ticker="NVDA",
    session="us",
    n_urls=5,
    sources_count=None,
    words_per_section=60,
    titles=briefs.TICKER_SECTIONS,
    omit_fields=(),
    final_impact=True,
):
    if sources_count is None:
        sources_count = n_urls
    fields = {
        "as_of_date": date_str,
        "ticker": ticker,
        "session": session,
        "generated_at": "2026-08-03T12:40:00Z",
        "generator": "claude-deep-search",
        "sources_count": sources_count,
        "catalyst_score": 7.5,
        "catalyst_type": "earnings",
        "catalyst_window": "2026-08-27",
    }
    text = _frontmatter(fields, omit_fields) + _body(titles, (), n_urls, words_per_section)
    if final_impact:
        text += "\n**Impact**: bullish — dated catalyst inside two weeks.\n"
    return text


def errors_of(excinfo):
    return excinfo.value.errors


# ---------------------------------------------------------------------------
# Macro brief structure
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_valid_macro_brief_parses():
    meta, body = briefs.parse_macro_brief(macro_text())
    assert meta.as_of_date == date(2026, 8, 3)
    assert meta.session == "us"
    assert meta.sources_count == 8
    assert meta.generated_at.tzinfo is not None
    assert body.lstrip().startswith("## Monetary Policy & Rates")


@pytest.mark.unit
def test_macro_missing_section_rejected():
    titles = tuple(t for t in briefs.MACRO_SECTIONS if t != "China & Asia")
    with pytest.raises(ContractError) as excinfo:
        briefs.parse_macro_brief(macro_text(titles=titles))
    assert any("missing section '## China & Asia'" in e for e in errors_of(excinfo))


@pytest.mark.unit
def test_macro_sections_out_of_order_rejected():
    titles = list(briefs.MACRO_SECTIONS)
    titles[0], titles[1] = titles[1], titles[0]
    with pytest.raises(ContractError) as excinfo:
        briefs.parse_macro_brief(macro_text(titles=tuple(titles)))
    assert any("out of order" in e for e in errors_of(excinfo))


@pytest.mark.unit
def test_macro_unexpected_section_rejected():
    titles = briefs.MACRO_SECTIONS + ("Crypto Corner",)
    with pytest.raises(ContractError) as excinfo:
        briefs.parse_macro_brief(macro_text(titles=titles))
    assert any("unexpected section '## Crypto Corner'" in e for e in errors_of(excinfo))


@pytest.mark.unit
def test_macro_missing_impact_line_rejected():
    impact = tuple(t for t in briefs.MACRO_IMPACT_SECTIONS if t != "Geopolitics & Trade")
    with pytest.raises(ContractError) as excinfo:
        briefs.parse_macro_brief(macro_text(impact_titles=impact))
    messages = errors_of(excinfo)
    assert any("Geopolitics & Trade" in e and "**Impact**" in e for e in messages)
    # The Watchlist section never requires an Impact line.
    assert not any("Surprises & Watchlist" in e for e in messages)


@pytest.mark.unit
def test_macro_citation_floor_counted_from_body():
    with pytest.raises(ContractError) as excinfo:
        briefs.parse_macro_brief(macro_text(n_urls=7))
    assert any("7 distinct citation URLs" in e and "at least 8" in e for e in errors_of(excinfo))


@pytest.mark.unit
def test_macro_sources_count_must_match_body_count():
    with pytest.raises(ContractError) as excinfo:
        briefs.parse_macro_brief(macro_text(n_urls=9, sources_count=14))
    assert any("sources_count=14" in e and "9 distinct" in e for e in errors_of(excinfo))


@pytest.mark.unit
def test_macro_duplicate_urls_count_once():
    # 8 distinct URLs, one cited twice: still 8 distinct — valid.
    meta, _ = briefs.parse_macro_brief(macro_text(n_urls=8, dup_first_url=True))
    assert meta.sources_count == 8


@pytest.mark.unit
@pytest.mark.parametrize("words_per_section", [20, 400])
def test_macro_word_count_hard_bounds(words_per_section):
    with pytest.raises(ContractError) as excinfo:
        briefs.parse_macro_brief(macro_text(words_per_section=words_per_section))
    assert any("word count" in e and "[500, 2500]" in e for e in errors_of(excinfo))


@pytest.mark.unit
def test_macro_missing_frontmatter_field_rejected():
    with pytest.raises(ContractError) as excinfo:
        briefs.parse_macro_brief(macro_text(omit_fields=("session",)))
    assert any(e.startswith("frontmatter: session") for e in errors_of(excinfo))


@pytest.mark.unit
def test_macro_missing_frontmatter_entirely_rejected():
    with pytest.raises(ContractError) as excinfo:
        briefs.parse_macro_brief(macro_text(with_frontmatter=False))
    assert any("missing YAML frontmatter" in e for e in errors_of(excinfo))


@pytest.mark.unit
def test_macro_error_list_is_complete_for_retry_prompt():
    # Collector R3 feeds the whole list back on retry: one broken brief with
    # several independent defects must report them all at once.
    text = macro_text(
        titles=tuple(t for t in briefs.MACRO_SECTIONS if t != "China & Asia"),
        n_urls=6,
        sources_count=9,
        words_per_section=20,
    )
    with pytest.raises(ContractError) as excinfo:
        briefs.parse_macro_brief(text)
    messages = errors_of(excinfo)
    assert any("missing section" in e for e in messages)
    assert any("at least 8" in e for e in messages)
    assert any("sources_count=9" in e for e in messages)
    assert any("word count" in e for e in messages)


@pytest.mark.unit
def test_macro_filename_cross_check():
    meta, _ = briefs.parse_macro_brief(macro_text())
    briefs.validate_macro_brief_path("/x/macro_briefs/2026-08-03.us.md", meta)
    with pytest.raises(ContractError, match="2026-08-03.us.md"):
        briefs.validate_macro_brief_path("/x/macro_briefs/2026-08-03.cn.md", meta)
    with pytest.raises(ContractError):
        briefs.validate_macro_brief_path("/x/macro_briefs/2026-08-04.us.md", meta)


# ---------------------------------------------------------------------------
# Ticker brief structure
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_valid_ticker_brief_parses():
    meta, body = briefs.parse_ticker_brief(ticker_text())
    assert meta.ticker == "NVDA"
    assert meta.catalyst_score == 7.5
    assert meta.catalyst_type == "earnings"
    assert body.rstrip().endswith("**Impact**: bullish — dated catalyst inside two weeks.")


@pytest.mark.unit
def test_valid_cn_ticker_brief_parses():
    meta, _ = briefs.parse_ticker_brief(ticker_text(ticker="0700.HK", session="cn"))
    assert meta.session == "cn"


@pytest.mark.unit
def test_ticker_missing_final_impact_rejected():
    with pytest.raises(ContractError) as excinfo:
        briefs.parse_ticker_brief(ticker_text(final_impact=False))
    assert any("final" in e and "**Impact**" in e for e in errors_of(excinfo))


@pytest.mark.unit
def test_ticker_missing_section_rejected():
    titles = tuple(t for t in briefs.TICKER_SECTIONS if t != "Risks")
    with pytest.raises(ContractError) as excinfo:
        briefs.parse_ticker_brief(ticker_text(titles=titles))
    assert any("missing section '## Risks'" in e for e in errors_of(excinfo))


@pytest.mark.unit
def test_ticker_citation_floor_is_five():
    with pytest.raises(ContractError) as excinfo:
        briefs.parse_ticker_brief(ticker_text(n_urls=4))
    assert any("at least 5" in e for e in errors_of(excinfo))


@pytest.mark.unit
@pytest.mark.parametrize("words_per_section", [30, 450])
def test_ticker_word_count_hard_bounds(words_per_section):
    with pytest.raises(ContractError) as excinfo:
        briefs.parse_ticker_brief(ticker_text(words_per_section=words_per_section))
    assert any("[250, 2000]" in e for e in errors_of(excinfo))


@pytest.mark.unit
def test_ticker_session_must_match_symbol_suffix():
    with pytest.raises(ContractError) as excinfo:
        briefs.parse_ticker_brief(ticker_text(ticker="0700.HK", session="us"))
    assert any("does not match ticker '0700.HK'" in e for e in errors_of(excinfo))


@pytest.mark.unit
def test_ticker_invalid_catalyst_type_rejected():
    text = ticker_text().replace("catalyst_type: earnings", "catalyst_type: vibes")
    with pytest.raises(ContractError) as excinfo:
        briefs.parse_ticker_brief(text)
    assert any(e.startswith("frontmatter: catalyst_type") for e in errors_of(excinfo))


@pytest.mark.unit
def test_ticker_filename_and_directory_cross_check():
    meta, _ = briefs.parse_ticker_brief(ticker_text())
    briefs.validate_ticker_brief_path("/x/ticker_briefs/NVDA/2026-08-03.md", meta)
    with pytest.raises(ContractError, match="directory 'AMD'"):
        briefs.validate_ticker_brief_path("/x/ticker_briefs/AMD/2026-08-03.md", meta)
    with pytest.raises(ContractError, match="as_of_date"):
        briefs.validate_ticker_brief_path("/x/ticker_briefs/NVDA/2026-08-04.md", meta)


@pytest.mark.unit
def test_brief_meta_rejects_unknown_fields_strictly_but_lenient_warns(caplog):
    data = {
        "as_of_date": "2026-08-03",
        "session": "us",
        "generated_at": "2026-08-03T12:35:00Z",
        "generator": "claude-deep-search",
        "sources_count": 9,
        "novel_field": "from a newer writer",
    }
    with pytest.raises(ValidationError):
        briefs.MacroBriefMeta.model_validate(data)
    with caplog.at_level("WARNING", logger="pipeline.contracts"):
        meta = briefs.MacroBriefMeta.parse_lenient(data)
    assert meta.sources_count == 9
    assert "novel_field" in caplog.text


# ---------------------------------------------------------------------------
# Eval reports + verdict rule (evaluator R4)
# ---------------------------------------------------------------------------


def make_scores(**overrides):
    values = dict.fromkeys(evals.SCORE_DIMENSIONS, 8.0)
    values.update(overrides)
    return evals.EvalScores(**values)


def flag(severity):
    return evals.FlaggedClaim(section="Risks", claim="c", issue="i", severity=severity)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("scores", "flags", "expected"),
    [
        (make_scores(), [], "pass"),
        (make_scores(), [flag("minor")], "pass"),
        (make_scores(factual_accuracy=4.9), [], "fail"),
        (make_scores(), [flag("fabrication")], "fail"),
        (make_scores(factual_accuracy=4.0), [flag("major")], "fail"),  # fail beats warn
        (make_scores(), [flag("major")], "warn"),
        (make_scores(coverage=5.5), [], "warn"),
        (make_scores(factual_accuracy=5.0), [], "warn"),  # ≥5 not fail, <6 warns
        (make_scores(consistency=6.0), [], "pass"),
    ],
)
def test_compute_verdict_matrix(scores, flags, expected):
    assert evals.compute_verdict(scores, flags) == expected


EVAL_BASE = {
    "as_of_date": "2026-08-03",
    "brief_sha256": "a" * 64,
    "brief_generated_at": "2026-08-03T12:35:00Z",
    "evaluator": "gpt-5.6-terra",
    "evaluated_at": "2026-08-03T13:00:00Z",
    "scores": {
        "factual_accuracy": 8.0,
        "citation_support": 8.5,
        "coverage": 7.5,
        "timeliness": 8.0,
        "consistency": 9.0,
    },
    "flagged_claims": [
        {"section": "Risks", "claim": "x", "issue": "y", "severity": "minor"},
    ],
    "verdict": "pass",
    "notes": "free text",
}


@pytest.mark.unit
def test_macro_eval_report_requires_session():
    evals.MacroEvalReport.model_validate({**EVAL_BASE, "session": "us"})
    with pytest.raises(ValidationError, match="session"):
        evals.MacroEvalReport.model_validate(EVAL_BASE)


@pytest.mark.unit
def test_ticker_eval_report_requires_ticker_and_session():
    evals.TickerEvalReport.model_validate({**EVAL_BASE, "ticker": "NVDA", "session": "us"})
    with pytest.raises(ValidationError, match="ticker"):
        evals.TickerEvalReport.model_validate({**EVAL_BASE, "session": "us"})


@pytest.mark.unit
def test_eval_scores_bounded_and_severity_enum_enforced():
    with pytest.raises(ValidationError):
        make_scores(coverage=10.5)
    with pytest.raises(ValidationError):
        flag("catastrophic")
    with pytest.raises(ValidationError):
        evals.MacroEvalReport.model_validate({**EVAL_BASE, "session": "us", "verdict": "maybe"})


# ---------------------------------------------------------------------------
# Macro prompt rendering (collector R1 + AC5)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_render_macro_prompt_cn_us_differ_for_same_date():
    cn = render_macro_prompt(date(2026, 8, 3), "cn", "claude-deep-search")
    us = render_macro_prompt(date(2026, 8, 3), "us", "claude-deep-search")
    assert cn != us
    assert SESSION_BLOCKS["cn"] in cn and SESSION_BLOCKS["us"] not in cn
    assert SESSION_BLOCKS["us"] in us and SESSION_BLOCKS["cn"] not in us


@pytest.mark.unit
def test_render_macro_prompt_fills_every_placeholder():
    out = render_macro_prompt("2026-08-03", "us", "codex-deep-search")
    assert "{{" not in out
    assert "session: us" in out
    assert "as_of_date: 2026-08-03" in out
    assert "generator: codex-deep-search" in out


@pytest.mark.unit
def test_render_macro_prompt_rejects_unknown_session():
    with pytest.raises(ValueError, match="unknown session"):
        render_macro_prompt("2026-08-03", "eu", "claude-deep-search")

# ---------------------------------------------------------------------------
# Frontmatter edge cases (review round: enforced-but-untested branches)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_macro_unterminated_frontmatter_rejected():
    text = "---\nas_of_date: 2026-08-03\nsession: us\nno closing fence anywhere"
    with pytest.raises(ContractError) as excinfo:
        briefs.parse_macro_brief(text)
    assert any("unterminated YAML frontmatter" in e for e in errors_of(excinfo))


@pytest.mark.unit
def test_macro_frontmatter_invalid_yaml_rejected():
    body = _body(briefs.MACRO_SECTIONS, briefs.MACRO_IMPACT_SECTIONS, 8, 80)
    text = "---\nas_of_date: [unclosed\n---\n\n" + body
    with pytest.raises(ContractError) as excinfo:
        briefs.parse_macro_brief(text)
    assert any("not valid YAML" in e for e in errors_of(excinfo))


@pytest.mark.unit
def test_macro_frontmatter_non_mapping_rejected():
    body = _body(briefs.MACRO_SECTIONS, briefs.MACRO_IMPACT_SECTIONS, 8, 80)
    text = "---\n- just\n- a list\n---\n\n" + body
    with pytest.raises(ContractError) as excinfo:
        briefs.parse_macro_brief(text)
    assert any("not a YAML mapping" in e for e in errors_of(excinfo))


@pytest.mark.unit
def test_macro_naive_generated_at_rejected():
    text = macro_text().replace(
        "generated_at: 2026-08-03T12:35:00Z", "generated_at: 2026-08-03T12:35:00"
    )
    with pytest.raises(ContractError) as excinfo:
        briefs.parse_macro_brief(text)
    assert any("generated_at" in e and "aware" in e for e in errors_of(excinfo))


@pytest.mark.unit
def test_macro_crlf_line_endings_parse():
    meta, body = briefs.parse_macro_brief(macro_text().replace("\n", "\r\n"))
    assert meta.session == "us"
    assert "## Monetary Policy & Rates" in body


@pytest.mark.unit
def test_macro_duplicated_section_rejected():
    titles = briefs.MACRO_SECTIONS + ("China & Asia",)
    with pytest.raises(ContractError) as excinfo:
        briefs.parse_macro_brief(macro_text(titles=titles))
    assert any("duplicated section '## China & Asia'" in e for e in errors_of(excinfo))


@pytest.mark.unit
def test_macro_invalid_impact_direction_rejected():
    text = macro_text().replace("**Impact**: neutral —", "**Impact**: positive —")
    with pytest.raises(ContractError) as excinfo:
        briefs.parse_macro_brief(text)
    assert any("**Impact**" in e for e in errors_of(excinfo))


@pytest.mark.unit
def test_macro_impact_line_requires_rationale():
    # Hard requirement 3 is direction + one-line rationale, not the bare word.
    text = macro_text().replace("**Impact**: neutral — no marginal change.", "**Impact**: neutral")
    with pytest.raises(ContractError) as excinfo:
        briefs.parse_macro_brief(text)
    assert any("**Impact**" in e for e in errors_of(excinfo))


@pytest.mark.unit
def test_ticker_impact_line_requires_rationale():
    text = ticker_text().replace(
        "**Impact**: bullish — dated catalyst inside two weeks.", "**Impact**: bullish"
    )
    with pytest.raises(ContractError) as excinfo:
        briefs.parse_ticker_brief(text)
    assert any("final" in e and "**Impact**" in e for e in errors_of(excinfo))


@pytest.mark.unit
def test_ticker_catalyst_score_out_of_range_rejected():
    text = ticker_text().replace("catalyst_score: 7.5", "catalyst_score: 11")
    with pytest.raises(ContractError) as excinfo:
        briefs.parse_ticker_brief(text)
    assert any("catalyst_score" in e for e in errors_of(excinfo))


@pytest.mark.unit
def test_ticker_empty_catalyst_window_rejected():
    text = ticker_text().replace("catalyst_window: 2026-08-27", "catalyst_window: '   '")
    with pytest.raises(ContractError) as excinfo:
        briefs.parse_ticker_brief(text)
    assert any("catalyst_window" in e for e in errors_of(excinfo))


@pytest.mark.unit
def test_ticker_sources_count_must_match_body_count():
    with pytest.raises(ContractError) as excinfo:
        briefs.parse_ticker_brief(ticker_text(n_urls=6, sources_count=9))
    assert any("sources_count=9" in e and "6 distinct" in e for e in errors_of(excinfo))


# ---------------------------------------------------------------------------
# Structural-only mode (evaluator R1) vs collector quality gates (R3)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_macro_structural_only_skips_collector_quality_gates():
    # 7 citations + a 450-word body violate collector gates but the macro
    # contract's carve-out keeps them outside structural validity — the
    # evaluator must evaluate such a brief, not refuse it.
    text = macro_text(n_urls=7, words_per_section=20)
    meta, _ = briefs.parse_macro_brief(text, structural_only=True)
    assert meta.sources_count == 7
    with pytest.raises(ContractError):
        briefs.parse_macro_brief(text)
    # sources_count verification is likewise the collector's, not structure.
    meta, _ = briefs.parse_macro_brief(macro_text(n_urls=9, sources_count=14), structural_only=True)
    assert meta.sources_count == 14


@pytest.mark.unit
def test_macro_structural_only_still_rejects_hard_requirement_violations():
    titles = tuple(t for t in briefs.MACRO_SECTIONS if t != "China & Asia")
    with pytest.raises(ContractError) as excinfo:
        briefs.parse_macro_brief(macro_text(titles=titles), structural_only=True)
    assert any("missing section" in e for e in errors_of(excinfo))


@pytest.mark.unit
def test_ticker_structural_only_skips_word_count_but_keeps_citation_rules():
    # Word count is the ticker contract's only quality-gate carve-out; the
    # >=5 floor and the sources_count equality are hard requirement 2.
    meta, _ = briefs.parse_ticker_brief(ticker_text(words_per_section=10), structural_only=True)
    assert meta.ticker == "NVDA"
    with pytest.raises(ContractError) as excinfo:
        briefs.parse_ticker_brief(ticker_text(n_urls=4), structural_only=True)
    assert any("at least 5" in e for e in errors_of(excinfo))
    with pytest.raises(ContractError) as excinfo:
        briefs.parse_ticker_brief(ticker_text(n_urls=6, sources_count=9), structural_only=True)
    assert any("sources_count=9" in e for e in errors_of(excinfo))


@pytest.mark.unit
def test_macro_word_count_soft_target_warns_but_passes(caplog):
    # ~640 words: inside the 500-2500 hard bounds, below the 800-1500 target.
    with caplog.at_level("WARNING", logger="pipeline.contracts.briefs"):
        briefs.parse_macro_brief(macro_text(words_per_section=80))
    assert "outside the 800-1500 target" in caplog.text


@pytest.mark.unit
def test_macro_generator_must_match_invoked_backend_when_given():
    briefs.parse_macro_brief(macro_text(), expected_generator="claude-deep-search")
    with pytest.raises(ContractError) as excinfo:
        briefs.parse_macro_brief(macro_text(), expected_generator="codex-deep-search")
    assert any("does not match the invoked backend" in e for e in errors_of(excinfo))


# ---------------------------------------------------------------------------
# Legacy v1 macro briefs (session-agnostic candidates)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_macro_legacy_v1_brief_parses_without_session():
    text = macro_text(omit_fields=("session",))
    with pytest.raises(ContractError):  # v2 files still require session
        briefs.parse_macro_brief(text)
    meta, _ = briefs.parse_macro_brief(text, legacy=True)
    assert meta.session is None
    briefs.validate_macro_brief_path("/x/macro_briefs/2026-08-03.md", meta)
    with pytest.raises(ContractError):
        briefs.validate_macro_brief_path("/x/macro_briefs/2026-08-03.us.md", meta)


# ---------------------------------------------------------------------------
# Eval report field constraints
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_eval_report_rejects_empty_sha_and_naive_timestamps():
    with pytest.raises(ValidationError, match="brief_sha256"):
        evals.MacroEvalReport.model_validate({**EVAL_BASE, "session": "us", "brief_sha256": ""})
    with pytest.raises(ValidationError, match="evaluated_at"):
        evals.MacroEvalReport.model_validate(
            {**EVAL_BASE, "session": "us", "evaluated_at": "2026-08-03T13:00:00"}
        )
    with pytest.raises(ValidationError, match="brief_generated_at"):
        evals.MacroEvalReport.model_validate(
            {**EVAL_BASE, "session": "us", "brief_generated_at": "2026-08-03T12:35:00"}
        )
