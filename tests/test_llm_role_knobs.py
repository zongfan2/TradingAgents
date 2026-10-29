"""Per-role LLM knobs in the graph and clients — specs/llm-cost.md R3/R4."""

import pytest

# ---------------------------------------------------------------------------
# R3 / R4 — per-role knobs in the graph and clients
# ---------------------------------------------------------------------------


def _graph_with(config):
    from tradingagents.graph.trading_graph import TradingAgentsGraph

    graph = TradingAgentsGraph.__new__(TradingAgentsGraph)
    graph.config = config
    return graph


@pytest.mark.unit
def test_provider_kwargs_are_per_role():
    graph = _graph_with({"llm_provider": "anthropic", "anthropic_effort": "high",
                         "anthropic_deep_effort": "medium", "anthropic_quick_effort": "low"})
    assert graph._get_provider_kwargs("deep") == {"effort": "medium"}
    assert graph._get_provider_kwargs("quick") == {"effort": "low"}
    shared = _graph_with({"llm_provider": "anthropic", "anthropic_effort": "high"})
    assert shared._get_provider_kwargs("quick") == {"effort": "high"}
    deepseek = _graph_with({"llm_provider": "deepseek", "deepseek_quick_thinking": "disabled"})
    assert deepseek._get_provider_kwargs("quick") == {"extra_body": {"thinking": {"type": "disabled"}}}
    assert deepseek._get_provider_kwargs("deep") == {}
    assert _graph_with({"llm_provider": "deepseek"})._get_provider_kwargs("quick") == {}


@pytest.mark.unit
def test_deepseek_quick_thinking_env_choice(monkeypatch):
    from tradingagents.default_config import _apply_env_overrides

    config = _apply_env_overrides({"deepseek_quick_thinking": None},
                                  {"TRADINGAGENTS_DEEPSEEK_QUICK_THINKING": " Disabled "})
    assert config["deepseek_quick_thinking"] == "disabled"
    with pytest.raises(ValueError):
        _apply_env_overrides({"deepseek_quick_thinking": None},
                             {"TRADINGAGENTS_DEEPSEEK_QUICK_THINKING": "off"})


@pytest.mark.unit
def test_extra_body_reaches_the_deepseek_client():
    from tradingagents.llm_clients.factory import create_llm_client

    llm = create_llm_client(provider="deepseek", model="deepseek-flash", api_key="sk-test",
                            extra_body={"thinking": {"type": "disabled"}}).get_llm()
    assert llm.extra_body == {"thinking": {"type": "disabled"}}


@pytest.mark.unit
def test_claude_5_5_structured_output_avoids_forced_tool_choice(monkeypatch):
    from langchain_anthropic import ChatAnthropic

    from tradingagents.llm_clients.anthropic_client import AnthropicClient, _supports_effort

    seen = []
    monkeypatch.setattr(
        ChatAnthropic, "with_structured_output",
        lambda self, schema, include_raw=False, **kw: seen.append((self.model, kw.get("method"))),
    )
    for model in ("claude-opus-5-5", "claude-sonnet-5-5", "claude-opus-4-8", "claude-haiku-5-5"):
        AnthropicClient(model, api_key="sk-test").get_llm().with_structured_output(dict)
    assert seen == [
        ("claude-opus-5-5", "json_schema"), ("claude-sonnet-5-5", "json_schema"),
        ("claude-opus-4-8", None), ("claude-haiku-5-5", None),
    ]
    assert _supports_effort("claude-haiku-5-5") and not _supports_effort("claude-haiku-4-5")
    haiku = AnthropicClient("claude-haiku-5-5", api_key="sk-test", effort="low").get_llm()
    assert haiku.effort == "low"
