"""News-analyst tool selection across the macro/ticker arm matrix
(specs/pipeline-consumption-v2.md).

Each tool is governed by exactly one switch, so every combination is fully
determined — and the default feeds/feeds combination must reproduce the exact
pre-change behavior: same tools bound (same order) and the same system prompt
(AC3, the prime directive of the v2 change).
"""
import pytest
from langchain_core.messages import AIMessage, HumanMessage

from tradingagents.agents.analysts.news_analyst import (
    _select_news_tools,
    create_news_analyst,
)
from tradingagents.dataflows.config import set_config

# The pre-change tool list, in the pre-change binding order (v1
# news_analyst.py: [get_news, *macro_tools, search_news, get_prediction_markets]).
PRECHANGE_FEEDS_TOOLS = [
    "get_news",
    "get_global_news",
    "get_macro_indicators",
    "search_news",
    "get_prediction_markets",
]

# The pre-change news-analyst system message, verbatim (asset_label="company",
# English output). Pinned so the default configuration provably keeps the
# exact upstream prompt.
PRECHANGE_FEEDS_SYSTEM_MESSAGE = (
    "You are a news researcher tasked with analyzing recent news and trends "
    "over the past week. Please write a comprehensive report of the current "
    "state of the world that is relevant for trading and macroeconomics. Use "
    "the available tools: get_news(ticker, start_date, end_date) for "
    "company-specific news by ticker symbol, get_global_news(curr_date, "
    "look_back_days, limit) for broader macroeconomic news, "
    "get_macro_indicators(indicator, curr_date, look_back_days) to ground "
    "macro commentary in actual data from FRED (e.g. 'cpi', 'core_pce', "
    "'unemployment', 'fed_funds_rate', '10y_treasury', 'yield_curve'), and "
    "get_prediction_markets(topic, limit) for live market-implied "
    "probabilities of forward-looking events (e.g. 'Fed rate cut', "
    "'recession 2026', geopolitical or sector events). Beyond those, use "
    "search_news(query, curr_date) to investigate angles you judge relevant "
    "on your own initiative — competitors, suppliers, customers, regulation, "
    "sector dynamics — and follow up on leads from headlines with refined "
    "queries. Provide specific, actionable insights with supporting evidence "
    "to help traders make informed decisions. Make sure to append a Markdown "
    "table at the end of the report to organize key points in the report, "
    "organized and easy to read."
)


def _names(tools):
    return [t.name for t in tools]


@pytest.mark.unit
def test_feeds_feeds_pins_prechange_tool_list():
    # AC3 prime directive: the default arms bind the exact pre-change tools,
    # in the exact pre-change order.
    tools, _ = _select_news_tools("feeds", "feeds")
    assert _names(tools) == PRECHANGE_FEEDS_TOOLS


@pytest.mark.unit
@pytest.mark.parametrize(
    "macro_source,ticker_source,expected",
    [
        ("feeds", "feeds", PRECHANGE_FEEDS_TOOLS),
        ("feeds", "brief", [
            "get_ticker_brief", "get_global_news", "get_macro_indicators",
            "search_news", "get_prediction_markets",
        ]),
        ("brief", "feeds", [
            "get_news", "get_macro_brief", "search_news",
            "get_prediction_markets",
        ]),
        ("brief", "brief", [
            "get_ticker_brief", "get_macro_brief", "get_prediction_markets",
        ]),
    ],
)
def test_tool_exposure_matrix(macro_source, ticker_source, expected):
    # Per-switch governance: get_news iff ticker=feeds, get_ticker_brief iff
    # ticker=brief, global-news+FRED iff macro=feeds, get_macro_brief iff
    # macro=brief, search_news iff either=feeds, prediction markets always.
    tools, _ = _select_news_tools(macro_source, ticker_source)
    assert _names(tools) == expected


@pytest.mark.unit
@pytest.mark.parametrize("macro_source,ticker_source", [
    ("feeds", "feeds"), ("feeds", "brief"), ("brief", "feeds"), ("brief", "brief"),
])
def test_polymarket_disabled_removes_prediction_markets(macro_source, ticker_source):
    tools, desc = _select_news_tools(
        macro_source, ticker_source, polymarket_enabled=False
    )
    assert "get_prediction_markets" not in _names(tools)
    assert "get_prediction_markets" not in desc
    # The tool enumeration closes as a well-formed sentence (". "), with no
    # characters chopped from the last arm fragment.
    assert ", . " not in desc and ",. " not in desc
    assert ". " in desc


@pytest.mark.unit
def test_arm_fragments_leave_enumeration_open():
    # The polymarket-disabled path closes the tool enumeration by removing a
    # trailing ", " from the assembled arm fragments; every fragment must end
    # with that exact suffix or the closing degrades (guarded by removesuffix
    # in _select_news_tools, pinned here so a fragment edit fails loudly).
    from tradingagents.agents.analysts.news_analyst import (
        _select_macro_tools,
        _select_ticker_tools,
    )

    for arm in ("feeds", "brief"):
        assert _select_macro_tools(arm)[1].endswith(", ")
        assert _select_ticker_tools(arm)[1].endswith(", ")


@pytest.mark.unit
def test_full_brief_bundle_is_offline():
    # Zero news-API tools in the brief bundle: no get_news, no get_global_news,
    # and no autonomous search_news (removed only in this combination).
    tools, desc = _select_news_tools("brief", "brief")
    names = set(_names(tools))
    assert not names & {"get_news", "get_global_news", "search_news"}
    assert "search_news" not in desc


@pytest.mark.unit
def test_brief_bundle_prompt_grounds_both_briefs():
    _, desc = _select_news_tools("brief", "brief")
    assert "get_macro_brief(curr_date, ticker)" in desc
    assert "get_ticker_brief(ticker, curr_date)" in desc
    # Ground macro commentary in the macro brief, company commentary in the
    # ticker brief, citing as-of dates in both cases.
    assert desc.count("cite its as-of date") == 2
    assert "ground ALL macro commentary" in desc
    assert "ground ALL {asset_label}-specific commentary" in desc
    # A missing brief must be stated, not improvised around.
    assert "DATA_UNAVAILABLE" in desc


@pytest.mark.unit
def test_single_brief_arm_still_states_unavailability():
    for arms in (("brief", "feeds"), ("feeds", "brief")):
        _, desc = _select_news_tools(*arms)
        assert "DATA_UNAVAILABLE" in desc


@pytest.mark.unit
def test_arm_validation_mirrors_v1():
    # Normalized values pass; a typo'd arm raises rather than silently running
    # the wrong A/B arm (same policy as the v1 macro switch).
    tools, _ = _select_news_tools("feeds", " BRIEF ")
    assert "get_ticker_brief" in _names(tools)
    for empty in (None, ""):
        tools, _ = _select_news_tools("feeds", empty)
        assert "get_news" in _names(tools)
    with pytest.raises(ValueError, match="ticker_source"):
        _select_news_tools("feeds", "briefs")
    with pytest.raises(ValueError, match="macro_source"):
        _select_news_tools("feed", "feeds")


class _RecordingLLM:
    """Captures the tools bound to the LLM and the rendered prompt."""

    def __init__(self):
        self.bound_tools = None
        self.prompt_value = None

    def bind_tools(self, tools):
        self.bound_tools = list(tools)

        def _record(prompt_value):
            self.prompt_value = prompt_value
            return AIMessage(content="stub news report")

        return _record


def _run_news_analyst():
    llm = _RecordingLLM()
    node = create_news_analyst(llm)
    node({
        "trade_date": "2026-08-03",
        "company_of_interest": "NVDA",
        "messages": [HumanMessage(content="NVDA")],
    })
    return llm


@pytest.mark.unit
def test_default_config_reproduces_prechange_node_behavior():
    # AC3 prime directive, end to end through the real node: with the default
    # configuration (feeds/feeds, polymarket enabled) the LLM is bound the
    # exact pre-change tools and receives the exact pre-change system message.
    llm = _run_news_analyst()
    assert _names(llm.bound_tools) == PRECHANGE_FEEDS_TOOLS
    system_content = llm.prompt_value.to_messages()[0].content
    assert PRECHANGE_FEEDS_SYSTEM_MESSAGE in system_content


@pytest.mark.unit
def test_brief_bundle_node_binds_brief_tools():
    set_config({"macro_source": "brief", "ticker_source": "brief"})
    llm = _run_news_analyst()
    assert _names(llm.bound_tools) == [
        "get_ticker_brief", "get_macro_brief", "get_prediction_markets",
    ]
    system_content = llm.prompt_value.to_messages()[0].content
    assert "DATA_UNAVAILABLE" in system_content
    # {asset_label} placeholders are resolved before the prompt is rendered.
    assert "{asset_label}" not in system_content
    assert "ground ALL company-specific commentary" in system_content


@pytest.mark.unit
def test_polymarket_config_gates_node_binding():
    set_config({"polymarket_enabled": False})
    llm = _run_news_analyst()
    assert "get_prediction_markets" not in _names(llm.bound_tools)
