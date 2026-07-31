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
    assert "get_macro_brief(curr_date)" in brief_desc


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
        "get_macro_indicators", "get_macro_brief", "get_prediction_markets",
    } <= news_tools


@pytest.mark.unit
def test_default_config_wires_the_arm():
    from tradingagents.default_config import DEFAULT_CONFIG

    assert DEFAULT_CONFIG["macro_source"] == "feeds"
    assert DEFAULT_CONFIG["data_vendors"]["macro_brief"] == "local"
    assert DEFAULT_CONFIG["macro_brief_dir"]
