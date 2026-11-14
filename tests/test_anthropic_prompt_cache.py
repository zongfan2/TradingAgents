"""Automatic prompt caching for the analysts' tool loops on Anthropic (specs/llm-cost.md R12)."""

import pytest

from pipeline import analysis_runner as ar
from pipeline.llm_routing import CLAUDE, DEEPSEEK, OPENAI
from tradingagents.llm_clients import anthropic_client as mod


@pytest.mark.unit
def test_the_client_sets_top_level_cache_control_only_when_asked(monkeypatch):
    captured = []
    monkeypatch.setattr(mod, "NormalizedChatAnthropic", lambda **kwargs: captured.append(kwargs))
    mod.AnthropicClient("claude-haiku-5-5", prompt_cache=True).get_llm()
    mod.AnthropicClient("claude-opus-5-5").get_llm()
    assert captured[0]["model_kwargs"] == {"cache_control": {"type": "ephemeral"}}
    assert "model_kwargs" not in captured[1] and "prompt_cache" not in captured[0]


@pytest.mark.unit
def test_the_request_payload_carries_cache_control(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    llm = mod.AnthropicClient("claude-haiku-5-5", prompt_cache=True).get_llm()
    payload = llm._get_request_payload([("system", "You are an analyst."), ("human", "Analyse NVDA.")])
    assert payload["cache_control"] == {"type": "ephemeral"}
    plain = mod.AnthropicClient("claude-haiku-5-5").get_llm()
    assert "cache_control" not in plain._get_request_payload([("human", "Analyse NVDA.")])


def _built_graph(monkeypatch, tmp_path, **config):
    from tradingagents.default_config import DEFAULT_CONFIG
    from tradingagents.graph import trading_graph as tg

    built = []

    class FakeClient:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def get_llm(self):
            built.append(self.kwargs)
            return f"llm-{len(built)}"

    monkeypatch.setattr(tg, "create_llm_client", lambda **kwargs: FakeClient(**kwargs))
    class _Workflow:
        def compile(self, **kwargs):
            return None

    monkeypatch.setattr(tg.GraphSetup, "setup_graph", lambda self, *a, **k: _Workflow())
    cfg = {**DEFAULT_CONFIG, "llm_provider": "anthropic", "deep_think_llm": "claude-opus-5-5",
           "quick_think_llm": "claude-haiku-5-5", "research_manager_llm": "claude-sonnet-5-5",
           "anthropic_deep_effort": "medium", "anthropic_quick_effort": "low",
           "results_dir": str(tmp_path / "r"), "data_cache_dir": str(tmp_path / "c"),
           "memory_log_path": str(tmp_path / "m.md"), **config}
    graph = tg.TradingAgentsGraph(config=cfg)
    return graph, built


@pytest.mark.unit
def test_only_the_analysts_tool_loops_get_the_cache(monkeypatch, tmp_path):
    graph, built = _built_graph(monkeypatch, tmp_path, anthropic_quick_prompt_cache=True)
    cached = [k for k in built if k.get("prompt_cache")]
    assert len(cached) == 1 and cached[0]["model"] == "claude-haiku-5-5"
    assert graph.analyst_llm != graph.quick_thinking_llm  # researchers, debaters, trader: no marker
    assert graph.graph_setup.analyst_llm == graph.analyst_llm
    assert graph.graph_setup.quick_thinking_llm == graph.quick_thinking_llm
    assert all(not k.get("prompt_cache") for k in built if k["model"] != "claude-haiku-5-5")


@pytest.mark.unit
def test_without_the_flag_the_analysts_share_the_quick_model(monkeypatch, tmp_path):
    graph, built = _built_graph(monkeypatch, tmp_path)
    assert not any(k.get("prompt_cache") for k in built) and graph.analyst_llm == graph.quick_thinking_llm
    graph, built = _built_graph(monkeypatch, tmp_path, llm_provider="openai", anthropic_quick_prompt_cache=True,
                                deep_think_llm="gpt-6.1-sol", quick_think_llm="gpt-6-luna")
    assert not any(k.get("prompt_cache") for k in built)


@pytest.mark.unit
def test_the_claude_profile_turns_it_on_and_the_others_clear_it():
    from tradingagents.default_config import DEFAULT_CONFIG, _apply_env_overrides

    assert DEFAULT_CONFIG["anthropic_quick_prompt_cache"] is False  # upstream default: off
    assert _apply_env_overrides({"anthropic_quick_prompt_cache": False}, CLAUDE.env)["anthropic_quick_prompt_cache"] is True
    for profile in (OPENAI, DEEPSEEK):
        assert profile.env["TRADINGAGENTS_ANTHROPIC_QUICK_PROMPT_CACHE"] == ""


@pytest.mark.unit
def test_caching_never_changes_the_analysis_config_digest():
    # Caching changes billing, never what the model sees: runs stay comparable.
    assert "anthropic_quick_prompt_cache" not in ar._GRAPH_CONFIG_KEYS


@pytest.mark.unit
def test_a_cache_write_reported_by_ttl_is_billed_as_a_write():
    from langchain_core.messages import AIMessage

    from pipeline.llm_usage import _tokens_from_message, call_cost

    message = AIMessage(content="OK", usage_metadata={
        "input_tokens": 2656, "output_tokens": 4, "total_tokens": 2660,
        "input_token_details": {"cache_read": 0, "cache_creation": 0, "ephemeral_5m_input_tokens": 2652,
                                "ephemeral_1h_input_tokens": 0}},
        response_metadata={"model_name": "claude-haiku-5-5"})
    model, tokens = _tokens_from_message(message)
    assert tokens["cache_write"] == 2652 and tokens["input"] == 2656
    read = {**tokens, "cache_write": 0, "cache_read": 2652}
    assert call_cost(model, tokens) > call_cost(model, read)  # a write costs more than a read


@pytest.mark.unit
def test_a_long_haiku_cache_write_is_never_billed_below_input():
    from pipeline.llm_usage import call_cost

    plain = call_cost("claude-haiku-5-5", {"input": 120_000, "cache_write": 0, "cache_read": 0, "output": 0})
    write = call_cost("claude-haiku-5-5", {"input": 120_000, "cache_write": 120_000, "cache_read": 0, "output": 0})
    assert write == pytest.approx(0.075) and write > plain


@pytest.mark.unit
def test_the_graph_hands_the_cached_model_to_the_four_analysts_only(monkeypatch):
    from tradingagents.graph import setup as gs

    got = {}

    def factory(name):
        def build(llm, *a, **k):
            got[name] = llm
            return lambda state: state
        return build

    for name in ("create_market_analyst", "create_sentiment_analyst", "create_news_analyst",
                 "create_fundamentals_analyst", "create_bull_researcher", "create_bear_researcher",
                 "create_trader", "create_aggressive_debator", "create_neutral_debator",
                 "create_conservative_debator", "create_research_manager", "create_portfolio_manager"):
        if hasattr(gs, name):
            monkeypatch.setattr(gs, name, factory(name))
    from types import SimpleNamespace

    quick, analyst, deep = object(), object(), object()
    keys = ("market", "social", "news", "fundamentals")
    logic = SimpleNamespace(**{f"should_continue_{k}": (lambda state: "x") for k in keys},
                            should_continue_debate=lambda s: "x", should_continue_risk_analysis=lambda s: "x")
    setup = gs.GraphSetup(quick, deep, tool_nodes={k: (lambda state: state) for k in keys},
                          conditional_logic=logic, analyst_llm=analyst)
    setup.setup_graph()
    analysts = {"create_market_analyst", "create_sentiment_analyst", "create_news_analyst",
                "create_fundamentals_analyst"}
    assert {n for n in analysts if n in got} == analysts and all(got[n] is analyst for n in analysts)
    others = [n for n in got if n not in analysts]
    assert others and all(got[n] is not analyst for n in others)
