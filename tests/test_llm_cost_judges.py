"""Research Manager model and ticker search cap — specs/llm-cost.md R9/R10."""

import json
from datetime import date

import pytest

# ---------------------------------------------------------------------------
# R9 — the Research Manager judges on its own model; the PM keeps the deep one
# ---------------------------------------------------------------------------


class _FakeClient:
    def __init__(self, model, kwargs):
        self.llm = ("llm", model, tuple(sorted(kwargs.items())))

    def get_llm(self):
        return self.llm


def _build_graph(monkeypatch, tmp_path, **overrides):
    import tradingagents.graph.setup as setup_mod
    import tradingagents.graph.trading_graph as tg
    from tradingagents.default_config import DEFAULT_CONFIG

    monkeypatch.setattr(tg, "create_llm_client",
                        lambda provider, model, base_url=None, **kw: _FakeClient(model, kw))
    judges = {}
    real_rm, real_pm = setup_mod.create_research_manager, setup_mod.create_portfolio_manager
    monkeypatch.setattr(setup_mod, "create_research_manager",
                        lambda llm: judges.setdefault("rm", llm) and real_rm(llm))
    monkeypatch.setattr(setup_mod, "create_portfolio_manager",
                        lambda llm: judges.setdefault("pm", llm) and real_pm(llm))
    config = {
        **DEFAULT_CONFIG,
        "llm_provider": "anthropic",
        "deep_think_llm": "claude-opus-5-5",
        "quick_think_llm": "claude-haiku-5-5",
        "anthropic_deep_effort": "medium",
        "anthropic_quick_effort": "low",
        "results_dir": str(tmp_path / "logs"),
        "data_cache_dir": str(tmp_path / "cache"),
        "memory_log_path": str(tmp_path / "memory.md"),
        "checkpoint_enabled": False,
        **overrides,
    }
    graph = tg.TradingAgentsGraph(selected_analysts=["market"], config=config)
    return graph, judges


@pytest.mark.unit
def test_research_manager_gets_its_own_model_with_deep_knobs(monkeypatch, tmp_path):
    graph, judges = _build_graph(monkeypatch, tmp_path, research_manager_llm="claude-sonnet-5-5")
    assert judges["rm"] == ("llm", "claude-sonnet-5-5", (("effort", "medium"),))
    assert judges["pm"] == ("llm", "claude-opus-5-5", (("effort", "medium"),))
    assert graph.deep_thinking_llm == judges["pm"]


@pytest.mark.unit
@pytest.mark.parametrize("rm_model", [None, "", "claude-opus-5-5"])
def test_research_manager_defaults_to_the_deep_model(monkeypatch, tmp_path, rm_model):
    graph, judges = _build_graph(monkeypatch, tmp_path, research_manager_llm=rm_model)
    assert judges["rm"] is judges["pm"] is graph.deep_thinking_llm


@pytest.mark.unit
def test_profiles_and_digest_carry_the_research_manager_model():
    import pipeline.analysis_runner as ar
    from pipeline.llm_routing import CLAUDE, DEEPSEEK
    from tradingagents.default_config import _apply_env_overrides

    assert CLAUDE.env["TRADINGAGENTS_RESEARCH_MANAGER_LLM"] == "claude-sonnet-5-5"
    assert CLAUDE.env["TRADINGAGENTS_DEEP_THINK_LLM"] == "claude-opus-5-5"
    # DeepSeek clears it, so a stray .env value can never send its RM to Claude.
    assert DEEPSEEK.env["TRADINGAGENTS_RESEARCH_MANAGER_LLM"] == ""
    assert ar.effective_analysis_config(CLAUDE.env)["graph"]["research_manager_llm"] == "claude-sonnet-5-5"
    assert ar.effective_analysis_config(DEEPSEEK.env)["graph"]["research_manager_llm"] is None
    config = _apply_env_overrides({"research_manager_llm": None},
                                  {"TRADINGAGENTS_RESEARCH_MANAGER_LLM": "claude-sonnet-5-5"})
    assert config["research_manager_llm"] == "claude-sonnet-5-5"


# ---------------------------------------------------------------------------
# R10 — per-brief web-search cap (env switch) and search counts in the ledger
# ---------------------------------------------------------------------------

_ORIGINAL_PROTOCOL = (
    "Work section by section. For each of the five sections below, run at least two\n"
    "distinct web searches with different angles before writing. Emphasize the\n"
)


def _render(max_searches=None):
    from pipeline.ticker_collector import render_ticker_prompt

    return render_ticker_prompt(date(2026, 11, 2), "AAPL", "us", "claude", "seed",
                                max_searches=max_searches)


@pytest.mark.unit
def test_uncapped_prompt_keeps_the_original_protocol():
    assert _ORIGINAL_PROTOCOL in _render()


@pytest.mark.unit
def test_capped_prompt_states_the_cap():
    text = _render(8)
    assert "Run at most 8 web searches in total for this brief" in text
    assert "at least two" not in text
    assert "{{" not in text


@pytest.mark.unit
@pytest.mark.parametrize(("raw", "cap"), [(None, None), ("", None), (" ", None), ("0", None),
                                          ("8", 8), (" 12 ", 12), ("5", 5)])
def test_collect_max_searches_parses(raw, cap):
    from pipeline.config import COLLECT_MAX_SEARCHES_ENV, collect_max_searches

    env = {} if raw is None else {COLLECT_MAX_SEARCHES_ENV: raw}
    assert collect_max_searches(env) == cap


@pytest.mark.unit
@pytest.mark.parametrize("raw", ["eight", "8.5", "4", "-1"])
def test_collect_max_searches_is_loud_on_junk(raw):
    from pipeline.config import COLLECT_MAX_SEARCHES_ENV, collect_max_searches

    with pytest.raises(ValueError, match=COLLECT_MAX_SEARCHES_ENV):
        collect_max_searches({COLLECT_MAX_SEARCHES_ENV: raw})


@pytest.mark.unit
def test_collector_passes_the_cap_to_every_job(monkeypatch, tmp_path):
    import pipeline.ticker_collector as tc

    seen = []
    monkeypatch.setenv("TRADINGAGENTS_COLLECT_MAX_SEARCHES", "8")
    monkeypatch.setattr(tc, "render_ticker_prompt",
                        lambda *a, max_searches=None, **k: seen.append(max_searches) or "prompt")
    monkeypatch.setattr(tc, "_generate_validated",
                        lambda *a, **k: (_ for _ in ()).throw(tc.CollectorError("stop")))
    jobs = [tc.TickerJob("AAPL"), tc.TickerJob("MSFT", "earnings", "dated print")]
    monkeypatch.setattr(tc, "resolve_jobs", lambda *a, **k: (jobs, None))
    monkeypatch.setattr(tc, "_emit_collect_funnel", lambda *a, **k: None)
    summary = tc.collect_all("us", "2026-11-02", backend="claude", runner=lambda *a, **k: "",
                             brief_dir=tmp_path / "briefs", pool_dir=tmp_path / "pool")
    assert seen == [8, 8]
    assert [o.outcome for o in summary.outcomes] == ["failed", "failed"]


@pytest.mark.unit
def test_collector_rejects_a_junk_cap_before_any_job(monkeypatch, tmp_path):
    import pipeline.ticker_collector as tc

    monkeypatch.setenv("TRADINGAGENTS_COLLECT_MAX_SEARCHES", "lots")
    monkeypatch.setattr(tc, "resolve_jobs", lambda *a, **k: pytest.fail("resolved jobs"))
    with pytest.raises(tc.CollectorError, match="TRADINGAGENTS_COLLECT_MAX_SEARCHES"):
        tc.collect_all("us", "2026-11-02", backend="claude", runner=lambda *a, **k: "",
                       brief_dir=tmp_path / "briefs", pool_dir=tmp_path / "pool")


@pytest.mark.unit
@pytest.mark.parametrize(("server_tool_use", "searches", "fetches"), [
    ({"web_search_requests": 7, "web_fetch_requests": 2}, 7, 2),
    ({"web_search_requests": "junk"}, 0, 0),
    ("not-a-dict", 0, 0),
    (None, 0, 0),
])
def test_usage_line_records_server_tool_requests(monkeypatch, tmp_path, server_tool_use, searches, fetches):
    import pipeline.claude_cli as cc

    log = tmp_path / "claude_usage.jsonl"
    monkeypatch.setattr(cc, "usage_log_path", lambda: log)
    usage = {"input_tokens": 10, "output_tokens": 5}
    if server_tool_use is not None:
        usage["server_tool_use"] = server_tool_use
    cc.record_usage("ticker_collector", "claude-haiku-5-5", "low",
                    {"usage": usage, "total_cost_usd": 0.01})
    line = json.loads(log.read_text().splitlines()[-1])
    assert (line["web_search_requests"], line["web_fetch_requests"]) == (searches, fetches)
