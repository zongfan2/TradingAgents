"""search_news: agent-chosen free-form query search (yfinance-backed).

The query is caller-supplied — the news analyst decides what to investigate —
but results still flow through the same UTC window filter as the fixed feeds,
are deduplicated by title, and are capped at ``limit``.
"""
from datetime import datetime, timezone

import pytest

import tradingagents.dataflows.interface as interface
import tradingagents.dataflows.yfinance_news as ynews


def _article(title, date_str):
    return {
        "content": {
            "title": title,
            "summary": f"summary of {title}",
            "provider": {"displayName": "Pub"},
            "canonicalUrl": {"url": f"https://example.com/{title}"},
            "pubDate": f"{date_str}T12:00:00Z",
        }
    }


def _fake_search(monkeypatch, articles):
    captured = {}

    class FakeSearch:
        def __init__(self, query, news_count, enable_fuzzy_query):
            captured["query"] = query
            captured["news_count"] = news_count
            self.news = articles

    monkeypatch.setattr(ynews.yf, "Search", FakeSearch)
    return captured


@pytest.mark.unit
def test_search_news_passes_query_and_formats(monkeypatch):
    captured = _fake_search(
        monkeypatch,
        [_article("A", "2026-07-20"), _article("B", "2026-07-21")],
    )
    result = ynews.search_news_yfinance("nvda supply chain", "2026-07-22", 7, 10)
    assert captured["query"] == "nvda supply chain"
    assert "News search results for 'nvda supply chain'" in result
    assert "### A" in result and "### B" in result


@pytest.mark.unit
def test_search_news_blocks_lookahead(monkeypatch):
    # Historical run: an article published after curr_date must not leak in.
    _fake_search(
        monkeypatch,
        [_article("inside", "2026-07-20"), _article("future", "2026-07-25")],
    )
    result = ynews.search_news_yfinance("q", "2026-07-22", 7, 10)
    assert "### inside" in result
    assert "future" not in result


@pytest.mark.unit
def test_search_news_dedupes_and_caps(monkeypatch):
    _fake_search(
        monkeypatch,
        [_article("same", "2026-07-20"), _article("same", "2026-07-20")]
        + [_article(f"t{i}", "2026-07-20") for i in range(5)],
    )
    result = ynews.search_news_yfinance("q", "2026-07-22", 7, 3)
    assert result.count("### same") == 1
    # limit=3: 'same' plus two of the t* articles, nothing more
    assert sum(result.count(f"### t{i}") for i in range(5)) == 2


@pytest.mark.unit
def test_search_news_empty_result_message(monkeypatch):
    _fake_search(monkeypatch, [])
    result = ynews.search_news_yfinance("obscure query", "2026-07-22", 7, 10)
    assert "No news found for query 'obscure query'" in result


@pytest.mark.unit
def test_search_news_registered_for_routing(monkeypatch):
    # Registered in the vendor registry under news_data, so route_to_vendor
    # resolves it with the default config (news_data -> yfinance).
    assert interface.get_category_for_method("search_news") == "news_data"
    _fake_search(monkeypatch, [_article("routed", "2026-07-20")])
    result = interface.route_to_vendor("search_news", "q", "2026-07-22", 7, 10)
    assert "### routed" in result


@pytest.mark.unit
def test_search_news_tool_exposed():
    from tradingagents.agents.utils.news_data_tools import search_news

    assert search_news.name == "search_news"
    args = search_news.args
    assert "query" in args and "curr_date" in args


@pytest.mark.unit
def test_undated_article_kept_in_live_window(monkeypatch):
    # A window ending "now" may keep undated articles (cannot be future news).
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    undated = {"title": "live", "publisher": "P", "link": "l"}
    _fake_search(monkeypatch, [undated])
    result = ynews.search_news_yfinance("q", today, 7, 10)
    assert "### live" in result
