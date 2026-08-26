#!/usr/bin/env python3
"""Run one TradingAgents analysis under a named comparison preset.

Usage:
    python compare/run.py --preset deepseek --ticker NVDA --date 2026-07-18

Presets live next to this script as env.<name> files. API keys are read from
the repo-root .env; the preset file wins on overlapping keys. Results and the
reflection memory log are isolated per preset under
~/.tradingagents/compare/<preset>/ so one backend's lessons never leak into
another backend's next run.
"""

import argparse
import logging
import os
import sys
from pathlib import Path

COMPARE_DIR = Path(__file__).resolve().parent
REPO_ROOT = COMPARE_DIR.parent


def load_env_file(path: Path) -> None:
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if value.strip():
            os.environ[key.strip()] = value.strip()


def main() -> None:
    # INFO so each autonomous search_news query is visible in the run output —
    # a key axis when comparing how different backends investigate.
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    presets = sorted(p.name[len("env."):] for p in COMPARE_DIR.glob("env.*"))
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--preset", required=True, choices=presets)
    parser.add_argument("--ticker", required=True)
    parser.add_argument("--date", required=True, help="YYYY-MM-DD")
    parser.add_argument(
        "--macro-source",
        choices=["feeds", "brief"],
        default=None,
        help="macro input arm: fixed feeds (upstream behavior) or the cached deep-search "
        "brief; omit to honor TRADINGAGENTS_MACRO_SOURCE from .env/preset (default feeds)",
    )
    parser.add_argument("--debug", action="store_true", help="stream agent traces")
    args = parser.parse_args()

    root_env = REPO_ROOT / ".env"
    if root_env.exists():
        load_env_file(root_env)
    load_env_file(COMPARE_DIR / f"env.{args.preset}")
    if args.macro_source:  # explicit flag wins over .env/preset
        os.environ["TRADINGAGENTS_MACRO_SOURCE"] = args.macro_source

    iso = Path.home() / ".tradingagents" / "compare" / args.preset
    os.environ.setdefault("TRADINGAGENTS_RESULTS_DIR", str(iso / "logs"))
    os.environ.setdefault(
        "TRADINGAGENTS_MEMORY_LOG_PATH", str(iso / "memory" / "trading_memory.md")
    )

    sys.path.insert(0, str(REPO_ROOT))
    # Import after the environment is final: DEFAULT_CONFIG applies
    # TRADINGAGENTS_* overrides at import time.
    from tradingagents.default_config import DEFAULT_CONFIG
    from tradingagents.graph.trading_graph import TradingAgentsGraph

    config = DEFAULT_CONFIG.copy()
    # Read the arm from config (validated/normalized by the env overlay), not
    # the raw env var — banner and preflight must agree with what actually runs.
    macro_source = config["macro_source"]
    print(
        f"[compare:{args.preset}] provider={config['llm_provider']} "
        f"deep={config['deep_think_llm']} quick={config['quick_think_llm']} "
        f"backend={config['backend_url'] or 'provider default'} "
        f"macro={macro_source}"
    )

    if macro_source == "brief":
        # Fail fast before any LLM spend: a missing brief would silently turn
        # the brief arm into "feeds arm minus data" and ruin the A/B.
        from tradingagents.dataflows.macro_brief import get_macro_brief_local

        try:
            brief_head = get_macro_brief_local(args.date).splitlines()[0]
            print(f"[preflight] macro brief OK: {brief_head}")
        except Exception as exc:
            sys.exit(f"[preflight] macro brief unavailable: {exc}")

    asset_type = "crypto" if args.ticker.upper().endswith("-USD") else "stock"
    graph = TradingAgentsGraph(debug=args.debug, config=config)
    final_state, decision = graph.propagate(args.ticker, args.date, asset_type=asset_type)

    report_dir = graph.save_reports(final_state, args.ticker)
    print("\n=== FINAL DECISION ===")
    print(decision)
    print(f"\nReports: {report_dir}")


if __name__ == "__main__":
    main()
