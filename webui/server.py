"""TradingAgents Web UI — local FastAPI server.

Serves a Robinhood-style single-page frontend and a small JSON API that wraps
the TradingAgentsGraph pipeline:

- GET  /api/meta          provider/model/vendor/language option lists
- GET  /api/config        current settings + which API keys are configured
                          (key VALUES are never returned, only presence)
- POST /api/config        persist settings (webui/settings.json + repo .env)
- GET  /api/quote/{t}     price + sparkline series for the hero chart
- POST /api/analyze       start one analysis run (single-flight; 409 when busy)
- GET  /api/runs/{id}     poll status, live log tail, and final reports
- GET  /api/reports       list past report trees from results_dir

Run:  .venv/bin/uvicorn webui.server:app --port 8321
"""

from __future__ import annotations

import copy
import json
import logging
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

REPO_ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = REPO_ROOT / ".env"
SETTINGS_PATH = Path(__file__).resolve().parent / "settings.json"

# Env vars the settings page may write. Everything else in .env is left alone.
KEY_ENV_VARS = [
    "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GOOGLE_API_KEY", "XAI_API_KEY",
    "DEEPSEEK_API_KEY", "DASHSCOPE_API_KEY", "DASHSCOPE_CN_API_KEY",
    "ZHIPU_API_KEY", "ZHIPU_CN_API_KEY", "MINIMAX_API_KEY", "MINIMAX_CN_API_KEY",
    "OPENROUTER_API_KEY", "MISTRAL_API_KEY", "MOONSHOT_API_KEY", "GROQ_API_KEY",
    "NVIDIA_API_KEY", "OPENAI_COMPATIBLE_API_KEY", "ALPHA_VANTAGE_API_KEY",
    "FRED_API_KEY", "OLLAMA_BASE_URL",
]

SETTING_ENV_MAP = {
    "llm_provider":            "TRADINGAGENTS_LLM_PROVIDER",
    "deep_think_llm":          "TRADINGAGENTS_DEEP_THINK_LLM",
    "quick_think_llm":         "TRADINGAGENTS_QUICK_THINK_LLM",
    "backend_url":             "TRADINGAGENTS_LLM_BACKEND_URL",
    "output_language":         "TRADINGAGENTS_OUTPUT_LANGUAGE",
    "max_debate_rounds":       "TRADINGAGENTS_MAX_DEBATE_ROUNDS",
    "max_risk_discuss_rounds": "TRADINGAGENTS_MAX_RISK_ROUNDS",
    "checkpoint_enabled":      "TRADINGAGENTS_CHECKPOINT_ENABLED",
    "temperature":             "TRADINGAGENTS_TEMPERATURE",
    "openai_reasoning_effort": "TRADINGAGENTS_OPENAI_REASONING_EFFORT",
    "google_thinking_level":   "TRADINGAGENTS_GOOGLE_THINKING_LEVEL",
    "anthropic_effort":        "TRADINGAGENTS_ANTHROPIC_EFFORT",
}

VENDOR_OPTIONS = {
    "core_stock_apis":     ["yfinance", "alpha_vantage", "yfinance,alpha_vantage", "default"],
    "technical_indicators": ["yfinance", "alpha_vantage", "yfinance,alpha_vantage", "default"],
    "fundamental_data":    ["yfinance", "alpha_vantage", "yfinance,alpha_vantage", "default"],
    "news_data":           ["yfinance", "alpha_vantage", "yfinance,alpha_vantage", "default"],
    "macro_data":          ["fred"],
    "prediction_markets":  ["polymarket"],
}

LANGUAGES = ["English", "Chinese", "Japanese", "Korean", "Hindi", "Spanish",
             "Portuguese", "French", "German", "Arabic", "Russian"]

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

app = FastAPI(title="TradingAgents Web UI")

_runs: dict[str, dict] = {}
_run_lock = threading.Lock()


# ---------------------------------------------------------------- helpers

def _parse_env_line(raw: str) -> tuple[str, str] | None:
    """Return (key, value) for an assignment line, else None.

    Handles the ``export KEY=value`` form and strips surrounding quotes, so a
    key written either way is still detected as configured.
    """
    line = raw.strip()
    if not line or line.startswith("#") or "=" not in line:
        return None
    key, value = line.split("=", 1)
    key = key.strip()
    if key.startswith("export "):
        key = key[len("export "):].strip()
    if not key:
        return None
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1]
    return key, value


def _read_env_file() -> dict[str, str]:
    values: dict[str, str] = {}
    if ENV_PATH.exists():
        for raw in ENV_PATH.read_text(encoding="utf-8").splitlines():
            parsed = _parse_env_line(raw)
            if parsed:
                # Later duplicates win, matching dotenv's own precedence.
                values[parsed[0]] = parsed[1]
    return values


def _write_env_updates(updates: dict[str, str | None]) -> None:
    """Update, append, or delete KEY=value lines in .env, preserving the rest.

    A ``None`` value deletes every line assigning that key. Deletion matters:
    without it a setting cleared in the UI would keep its stale ``.env`` line
    and silently drive every subsequent run (e.g. a backend_url pointing at a
    proxy that is no longer wanted).
    """
    lines = ENV_PATH.read_text(encoding="utf-8").splitlines() if ENV_PATH.exists() else []
    remaining = dict(updates)
    out: list[str] = []
    for raw in lines:
        parsed = _parse_env_line(raw)
        key = parsed[0] if parsed else None
        if key and key in updates:
            if updates[key] is None:
                continue  # delete this assignment (all duplicates)
            if key in remaining:
                out.append(f"{key}={remaining.pop(key)}")
            # A duplicate assignment of an updated key is dropped, so the
            # written value is the one that actually takes effect.
            continue
        out.append(raw)
    for k, v in remaining.items():
        if v is not None:
            out.append(f"{k}={v}")
    ENV_PATH.write_text("\n".join(out) + "\n", encoding="utf-8")


def _load_settings() -> dict:
    if SETTINGS_PATH.exists():
        return json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    return {"settings": {}, "vendors": {}}


def _save_settings(data: dict) -> None:
    SETTINGS_PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def _build_run_config() -> dict:
    """Fresh pipeline config: .env (re-read) + saved vendor overrides."""
    load_dotenv(ENV_PATH, override=True)
    from tradingagents.default_config import DEFAULT_CONFIG, _apply_env_overrides

    config = _apply_env_overrides(copy.deepcopy(DEFAULT_CONFIG))
    vendors = _load_settings().get("vendors", {})
    for category, vendor in vendors.items():
        if vendor:
            config["data_vendors"][category] = vendor
    return config


class _RunLogHandler(logging.Handler):
    def __init__(self, sink: list[str]):
        super().__init__(level=logging.INFO)
        self.sink = sink
        self.setFormatter(logging.Formatter("%(asctime)s %(name)s: %(message)s", "%H:%M:%S"))

    def emit(self, record):
        self.sink.append(self.format(record))
        del self.sink[:-500]


def _run_analysis(run_id: str, ticker: str, date: str) -> None:
    run = _runs[run_id]
    handler = _RunLogHandler(run["log"])
    logging.getLogger().addHandler(handler)
    try:
        config = _build_run_config()
        run["log"].append(
            f"config: provider={config['llm_provider']} deep={config['deep_think_llm']} "
            f"quick={config['quick_think_llm']}"
        )
        # Reuse the CLI's canonical detection so BTCUSD / BTC-USDT route to the
        # crypto pipeline too, and drop the fundamentals analyst for crypto
        # exactly like the CLI does (no company financials to analyze).
        from cli.models import AnalystType
        from cli.utils import detect_asset_type, filter_analysts_for_asset_type
        from tradingagents.graph.trading_graph import TradingAgentsGraph

        asset = detect_asset_type(ticker)
        analysts = filter_analysts_for_asset_type(list(AnalystType), asset)
        run["log"].append(
            f"asset_type={asset.value} analysts={','.join(a.value for a in analysts)}"
        )
        graph = TradingAgentsGraph(
            debug=False,
            config=config,
            selected_analysts=tuple(a.value for a in analysts),
        )
        final_state, decision = graph.propagate(ticker, date, asset_type=asset.value)
        # save_reports returns the complete_report.md FILE path; the tree lives
        # in its parent directory.
        report_path = Path(graph.save_reports(final_state, ticker))
        report_dir = report_path.parent if report_path.is_file() else report_path
        run.update(
            status="done",
            decision=decision,
            final_decision_md=final_state.get("final_trade_decision", ""),
            report_dir=str(report_dir),
            reports=_read_report_tree(report_dir),
        )
    except Exception as exc:  # surface the failure to the UI, don't die silently
        logging.getLogger(__name__).exception("analysis run failed")
        run.update(status="error", error=f"{type(exc).__name__}: {exc}")
    finally:
        run["finished_at"] = datetime.now(timezone.utc).isoformat()
        logging.getLogger().removeHandler(handler)


def _read_report_tree(report_dir: Path) -> dict[str, str]:
    sections: dict[str, str] = {}
    if not report_dir.exists():
        return sections
    for md in sorted(report_dir.rglob("*.md")):
        rel = md.relative_to(report_dir)
        sections[str(rel)] = md.read_text(encoding="utf-8")
    return sections


def _results_reports_dir() -> Path:
    from tradingagents.default_config import DEFAULT_CONFIG

    return Path(DEFAULT_CONFIG["results_dir"]) / "reports"


# ---------------------------------------------------------------- API models

class ConfigPayload(BaseModel):
    settings: dict = {}
    vendors: dict = {}
    keys: dict = {}


class AnalyzePayload(BaseModel):
    ticker: str
    date: str


# ---------------------------------------------------------------- endpoints

@app.get("/api/meta")
def get_meta():
    from tradingagents.llm_clients.api_key_env import PROVIDER_API_KEY_ENV
    from tradingagents.llm_clients.model_catalog import MODEL_OPTIONS

    providers = [p for p in PROVIDER_API_KEY_ENV if p != "azure"]
    model_suggestions = {
        provider: {
            mode: [
                {"label": label, "value": value}
                for label, value in options
                if value != "custom"
            ]
            for mode, options in modes.items()
        }
        for provider, modes in MODEL_OPTIONS.items()
    }
    return {
        "providers": providers,
        "provider_key_env": PROVIDER_API_KEY_ENV,
        "model_suggestions": model_suggestions,
        "vendor_options": VENDOR_OPTIONS,
        "languages": LANGUAGES,
        "key_env_vars": KEY_ENV_VARS,
    }


@app.get("/api/config")
def get_config():
    env = _read_env_file()
    saved = _load_settings()
    from tradingagents.default_config import DEFAULT_CONFIG

    settings = {k: DEFAULT_CONFIG.get(k) for k in SETTING_ENV_MAP}
    settings.update({k: v for k, v in saved.get("settings", {}).items() if k in SETTING_ENV_MAP})
    vendors = dict(DEFAULT_CONFIG["data_vendors"])
    vendors.update(saved.get("vendors", {}))
    return {
        "settings": settings,
        "vendors": vendors,
        # presence only — never the values
        "keys": {name: bool(env.get(name)) for name in KEY_ENV_VARS},
    }


@app.post("/api/config")
def post_config(payload: ConfigPayload):
    saved = _load_settings()
    if payload.settings:
        clean = {k: v for k, v in payload.settings.items() if k in SETTING_ENV_MAP}
        saved.setdefault("settings", {}).update(clean)
        # A cleared field maps to None so its .env line is removed; leaving the
        # old line would keep overriding the run while the UI shows "default".
        env_updates: dict[str, str | None] = {
            SETTING_ENV_MAP[k]: (None if v in (None, "") else str(v))
            for k, v in clean.items()
        }
        if env_updates:
            _write_env_updates(env_updates)
    if payload.vendors:
        saved.setdefault("vendors", {}).update(
            {k: v for k, v in payload.vendors.items() if k in VENDOR_OPTIONS}
        )
    _save_settings(saved)
    if payload.keys:
        key_updates = {
            k: str(v).strip()
            for k, v in payload.keys.items()
            if k in KEY_ENV_VARS and str(v).strip()
        }
        if key_updates:
            _write_env_updates(key_updates)
    return get_config()


@app.get("/api/quote/{ticker}")
def get_quote(ticker: str, range: str = "1mo"):
    import yfinance as yf

    period = {"1w": "5d", "1mo": "1mo", "3mo": "3mo", "1y": "1y"}.get(range, "1mo")
    try:
        t = yf.Ticker(ticker)
        hist = t.history(period=period, interval="1d")
        if hist.empty:
            raise ValueError("no price data")
        closes = hist["Close"].tolist()
        dates = [d.strftime("%Y-%m-%d") for d in hist.index]
        price = closes[-1]
        base = closes[0]
        # fast_info carries no display name; get_info() does, but it is a slower
        # network call and may fail — fall back to the ticker rather than error.
        info_name = ticker
        try:
            info = t.get_info() or {}
            info_name = info.get("shortName") or info.get("longName") or ticker
        except Exception:
            pass
        return {
            "ticker": ticker.upper(),
            "name": info_name,
            "price": round(price, 2),
            "change": round(price - base, 2),
            "change_pct": round((price - base) / base * 100, 2) if base else 0,
            "points": [
                {"date": d, "close": round(c, 4)}
                for d, c in zip(dates, closes, strict=True)
            ],
        }
    except Exception as exc:
        raise HTTPException(status_code=404, detail=f"quote unavailable: {exc}") from exc


@app.post("/api/analyze")
def post_analyze(payload: AnalyzePayload):
    ticker = payload.ticker.strip().upper()
    if not ticker:
        raise HTTPException(status_code=400, detail="ticker required")
    try:
        d = datetime.strptime(payload.date, "%Y-%m-%d").date()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="date must be YYYY-MM-DD") from exc
    if d > datetime.now().date():
        raise HTTPException(status_code=400, detail="analysis date cannot be in the future")

    with _run_lock:
        if any(r["status"] == "running" for r in _runs.values()):
            raise HTTPException(status_code=409, detail="a run is already in progress")
        run_id = uuid.uuid4().hex[:12]
        _runs[run_id] = {
            "id": run_id, "ticker": ticker, "date": payload.date,
            "status": "running", "log": [],
            "started_at": datetime.now(timezone.utc).isoformat(),
        }
    threading.Thread(
        target=_run_analysis, args=(run_id, ticker, payload.date), daemon=True
    ).start()
    return {"run_id": run_id}


@app.get("/api/runs/{run_id}")
def get_run(run_id: str):
    run = _runs.get(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="unknown run")
    # Snapshot under the lock: the worker thread mutates this dict, and
    # iterating it while it grows raises "dictionary changed size during
    # iteration" — a 500 exactly at completion, which stalls the poller.
    with _run_lock:
        view = {k: v for k, v in run.items() if k != "log"}
        view["log_tail"] = list(run["log"][-120:])
    return view


@app.get("/api/reports")
def list_reports():
    base = _results_reports_dir()
    if not base.exists():
        return []
    entries = [
        {"name": d.name, "mtime": d.stat().st_mtime}
        for d in base.iterdir()
        if d.is_dir()
    ]
    # Sort by recency, not name: a ticker sorting late alphabetically would
    # otherwise push the newest runs past the 50-entry cut.
    entries.sort(key=lambda e: e["mtime"], reverse=True)
    return entries[:50]


@app.get("/api/reports/{name}")
def get_report(name: str):
    base = _results_reports_dir()
    target = (base / name).resolve()
    if base.resolve() not in target.parents or not target.is_dir():
        raise HTTPException(status_code=404, detail="unknown report")
    return {"name": name, "sections": _read_report_tree(target)}


app.mount("/", StaticFiles(directory=Path(__file__).resolve().parent / "static", html=True), name="static")
