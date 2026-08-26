"""Trader TradePlan mode (specs/analysis-runner.md "TradePlan production").

The extension is additive and config-gated: with neither ``trade_plan_emit``
nor ``position_context`` set (they are NOT in ``DEFAULT_CONFIG``), the node
must behave exactly as before — structured TraderProposal path, byte-identical
prompt. That no-regression is pinned here; plan mode itself must emit the
fenced-TradePlan instructions, include the market analyst's report for level
anchoring, and inject the position block with the contract's maintain
semantics only when a position context is supplied.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from tradingagents.agents.schemas import TraderAction, TraderProposal
from tradingagents.agents.trader.trader import create_trader
from tradingagents.agents.utils.agent_utils import get_language_instruction
from tradingagents.agents.utils.structured import NO_EXTERNAL_TOOLS
from tradingagents.dataflows.config import set_config


def _state():
    return {
        "company_of_interest": "NVDA",
        "investment_plan": "**Recommendation**: Buy\n**Rationale**: ...",
        "market_report": "Daily BOLL mid 280.0, lower band 268.0, weekly mid 262.0.",
    }


def _plan_mode_llm(captured: dict, reply: str = "free text with plan"):
    """LLM whose plain ``invoke`` records the prompt (plan mode is free-text)."""
    structured = MagicMock()
    llm = MagicMock()
    llm.with_structured_output.return_value = structured
    llm.invoke.side_effect = lambda prompt: (
        captured.__setitem__("prompt", prompt) or SimpleNamespace(content=reply)
    )
    return llm, structured


def _prompt_text(prompt) -> str:
    return "\n".join(m["content"] for m in prompt)


# ---------------------------------------------------------------------------
# No-regression pin (guard required by the runner change set)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_default_config_prompt_is_byte_identical_to_upstream():
    """Without trade_plan_emit, the prompt is EXACTLY the pre-change text and
    the structured path is used — the runner extension leaves defaults alone."""
    captured = {}
    structured = MagicMock()
    structured.invoke.side_effect = lambda prompt: (
        captured.__setitem__("prompt", prompt)
        or TraderProposal(action=TraderAction.BUY, reasoning="x")
    )
    llm = MagicMock()
    llm.with_structured_output.return_value = structured

    state = _state()
    result = create_trader(llm)(state)

    from tradingagents.agents.utils.agent_utils import get_instrument_context_from_state

    instrument_context = get_instrument_context_from_state(state)
    expected_system = (
        "You are a trading agent analyzing market data to make investment decisions. "
        "Based on your analysis, provide a specific recommendation to buy, sell, or hold. "
        "Anchor your reasoning in the analysts' reports and the research plan. "
        + NO_EXTERNAL_TOOLS
        + get_language_instruction()
    )
    expected_user = (
        f"Based on a comprehensive analysis by a team of analysts, here is an investment "
        f"plan tailored for NVDA. {instrument_context} This plan incorporates "
        f"insights from current technical market trends, macroeconomic indicators, and "
        f"social media sentiment. Use this plan as a foundation for evaluating your next "
        f"trading decision.\n\nProposed Investment Plan: {state['investment_plan']}\n\n"
        f"Leverage these insights to make an informed and strategic decision."
    )
    assert captured["prompt"][0] == {"role": "system", "content": expected_system}
    assert captured["prompt"][1] == {"role": "user", "content": expected_user}
    # No plan-mode markers leak into the default prompt.
    text = _prompt_text(captured["prompt"])
    assert "TradePlan" not in text
    assert "Current position" not in text
    # The structured render path produced the output (not free text).
    assert "**Action**: Buy" in result["trader_investment_plan"]
    llm.invoke.assert_not_called()


# ---------------------------------------------------------------------------
# Plan mode
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_plan_mode_uses_free_text_and_emits_schema_instructions():
    set_config({"trade_plan_emit": True})
    captured = {}
    llm, structured = _plan_mode_llm(captured)

    result = create_trader(llm)(_state())

    # Free text on purpose — the structured path would strip the fenced block.
    structured.invoke.assert_not_called()
    assert result["trader_investment_plan"] == "free text with plan"

    text = _prompt_text(captured["prompt"])
    # Schema fields verbatim from the ledger contract.
    for field in (
        '"action"', '"conviction"', '"add_intent"', '"add_rationale"',
        '"entry_zone"', '"stop"', '"targets"', '"horizon_days"',
        '"invalidation"', '"sizing"', '"risk_pct"', '"source_levels"',
    ):
        assert field in text, f"schema field {field} missing from plan-mode prompt"
    assert "FINAL TRANSACTION PROPOSAL: **BUY/HOLD/SELL**" in text
    assert "```json" in text
    # Levels anchored to the market analyst's technical report (included).
    assert "Market analyst technical report" in text
    assert "Daily BOLL mid 280.0" in text
    # No position context supplied — no position block, no maintain semantics.
    assert "Current position" not in text
    assert "add-on tranche" not in text


@pytest.mark.unit
def test_plan_mode_injects_position_block_with_maintain_semantics():
    set_config(
        {
            "trade_plan_emit": True,
            "position_context": "- qty: 12\n- avg_entry: 283.6\n- unrealized_pl: 114.7",
        }
    )
    captured = {}
    llm, _structured = _plan_mode_llm(captured)
    create_trader(llm)(_state())

    text = _prompt_text(captured["prompt"])
    assert "## Current position (execution ledger snapshot)" in text
    assert "- qty: 12" in text
    assert "- avg_entry: 283.6" in text
    # Contract maintain semantics: BUY without add_intent is a maintain.
    assert '"add_intent": true is a maintain and produces NO order' in text
    assert '"add_rationale"' in text
    assert "not new information" in text


@pytest.mark.unit
def test_plan_mode_handles_missing_market_report_and_blank_context():
    set_config({"trade_plan_emit": True, "position_context": "   "})
    captured = {}
    llm, _structured = _plan_mode_llm(captured)
    state = _state()
    del state["market_report"]
    create_trader(llm)(state)
    text = _prompt_text(captured["prompt"])
    assert "(market analyst report unavailable)" in text
    assert "Current position" not in text  # blank context injects nothing
