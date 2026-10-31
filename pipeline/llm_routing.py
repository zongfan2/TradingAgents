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


#: Provider-agnostic knobs a profile must not inherit from the legacy
#: single-provider env/.env (a relay ``backend_url`` would send Claude
#: requests to DeepSeek; Claude rejects ``temperature``). An empty value
#: clears the key: the child skips empty overrides and run_one's .env load
#: never overwrites a key that is present.
_CLEARED = {
    "TRADINGAGENTS_LLM_BACKEND_URL": "",
    "TRADINGAGENTS_TEMPERATURE": "",
    "TRADINGAGENTS_ANTHROPIC_EFFORT": "",
    "TRADINGAGENTS_OPENAI_REASONING_EFFORT": "",
    "TRADINGAGENTS_GOOGLE_THINKING_LEVEL": "",
}


@dataclass(frozen=True)
class Profile:
    name: str
    provider: str
    env: dict[str, str] = field(default_factory=dict)


#: User decision 2026-10-29: Haiku 5.5 (low) for the quick roles, Opus 5.5
#: (medium) for the judging roles; 2026-10-31 (R9): the Research Manager
#: judges on Sonnet 5.5, the Portfolio Manager stays on Opus.
CLAUDE = Profile(
    "claude",
    "anthropic",
    {
        **_CLEARED,
        "TRADINGAGENTS_LLM_PROVIDER": "anthropic",
        "TRADINGAGENTS_DEEP_THINK_LLM": "claude-opus-5-5",
        "TRADINGAGENTS_QUICK_THINK_LLM": "claude-haiku-5-5",
        "TRADINGAGENTS_RESEARCH_MANAGER_LLM": "claude-sonnet-5-5",
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
        **_CLEARED,
        "TRADINGAGENTS_ANTHROPIC_DEEP_EFFORT": "",
        "TRADINGAGENTS_ANTHROPIC_QUICK_EFFORT": "",
        "TRADINGAGENTS_RESEARCH_MANAGER_LLM": "",
        "TRADINGAGENTS_LLM_PROVIDER": "deepseek",
        "TRADINGAGENTS_DEEP_THINK_LLM": "deepseek-v4-pro",
        "TRADINGAGENTS_QUICK_THINK_LLM": "deepseek-flash",
        "TRADINGAGENTS_DEEPSEEK_QUICK_THINKING": "disabled",
    },
)

PROFILES = {CLAUDE.name: CLAUDE, DEEPSEEK.name: DEEPSEEK}


#: DeepSeek's Anthropic-compatible endpoint (llm-cost R7). Claude model names
#: are mapped there: claude-sonnet*/claude-haiku* -> deepseek-flash.
DEEPSEEK_ANTHROPIC_BASE_URL = "https://api.deepseek.com/anthropic"
DEEPSEEK_CLI_MODEL = "claude-sonnet-5-5"


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


def cli_uses_deepseek(
    state_dir: str | Path, routing: str | None, budget_usd: float, now: datetime
) -> tuple[bool, str]:
    """Whether the Claude CLI steps run on DeepSeek Flash (llm-cost R7): the
    R2 rule, except that unset routing keeps the legacy Anthropic CLI."""
    if not routing:
        return False, "routing unset (legacy)"
    decision = choose_profile(state_dir, routing, budget_usd, now)
    return decision.profile is DEEPSEEK, decision.reason
