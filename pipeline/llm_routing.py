"""Analysis LLM profiles and Claude-first routing (specs/llm-cost.md R2).

A profile is the set of ``TRADINGAGENTS_*`` variables a ``run_one`` child
needs to build its graph on one provider; the runner overlays it on the
child's environment (``run_one`` loads ``.env`` with setdefault semantics,
so the profile wins) and records the profile name as the ledger ``preset``.

Routing is decided per run, never mid-graph (one decision = one provider):

- ``claude``     — always the Claude profile;
- ``deepseek``   — always the DeepSeek profile;
- ``claude-first`` — Claude while this calendar month's (UTC) Anthropic
  spend is below the budget and no credit-exhausted marker exists, then
  DeepSeek until the next month.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from pipeline.llm_usage import exhausted_marker, month_spend

ROUTING_CHOICES = ("claude-first", "claude", "deepseek")
DEFAULT_ANTHROPIC_MONTHLY_BUDGET_USD = 190.0


@dataclass(frozen=True)
class Profile:
    name: str
    provider: str
    env: dict[str, str] = field(default_factory=dict)


#: User decision 2026-10-29: Haiku 5.5 (low) for the quick roles, Opus 5.5
#: (medium) for the two judging roles.
CLAUDE = Profile(
    "claude",
    "anthropic",
    {
        "TRADINGAGENTS_LLM_PROVIDER": "anthropic",
        "TRADINGAGENTS_DEEP_THINK_LLM": "claude-opus-5-5",
        "TRADINGAGENTS_QUICK_THINK_LLM": "claude-haiku-5-5",
        "TRADINGAGENTS_ANTHROPIC_DEEP_EFFORT": "medium",
        "TRADINGAGENTS_ANTHROPIC_QUICK_EFFORT": "low",
    },
)

#: deepseek-flash is the current name (v4-flash is a retired alias billed
#: as Flash); the quick role runs without thinking (R4).
DEEPSEEK = Profile(
    "deepseek",
    "deepseek",
    {
        "TRADINGAGENTS_LLM_PROVIDER": "deepseek",
        "TRADINGAGENTS_DEEP_THINK_LLM": "deepseek-v4-pro",
        "TRADINGAGENTS_QUICK_THINK_LLM": "deepseek-flash",
        "TRADINGAGENTS_DEEPSEEK_QUICK_THINKING": "disabled",
    },
)

PROFILES = {CLAUDE.name: CLAUDE, DEEPSEEK.name: DEEPSEEK}


@dataclass(frozen=True)
class RoutingDecision:
    profile: Profile
    reason: str
    month: str
    anthropic_spend_usd: float
    budget_usd: float


def choose_profile(
    state_dir: str | Path,
    routing: str,
    budget_usd: float,
    now: datetime,
) -> RoutingDecision:
    """Pick the profile for the next run (pure over the usage ledger and the
    exhaustion marker)."""
    if routing not in ROUTING_CHOICES:
        raise ValueError(f"llm routing {routing!r} — expected one of {ROUTING_CHOICES}")
    month = now.strftime("%Y-%m")
    spend = month_spend(state_dir, month, "anthropic")
    if routing == "claude":
        return RoutingDecision(CLAUDE, "routing=claude", month, spend, budget_usd)
    if routing == "deepseek":
        return RoutingDecision(DEEPSEEK, "routing=deepseek", month, spend, budget_usd)
    if exhausted_marker(state_dir, month).exists():
        return RoutingDecision(DEEPSEEK, "anthropic credits exhausted this month", month, spend, budget_usd)
    if spend >= budget_usd:
        return RoutingDecision(
            DEEPSEEK, f"anthropic month spend ${spend:.2f} ≥ budget ${budget_usd:.2f}",
            month, spend, budget_usd,
        )
    return RoutingDecision(
        CLAUDE, f"anthropic month spend ${spend:.2f} < budget ${budget_usd:.2f}",
        month, spend, budget_usd,
    )
