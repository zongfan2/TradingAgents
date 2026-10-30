"""LLM token usage and cost (specs/llm-cost.md R1).

- :class:`UsageCallback` (child side): a LangChain callback summing every
  LLM call's ``usage_metadata`` per model, and pricing each call.
- :func:`append_usage` / :func:`month_spend` (runner side): one line per run
  in ``<state_dir>/llm_usage.jsonl`` (append-only, ``locked_append``); the
  month-to-date spend is a pure fold over that file — no mutable budget
  state to lose updates on (the LiteLLM BudgetManager failure modes).
- The per-month exhaustion marker written when Anthropic reports the credit
  balance is gone.

Everything here is best-effort: accounting never fails a run.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pipeline.common import atomic_write, locked_append, to_utc_iso

logger = logging.getLogger(__name__)

USAGE_SCHEMA = 1
USAGE_NAME = "llm_usage.jsonl"
BUDGET_DIRNAME = "llm_budget"
CREDIT_EXHAUSTED_MARKERS = ("credit balance is too low", "credit balance too low")


@dataclass(frozen=True)
class Price:
    """USD per 1 M tokens. ``long_*`` apply to a call whose prompt exceeds
    ``long_prompt_tokens`` (Haiku 5.5's > 100 K tier)."""

    input: float
    output: float
    cache_read: float
    cache_write: float
    long_prompt_tokens: int | None = None
    long_input: float | None = None
    long_output: float | None = None
    long_cache_read: float | None = None


#: Anthropic first-party list prices (claude-api reference, 2026-10); cache
#: writes at the 5-minute rate (1.25 × input). DeepSeek: third-party listings
#: (the official page did not render) — V4-Pro at the standard (not the
#: discounted) rate, which reconciles with the user's 2026-10 bill. DeepSeek
#: figures are informational; only Anthropic spend gates routing.
PRICES: dict[str, Price] = {
    "claude-opus-5-5": Price(4.00, 20.00, 0.20, 5.00),
    "claude-sonnet-5-5": Price(2.00, 10.00, 0.20, 2.50),
    "claude-haiku-5-5": Price(
        0.10, 0.50, 0.01, 0.125,
        long_prompt_tokens=100_000, long_input=0.50, long_output=2.50, long_cache_read=0.05,
    ),
    "deepseek-v4-pro": Price(1.74, 3.48, 0.0145, 1.74),
    "deepseek-flash": Price(0.14, 0.28, 0.0028, 0.14),
    "deepseek-v4-flash": Price(0.14, 0.28, 0.0028, 0.14),
}


def provider_for_model(model: str) -> str:
    if model.startswith("claude-"):
        return "anthropic"
    if model.startswith("deepseek-"):
        return "deepseek"
    return "other"


_DATE_SUFFIX = re.compile(r"-\d{8}$")
#: An unlisted Claude model is billed at the most expensive listed Claude
#: rate (flagged ``priced: false``) so the Anthropic budget never undercounts.
_CLAUDE_CEILING = max(
    (price for model, price in PRICES.items() if model.startswith("claude-")),
    key=lambda price: price.output,
)


def price_for(model: str) -> tuple[Price | None, bool]:
    """``(price, exact)``: a snapshot id (``-YYYYMMDD``) resolves to its
    alias; an unknown Claude model gets the ceiling rate (``exact=False``);
    any other unknown model has no price (never guessed)."""
    price = PRICES.get(model) or PRICES.get(_DATE_SUFFIX.sub("", model))
    if price is not None:
        return price, True
    if model.startswith("claude-"):
        logger.warning("no price for %s — budgeting it at the Claude ceiling rate", model)
        return _CLAUDE_CEILING, False
    return None, False


def call_cost(model: str, tokens: dict[str, int]) -> float | None:
    """USD for one call; ``None`` for a non-Claude model without a price."""
    price, _exact = price_for(model)
    if price is None:
        return None
    input_total = tokens.get("input", 0)
    cache_read = tokens.get("cache_read", 0)
    cache_write = tokens.get("cache_write", 0)
    uncached = max(input_total - cache_read - cache_write, 0)
    long = price.long_prompt_tokens is not None and input_total > price.long_prompt_tokens
    rate_in = price.long_input if long else price.input
    rate_out = price.long_output if long else price.output
    rate_read = price.long_cache_read if long else price.cache_read
    return (
        uncached * rate_in
        + cache_read * rate_read
        + cache_write * price.cache_write
        + tokens.get("output", 0) * rate_out
    ) / 1_000_000


def _tokens_from_message(message: Any) -> tuple[str | None, dict[str, int]] | None:
    usage = getattr(message, "usage_metadata", None)
    if not usage:
        return None
    meta = getattr(message, "response_metadata", None) or {}
    model = meta.get("model_name") or meta.get("model")
    in_details = usage.get("input_token_details") or {}
    out_details = usage.get("output_token_details") or {}
    cache_read = int(in_details.get("cache_read") or 0)
    if not cache_read:  # DeepSeek reports its own field on the raw usage
        raw = meta.get("token_usage") or {}
        cache_read = int(raw.get("prompt_cache_hit_tokens") or 0)
    return model, {
        "input": int(usage.get("input_tokens") or 0),
        "output": int(usage.get("output_tokens") or 0),
        "cache_read": cache_read,
        "cache_write": int(in_details.get("cache_creation") or 0),
        "reasoning": int(out_details.get("reasoning") or 0),
    }


try:  # the child imports langchain anyway; the runner side does not need it
    from langchain_core.callbacks import BaseCallbackHandler
except ImportError:  # pragma: no cover
    BaseCallbackHandler = object  # type: ignore[assignment,misc]


class UsageCallback(BaseCallbackHandler):
    """Sum token usage per model over every LLM call of one run.

    With ``stream`` (run_one passes the real stdout) the running total is
    written after every call as a ``sentinel`` line, so a run the parent
    kills on timeout still reports what it spent."""

    def __init__(self, stream: Any = None, sentinel: str = "") -> None:
        super().__init__()
        self._lock = threading.Lock()
        self.models: dict[str, dict[str, Any]] = {}
        self._stream = stream
        self._sentinel = sentinel
        # One lock covers snapshot + write, so the LAST streamed line is
        # always the latest total even when graph nodes run in parallel.
        self._print_lock = threading.Lock()

    def on_llm_end(self, response: Any, **kwargs: Any) -> None:
        try:
            for generations in getattr(response, "generations", None) or []:
                for generation in generations:
                    parsed = _tokens_from_message(getattr(generation, "message", None))
                    if parsed is None:
                        continue
                    model, tokens = parsed
                    self._add(model or "unknown", tokens)
            if self._stream is not None:
                with self._print_lock:
                    if self.models:
                        print(self._sentinel + json.dumps(self.summary(), ensure_ascii=False),
                              file=self._stream, flush=True)
        except Exception as exc:  # accounting never fails a run
            logger.warning("usage accounting skipped a call: %s", exc)

    def _add(self, model: str, tokens: dict[str, int]) -> None:
        cost = call_cost(model, tokens)
        _price, exact = price_for(model)
        with self._lock:
            row = self.models.setdefault(
                model,
                {"calls": 0, "input": 0, "output": 0, "cache_read": 0, "cache_write": 0,
                 "reasoning": 0, "cost_usd": 0.0, "priced": True},
            )
            row["calls"] += 1
            for key, value in tokens.items():
                row[key] += value
            if cost is None or not exact:
                row["priced"] = False
            if cost is not None:
                row["cost_usd"] = round(row["cost_usd"] + cost, 6)

    def summary(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            return {model: dict(row) for model, row in self.models.items()}


# ---------------------------------------------------------------------------
# Runner side: the ledger
# ---------------------------------------------------------------------------


def usage_path(state_dir: str | Path) -> Path:
    return Path(state_dir) / USAGE_NAME


def summarize_models(models: dict[str, dict[str, Any]]) -> tuple[float, bool, str]:
    """``(cost_usd, priced, provider)`` over a run's per-model usage."""
    cost = round(sum(float(row.get("cost_usd") or 0.0) for row in models.values()), 6)
    priced = all(row.get("priced", False) for row in models.values()) if models else True
    providers = {provider_for_model(model) for model in models}
    provider = providers.pop() if len(providers) == 1 else ("mixed" if providers else "none")
    return cost, priced, provider


def append_usage(
    state_dir: str | Path,
    *,
    run_id: str | None,
    date: str,
    session: str,
    ticker: str,
    arm: str,
    profile: str | None,
    models: dict[str, dict[str, Any]],
    error: str | None = None,
    clock: Any = None,
) -> dict | None:
    """Append one run's usage line; returns it, or ``None`` when nothing was
    spent (no models) or the write failed (logged, never raised)."""
    if not models:
        return None
    cost, priced, provider = summarize_models(models)
    now = (clock or (lambda: datetime.now(timezone.utc)))()
    line = {
        "schema": USAGE_SCHEMA,
        "written_at": to_utc_iso(now),
        "month": now.strftime("%Y-%m"),
        "run_id": run_id,
        "date": date,
        "session": session,
        "ticker": ticker,
        "arm": arm,
        "profile": profile,
        "provider": provider,
        "models": models,
        "cost_usd": cost,
        "priced": priced,
        "error": error,
    }
    try:
        locked_append(usage_path(state_dir), json.dumps(line, ensure_ascii=False, sort_keys=True))
    except Exception as exc:
        logger.warning("usage line not recorded for %s: %s", run_id or ticker, exc)
        return None
    return line


def read_usage(state_dir: str | Path) -> list[dict]:
    try:
        text = usage_path(state_dir).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    rows = []
    for raw in text.splitlines():
        try:
            row = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


#: The Claude CLI usage log (``pipeline.claude_cli``) and its test override.
CLI_USAGE_NAME = "claude_usage.jsonl"
CLI_USAGE_LOG_ENV = "TRADINGAGENTS_CLAUDE_USAGE_LOG"


def cli_usage_path(state_dir: str | Path) -> Path:
    raw = os.environ.get(CLI_USAGE_LOG_ENV, "").strip()
    return Path(raw).expanduser() if raw else Path(state_dir) / CLI_USAGE_NAME


def cli_month_spend(state_dir: str | Path, month: str) -> float:
    """API-billed Claude CLI spend in month ``YYYY-MM`` (UTC) — llm-cost R7.
    Only lines marked ``billing: anthropic-api`` count: older lines ran on
    the subscription and were never billed to the API."""
    try:
        text = cli_usage_path(state_dir).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return 0.0
    total = 0.0
    for raw in text.splitlines():
        try:
            row = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if (
            isinstance(row, dict)
            and row.get("billing") == "anthropic-api"
            and str(row.get("ts") or "")[:7] == month
        ):
            try:
                total += float(row.get("billed_usd") or 0.0)
            except (TypeError, ValueError):
                continue
    return round(total, 6)


def month_spend(state_dir: str | Path, month: str, provider: str = "anthropic") -> float:
    """USD spent with ``provider`` in calendar month ``YYYY-MM`` (UTC),
    summed per model so a mixed run still counts its Anthropic part. For
    ``anthropic`` this includes the API-billed Claude CLI spend (R7): one
    budget covers analysis, collection and evaluation."""
    total = cli_month_spend(state_dir, month) if provider == "anthropic" else 0.0
    for row in read_usage(state_dir):
        if row.get("month") != month:
            continue
        for model, usage in (row.get("models") or {}).items():
            if provider_for_model(model) == provider and isinstance(usage, dict):
                total += float(usage.get("cost_usd") or 0.0)
    return round(total, 6)


# ---------------------------------------------------------------------------
# Credit exhaustion marker
# ---------------------------------------------------------------------------


def exhausted_marker(state_dir: str | Path, month: str) -> Path:
    return Path(state_dir) / BUDGET_DIRNAME / f"{month}.exhausted"


def is_credit_exhausted_error(error: str | None) -> bool:
    text = (error or "").lower()
    return any(marker in text for marker in CREDIT_EXHAUSTED_MARKERS)


def mark_exhausted(state_dir: str | Path, month: str, reason: str) -> None:
    try:
        atomic_write(exhausted_marker(state_dir, month), reason.strip()[:500] + "\n")
    except Exception as exc:
        logger.warning("could not write the credit-exhausted marker: %s", exc)
