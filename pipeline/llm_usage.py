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
import math
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
    ``long_prompt_tokens`` (Haiku 5.5's > 100 K tier, OpenAI's > 272 K)."""

    input: float
    output: float
    cache_read: float
    cache_write: float
    long_prompt_tokens: int | None = None
    long_input: float | None = None
    long_output: float | None = None
    long_cache_read: float | None = None
    long_cache_write: float | None = None


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
    # OpenAI list prices (developers.openai.com/api/docs/pricing, standard
    # tier, 2026-11-01); no cache-write surcharge, so writes bill as input;
    # prompts over 272K tokens bill at the long-context tier.
    "gpt-6-luna": Price(0.10, 0.50, 0.01, 0.10, long_prompt_tokens=272_000,
                        long_input=0.20, long_output=0.75, long_cache_read=0.02, long_cache_write=0.20),
    "gpt-6.1-terra": Price(1.00, 5.00, 0.05, 1.00, long_prompt_tokens=272_000,
                           long_input=2.00, long_output=7.50, long_cache_read=0.10, long_cache_write=2.00),
    "gpt-6.1-sol": Price(2.00, 10.00, 0.10, 2.00, long_prompt_tokens=272_000,
                         long_input=4.00, long_output=15.00, long_cache_read=0.20, long_cache_write=4.00),
    # Previous generation, kept so older ledger lines and overrides price.
    "gpt-5.6-sol": Price(4.00, 20.00, 0.40, 4.00, long_prompt_tokens=272_000,
                         long_input=8.00, long_output=30.00, long_cache_read=0.80, long_cache_write=8.00),
    "gpt-5.6-luna": Price(0.20, 1.20, 0.02, 0.20, long_prompt_tokens=272_000,
                          long_input=0.40, long_output=1.80, long_cache_read=0.04, long_cache_write=0.40),
    "deepseek-v4-pro": Price(1.74, 3.48, 0.0145, 1.74),
    "deepseek-flash": Price(0.14, 0.28, 0.0028, 0.14),
    "deepseek-v4-flash": Price(0.14, 0.28, 0.0028, 0.14),
}


def provider_for_model(model: str) -> str:
    if model.startswith("claude-"):
        return "anthropic"
    if model.startswith("deepseek-"):
        return "deepseek"
    if model.startswith("gpt-"):
        return "openai"
    return "other"


#: Snapshot suffixes: Anthropic ``-YYYYMMDD``, OpenAI ``-YYYY-MM-DD``.
_DATE_SUFFIX = re.compile(r"-(\d{8}|\d{4}-\d{2}-\d{2})$")
#: An unlisted Claude model is billed at the most expensive listed Claude
#: rate (flagged ``priced: false``) so the Anthropic budget never undercounts.
_CLAUDE_CEILING = max(
    (price for model, price in PRICES.items() if model.startswith("claude-")),
    key=lambda price: price.output,
)


#: Same rule for an unlisted GPT model and the OpenAI budget (ladder L4).
_OPENAI_CEILING = max(
    (price for model, price in PRICES.items() if model.startswith("gpt-")),
    key=lambda price: price.output,
)

#: Web search tool calls bill per call on both Anthropic and OpenAI.
WEB_SEARCH_USD_PER_CALL = 0.01


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
    if model.startswith("gpt-"):
        logger.warning("no price for %s — budgeting it at the OpenAI ceiling rate", model)
        return _OPENAI_CEILING, False
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
        + cache_write * (price.long_cache_write if long and price.long_cache_write is not None
                         else price.cache_write)
        + tokens.get("output", 0) * rate_out
    ) / 1_000_000


def openai_call_cost(model: str, tokens: dict[str, int]) -> float:
    """USD for one call billed to the OpenAI account: any model without a
    listed price (an ``o``-series or ``codex-`` override) is budgeted at the
    OpenAI ceiling, never at 0 (ladder L4)."""
    cost = call_cost(model, tokens)
    if cost is not None:
        return cost
    logger.warning("no price for %s — budgeting it at the OpenAI ceiling rate", model)
    return call_cost("gpt-ceiling-rate", tokens) or 0.0


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
        row = _json_line(raw)
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _spend(value: object) -> float:
    """A cost read back from disk: anything non-numeric, non-finite, negative
    or overflowing counts as 0 — one corrupt line never poisons a fold."""
    try:
        number = float(value or 0.0)
    except Exception:  # TypeError, ValueError, OverflowError (huge ints), ...
        return 0.0
    return number if math.isfinite(number) and number > 0 else 0.0


def _json_line(raw: str) -> object:
    try:
        return json.loads(raw)
    except Exception:  # JSONDecodeError, and ValueError for >4300-digit ints
        return None


#: The Claude CLI usage log (``pipeline.claude_cli``) and its test override.
CLI_USAGE_NAME = "claude_usage.jsonl"
CLI_USAGE_LOG_ENV = "TRADINGAGENTS_CLAUDE_USAGE_LOG"


def cli_usage_path(state_dir: str | Path) -> Path:
    raw = os.environ.get(CLI_USAGE_LOG_ENV, "").strip()
    return Path(raw).expanduser() if raw else Path(state_dir) / CLI_USAGE_NAME


#: The CLI usage-line ``billing`` tag each provider's API spend carries.
CLI_BILLING = {"anthropic": "anthropic-api", "openai": "openai-api"}


def cli_month_spend(
    state_dir: str | Path, month: str, provider: str = "anthropic", since: str | None = None
) -> float:
    """API-billed CLI spend with ``provider`` in month ``YYYY-MM`` (UTC) —
    llm-cost R7 / ladder L3 — optionally only lines at or after the UTC ISO
    instant ``since``. Only lines with the provider's ``billing`` tag count
    (older Anthropic lines ran on the subscription, never billed)."""
    billing = CLI_BILLING.get(provider)
    if billing is None:
        return 0.0
    try:
        text = cli_usage_path(state_dir).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return 0.0
    since_at = _since(since)
    total = 0.0
    for raw in text.splitlines():
        row = _json_line(raw)
        try:
            if not isinstance(row, dict) or row.get("billing") != billing:
                continue
            ts = _utc_instant(row.get("ts"))
            if ts is None:
                # A naive/junk ts keeps the old prefix rule and, with
                # ``since``, counts (over-counting is budget-safe).
                if str(row.get("ts") or "")[:7] != month:
                    continue
            elif ts.strftime("%Y-%m") != month or (since_at and ts < since_at):
                continue
            total += _spend(row.get("billed_usd"))
        except Exception:  # a malformed line never breaks the fold
            continue
    return round(total, 6)


def _since(since: str | None) -> datetime | None:
    """The ``since`` cut-off; an unparsable one is ignored, so the fold
    over-counts (budget-safe) rather than dropping spend."""
    return _utc_instant(since) if since else None


def _utc_instant(value: object) -> datetime | None:
    """A ledger timestamp as an aware UTC datetime; junk → ``None``."""
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def month_spend(
    state_dir: str | Path, month: str, provider: str = "anthropic", since: str | None = None
) -> float:
    """USD spent with ``provider`` in calendar month ``YYYY-MM`` (UTC),
    summed per model so a mixed run still counts its own part, plus the
    provider's API-billed CLI spend (R7, ladder L3): one budget covers
    analysis, collection and evaluation. ``since`` (UTC ISO) keeps only
    spend recorded at or after that instant (ladder: after the balance
    confirmation)."""
    total = cli_month_spend(state_dir, month, provider, since)
    since_at = _since(since)
    for row in read_usage(state_dir):
        total += _row_spend(row, month, provider, since_at)
    return round(total, 6)


def _row_spend(row: dict, month: str, provider: str, since_at: datetime | None = None) -> float:
    """One ledger line's spend with ``provider``; a line of any malformed
    shape counts as 0 — a corrupt line never breaks the monthly fold."""
    try:
        models = row.get("models")
        if row.get("month") != month or not isinstance(models, dict):
            return 0.0
        if since_at is not None:
            written = _utc_instant(row.get("written_at"))
            # An unreadable timestamp counts (over-count is budget-safe).
            if written is not None and written < since_at:
                return 0.0
        row_provider = _PROFILE_PROVIDER.get(row.get("profile"))
        total = 0.0
        for model, usage in models.items():
            if not isinstance(model, str) or not isinstance(usage, dict):
                continue
            owner = provider_for_model(model)
            cost = _spend(usage.get("cost_usd"))
            if owner == "other" and row_provider is not None:
                # A model name the provider did not report ("unknown") on a
                # routed run belongs to the run's provider; unpriced, it is
                # budgeted at that provider's ceiling — never at $0.
                owner = row_provider
                if not cost and owner in _CEILING_MODEL:
                    cost = _spend(call_cost(_CEILING_MODEL[owner], _row_tokens(usage)))
            if owner == provider:
                total += cost
        return total
    except Exception:
        return 0.0


#: Routed-run profile → provider (pipeline.llm_routing profile names).
_PROFILE_PROVIDER = {"claude": "anthropic", "openai": "openai", "deepseek": "deepseek"}
#: A name ``price_for`` budgets at each provider's ceiling rate.
_CEILING_MODEL = {"anthropic": "claude-unpriced", "openai": "gpt-unpriced"}


def _row_tokens(usage: dict) -> dict[str, int]:
    tokens = {}
    for key in ("input", "output", "cache_read", "cache_write"):
        try:
            tokens[key] = max(int(usage.get(key) or 0), 0)
        except Exception:
            tokens[key] = 0
    return tokens


# ---------------------------------------------------------------------------
# Credit exhaustion marker
# ---------------------------------------------------------------------------


def exhausted_marker(state_dir: str | Path, month: str, provider: str = "anthropic") -> Path:
    """Anthropic keeps its original path; other providers get a subdir."""
    if provider == "anthropic":
        return Path(state_dir) / BUDGET_DIRNAME / f"{month}.exhausted"
    return Path(state_dir) / BUDGET_DIRNAME / provider / f"{month}.exhausted"


#: OpenAI's out-of-credit error: the API's HTTP 429 ``insufficient_quota``
#: (SDK text), and codex's own rewrite of it (strings in codex-cli 0.164.0:
#: "Quota exceeded. Check your plan and billing details.", ``quota_exceeded``).
OPENAI_QUOTA_MARKERS = (
    "insufficient_quota",
    "exceeded your current quota",
    "quota exceeded. check your plan and billing details",
    "quota_exceeded",
)


def is_credit_exhausted_error(error: str | None, provider: str = "anthropic") -> bool:
    text = (error or "").lower()
    markers = OPENAI_QUOTA_MARKERS if provider == "openai" else CREDIT_EXHAUSTED_MARKERS
    return any(marker in text for marker in markers)


def mark_exhausted(state_dir: str | Path, month: str, reason: str, provider: str = "anthropic") -> None:
    try:
        atomic_write(exhausted_marker(state_dir, month, provider), reason.strip()[:500] + "\n")
    except Exception as exc:
        logger.warning("could not write the credit-exhausted marker: %s", exc)


# ---------------------------------------------------------------------------
# OpenAI balance confirmation (ladder L1: no Admin key, so the user confirms)
# ---------------------------------------------------------------------------


def confirmation_path(state_dir: str | Path, month: str, provider: str = "openai") -> Path:
    return Path(state_dir) / BUDGET_DIRNAME / provider / f"{month}.confirmed"


def confirm_balance(
    state_dir: str | Path, month: str, balance_usd: float, now: datetime, provider: str = "openai"
) -> dict:
    """Record the balance the user read off the console for ``month``;
    spend after ``confirmed_at`` is deducted from it."""
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month):
        raise ValueError(f"month must be YYYY-MM, got {month!r}")
    if not (math.isfinite(balance_usd) and balance_usd >= 0):
        raise ValueError(f"balance must be a finite amount >= 0, got {balance_usd!r}")
    record = {"provider": provider, "month": month, "balance_usd": round(float(balance_usd), 2),
              "confirmed_at": to_utc_iso(now)}
    atomic_write(confirmation_path(state_dir, month, provider), json.dumps(record) + "\n")
    return record


def read_confirmation(state_dir: str | Path, month: str, provider: str = "openai") -> dict | None:
    """The month's confirmation, or ``None`` when absent or unreadable."""
    try:
        record = json.loads(confirmation_path(state_dir, month, provider).read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(record, dict) or record.get("month") != month:
        return None
    balance = record.get("balance_usd")
    if isinstance(balance, bool) or not isinstance(balance, (int, float)) or not math.isfinite(balance):
        return None
    if balance < 0 or _utc_instant(record.get("confirmed_at")) is None:
        return None
    return record


def remaining_balance(state_dir: str | Path, month: str, provider: str = "openai") -> float | None:
    """Confirmed balance minus the spend recorded since it was confirmed;
    ``None`` when the month has no valid confirmation."""
    record = read_confirmation(state_dir, month, provider)
    if record is None:
        return None
    spent = month_spend(state_dir, month, provider, since=record["confirmed_at"])
    return round(float(record["balance_usd"]) - spent, 6)
