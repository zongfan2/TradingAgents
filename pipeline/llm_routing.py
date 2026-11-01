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
  DeepSeek until the next month;
- ``ladder`` — Claude as in ``claude-first``, then OpenAI while the key is
  set, the month's balance is confirmed (``pipeline.llm_budget confirm``)
  and the confirmed balance minus the spend since exceeds the margin, then
  DeepSeek (specs/llm-provider-ladder.md L1).
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from pipeline.llm_usage import exhausted_marker, month_spend, remaining_balance

ROUTING_CHOICES = ("claude-first", "claude", "deepseek", "ladder")
DEFAULT_ANTHROPIC_MONTHLY_BUDGET_USD = 190.0
#: Kept unspent of the confirmed OpenAI balance (the local ledger lags the
#: provider's own meter by in-flight calls).
DEFAULT_OPENAI_BALANCE_MARGIN_USD = 5.0
OPENAI_MARGIN_ENV = "TRADINGAGENTS_OPENAI_BALANCE_MARGIN_USD"


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
    "TRADINGAGENTS_OPENAI_DEEP_REASONING_EFFORT": "",
    "TRADINGAGENTS_OPENAI_QUICK_REASONING_EFFORT": "",
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

#: User decision 2026-11-01 (ladder Q2): gpt-5.6-luna for the quick roles,
#: gpt-5.6-sol for both judges (no Research Manager override).
OPENAI = Profile(
    "openai",
    "openai",
    {
        **_CLEARED,
        "TRADINGAGENTS_ANTHROPIC_DEEP_EFFORT": "",
        "TRADINGAGENTS_ANTHROPIC_QUICK_EFFORT": "",
        "TRADINGAGENTS_RESEARCH_MANAGER_LLM": "",
        "TRADINGAGENTS_DEEPSEEK_QUICK_THINKING": "",
        "TRADINGAGENTS_LLM_PROVIDER": "openai",
        "TRADINGAGENTS_DEEP_THINK_LLM": "gpt-5.6-sol",
        "TRADINGAGENTS_QUICK_THINK_LLM": "gpt-5.6-luna",
        "TRADINGAGENTS_OPENAI_DEEP_REASONING_EFFORT": "medium",
        "TRADINGAGENTS_OPENAI_QUICK_REASONING_EFFORT": "low",
    },
)

PROFILES = {CLAUDE.name: CLAUDE, OPENAI.name: OPENAI, DEEPSEEK.name: DEEPSEEK}


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
    env: Mapping[str, str] | None = None,
) -> RoutingDecision:
    """Pick the profile for the next run (pure over the usage ledger, the
    exhaustion markers, the OpenAI confirmation and ``env``)."""
    if routing not in ROUTING_CHOICES:
        raise ValueError(f"llm routing {routing!r} — expected one of {ROUTING_CHOICES}")
    month = now.strftime("%Y-%m")
    spend = month_spend(state_dir, month, "anthropic")
    if routing == "claude":
        return RoutingDecision(CLAUDE, "routing=claude", month, spend, budget_usd)
    if routing == "deepseek":
        return RoutingDecision(DEEPSEEK, "routing=deepseek", month, spend, budget_usd)
    if exhausted_marker(state_dir, month).exists():
        claude_off = "anthropic credits exhausted this month"
    elif spend >= budget_usd:
        claude_off = f"anthropic month spend ${spend:.2f} ≥ budget ${budget_usd:.2f}"
    else:
        return RoutingDecision(
            CLAUDE, f"anthropic month spend ${spend:.2f} < budget ${budget_usd:.2f}",
            month, spend, budget_usd,
        )
    if routing == "ladder":
        usable, why = openai_usable(state_dir, month, env)
        if usable:
            return RoutingDecision(OPENAI, f"{claude_off}; {why}", month, spend, budget_usd)
        return RoutingDecision(DEEPSEEK, f"{claude_off}; {why}", month, spend, budget_usd)
    return RoutingDecision(DEEPSEEK, claude_off, month, spend, budget_usd)


def openai_usable(
    state_dir: str | Path, month: str, env: Mapping[str, str] | None = None
) -> tuple[bool, str]:
    """Whether the OpenAI rung may run (ladder L1), with the reason."""
    env = os.environ if env is None else env
    if not (env.get("OPENAI_API_KEY") or "").strip():
        return False, "OPENAI_API_KEY unset"
    if exhausted_marker(state_dir, month, "openai").exists():
        return False, "openai credits exhausted this month"
    remaining = remaining_balance(state_dir, month, "openai")
    if remaining is None:
        return False, f"openai balance not confirmed for {month}"
    try:
        margin = float((env.get(OPENAI_MARGIN_ENV) or "").strip() or DEFAULT_OPENAI_BALANCE_MARGIN_USD)
    except ValueError:
        margin = DEFAULT_OPENAI_BALANCE_MARGIN_USD
    if not remaining > margin:
        return False, f"openai remaining ${remaining:.2f} ≤ margin ${margin:.2f}"
    return True, f"openai remaining ${remaining:.2f}"


def cli_rung(
    state_dir: str | Path,
    routing: str | None,
    budget_usd: float,
    now: datetime,
    env: Mapping[str, str] | None = None,
) -> tuple[str, str]:
    """The provider the CLI steps run on — ``anthropic``, ``openai`` (codex,
    ladder only) or ``deepseek`` (Flash via the claude CLI) — and why: the
    R2/L1 rule, except that unset routing keeps the legacy Anthropic CLI."""
    if not routing:
        return "anthropic", "routing unset (legacy)"
    decision = choose_profile(state_dir, routing, budget_usd, now, env)
    return decision.profile.provider, decision.reason


def cli_uses_deepseek(
    state_dir: str | Path, routing: str | None, budget_usd: float, now: datetime
) -> tuple[bool, str]:
    """Whether the Claude CLI steps run on DeepSeek Flash (llm-cost R7)."""
    rung, reason = cli_rung(state_dir, routing, budget_usd, now)
    return rung == "deepseek", reason
