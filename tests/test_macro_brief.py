"""Macro brief pipeline side (specs/macro-brief-pipeline.md).

The local vendor reads the newest brief dated on or before the analysis date,
warns when stale, and degrades to the optional-category sentinel when no brief
exists. The news analyst swaps its macro tools by the ``macro_source`` arm.
"""
import pytest

import tradingagents.dataflows.interface as interface
import tradingagents.dataflows.macro_brief as mb
from tradingagents.dataflows.errors import VendorNotConfiguredError


@pytest.fixture
def brief_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(mb, "get_config", lambda: {"macro_brief_dir": str(tmp_path)})
    return tmp_path


def _write(brief_dir, date_str, body="## Monetary Policy & Rates\ncontent"):
    (brief_dir / f"{date_str}.md").write_text(
        f"---\nas_of_date: {date_str}\n---\n\n{body}", encoding="utf-8"
    )


def _write_session(brief_dir, date_str, session, body=None):
    body = body or f"## Monetary Policy & Rates\n{session} content"
    (brief_dir / f"{date_str}.{session}.md").write_text(
        f"---\nas_of_date: {date_str}\nsession: {session}\n---\n\n{body}",
        encoding="utf-8",
    )


@pytest.mark.unit
def test_picks_newest_on_or_before_date(brief_dir):
    _write(brief_dir, "2026-07-25")
    _write(brief_dir, "2026-07-27")
    _write(brief_dir, "2026-07-29")  # future relative to analysis date
    result = mb.get_macro_brief_local("2026-07-28")
    assert "as of 2026-07-27" in result
    assert "as_of_date: 2026-07-27" in result


@pytest.mark.unit
def test_future_only_briefs_raise(brief_dir):
    _write(brief_dir, "2026-07-29")
    with pytest.raises(VendorNotConfiguredError):
        mb.get_macro_brief_local("2026-07-28")


@pytest.mark.unit
def test_empty_dir_raises_with_instructions(brief_dir):
    with pytest.raises(VendorNotConfiguredError, match="collector"):
        mb.get_macro_brief_local("2026-07-28")


@pytest.mark.unit
def test_missing_dir_raises_with_instructions(tmp_path, monkeypatch):
    missing = tmp_path / "does-not-exist"
    monkeypatch.setattr(mb, "get_config", lambda: {"macro_brief_dir": str(missing)})
    with pytest.raises(VendorNotConfiguredError, match="collector"):
        mb.get_macro_brief_local("2026-07-28")


@pytest.mark.unit
def test_ignores_non_brief_files(brief_dir):
    (brief_dir / "2026-07-27.eval.json").write_text("{}", encoding="utf-8")
    (brief_dir / "collector.log").write_text("", encoding="utf-8")
    with pytest.raises(VendorNotConfiguredError):
        mb.get_macro_brief_local("2026-07-28")


@pytest.mark.unit
def test_stale_brief_gets_warning(brief_dir):
    _write(brief_dir, "2026-07-20")
    result = mb.get_macro_brief_local("2026-07-28")
    assert "WARNING" in result
    assert "8 days older" in result


@pytest.mark.unit
def test_fresh_brief_no_warning(brief_dir):
    _write(brief_dir, "2026-07-26")  # 2-day gap (weekend) is normal
    result = mb.get_macro_brief_local("2026-07-28")
    assert "WARNING" not in result


@pytest.mark.unit
def test_routing_registered_and_degrades_when_missing(brief_dir):
    assert interface.get_category_for_method("get_macro_brief") == "macro_brief"
    assert "macro_brief" in interface.OPTIONAL_CATEGORIES
    # Empty dir -> optional category degrades to sentinel instead of raising.
    result = interface.route_to_vendor("get_macro_brief", "2026-07-28")
    assert result.startswith("DATA_UNAVAILABLE")


@pytest.mark.unit
def test_routing_serves_brief(brief_dir):
    _write(brief_dir, "2026-07-28")
    result = interface.route_to_vendor("get_macro_brief", "2026-07-28")
    assert "as of 2026-07-28" in result


@pytest.mark.unit
def test_tool_exposed():
    from tradingagents.agents.utils.macro_data_tools import get_macro_brief

    assert get_macro_brief.name == "get_macro_brief"
    assert "curr_date" in get_macro_brief.args


@pytest.mark.unit
def test_macro_source_arm_selection():
    from tradingagents.agents.analysts.news_analyst import _select_macro_tools

    feeds_tools, feeds_desc = _select_macro_tools("feeds")
    feeds_names = {t.name for t in feeds_tools}
    assert feeds_names == {"get_global_news", "get_macro_indicators"}
    assert "get_global_news" in feeds_desc

    brief_tools, brief_desc = _select_macro_tools("brief")
    assert {t.name for t in brief_tools} == {"get_macro_brief"}
    # v2: the tool signature gained an optional ticker (session derivation),
    # and the advertised signature must track it (prompt/signature drift guard).
    assert "get_macro_brief(curr_date, ticker)" in brief_desc


@pytest.mark.unit
def test_macro_source_arm_normalizes_and_rejects_unknown():
    # A typo'd arm must raise, not silently run the feeds arm (A/B integrity).
    from tradingagents.agents.analysts.news_analyst import _select_macro_tools

    for variant in ("Brief", " BRIEF ", "brief"):
        tools, _ = _select_macro_tools(variant)
        assert {t.name for t in tools} == {"get_macro_brief"}
    for empty in (None, ""):
        tools, _ = _select_macro_tools(empty)
        assert {t.name for t in tools} == {"get_global_news", "get_macro_indicators"}
    with pytest.raises(ValueError, match="macro_source"):
        _select_macro_tools("briefs")


@pytest.mark.unit
def test_news_toolnode_executes_both_arms():
    # Superset invariant: every tool either arm binds must be executable by the
    # news ToolNode, or the model's call fails at runtime (see market analog).
    from tradingagents.graph.trading_graph import TradingAgentsGraph

    nodes = TradingAgentsGraph._create_tool_nodes(None)
    news_tools = set(nodes["news"].tools_by_name)
    assert {
        "get_news", "get_global_news", "search_news", "get_insider_transactions",
        "get_macro_indicators", "get_macro_brief", "get_ticker_brief",
        "get_prediction_markets",
    } <= news_tools


@pytest.mark.unit
def test_default_config_wires_the_arm():
    from tradingagents.default_config import DEFAULT_CONFIG

    assert DEFAULT_CONFIG["macro_source"] == "feeds"
    assert DEFAULT_CONFIG["data_vendors"]["macro_brief"] == "local"
    assert DEFAULT_CONFIG["macro_brief_dir"]


# ---------------------------------------------------------------------------
# v2 session-aware selection (specs/macro-brief-data-contract.md v2)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_session_match_preferred_on_date_tie(brief_dir):
    _write_session(brief_dir, "2026-08-03", "cn")
    _write_session(brief_dir, "2026-08-03", "us")
    result = mb.get_macro_brief_local("2026-08-03", session="cn")
    assert "(cn session)" in result
    assert "cn content" in result
    assert "NOTE" not in result
    result = mb.get_macro_brief_local("2026-08-03", session="us")
    assert "(us session)" in result
    assert "us content" in result
    assert "NOTE" not in result


@pytest.mark.unit
def test_date_beats_session_match(brief_dir):
    # Ranking is date-first: a newer other-session brief beats an older
    # session-matching one, and the fallback is noted in the header.
    _write_session(brief_dir, "2026-08-02", "cn")
    _write_session(brief_dir, "2026-08-03", "us")
    result = mb.get_macro_brief_local("2026-08-03", session="cn")
    assert "as of 2026-08-03 (us session)" in result
    assert "NOTE" in result and "us-session" in result


@pytest.mark.unit
def test_session_file_beats_legacy_on_date_tie(brief_dir):
    _write(brief_dir, "2026-08-03", body="legacy content")
    _write_session(brief_dir, "2026-08-03", "us")
    result = mb.get_macro_brief_local("2026-08-03", session="us")
    assert "(us session)" in result
    assert "us content" in result
    assert "NOTE" not in result


@pytest.mark.unit
def test_legacy_fallback_adds_note(brief_dir):
    _write(brief_dir, "2026-08-03", body="legacy content")
    result = mb.get_macro_brief_local("2026-08-03", session="us")
    assert "legacy content" in result
    assert "NOTE" in result and "legacy" in result
    # Legacy files have no session to surface in the base header.
    assert "session)" not in result


@pytest.mark.unit
def test_cross_session_fallback_adds_note(brief_dir):
    _write_session(brief_dir, "2026-08-03", "us")
    result = mb.get_macro_brief_local("2026-08-03", session="cn")
    assert "us content" in result
    assert "NOTE" in result and "us-session" in result


@pytest.mark.unit
def test_session_none_keeps_v1_view(brief_dir):
    # No requested session: date-first, legacy fully acceptable, no NOTE.
    _write_session(brief_dir, "2026-08-01", "cn")
    _write(brief_dir, "2026-08-02", body="legacy content")
    result = mb.get_macro_brief_local("2026-08-03")
    assert "as of 2026-08-02" in result
    assert "legacy content" in result
    assert "NOTE" not in result


@pytest.mark.unit
def test_session_none_prefers_session_files_on_tie(brief_dir):
    # Deterministic tiebreak for the v1 view: session-scoped beats legacy.
    _write(brief_dir, "2026-08-03", body="legacy content")
    _write_session(brief_dir, "2026-08-03", "cn")
    result = mb.get_macro_brief_local("2026-08-03")
    assert "cn content" in result
    assert "NOTE" not in result


@pytest.mark.unit
def test_session_none_cn_us_tie_is_deterministic(brief_dir):
    # v1 view, same-date cn+us pair: both rank as plain session files, so the
    # filename tiebreak decides — "….us.md" wins lexicographically. Pinned so
    # a candidate-tuple layout change cannot silently flip which brief the
    # no-session view (compare/run.py pre-flight) serves.
    _write_session(brief_dir, "2026-08-03", "cn")
    _write_session(brief_dir, "2026-08-03", "us")
    result = mb.get_macro_brief_local("2026-08-03")
    assert "us content" in result
    assert "NOTE" not in result


@pytest.mark.unit
def test_session_briefs_respect_future_exclusion_and_staleness(brief_dir):
    _write_session(brief_dir, "2026-07-25", "us")
    _write_session(brief_dir, "2026-08-04", "us")  # future: excluded
    result = mb.get_macro_brief_local("2026-08-03", session="us")
    assert "as of 2026-07-25" in result
    assert "WARNING" in result and "9 days older" in result


@pytest.mark.unit
def test_unknown_session_raises(brief_dir):
    _write_session(brief_dir, "2026-08-03", "us")
    with pytest.raises(ValueError, match="session"):
        mb.get_macro_brief_local("2026-08-03", session="tokyo")


@pytest.mark.unit
def test_routing_passes_session_through(brief_dir):
    _write_session(brief_dir, "2026-08-03", "cn")
    _write_session(brief_dir, "2026-08-03", "us")
    result = interface.route_to_vendor("get_macro_brief", "2026-08-03", "cn")
    assert "(cn session)" in result and "cn content" in result


# ---------------------------------------------------------------------------
# Path resolver (eval-verdict revision binding, specs/pipeline-consumption-v2.md §5)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_resolve_path_matches_served_brief(brief_dir):
    # The resolver shares the reader's selection core: a cn request still
    # resolves to the newer us file the reader would serve (date-first rule),
    # so eval lookups bind to the exact served revision.
    _write_session(brief_dir, "2026-08-02", "cn")
    _write_session(brief_dir, "2026-08-03", "us")
    path = mb.resolve_macro_brief_path("2026-08-03", session="cn")
    assert path == str(brief_dir / "2026-08-03.us.md")
    served = mb.get_macro_brief_local("2026-08-03", session="cn")
    assert (brief_dir / "2026-08-03.us.md").read_text(encoding="utf-8") in served


@pytest.mark.unit
def test_resolve_path_session_match_and_legacy(brief_dir):
    _write(brief_dir, "2026-08-02")
    _write_session(brief_dir, "2026-08-03", "cn")
    _write_session(brief_dir, "2026-08-03", "us")
    assert mb.resolve_macro_brief_path("2026-08-03", session="cn") == str(
        brief_dir / "2026-08-03.cn.md"
    )
    # No session: v1 view, date first (legacy loses the tie to session files).
    assert mb.resolve_macro_brief_path("2026-08-03") == str(
        brief_dir / "2026-08-03.us.md"
    )


@pytest.mark.unit
def test_resolve_path_raises_when_no_brief(brief_dir):
    with pytest.raises(VendorNotConfiguredError):
        mb.resolve_macro_brief_path("2026-08-03", session="us")


@pytest.mark.unit
def test_resolve_path_rejects_unknown_session(brief_dir):
    _write_session(brief_dir, "2026-08-03", "us")
    with pytest.raises(ValueError, match="session"):
        mb.resolve_macro_brief_path("2026-08-03", session="tokyo")
