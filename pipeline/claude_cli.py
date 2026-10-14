"""Shared ``claude -p`` plumbing for the collectors and the evaluator (D20).

Every pipeline ``claude -p`` call pins its model and effort explicitly — a
bare ``claude -p`` silently rides the user's global Claude Code default
(``~/.claude/settings.json``), which changes the pipeline whenever the user
changes their interactive model. Calls run hermetically (see
:data:`HERMETIC_FLAGS`) and with ``--output-format json`` so the result
envelope's ``usage`` / ``total_cost_usd`` can be appended to a jsonl usage
log; logging is best-effort and never fails a run.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from pipeline.common import locked_append

#: ``claude --effort`` levels accepted by the pipeline config.
EFFORT_CHOICES = ("low", "medium", "high", "xhigh", "max")

#: Overrides the usage log location (tests point it at a temp dir).
USAGE_LOG_ENV = "TRADINGAGENTS_CLAUDE_USAGE_LOG"
USAGE_LOG_NAME = "claude_usage.jsonl"

_USAGE_FIELDS = (
    "input_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
    "output_tokens",
)


class EnvelopeError(ValueError):
    """The ``claude -p`` JSON result envelope is unusable."""


#: Hermetic, web-only session. Pipeline calls read untrusted web pages, so
#: they must not inherit the user's interactive setup (``defaultMode: auto``,
#: hooks, plugins, MCP servers — trading tools among them — CLAUDE.md, skills):
#: ``--safe-mode`` drops those customizations while keeping subscription
#: OAuth, ``--tools`` leaves only the two web tools in the session at all,
#: ``--strict-mcp-config`` loads no MCP server, and ``dontAsk`` denies
#: anything not pre-allowed. Measured 2026-10-14: a trivial call's context fell
#: from ~50K to ~4K tokens. ``--no-session-persistence`` keeps unattended runs
#: out of ``~/.claude/projects``.
HERMETIC_FLAGS = (
    "--safe-mode",
    "--tools",
    "WebSearch,WebFetch",
    "--allowedTools",
    "WebSearch,WebFetch",
    "--strict-mcp-config",
    "--permission-mode",
    "dontAsk",
    "--no-session-persistence",
)


def claude_command(model: str, effort: str, *extra: str) -> list[str]:
    """Hermetic web-only ``claude -p`` on a pinned model/effort, JSON envelope."""
    return [
        "claude",
        "-p",
        *HERMETIC_FLAGS,
        "--model",
        model,
        "--effort",
        effort,
        "--output-format",
        "json",
        *extra,
    ]


def parse_envelope(stdout: str) -> dict:
    """Validate a successful ``claude -p --output-format json`` envelope."""
    try:
        envelope = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise EnvelopeError("claude -p returned invalid JSON envelope") from exc
    if not isinstance(envelope, dict) or envelope.get("type") != "result":
        raise EnvelopeError("claude -p returned malformed result envelope")
    is_error = envelope.get("is_error")
    if not isinstance(is_error, bool):
        raise EnvelopeError("claude -p returned malformed result envelope")
    if is_error:
        detail = " ".join(str(envelope.get("result", "")).split())
        raise EnvelopeError(f"claude -p returned error result: {detail}")
    if envelope.get("subtype") != "success":
        raise EnvelopeError("claude -p returned malformed result envelope")
    return envelope


def result_text(envelope: dict) -> str:
    """The free-text ``result`` of a validated envelope (collector output)."""
    result = envelope.get("result")
    if not isinstance(result, str):
        raise EnvelopeError("claude -p result envelope missing result text")
    return result


def collect_command(backend: str, other_commands: dict[str, tuple[str, ...]]) -> list[str]:
    """Collector argv: claude on the pinned collect model/effort, any other
    backend from the collector's own command table."""
    if backend != "claude":
        return list(other_commands[backend])
    return claude_command(*_collect_model_effort())


def collect_output(backend: str, stdout: str, *, component: str) -> str:
    """Collector output text. claude's envelope is unwrapped and its usage
    logged — an unusable envelope still logs whatever usage it carries, then
    raises :class:`EnvelopeError`."""
    if backend != "claude":
        return stdout
    model, effort = _collect_model_effort()
    try:
        envelope = parse_envelope(stdout)
        text = result_text(envelope)
    except EnvelopeError:
        record_failed_call(component, model, effort, stdout)
        raise
    record_usage(component, model, effort, envelope)
    return text


def collect_failed(backend: str, stdout: str, *, component: str) -> None:
    """Log the usage of a collector call that exited non-zero (claude only)."""
    if backend == "claude":
        record_failed_call(component, *_collect_model_effort(), stdout)


def _collect_model_effort() -> tuple[str, str]:
    from pipeline.config import load_config  # config imports this module

    config = load_config()
    return config.claude_collect_model, config.claude_collect_effort


def record_failed_call(component: str, model: str, effort: str, stdout: str) -> None:
    """A failed call (error envelope, non-zero exit) can still have spent
    quota: log the usage of any JSON envelope on its stdout as ``error``. A
    call that printed no envelope (timeout, crash) has no usage to report."""
    try:
        envelope = json.loads(stdout)
    except (json.JSONDecodeError, TypeError):
        return
    if isinstance(envelope, dict):
        record_usage(component, model, effort, envelope, outcome="error")


def usage_log_path() -> Path:
    raw = os.environ.get(USAGE_LOG_ENV, "").strip()
    if raw:
        return Path(raw).expanduser()
    state_dir = os.environ.get("TRADINGAGENTS_STATE_DIR", "").strip() or "~/.tradingagents"
    return Path(state_dir).expanduser() / USAGE_LOG_NAME


def record_usage(
    component: str, model: str, effort: str, envelope: dict, *, outcome: str = "ok"
) -> None:
    """Append one usage line for a finished call (``outcome`` ``ok`` or
    ``error``); never raises."""
    usage = envelope.get("usage") if isinstance(envelope.get("usage"), dict) else {}
    line = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "component": component,
        "model": model,
        "effort": effort,
        "outcome": outcome,
        **{name: usage.get(name) for name in _USAGE_FIELDS},
        "total_cost_usd": envelope.get("total_cost_usd"),
        "num_turns": envelope.get("num_turns"),
        "duration_ms": envelope.get("duration_ms"),
    }
    path = usage_log_path()
    try:
        # Ticker collectors/evaluators fan out concurrently: append under flock.
        locked_append(path, json.dumps(line, separators=(",", ":")))
    except OSError as exc:
        print(f"warning: could not record claude usage to {path}: {exc}", file=sys.stderr)
