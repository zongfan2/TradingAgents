from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder

from tradingagents.agents.utils.agent_utils import (
    get_global_news,
    get_instrument_context_from_state,
    get_language_instruction,
    get_macro_brief,
    get_macro_indicators,
    get_news,
    get_prediction_markets,
    get_ticker_brief,
    search_news,
)
from tradingagents.dataflows.config import get_config


def _validate_arm(name, value):
    """Normalize (strip/lower) a feeds/brief arm value; anything else raises —
    a typo'd arm must not silently run the wrong A/B arm."""
    value = (value or "feeds").strip().lower()
    if value not in ("feeds", "brief"):
        raise ValueError(f"Unknown {name} {value!r}; expected 'feeds' or 'brief'")
    return value


def _select_macro_tools(macro_source):
    """Macro-arm tools plus the matching prompt fragment (A/B switch).

    "brief" consumes the pre-compiled daily deep-search brief; "feeds" is the
    upstream fixed-feed behavior. See specs/macro-brief-pipeline.md.
    """
    macro_source = _validate_arm("macro_source", macro_source)
    if macro_source == "brief":
        return [get_macro_brief], (
            "get_macro_brief(curr_date, ticker) for the pre-compiled daily macro research "
            "brief covering monetary policy, growth and earnings, geopolitics, "
            "global liquidity, commodities, and China/Asia — ground ALL macro "
            "commentary in this brief and cite its as-of date, "
        )
    return [get_global_news, get_macro_indicators], (
        "get_global_news(curr_date, look_back_days, limit) for broader macroeconomic news, "
        "get_macro_indicators(indicator, curr_date, look_back_days) to ground macro "
        "commentary in actual data from FRED (e.g. 'cpi', 'core_pce', 'unemployment', "
        "'fed_funds_rate', '10y_treasury', 'yield_curve'), "
    )


def _select_ticker_tools(ticker_source):
    """Ticker-arm tools plus the matching prompt fragment (A/B switch).

    "brief" consumes the pre-compiled per-ticker deep-search brief; "feeds" is
    the upstream ticker-news-API behavior. Fragments may reference
    ``{asset_label}``, filled in by the caller.
    See specs/pipeline-consumption-v2.md.
    """
    ticker_source = _validate_arm("ticker_source", ticker_source)
    if ticker_source == "brief":
        return [get_ticker_brief], (
            "get_ticker_brief(ticker, curr_date) for the pre-compiled daily research "
            "brief on this {asset_label} covering the trailing 48h of developments, "
            "catalysts, supply chain, institutional views, and risks — ground ALL "
            "{asset_label}-specific commentary in this brief and cite its as-of date, "
        )
    return [get_news], (
        "get_news(ticker, start_date, end_date) for {asset_label}-specific news by ticker symbol, "
    )


def _select_news_tools(macro_source, ticker_source, polymarket_enabled=True):
    """News-analyst tools plus the matching prompt fragment for the given
    macro/ticker arm combination (specs/pipeline-consumption-v2.md).

    Each tool is governed by exactly one switch: get_news iff
    ticker_source=feeds, get_ticker_brief iff ticker_source=brief,
    get_global_news+get_macro_indicators iff macro_source=feeds,
    get_macro_brief iff macro_source=brief, search_news iff either switch is
    feeds (the full brief bundle is offline at analysis time), and
    get_prediction_markets always — behind ``polymarket_enabled``, independent
    of the arms. With feeds/feeds this reproduces the exact pre-change tool
    list and prompt.

    Returns ``(tools, desc)`` where ``desc`` is the "Use the available tools:"
    body; it may reference ``{asset_label}``, filled in by the caller.
    """
    macro_source = _validate_arm("macro_source", macro_source)
    ticker_source = _validate_arm("ticker_source", ticker_source)
    ticker_tools, ticker_desc = _select_ticker_tools(ticker_source)
    macro_tools, macro_desc = _select_macro_tools(macro_source)

    tools = [*ticker_tools, *macro_tools]
    desc = ticker_desc + macro_desc
    if polymarket_enabled:
        desc += (
            "and get_prediction_markets(topic, limit) for live market-implied "
            "probabilities of forward-looking events (e.g. 'Fed rate cut', "
            "'recession 2026', geopolitical or sector events). "
        )
    else:
        # Close the tool enumeration sentence the arm fragments left open.
        # ``removesuffix`` rather than a fixed-width slice: every arm fragment
        # ends with ", " (pinned by test_news_tool_selection), and if one ever
        # stops doing so this degrades to a slightly awkward join instead of
        # silently chopping two arbitrary characters from the prompt.
        desc = desc.removesuffix(", ") + ". "

    if "feeds" in (macro_source, ticker_source):
        tools.append(search_news)
        desc += (
            "Beyond those, use search_news(query, curr_date) to investigate "
            "angles you judge relevant on your own initiative — competitors, "
            "suppliers, customers, regulation, sector dynamics — and follow up "
            "on leads from headlines with refined queries. "
        )
    if polymarket_enabled:
        tools.append(get_prediction_markets)
    if "brief" in (macro_source, ticker_source):
        desc += (
            "When a brief tool returns DATA_UNAVAILABLE, state explicitly in "
            "your report that the brief is unavailable instead of improvising "
            "or substituting other sources. "
        )
    return tools, desc


def create_news_analyst(llm):
    def news_analyst_node(state):
        current_date = state["trade_date"]
        asset_type = state.get("asset_type", "stock")
        asset_label = "company" if asset_type == "stock" else "asset"
        instrument_context = get_instrument_context_from_state(state)

        config = get_config()
        tools, tools_desc = _select_news_tools(
            config.get("macro_source", "feeds"),
            config.get("ticker_source", "feeds"),
            polymarket_enabled=config.get("polymarket_enabled", True),
        )

        system_message = (
            f"You are a news researcher tasked with analyzing recent news and trends over the past week. Please write a comprehensive report of the current state of the world that is relevant for trading and macroeconomics. Use the available tools: {tools_desc.format(asset_label=asset_label)}Provide specific, actionable insights with supporting evidence to help traders make informed decisions."
            + """ Make sure to append a Markdown table at the end of the report to organize key points in the report, organized and easy to read."""
            + get_language_instruction()
        )

        prompt = ChatPromptTemplate.from_messages(
            [
                (
                    "system",
                    "You are a helpful AI assistant, collaborating with other assistants."
                    " Use the provided tools to progress towards answering the question."
                    " If you are unable to fully answer, that's OK; another assistant with different tools"
                    " will help where you left off. Execute what you can to make progress."
                    " If you or any other assistant has the FINAL TRANSACTION PROPOSAL: **BUY/HOLD/SELL** or deliverable,"
                    " prefix your response with FINAL TRANSACTION PROPOSAL: **BUY/HOLD/SELL** so the team knows to stop."
                    " You have access to the following tools: {tool_names}."
                    " Today's date is {current_date}; treat it as 'now' for all analysis and tool-call date ranges. {instrument_context}\n"
                    "{system_message}",
                ),
                MessagesPlaceholder(variable_name="messages"),
            ]
        )

        prompt = prompt.partial(system_message=system_message)
        prompt = prompt.partial(tool_names=", ".join([tool.name for tool in tools]))
        prompt = prompt.partial(current_date=current_date)
        prompt = prompt.partial(instrument_context=instrument_context)

        chain = prompt | llm.bind_tools(tools)
        result = chain.invoke(state["messages"])

        report = ""

        if len(result.tool_calls) == 0:
            report = result.content

        return {
            "messages": [result],
            "news_report": report,
        }

    return news_analyst_node
