"""Quick-role automatic prompt caching on Anthropic (specs/llm-cost.md R12)."""

import pytest

from pipeline import analysis_runner as ar
from pipeline.llm_routing import CLAUDE, DEEPSEEK, OPENAI
from tradingagents.graph.trading_graph import TradingAgentsGraph
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


@pytest.mark.unit
def test_only_the_quick_role_gets_the_cache():
    g = TradingAgentsGraph.__new__(TradingAgentsGraph)
    g.config = {"llm_provider": "anthropic", "anthropic_deep_effort": "medium", "anthropic_quick_effort": "low",
                "anthropic_quick_prompt_cache": True}
    assert g._get_provider_kwargs("quick") == {"effort": "low", "prompt_cache": True}
    assert g._get_provider_kwargs("deep") == {"effort": "medium"}  # one-shot judges: no cache write
    g.config["anthropic_quick_prompt_cache"] = False
    assert g._get_provider_kwargs("quick") == {"effort": "low"}
    g.config = {"llm_provider": "openai", "anthropic_quick_prompt_cache": True}
    assert "prompt_cache" not in g._get_provider_kwargs("quick")


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
