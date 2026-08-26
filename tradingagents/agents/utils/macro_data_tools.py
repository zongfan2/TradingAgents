from typing import Annotated

from langchain_core.tools import tool

from tradingagents.dataflows.interface import route_to_vendor
from tradingagents.dataflows.symbol_utils import session_for_ticker


@tool
def get_macro_indicators(
    indicator: Annotated[
        str,
        "Macro indicator: a friendly alias such as 'cpi', 'core_pce', "
        "'unemployment', 'fed_funds_rate', '10y_treasury', 'yield_curve', "
        "'real_gdp', 'vix', or a raw FRED series ID such as 'CPIAUCSL'.",
    ],
    curr_date: Annotated[str, "Current date in yyyy-mm-dd format; the end of the window"],
    look_back_days: Annotated[
        int | None, "Trailing window length in days; omit for a 1-year window"
    ] = None,
) -> str:
    """
    Retrieve a macroeconomic indicator time series from FRED (Federal Reserve
    Economic Data): policy rates, Treasury yields, inflation, labor, and growth.
    Returns the series title, units, frequency, the latest value, the change
    over the window, and a recent observation table. Uses the configured
    macro_data vendor.

    Args:
        indicator (str): Friendly alias or raw FRED series ID
        curr_date (str): Current date in yyyy-mm-dd format
        look_back_days (int): Trailing window length; omit for a 1-year window

    Returns:
        str: A formatted markdown report of the macro series
    """
    return route_to_vendor("get_macro_indicators", indicator, curr_date, look_back_days)


@tool
def get_macro_brief(
    curr_date: Annotated[str, "Current date in yyyy-mm-dd format"],
    ticker: Annotated[
        str | None,
        "Ticker of the instrument under analysis (e.g. 'NVDA', '0700.HK'); "
        "used to pick the matching session's brief. Omit for the US session.",
    ] = None,
) -> str:
    """
    Retrieve the pre-compiled daily macro research brief: a cited, deep-search
    digest covering monetary policy, growth and earnings, geopolitics, global
    liquidity, commodities, and China/Asia, plus a surprises watchlist. Briefs
    are session-scoped (cn/us); pass the ticker under analysis so the matching
    session's brief is served (omitted = US session). The brief states its own
    as-of date; treat that date as the information cutoff.
    Uses the configured macro_brief vendor.

    Args:
        curr_date (str): Current date in yyyy-mm-dd format
        ticker (str): Instrument under analysis; derives the session (omit = us)

    Returns:
        str: The full macro brief in markdown, prefixed with its as-of header
    """
    return route_to_vendor("get_macro_brief", curr_date, session_for_ticker(ticker))


@tool
def get_ticker_brief(
    ticker: Annotated[str, "Ticker symbol, e.g. 'NVDA' or '0700.HK'"],
    curr_date: Annotated[str, "Current date in yyyy-mm-dd format"],
) -> str:
    """
    Retrieve the pre-compiled daily research brief for a single ticker: a
    cited, deep-search digest of the trailing 48h covering company
    developments, catalysts and calendar, supply chain and competitors,
    institutional views, and risks, ending with an impact read. The brief
    states its own as-of date; treat that date as the information cutoff.
    Uses the configured ticker_brief vendor.

    Args:
        ticker (str): Ticker symbol
        curr_date (str): Current date in yyyy-mm-dd format

    Returns:
        str: The full ticker brief in markdown, prefixed with its as-of header
    """
    return route_to_vendor("get_ticker_brief", ticker, curr_date)
