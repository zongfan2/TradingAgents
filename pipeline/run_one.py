"""Per-run analysis child — one TradingAgents graph run under one arm bundle.

Spec: specs/analysis-runner.md (R1). The analysis runner spawns one of these
per run because arm switching is impossible in-process: ``TRADINGAGENTS_*``
env overrides apply when ``tradingagents`` is imported (``compare/run.py``
proves the subprocess-env pattern). This child therefore applies its argv/env
contract *before* importing ``tradingagents``:

- arm bundle → ``TRADINGAGENTS_MACRO_SOURCE`` / ``TRADINGAGENTS_TICKER_SOURCE``
  (``brief`` arm = both ``brief``; ``feeds`` arm = both ``feeds`` — one config
  axis, never mixed per-switch);
- per-arm isolation → ``TRADINGAGENTS_RESULTS_DIR`` /
  ``TRADINGAGENTS_MEMORY_LOG_PATH`` under ``<state_dir>/analysis/<arm>/`` so
  reflection memories never leak across arms (compare-harness pattern, R1);
- ``--withhold-ticker-brief`` → ``TRADINGAGENTS_TICKER_BRIEF_DIR`` pointed at
  an empty directory so ``get_ticker_brief`` degrades to the standard
  DATA_UNAVAILABLE sentinel (eval-gating table: ticker eval ``fail`` on a core
  run withholds the contaminated input, never the coverage).

It then runs ``TradingAgentsGraph.propagate``, extracts the trader's fenced
TradePlan JSON block (one repair-reprompt on parse failure via the same LLM),
and prints EXACTLY ONE machine-readable result line on stdout — the
:data:`RESULT_SENTINEL` tag followed by the JSON payload — everything else
(agent logging, graph prints) is redirected to stderr. The sentinel exists
because ``redirect_stdout`` catches Python-level prints only: a C-extension
write to fd 1 lacking a trailing newline would concatenate with a bare JSON
line and silently void a completed, billed run; the parent scans for the tag
instead of trusting "the last line".

CLI::

    python -m pipeline.run_one --ticker NVDA --date 2026-08-03 --session us
                               --arm brief|feeds [--position-context TEXT]
                               [--withhold-ticker-brief] [--debug]

Exit codes: ``0`` — run completed (a missing/unparseable plan is reported in
the result line, not an exit failure); ``1`` — the graph run crashed (the
parent records a ``decision: "ERROR"`` ledger row per runner R3); ``2`` usage.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import logging
import os
import re
import sys
from collections.abc import Callable, MutableMapping
from pathlib import Path

from pipeline.common import SESSIONS
from pipeline.config import load_config
from pipeline.contracts.ledger import Arm, TradePlan

logger = logging.getLogger("pipeline.run_one")

ARMS: tuple[Arm, ...] = ("brief", "feeds")

#: Tag prefixing the single machine-readable stdout result line. The parent
#: (``pipeline.analysis_runner``) locates the result by this sentinel rather
#: than by line position, so stray fd-level stdout bytes that bypass
#: ``redirect_stdout`` (C extensions, future grandchildren inheriting fd 1)
#: cannot corrupt the protocol — even without a trailing newline.
RESULT_SENTINEL = "TRADINGAGENTS_RUN_ONE_RESULT "

#: Fenced code block, optionally tagged ``json`` — the trader emits the plan
#: as the LAST such block after its free-text reasoning.
_FENCE_RE = re.compile(r"```(?:json)?\s*\n(.*?)```", re.DOTALL | re.IGNORECASE)

REPAIR_PROMPT = (
    "Your previous trader response was supposed to end with one fenced ```json "
    "code block containing a single TradePlan JSON object "
    "(specs/trade-plan-ledger-contract.md), but it could not be parsed:\n"
    "{error}\n\n"
    "Previous response:\n{text}\n\n"
    "Reply with ONLY the corrected fenced ```json block — a single JSON object "
    "with the fields action, conviction, add_intent, add_rationale, entry_zone, "
    "stop, targets, horizon_days, invalidation, sizing (object with risk_pct), "
    "and source_levels. No other text."
)


def load_env_file(path: Path, env: MutableMapping[str, str]) -> None:
    """Fill missing keys from a ``KEY=value`` env file (setdefault semantics).

    Unlike ``compare/run.py`` this never overrides inherited env: the parent
    runner injects the authoritative ``TRADINGAGENTS_*`` values, and a stray
    ``TRADINGAGENTS_MACRO_SOURCE`` in ``.env`` must not fight the arm contract.
    """
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if value.strip():
            env.setdefault(key.strip(), value.strip())


def apply_run_env(
    arm: Arm,
    *,
    withhold_ticker_brief: bool = False,
    env: MutableMapping[str, str] | None = None,
) -> dict[str, str]:
    """Apply the argv/env contract; MUST run before importing ``tradingagents``.

    Returns the applied overrides (for the result line / tests). Overrides are
    unconditional — arm identity and per-arm isolation are normative, not
    defaults a stray inherited variable may veto.
    """
    if env is None:
        env = os.environ
    state_dir = load_config(env).state_dir
    iso = state_dir / "analysis" / arm
    overrides = {
        "TRADINGAGENTS_MACRO_SOURCE": arm,
        "TRADINGAGENTS_TICKER_SOURCE": arm,
        "TRADINGAGENTS_RESULTS_DIR": str(iso / "logs"),
        "TRADINGAGENTS_MEMORY_LOG_PATH": str(iso / "memory" / "trading_memory.md"),
    }
    if withhold_ticker_brief:
        empty = state_dir / "analysis" / "withheld_ticker_briefs"
        empty.mkdir(parents=True, exist_ok=True)
        overrides["TRADINGAGENTS_TICKER_BRIEF_DIR"] = str(empty)
    env.update(overrides)
    return overrides


# ---------------------------------------------------------------------------
# TradePlan extraction (+ one repair-reprompt)
# ---------------------------------------------------------------------------


def extract_trade_plan(text: str) -> tuple[dict | None, str | None]:
    """Parse the trader's LAST fenced JSON block as a TradePlan.

    Returns ``(plan_dict, None)`` on success — ``plan_dict`` is the validated
    shape re-dumped through the contract model — or ``(None, reason)`` when no
    block parses. Shape checks only; behavioral validation
    (``validate_trade_plan``) is the parent runner's job.
    """
    blocks = [match.group(1) for match in _FENCE_RE.finditer(text or "")]
    if not blocks and (text or "").strip().startswith("{"):
        blocks = [text.strip()]  # repair replies sometimes drop the fence
    if not blocks:
        return None, "no fenced JSON block found in the trader output"
    errors: list[str] = []
    for block in reversed(blocks):
        try:
            payload = json.loads(block)
        except json.JSONDecodeError as exc:
            errors.append(f"not valid JSON: {exc}")
            continue
        if not isinstance(payload, dict):
            errors.append(f"JSON block is a {type(payload).__name__}, not an object")
            continue
        try:
            plan = TradePlan.model_validate(payload)
        except Exception as exc:
            errors.append(f"TradePlan shape invalid: {exc}")
            continue
        return plan.model_dump(mode="json"), None
    return None, "; ".join(errors[:3])


def extract_with_repair(text: str, llm) -> tuple[dict | None, bool, str | None]:
    """Extract the plan; on failure, one repair-reprompt via the same LLM.

    Returns ``(plan, repair_used, error)``. A second failure is final — the
    parent records the decision as a directional opinion with
    ``plan_valid: false``.
    """
    plan, error = extract_trade_plan(text)
    if plan is not None:
        return plan, False, None
    logger.warning("TradePlan extraction failed (%s); repair-reprompting once", error)
    try:
        response = llm.invoke(REPAIR_PROMPT.format(error=error, text=text))
        repaired = getattr(response, "content", response)
        plan, second_error = extract_trade_plan(str(repaired))
    except Exception as exc:  # repair must never crash the run
        return None, True, f"{error}; repair-reprompt failed: {exc}"
    if plan is not None:
        return plan, True, None
    return None, True, f"{error}; after repair: {second_error}"


def normalize_decision(raw: object, plan: dict | None) -> str:
    """Map the graph's processed signal to ``BUY | SELL | HOLD``.

    Earliest token in the signal text wins; an unrecognizable signal falls
    back to the plan's action, then HOLD.
    """
    text = str(raw or "").upper()
    hits = [(text.index(token), token) for token in ("BUY", "SELL", "HOLD") if token in text]
    if hits:
        return min(hits)[1]
    if plan is not None and plan.get("action") in ("BUY", "SELL", "HOLD"):
        return plan["action"]
    return "HOLD"


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------

#: Injectable graph seam: ``(position_context, debug) -> graph`` where the
#: graph exposes ``propagate``, ``save_reports``, and ``quick_thinking_llm``
#: (the trader's LLM — the repair-reprompt uses the same model).
GraphFactory = Callable[[str | None, bool], object]


def _default_graph_factory(position_context: str | None, debug: bool):
    # Import AFTER apply_run_env: DEFAULT_CONFIG applies TRADINGAGENTS_*
    # overrides at import time (compare/run.py precedent).
    from tradingagents.default_config import DEFAULT_CONFIG
    from tradingagents.graph.trading_graph import TradingAgentsGraph

    config = DEFAULT_CONFIG.copy()
    config["trade_plan_emit"] = True
    config["position_context"] = position_context
    return TradingAgentsGraph(debug=debug, config=config)


def run_once(args, graph_factory: GraphFactory) -> dict:
    """Run the graph and assemble the result payload (stdout JSON line)."""
    asset_type = "crypto" if args.ticker.upper().endswith("-USD") else "stock"
    graph = graph_factory(args.position_context, args.debug)
    final_state, signal = graph.propagate(args.ticker, args.date, asset_type=asset_type)

    trader_text = str(final_state.get("trader_investment_plan") or "")
    plan, repair_used, plan_error = extract_with_repair(trader_text, graph.quick_thinking_llm)

    report_dir = None
    try:
        report_dir = str(graph.save_reports(final_state, args.ticker))
    except Exception as exc:  # reports are a convenience, not the contract
        logger.warning("save_reports failed: %s", exc)

    return {
        "ticker": args.ticker,
        "date": args.date,
        "session": args.session,
        "arm": args.arm,
        "decision": normalize_decision(signal, plan),
        "plan": plan,
        "plan_error": plan_error,
        "repair_used": repair_used,
        "report_dir": report_dir,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m pipeline.run_one",
        description="One TradingAgents analysis run under one arm bundle "
        "(specs/analysis-runner.md R1 child).",
    )
    parser.add_argument("--ticker", required=True)
    parser.add_argument("--date", required=True, metavar="YYYY-MM-DD")
    parser.add_argument("--session", required=True, choices=SESSIONS)
    parser.add_argument("--arm", required=True, choices=ARMS)
    parser.add_argument(
        "--position-context",
        default=None,
        help="rendered position snapshot text injected into the trader prompt",
    )
    parser.add_argument(
        "--withhold-ticker-brief",
        action="store_true",
        help="serve no ticker brief (eval-fail gating: contaminated input withheld)",
    )
    parser.add_argument("--debug", action="store_true", help="stream agent traces (stderr)")
    return parser


def main(argv: list[str] | None = None, graph_factory: GraphFactory | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.INFO, format="%(levelname)s %(name)s: %(message)s", stream=sys.stderr
    )

    # API keys for the analysis LLMs (launchd children inherit no shell rc).
    root_env = Path(__file__).resolve().parent.parent / ".env"
    if root_env.exists():
        load_env_file(root_env, os.environ)
    applied = apply_run_env(args.arm, withhold_ticker_brief=args.withhold_ticker_brief)
    logger.info("arm=%s env: %s", args.arm, applied)

    factory = graph_factory or _default_graph_factory
    real_stdout = sys.stdout
    try:
        # stdout discipline: the graph and its dependencies print freely; the
        # parent parses stdout as exactly one JSON line, so everything the run
        # produces is redirected to stderr and only the result line goes out.
        with contextlib.redirect_stdout(sys.stderr):
            result = run_once(args, factory)
    except KeyboardInterrupt:
        print("run-one: interrupted", file=sys.stderr)
        return 130
    except Exception as exc:
        logger.debug("run failure detail", exc_info=True)
        print(f"run-one: {' '.join(str(exc).split()) or exc.__class__.__name__}", file=sys.stderr)
        return 1
    print(RESULT_SENTINEL + json.dumps(result, ensure_ascii=False), file=real_stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
