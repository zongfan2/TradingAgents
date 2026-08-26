"""Ticker brief pipeline side (specs/pipeline-consumption-v2.md).

The local vendor reads the newest per-ticker brief dated on or before the
analysis date, warns at a 1-day gap, treats a 2+-day gap as absent, and
degrades to the optional-category sentinel when nothing qualifies
(specs/ticker-brief-data-contract.md).
"""
import pytest

import tradingagents.dataflows.interface as interface
import tradingagents.dataflows.ticker_brief as tb
from tradingagents.dataflows.errors import VendorNotConfiguredError
from tradingagents.dataflows.symbol_utils import session_for_ticker


@pytest.fixture
def brief_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(tb, "get_config", lambda: {"ticker_brief_dir": str(tmp_path)})
    return tmp_path


def _write(brief_dir, ticker, date_str, body=None):
    ticker_dir = brief_dir / ticker
    ticker_dir.mkdir(exist_ok=True)
    body = body or f"## Company Developments (48h)\n{ticker} {date_str} content"
    (ticker_dir / f"{date_str}.md").write_text(
        f"---\nas_of_date: {date_str}\nticker: {ticker}\n---\n\n{body}",
        encoding="utf-8",
    )


@pytest.mark.unit
def test_picks_newest_on_or_before_date(brief_dir):
    _write(brief_dir, "NVDA", "2026-08-02")
    _write(brief_dir, "NVDA", "2026-08-03")
    _write(brief_dir, "NVDA", "2026-08-05")  # future relative to analysis date
    result = tb.get_ticker_brief_local("NVDA", "2026-08-03")
    assert "as of 2026-08-03" in result
    assert "NVDA 2026-08-03 content" in result


@pytest.mark.unit
def test_same_day_brief_no_warning(brief_dir):
    _write(brief_dir, "NVDA", "2026-08-03")
    result = tb.get_ticker_brief_local("NVDA", "2026-08-03")
    assert "WARNING" not in result


@pytest.mark.unit
def test_one_day_gap_warns(brief_dir):
    _write(brief_dir, "NVDA", "2026-08-02")
    result = tb.get_ticker_brief_local("NVDA", "2026-08-03")
    assert "WARNING" in result
    assert "1 day(s) older" in result
    assert "as of 2026-08-02" in result


@pytest.mark.unit
def test_two_day_gap_is_absent(brief_dir):
    # Ticker news decays faster than macro: 2+ days => absent, not stale-warn.
    _write(brief_dir, "NVDA", "2026-08-01")
    with pytest.raises(VendorNotConfiguredError, match="treated as absent"):
        tb.get_ticker_brief_local("NVDA", "2026-08-03")


@pytest.mark.unit
def test_future_only_briefs_raise(brief_dir):
    _write(brief_dir, "NVDA", "2026-08-05")
    with pytest.raises(VendorNotConfiguredError):
        tb.get_ticker_brief_local("NVDA", "2026-08-03")


@pytest.mark.unit
def test_missing_ticker_dir_raises_with_instructions(brief_dir):
    _write(brief_dir, "AAPL", "2026-08-03")  # other ticker only
    with pytest.raises(VendorNotConfiguredError, match="collector"):
        tb.get_ticker_brief_local("NVDA", "2026-08-03")


@pytest.mark.unit
def test_ignores_non_brief_files(brief_dir):
    ticker_dir = brief_dir / "NVDA"
    ticker_dir.mkdir()
    (ticker_dir / "2026-08-03.eval.json").write_text("{}", encoding="utf-8")
    (ticker_dir / "collector.log").write_text("", encoding="utf-8")
    with pytest.raises(VendorNotConfiguredError):
        tb.get_ticker_brief_local("NVDA", "2026-08-03")


@pytest.mark.unit
def test_ticker_normalized_before_lookup(brief_dir):
    _write(brief_dir, "0700.HK", "2026-08-03")
    result = tb.get_ticker_brief_local(" 0700.hk ", "2026-08-03")
    assert "Ticker brief for 0700.HK" in result


@pytest.mark.unit
def test_path_traversal_ticker_rejected(brief_dir):
    with pytest.raises(ValueError):
        tb.get_ticker_brief_local("../evil", "2026-08-03")


@pytest.mark.unit
def test_resolve_path_matches_served_brief(brief_dir):
    # The resolver shares the reader's selection core, so eval lookups bind
    # to the exact revision the reader serves (revision binding).
    _write(brief_dir, "NVDA", "2026-08-02")
    _write(brief_dir, "NVDA", "2026-08-03")
    path = tb.resolve_ticker_brief_path(" nvda ", "2026-08-03")
    assert path == str(brief_dir / "NVDA" / "2026-08-03.md")
    served = tb.get_ticker_brief_local("NVDA", "2026-08-03")
    assert (brief_dir / "NVDA" / "2026-08-03.md").read_text(encoding="utf-8") in served


@pytest.mark.unit
def test_resolve_path_applies_absent_rule(brief_dir):
    # The resolver feeds gating/eval lookups, so it applies the same >= 2-day
    # absent rule as the reader — it never exposes a path the reader would
    # refuse to serve.
    _write(brief_dir, "NVDA", "2026-08-01")
    with pytest.raises(VendorNotConfiguredError, match="treated as absent"):
        tb.resolve_ticker_brief_path("NVDA", "2026-08-03")


@pytest.mark.unit
def test_resolve_path_raises_when_no_brief(brief_dir):
    with pytest.raises(VendorNotConfiguredError):
        tb.resolve_ticker_brief_path("NVDA", "2026-08-03")


@pytest.mark.unit
def test_routing_registered_and_degrades_when_missing(brief_dir):
    assert interface.get_category_for_method("get_ticker_brief") == "ticker_brief"
    assert "ticker_brief" in interface.OPTIONAL_CATEGORIES
    # Empty dir -> optional category degrades to sentinel instead of raising.
    result = interface.route_to_vendor("get_ticker_brief", "NVDA", "2026-08-03")
    assert result.startswith("DATA_UNAVAILABLE")


@pytest.mark.unit
def test_routing_serves_brief(brief_dir):
    _write(brief_dir, "NVDA", "2026-08-03")
    result = interface.route_to_vendor("get_ticker_brief", "NVDA", "2026-08-03")
    assert "as of 2026-08-03" in result


@pytest.mark.unit
def test_tool_exposed():
    from tradingagents.agents.utils.macro_data_tools import get_ticker_brief

    assert get_ticker_brief.name == "get_ticker_brief"
    assert "ticker" in get_ticker_brief.args
    assert "curr_date" in get_ticker_brief.args


@pytest.mark.unit
def test_macro_brief_tool_gains_optional_ticker(tmp_path, monkeypatch):
    import tradingagents.dataflows.macro_brief as mb
    from tradingagents.agents.utils.macro_data_tools import get_macro_brief

    assert "curr_date" in get_macro_brief.args
    assert "ticker" in get_macro_brief.args

    monkeypatch.setattr(mb, "get_config", lambda: {"macro_brief_dir": str(tmp_path)})
    (tmp_path / "2026-08-03.cn.md").write_text("cn body", encoding="utf-8")
    (tmp_path / "2026-08-03.us.md").write_text("us body", encoding="utf-8")
    # The ticker derives the session; omitted => us (existing invocations stay valid).
    assert "cn body" in get_macro_brief.invoke(
        {"curr_date": "2026-08-03", "ticker": "600519.SS"}
    )
    assert "us body" in get_macro_brief.invoke(
        {"curr_date": "2026-08-03", "ticker": "NVDA"}
    )
    assert "us body" in get_macro_brief.invoke({"curr_date": "2026-08-03"})


@pytest.mark.unit
@pytest.mark.parametrize(
    "ticker,session",
    [
        ("600519.SS", "cn"),
        ("000001.SZ", "cn"),
        ("0700.HK", "cn"),
        ("0700.hk", "cn"),
        ("NVDA", "us"),
        ("BRK-B", "us"),
        ("SPY", "us"),
        ("", "us"),
        (None, "us"),
    ],
)
def test_session_for_ticker(ticker, session):
    assert session_for_ticker(ticker) == session


@pytest.mark.unit
def test_default_config_wires_ticker_brief():
    from tradingagents.default_config import DEFAULT_CONFIG

    assert DEFAULT_CONFIG["ticker_source"] == "feeds"
    assert DEFAULT_CONFIG["polymarket_enabled"] is True
    assert DEFAULT_CONFIG["data_vendors"]["ticker_brief"] == "local"
    assert DEFAULT_CONFIG["ticker_brief_dir"]
