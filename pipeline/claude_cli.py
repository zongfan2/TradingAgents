"""Shared ``claude -p`` plumbing for the collectors and the evaluator (D20).

Every pipeline ``claude -p`` call pins its model and effort explicitly — a
bare ``claude -p`` silently rides the user's global Claude Code default
(``~/.claude/settings.json``), which changes the pipeline whenever the user
changes their interactive model. Calls run hermetically (see
:data:`HERMETIC_FLAGS`) and with ``--output-format stream-json`` so the final
``result`` event (the same envelope ``--output-format json`` prints) carries
``usage`` / ``total_cost_usd`` and the event stream carries each web search
query; both are appended to a jsonl usage log, best-effort, never failing a
run.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import shutil
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

from pipeline.common import atomic_write, locked_append

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


logger = logging.getLogger(__name__)

#: llm-cost R7 — this process's CLI route, decided at its first ``claude -p``
#: (``"anthropic"`` or ``"deepseek"``); the switch to DeepSeek is one-way.
_ROUTE: str | None = None
_ROUTE_LOCK = threading.Lock()
#: The route the current thread's last ``claude_command`` was built for: a
#: call is built, run and recorded on one thread, so its usage is billed to
#: the route it ran on even if the process switches while it is in flight.
_CALL = threading.local()


def _reset_route_for_tests() -> None:
    global _ROUTE
    with _ROUTE_LOCK:
        _ROUTE = None
    _CALL.__dict__.clear()


def _state_dir() -> Path:
    from pipeline.config import load_config  # config imports this module

    return load_config().state_dir


def _use_deepseek(reason: str) -> bool:
    """Point THIS process's env at DeepSeek's Anthropic-compatible endpoint
    (the key never goes on argv). False when no DeepSeek key is set."""
    from pipeline.llm_routing import DEEPSEEK_ANTHROPIC_BASE_URL

    key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not key:
        logger.warning("claude CLI should switch to DeepSeek (%s) but DEEPSEEK_API_KEY is unset", reason)
        return False
    os.environ["ANTHROPIC_BASE_URL"] = DEEPSEEK_ANTHROPIC_BASE_URL
    os.environ["ANTHROPIC_API_KEY"] = key
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)
    logger.warning("claude CLI runs on DeepSeek Flash: %s", reason)
    return True


def cli_route() -> str:
    """This process's CLI route, decided once (R7, ladder L1): ``anthropic``,
    ``openai`` (codex, ``ladder`` routing only) or ``deepseek``, from the
    month's spend, budgets, markers and the OpenAI balance confirmation
    under TRADINGAGENTS_LLM_ROUTING."""
    global _ROUTE
    with _ROUTE_LOCK:
        if _ROUTE is None:
            _ROUTE = "anthropic"
            try:
                from pipeline.llm_routing import (
                    DEFAULT_ANTHROPIC_MONTHLY_BUDGET_USD,
                    cli_rung,
                )

                routing = os.environ.get("TRADINGAGENTS_LLM_ROUTING", "").strip().lower() or None
                budget = float(
                    os.environ.get("TRADINGAGENTS_ANTHROPIC_MONTHLY_BUDGET_USD", "").strip()
                    or DEFAULT_ANTHROPIC_MONTHLY_BUDGET_USD
                )
                rung, reason = cli_rung(_state_dir(), routing, budget, datetime.now(timezone.utc))
                if rung == "deepseek" and _use_deepseek(reason):
                    _ROUTE = "deepseek"
                elif rung == "openai":
                    missing = [cli for cli in ("codex", "node") if shutil.which(cli) is None]
                    if missing:  # a missing CLI would fail every call: next rung
                        if _use_deepseek(f"{reason}; but {', '.join(missing)} not on PATH"):
                            _ROUTE = "deepseek"
                    else:
                        _ROUTE = "openai"  # codex on the API key (ladder L3)
                        logger.warning("CLI steps run on OpenAI via codex: %s", reason)
            except Exception as exc:  # routing never breaks a collection call
                logger.warning("claude CLI routing check failed (staying on Anthropic): %s", exc)
        return _ROUTE


def _switch_to_deepseek_after_exhaustion(detail: str, provider: str = "anthropic") -> None:
    """Mark ``provider``'s month exhausted and move this process to DeepSeek
    (one-way; from OpenAI the next rung is DeepSeek, from Anthropic the
    process skips OpenAI too — a mid-run switch never re-decides)."""
    global _ROUTE
    try:
        from pipeline.llm_usage import mark_exhausted

        mark_exhausted(_state_dir(), datetime.now(timezone.utc).strftime("%Y-%m"), detail, provider)
    except Exception as exc:
        logger.warning("could not record the credit exhaustion: %s", exc)
    with _ROUTE_LOCK:
        if _ROUTE != "deepseek" and _use_deepseek(f"{provider} credits exhausted"):
            _ROUTE = "deepseek"


def call_env(command: list[str]) -> dict[str, str] | None:
    """The env snapshot taken when this thread built ``command`` with
    :func:`claude_command`; ``None`` (inherit) for any other backend."""
    if command and command[0] in ("claude", "codex") and getattr(_CALL, "built", None) is command:
        return getattr(_CALL, "env", None)
    if command and command[0] == "claude":
        return getattr(_CALL, "env", None)
    return None


#: ladder L3 — codex models per CLI role (user decision 2026-11-01): the
#: collector and the evaluator stay on different models (evaluator R2).
OPENAI_CLI_MODELS = {"collect": "gpt-5.6-luna", "eval": "gpt-5.6-sol"}
OPENAI_CLI_MODEL_ENV = {"collect": "TRADINGAGENTS_OPENAI_COLLECT_MODEL", "eval": "TRADINGAGENTS_OPENAI_EVAL_MODEL"}
#: claude --effort → codex model_reasoning_effort.
_CODEX_EFFORT = {"low": "low", "medium": "medium", "high": "high", "xhigh": "high", "max": "high"}
#: Hermetic codex: no user config, no session files, read-only sandbox (it
#: only searches), web search on, JSONL events on stdout.
CODEX_FLAGS = (
    "--ignore-user-config",
    "--ephemeral",
    "--sandbox",
    "read-only",
    "--skip-git-repo-check",
    "--json",
    "-c",
    "tools.web_search=true",
)


def _codex_home() -> Path:
    """A pipeline-only CODEX_HOME: codex keeps auth/state there, never in
    the user's ``~/.codex``."""
    home = Path(_state_dir()) / "codex_home"
    home.mkdir(parents=True, exist_ok=True)
    return home


def _codex_command(role: str, effort: str, extra: tuple[str, ...]) -> tuple[list[str], str, dict[str, str]]:
    """``(argv, model, env)`` for one hermetic ``codex exec`` call. The API
    key goes in the env only (``CODEX_API_KEY``), never on argv."""
    model = (os.environ.get(OPENAI_CLI_MODEL_ENV[role]) or "").strip() or OPENAI_CLI_MODELS[role]
    argv = ["codex", "exec", *CODEX_FLAGS, "-m", model,
            "-c", f"model_reasoning_effort={_CODEX_EFFORT.get(effort, 'medium')}"]
    home = _codex_home()
    rest = list(extra)
    while rest:
        flag = rest.pop(0)
        if flag == "--json-schema" and rest:
            schema = rest.pop(0)
            path = home / "schemas" / f"{hashlib.sha256(schema.encode('utf-8')).hexdigest()[:16]}.json"
            if not path.exists():
                atomic_write(path, schema)
            argv += ["--output-schema", str(path)]
        else:
            raise ValueError(f"claude flag {flag!r} has no codex equivalent")
    argv.append("-")  # prompt on stdin
    env = dict(os.environ)
    env["CODEX_API_KEY"] = env.get("OPENAI_API_KEY", "")
    env["CODEX_HOME"] = str(home)
    return argv, model, env


def claude_command(model: str, effort: str, *extra: str, role: str = "collect") -> list[str]:
    """Hermetic web-only ``claude -p`` on a pinned model/effort, JSON envelope.
    On the DeepSeek route every model is pinned to a name DeepSeek serves as
    Flash (R7); on the OpenAI route (ladder L3) the call is a hermetic
    ``codex exec`` on the ``role``'s GPT model (``collect`` or ``eval``)."""
    if role not in OPENAI_CLI_MODELS:
        raise ValueError(f"unknown CLI role {role!r}")
    cli_route()
    with _ROUTE_LOCK:
        # Route and env are captured together under the lock the DeepSeek
        # switch holds while rewriting os.environ; the launch site passes
        # this env (call_env) so the call runs where it is billed.
        route = _ROUTE
        _CALL.route = route
        _CALL.env = dict(os.environ)
        _CALL.model = None
    if route == "openai":
        argv, cli_model, env = _codex_command(role, effort, extra)
        _CALL.env, _CALL.model, _CALL.built = env, cli_model, argv
        return argv
    if route == "deepseek":
        from pipeline.llm_routing import DEEPSEEK_CLI_MODEL

        model = DEEPSEEK_CLI_MODEL
    command = [
        "claude",
        "-p",
        *HERMETIC_FLAGS,
        "--model",
        model,
        "--effort",
        effort,
        # stream-json: one JSON event per line, ending with the result
        # envelope; -p requires --verbose for it.
        "--output-format",
        "stream-json",
        "--verbose",
        *extra,
    ]
    _CALL.built = command
    return command


#: Bounds on the tool calls kept per usage line (llm-cost R11).
_MAX_TOOL_CALLS = 60
_MAX_TOOL_ARG_CHARS = 300


def _events(stdout: str) -> list[dict]:
    """The JSON object lines of a ``stream-json`` stdout (a one-line ``json``
    envelope is a one-event stream); other lines are skipped. Events are
    split on ``\n`` only: ``str.splitlines`` also breaks on U+2028/U+0085,
    which JSON.stringify leaves raw inside strings."""
    events = []
    for line in (stdout or "").split("\n"):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def _tool_calls(events: list[dict]) -> dict[str, object]:
    """``{"web_search_queries": [...], "web_fetch_urls": [...]}`` from the
    assistant ``tool_use`` blocks, in call order, bounded, plus the
    unbounded ``web_fetch_requests`` count."""
    calls: dict[str, object] = {"web_search_queries": [], "web_fetch_urls": [], "web_fetch_requests": 0}
    wanted = {"WebSearch": ("web_search_queries", "query"), "WebFetch": ("web_fetch_urls", "url")}
    for event in events:
        if event.get("type") != "assistant":
            continue
        message = event.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        for block in content if isinstance(content, list) else ():
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            target = wanted.get(block.get("name"))
            if target is None:
                continue
            field, arg = target
            if field == "web_fetch_urls":  # every call counts, whatever its args
                calls["web_fetch_requests"] += 1
            args = block.get("input")
            value = args.get(arg) if isinstance(args, dict) else None
            # Only real queries/URLs: placeholders would read as duplicates.
            if isinstance(value, str) and value.strip() and len(calls[field]) < _MAX_TOOL_CALLS:
                calls[field].append(value[:_MAX_TOOL_ARG_CHARS])
    return calls


#: Event types only ``codex exec --json`` prints (ladder L3).
_CODEX_EVENT_TYPES = ("thread.started", "turn.started", "turn.completed", "turn.failed")


def _is_codex_stream(events: list[dict]) -> bool:
    return any(event.get("type") in _CODEX_EVENT_TYPES for event in events)


def _codex_envelope(events: list[dict]) -> tuple[dict | None, dict[str, object]]:
    """A claude-shaped result envelope from codex JSONL events: the last
    ``agent_message`` is the result, ``turn.completed`` usage is summed and
    priced (tokens + $0.01 per web search), ``turn.failed`` (or an ``error``
    event with no completed turn) is an error result. ``None`` when the
    stream ended before any turn finished or failed."""
    from pipeline.llm_usage import WEB_SEARCH_USD_PER_CALL, call_cost

    text: str | None = None
    failure: str | None = None
    last_error: str | None = None
    completed = False
    tokens = {"input": 0, "cache_read": 0, "cache_write": 0, "output": 0}
    calls: dict[str, object] = {"web_search_queries": [], "web_fetch_urls": [], "web_fetch_requests": 0}
    searches = 0
    for event in events:
        kind = event.get("type")
        if kind == "turn.completed":
            completed = True
            usage = event.get("usage") if isinstance(event.get("usage"), dict) else {}
            tokens["input"] += _count(usage.get("input_tokens"))
            tokens["cache_read"] += _count(usage.get("cached_input_tokens"))
            tokens["cache_write"] += _count(usage.get("cache_write_input_tokens"))
            tokens["output"] += _count(usage.get("output_tokens"))
        elif kind == "turn.failed":
            error = event.get("error")
            failure = str(error.get("message") if isinstance(error, dict) else error or "turn failed")
        elif kind == "error":
            last_error = str(event.get("message") or "")
        elif kind == "item.completed" and isinstance(event.get("item"), dict):
            item = event["item"]
            if item.get("type") == "agent_message" and isinstance(item.get("text"), str):
                text = item["text"]
            elif item.get("type") == "web_search":
                action = item.get("action") if isinstance(item.get("action"), dict) else {}
                if action.get("type") == "open_page":
                    calls["web_fetch_requests"] += 1
                    url = action.get("url")
                    if isinstance(url, str) and url.strip() and len(calls["web_fetch_urls"]) < _MAX_TOOL_CALLS:
                        calls["web_fetch_urls"].append(url[:_MAX_TOOL_ARG_CHARS])
                else:
                    searches += 1  # every search call bills, whatever its action
                    query = action.get("query")
                    if isinstance(query, str) and query.strip() and len(calls["web_search_queries"]) < _MAX_TOOL_CALLS:
                        calls["web_search_queries"].append(query[:_MAX_TOOL_ARG_CHARS])
    if not completed and failure is None and last_error is None:
        return None, calls
    model = getattr(_CALL, "model", None) or "gpt-unknown"
    try:
        cost = (call_cost(model, {**tokens}) or 0.0) + searches * WEB_SEARCH_USD_PER_CALL
    except Exception:
        cost = searches * WEB_SEARCH_USD_PER_CALL
    is_error = failure is not None or not completed
    envelope: dict = {
        "type": "result",
        "subtype": "error_during_execution" if is_error else "success",
        "is_error": is_error,
        "result": (failure or last_error or "codex turn did not complete") if is_error else (text or ""),
        "usage": {
            "input_tokens": max(tokens["input"] - tokens["cache_read"] - tokens["cache_write"], 0),
            "cache_creation_input_tokens": tokens["cache_write"],
            "cache_read_input_tokens": tokens["cache_read"],
            "output_tokens": tokens["output"],
        },
        "modelUsage": {model: {"webSearchRequests": searches}},
        "total_cost_usd": round(cost, 6),
        "codex": True,
    }
    if not is_error and text is not None:
        try:
            structured = json.loads(text)
        except json.JSONDecodeError:
            structured = None
        if isinstance(structured, dict):
            envelope["structured_output"] = structured
    return envelope, calls


def _result_event(stdout: str) -> tuple[dict | None, dict[str, object]]:
    """The last ``result`` event (``None`` if absent) and the tool calls; a
    codex stream is converted to the same envelope shape (ladder L3)."""
    events = _events(stdout)
    if _is_codex_stream(events):
        return _codex_envelope(events)
    result = next((e for e in reversed(events) if e.get("type") == "result"), None)
    return result, _tool_calls(events)


def envelope_line(stdout: str) -> str:
    """The result envelope as one JSON line (what ``--output-format json``
    printed) for failure messages; ``""`` for a stream cut before its result
    (a crash after ``init``: the old format printed nothing then), and
    ``stdout`` itself when it holds no JSON event (another backend's text)."""
    events = _events(stdout)
    if _is_codex_stream(events):
        result, _ = _codex_envelope(events)
    else:
        result = next((e for e in reversed(events) if e.get("type") == "result"), None)
    if result is not None:
        return json.dumps(result, ensure_ascii=False)  # as the CLI printed it
    return "" if events else stdout


def parse_envelope(stdout: str) -> dict:
    """Validate the successful result envelope of a ``claude -p`` call
    (``stream-json`` events, or a single ``json`` envelope). The web tool
    calls seen in the stream ride along under ``_tool_calls``."""
    envelope, calls = _result_event(stdout)
    if envelope is None:
        raise EnvelopeError("claude -p returned invalid JSON envelope")
    envelope = {**envelope, "_tool_calls": calls}
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


def failure_stdout(backend: str, stdout: str) -> str:
    """The stdout a failed collector call's message quotes: claude's result
    envelope (:func:`envelope_line`), any other backend's stdout as is."""
    return envelope_line(stdout) if backend == "claude" else stdout


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
        envelope, calls = _result_event(stdout)
        if envelope is None:  # a lone JSON object without a "type" still logs
            single = json.loads(stdout)
            # Any typed object is a stream event (the result one is handled
            # above): a stream cut after it printed no envelope.
            envelope = single if isinstance(single, dict) and "type" not in single else None
    except Exception:  # never raises (usage logging is best-effort)
        return
    if envelope is not None:
        record_usage(component, model, effort, {**envelope, "_tool_calls": calls}, outcome="error")


def usage_log_path() -> Path:
    raw = os.environ.get(USAGE_LOG_ENV, "").strip()
    if raw:
        return Path(raw).expanduser()
    state_dir = os.environ.get("TRADINGAGENTS_STATE_DIR", "").strip() or "~/.tradingagents"
    return Path(state_dir).expanduser() / USAGE_LOG_NAME


def _web_search_requests(envelope: dict) -> int:
    """Billed web searches: the CLI's WebSearch runs as its own request, so
    the count is in ``modelUsage[*].webSearchRequests`` (``usage``'s
    ``server_tool_use`` reads 0 for it — measured 2026-11-01)."""
    model_usage = envelope.get("modelUsage")
    if not isinstance(model_usage, dict):
        return 0
    return sum(
        _count(entry.get("webSearchRequests"))
        for entry in model_usage.values()
        if isinstance(entry, dict)
    )


def _tool_call_fields(envelope: dict) -> dict[str, object]:
    """The usage-line web tool fields from ``_tool_calls`` (junk → empty)."""
    calls = envelope.get("_tool_calls")
    calls = calls if isinstance(calls, dict) else {}
    fields: dict[str, object] = {
        field: [str(item) for item in calls[field]][:_MAX_TOOL_CALLS]
        if isinstance(calls.get(field), list) else []
        for field in ("web_search_queries", "web_fetch_urls")
    }
    fields["web_fetch_requests"] = _count(calls.get("web_fetch_requests"))
    return fields


def _count(value: object) -> int:
    """A token count from an envelope field; junk reads as 0 (never raises)."""
    try:
        return max(int(value or 0), 0)
    except Exception:  # TypeError, ValueError, OverflowError (Infinity), ...
        return 0


def _billing(usage: dict, total_cost: object) -> tuple[str, float]:
    """``(billing, billed_usd)`` for one call (R7): DeepSeek is repriced at the
    Flash rate (the CLI's figure is Anthropic-priced); an Anthropic key in
    the env bills the API; otherwise the subscription pays."""
    route = getattr(_CALL, "route", None) or _ROUTE
    if route == "openai":  # the codex envelope carries its own priced cost
        cost = _spend_value(total_cost)
        return "openai-api", cost
    if route == "deepseek":
        from pipeline.llm_usage import call_cost

        cache_read = _count(usage.get("cache_read_input_tokens"))
        tokens = {
            "input": _count(usage.get("input_tokens"))
            + cache_read
            + _count(usage.get("cache_creation_input_tokens")),
            "cache_read": cache_read,
            "output": _count(usage.get("output_tokens")),
        }
        try:
            return "deepseek", round(call_cost("deepseek-flash", tokens) or 0.0, 6)
        except Exception:  # accounting never fails a call
            return "deepseek", 0.0
    if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        return "anthropic-api", _spend_value(total_cost)
    return "subscription", 0.0


def _spend_value(total_cost: object) -> float:
    """A billed cost; NaN would disable the budget gate, Infinity would trip
    it all month, so junk reads as 0."""
    try:
        cost = float(total_cost or 0.0)
    except Exception:
        return 0.0
    return cost if math.isfinite(cost) and cost > 0 else 0.0


def record_usage(
    component: str, model: str, effort: str, envelope: dict, *, outcome: str = "ok"
) -> None:
    """Append one usage line for a finished call (``outcome`` ``ok`` or
    ``error``); never raises."""
    usage = envelope.get("usage") if isinstance(envelope.get("usage"), dict) else {}
    billing, billed = _billing(usage, envelope.get("total_cost_usd"))
    try:
        searches = _web_search_requests(envelope)
        calls = _tool_call_fields(envelope)
    except Exception:  # never raises
        searches, calls = 0, {"web_search_queries": [], "web_fetch_urls": [], "web_fetch_requests": 0}
    provider = {"anthropic-api": "anthropic", "openai-api": "openai"}.get(billing)
    if outcome == "error" and provider is not None:
        detail = str(envelope.get("result") or "")
        from pipeline.llm_usage import is_credit_exhausted_error

        if is_credit_exhausted_error(detail, provider):
            _switch_to_deepseek_after_exhaustion(" ".join(detail.split())[:300], provider)
    line = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "component": component,
        # The model that served the call (codex runs its role's GPT model).
        "model": (getattr(_CALL, "model", None) if billing == "openai-api" else None) or model,
        "effort": effort,
        "outcome": outcome,
        **{name: usage.get(name) for name in _USAGE_FIELDS},
        "total_cost_usd": envelope.get("total_cost_usd"),
        "billing": billing,
        "billed_usd": billed,
        # Web searches bill per request ($10/1K), apart from tokens (R10/R11).
        "web_search_requests": searches,
        **calls,
        "num_turns": envelope.get("num_turns"),
        "duration_ms": envelope.get("duration_ms"),
    }
    path = usage_log_path()
    try:
        # Ticker collectors/evaluators fan out concurrently: append under flock.
        locked_append(path, json.dumps(line, separators=(",", ":")))
    except OSError as exc:
        print(f"warning: could not record claude usage to {path}: {exc}", file=sys.stderr)
