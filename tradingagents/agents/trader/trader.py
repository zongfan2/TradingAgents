"""Trader: turns the Research Manager's investment plan into a concrete transaction proposal.

TradePlan mode (specs/analysis-runner.md "TradePlan production"): when the run
config carries a truthy ``trade_plan_emit`` key, the prompt additionally asks
for a fenced JSON block matching the TradePlan schema from
specs/trade-plan-ledger-contract.md, with levels anchored to the market
analyst's technical report, and optionally injects a position-context block
(``position_context`` config key) carrying the contract's maintain semantics.
Plan mode invokes the LLM as free text on purpose — structured output would
replace the prose+fenced-block shape the analysis runner extracts from.

Guard: neither key is in ``DEFAULT_CONFIG``, so without the analysis runner
setting them the node behaves exactly as before (structured TraderProposal
path, byte-identical prompt).
"""

from __future__ import annotations

import functools

from langchain_core.messages import AIMessage

from tradingagents.agents.schemas import TraderProposal, render_trader_proposal
from tradingagents.agents.utils.agent_utils import (
    get_instrument_context_from_state,
    get_language_instruction,
)
from tradingagents.agents.utils.structured import (
    NO_EXTERNAL_TOOLS,
    bind_structured,
    invoke_structured_or_freetext,
)
from tradingagents.dataflows.config import get_config

#: Schema fields verbatim from specs/trade-plan-ledger-contract.md — the
#: analysis runner parses this block and the deterministic validator checks it.
TRADE_PLAN_INSTRUCTIONS = """

## Required output format: TradePlan JSON

Write your reasoning first and conclude it with the line
'FINAL TRANSACTION PROPOSAL: **BUY/HOLD/SELL**' (pick exactly one). Then output
exactly one fenced ```json code block containing a single JSON object in this
schema (specs/trade-plan-ledger-contract.md):

```json
{
  "action": "BUY",
  "conviction": 0.7,
  "add_intent": false,
  "add_rationale": null,
  "entry_zone": [280.0, 285.0],
  "stop": 268.0,
  "targets": [301.0, 315.0],
  "horizon_days": 10,
  "invalidation": "daily close below the weekly mid-band",
  "sizing": {"risk_pct": 1.0},
  "source_levels": "entry = daily BOLL mid, stop = below daily lower band"
}
```

Rules:
- "action" is one of BUY | SELL | HOLD; "conviction" is a number in [0, 1].
- BUY requires entry_zone, stop, targets, horizon_days, and sizing, with
  stop < entry_zone[0] <= entry_zone[1] < min(targets); the entry zone must sit
  near the last close (within about 10%); the stop distance must be meaningful
  (at least ~0.5 ATR) but no more than ~15% of the entry mid;
  "sizing.risk_pct" in [0.1, 2.0]; "horizon_days" in [1, 30]; 1-2 targets,
  strictly ascending.
- SELL closes an existing long only (no short opening). HOLD carries no
  execution fields — set entry_zone, stop, targets, horizon_days, and sizing
  to null.
- Anchor every price level to concrete levels from the market analyst's
  technical report above, and name the levels you used in "source_levels".
"""

#: Contract maintain semantics (specs/trade-plan-ledger-contract.md): a daily
#: re-analysis loop must not compound "still bullish" into an uncapped position.
POSITION_MAINTAIN_INSTRUCTIONS = (
    'Maintain semantics: a BUY on a held ticker without "add_intent": true is a '
    "maintain and produces NO order. Emit \"add_intent\": true with a non-empty "
    '"add_rationale" naming the genuinely NEW information ONLY when new '
    "information justifies an add-on tranche — persistence of an existing "
    "bullish view is not new information."
)


def render_position_block(position_context: str) -> str:
    """Position-context block injected in TradePlan mode for a held ticker."""
    return (
        "\n\n## Current position (execution ledger snapshot)\n"
        + position_context.strip()
        + "\n\n"
        + POSITION_MAINTAIN_INSTRUCTIONS
    )


def _trade_plan_user_suffix(state: dict, position_context: str | None) -> str:
    """Additive prompt tail for TradePlan mode.

    The market analyst's technical report is included so the plan's levels can
    be anchored to it (analysis-runner spec: "levels explicitly anchored to the
    market analyst's technical report").
    """
    market_report = state.get("market_report") or "(market analyst report unavailable)"
    suffix = (
        "\n\n## Market analyst technical report (anchor your plan levels here)\n"
        + market_report
    )
    if position_context and position_context.strip():
        suffix += render_position_block(position_context)
    suffix += TRADE_PLAN_INSTRUCTIONS
    return suffix


def create_trader(llm):
    structured_llm = bind_structured(llm, TraderProposal, "Trader")

    def trader_node(state, name):
        company_name = state["company_of_interest"]
        instrument_context = get_instrument_context_from_state(state)
        investment_plan = state["investment_plan"]

        config = get_config()
        plan_mode = bool(config.get("trade_plan_emit"))

        user_content = (
            f"Based on a comprehensive analysis by a team of analysts, here is an investment "
            f"plan tailored for {company_name}. {instrument_context} This plan incorporates "
            f"insights from current technical market trends, macroeconomic indicators, and "
            f"social media sentiment. Use this plan as a foundation for evaluating your next "
            f"trading decision.\n\nProposed Investment Plan: {investment_plan}\n\n"
            f"Leverage these insights to make an informed and strategic decision."
        )
        if plan_mode:
            user_content += _trade_plan_user_suffix(state, config.get("position_context"))

        messages = [
            {
                "role": "system",
                "content": (
                    "You are a trading agent analyzing market data to make investment decisions. "
                    "Based on your analysis, provide a specific recommendation to buy, sell, or hold. "
                    "Anchor your reasoning in the analysts' reports and the research plan. "
                    + NO_EXTERNAL_TOOLS
                    + get_language_instruction()
                ),
            },
            {"role": "user", "content": user_content},
        ]

        if plan_mode:
            # Free text on purpose: the structured TraderProposal path would
            # discard the fenced TradePlan JSON block the runner extracts.
            trader_plan = llm.invoke(messages).content
        else:
            trader_plan = invoke_structured_or_freetext(
                structured_llm,
                llm,
                messages,
                render_trader_proposal,
                "Trader",
            )

        return {
            "messages": [AIMessage(content=trader_plan)],
            "trader_investment_plan": trader_plan,
            "sender": name,
        }

    return functools.partial(trader_node, name="Trader")
