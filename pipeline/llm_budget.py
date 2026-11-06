"""Confirm and show LLM provider balances (specs/llm-provider-ladder.md L1).

The OpenAI project key cannot read the account balance (the costs/usage/
billing endpoints need an Admin key), so the user confirms it each month
from the console; the ladder uses OpenAI only for a confirmed month, and
deducts the spend recorded after the confirmation.

    python -m pipeline.llm_budget confirm openai 71
    python -m pipeline.llm_budget status
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone

from pipeline.llm_routing import openai_usable
from pipeline.llm_usage import (
    confirm_balance,
    exhausted_marker,
    month_spend,
    read_confirmation,
    remaining_balance,
)

PROVIDERS = ("openai",)


def _state_dir():
    from pipeline.config import load_config

    return load_config().state_dir


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m pipeline.llm_budget", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    confirm = sub.add_parser("confirm", help="record the balance read off the provider console")
    confirm.add_argument("provider", choices=PROVIDERS)
    confirm.add_argument("balance_usd", type=float)
    confirm.add_argument("--month", help="YYYY-MM (UTC); default: the current month")
    sub.add_parser("status", help="show this month's spend and balances")
    return parser


def main(argv: list[str] | None = None, now: datetime | None = None) -> int:
    from pipeline.orchestrator import _load_repo_dotenv

    _load_repo_dotenv()  # OPENAI_API_KEY presence, state dir overrides
    args = build_parser().parse_args(argv)
    now = now or datetime.now(timezone.utc)
    month = now.strftime("%Y-%m")
    state_dir = _state_dir()
    if args.command == "confirm":
        target = args.month or month
        try:
            record = confirm_balance(state_dir, target, args.balance_usd, now, args.provider)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        print(f"{args.provider} {target}: balance ${record['balance_usd']:.2f} confirmed at {record['confirmed_at']}")
        return 0
    anthropic = month_spend(state_dir, month, "anthropic")
    print(f"month {month} (UTC)")
    path = exhausted_marker(state_dir, month)
    marker = ""
    if path.exists():
        reason = " ".join(path.read_text(encoding="utf-8", errors="replace").split())[:160]
        marker = f" (credits exhausted: {reason} — delete {path} after topping up or raising the limit)"
    try:  # the budget the analysis runner gates on
        from pipeline.analysis_runner import load_settings

        budget = f"${load_settings(os.environ).anthropic_monthly_budget_usd:.0f}"
    except Exception as exc:
        budget = f"unreadable ({exc})"
    print(f"  anthropic spend ${anthropic:.2f} / budget {budget}{marker}")
    record = read_confirmation(state_dir, month, "openai")
    if record is None:
        print("  openai    balance NOT confirmed — run: python -m pipeline.llm_budget confirm openai <USD>")
    else:
        remaining = remaining_balance(state_dir, month, "openai")
        print(f"  openai    confirmed ${record['balance_usd']:.2f} at {record['confirmed_at']}, "
              f"spent since ${record['balance_usd'] - remaining:.2f}, left ${remaining:.2f}")
    usable, why = openai_usable(state_dir, month)
    print(f"  openai rung {'usable' if usable else 'skipped'}: {why}")
    print(f"  deepseek spend ${month_spend(state_dir, month, 'deepseek'):.2f} (floor, no budget)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
