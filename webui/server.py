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

plus the pipeline-v2 pages (specs/webui-pages.md — read-only renderer over
local files; the ONLY two write actions are the manual analysis trigger and
the EXECUTION_HALT toggle):

- GET  /api/pipeline/status         both sessions' status files + staleness
- GET  /api/pipeline/pool/{session} latest pool file (all four lists)
- GET  /api/pipeline/briefs         macro + ticker briefs with eval verdicts
- GET  /api/pipeline/brief          one brief's markdown + eval flagged claims
- GET  /api/pipeline/ab             on-demand A/B aggregates (never persisted)
- GET  /api/pipeline/decisions      decision rows joined with orders/outcomes
- GET/POST /api/pipeline/halt       EXECUTION_HALT kill-switch toggle
- POST /api/pipeline/trigger        manual analysis run (never auto-executed)

Run:  .venv/bin/python -m webui.server   (binds 127.0.0.1 only — R4: the
write actions must not be reachable from the network; `uvicorn webui.server:app`
also defaults to 127.0.0.1, but the module entrypoint pins it explicitly)
"""

from __future__ import annotations

import copy
import json
import logging
import re
import subprocess
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from pipeline.common import SESSION_TZ, SESSIONS, session_date, session_for_ticker
from pipeline.config import PipelineConfig, load_config as load_pipeline_config, parse_slot_time
from webui import aggregates as ledger_aggregates

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
        # Mutations of the run dict happen under _run_lock: get_run iterates
        # it, and an unsynchronized update exactly at completion would raise
        # "dictionary changed size during iteration" in the poller.
        with _run_lock:
            run.update(
                status="done",
                decision=decision,
                final_decision_md=final_state.get("final_trade_decision", ""),
                report_dir=str(report_dir),
                reports=_read_report_tree(report_dir),
            )
    except Exception as exc:  # surface the failure to the UI, don't die silently
        logging.getLogger(__name__).exception("analysis run failed")
        with _run_lock:
            run.update(status="error", error=f"{type(exc).__name__}: {exc}")
    finally:
        with _run_lock:
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


# ---------------------------------------------------------------- pipeline v2
# specs/webui-pages.md: read-only renderer over local files (R3 — no external
# network calls). All reads tolerate missing files (R1: pre-first-run state
# renders as "not yet run", never a 500).

#: Adapter S3 kill-switch filename (pipeline.execution_adapter.HALT_FILENAME);
#: literal here so the webui never imports the broker module.
HALT_FILENAME = "EXECUTION_HALT"

#: The note pages 5/trigger must state (adapter S5 via specs/webui-pages.md).
MANUAL_TRIGGER_NOTE = (
    "Manual runs are recorded with trigger 'manual' and are NEVER auto-executed "
    "(execution adapter S5)."
)

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
#: Macro brief names: YYYY-MM-DD.<session>.md, or the legacy YYYY-MM-DD.md.
_MACRO_BRIEF_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})(?:\.(cn|us))?\.md$")

_trigger_runs: dict[str, dict] = {}
_trigger_lock = threading.Lock()


def _pipeline_config() -> PipelineConfig:
    """Fresh env-resolved pipeline config per request — the same resolution the
    orchestrator/components use, so the webui reads exactly their files."""
    return load_pipeline_config()


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)  # module-level so tests can pin the clock


def _read_json(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _halt_path(config: PipelineConfig) -> Path:
    return config.state_dir / HALT_FILENAME


def _halted(config: PipelineConfig) -> bool:
    return _halt_path(config).exists()


# -- page 1: status banner ---------------------------------------------------

def _session_staleness(
    session: str, slot_time: str, status: dict | None, now: datetime
) -> tuple[bool, str | None]:
    """Staleness rule: red when the session has no slot started by
    ``slot_time`` + 1h on its local weekday — evaluated in the SESSION
    timezone (the host tz never participates; orchestrator watchdog rule).
    Mirrors ``run_watchdog``'s two-day scan: a slot within an hour of
    midnight has its deadline on the NEXT calendar day, so yesterday's
    missed slot must be checked too — each slot day is flagged from its
    deadline until the end of the deadline's own calendar day."""
    zone = ZoneInfo(SESSION_TZ[session])
    local = now.astimezone(zone)
    hour, minute = parse_slot_time(slot_time)
    for day_offset in (0, -1):
        slot_day = local.date() + timedelta(days=day_offset)
        if slot_day.weekday() >= 5:  # weekday of the SLOT day, session-local
            continue
        deadline = datetime(
            slot_day.year, slot_day.month, slot_day.day, hour, minute, tzinfo=zone
        ) + timedelta(hours=1)
        if local < deadline or local.date() != deadline.date():
            continue
        date_iso = slot_day.isoformat()
        slot = (status or {}).get("slot") or {}
        if slot.get("date") == date_iso and slot.get("started_at"):
            continue
        return True, (
            f"no {session} slot started by {slot_time}+1h on {date_iso} ({SESSION_TZ[session]})"
        )
    return False, None


def _latest_completed_slot(status: dict | None) -> dict | None:
    """The most recent slot object with components: the current slot when its
    ``finished_at`` is set, else the last history entry."""
    if not status:
        return None
    slot = status.get("slot") or {}
    if slot.get("finished_at"):
        return {**slot, "components": status.get("components") or {}}
    history = status.get("history")
    if isinstance(history, list) and history:
        last = history[-1]
        return last if isinstance(last, dict) else None
    return None


def _summary_line(completed: dict | None) -> str | None:
    """Mirror the orchestrator's end-of-slot summary format (the notification
    text itself is not persisted, so the banner re-derives it)."""
    if not completed:
        return None
    counts: dict[str, int] = {}
    components = completed.get("components") or {}
    for record in components.values():
        status = (record or {}).get("status")
        counts[status] = counts.get(status, 0) + 1
    summary = ", ".join(
        f"{counts[s]} {s}" for s in ("ok", "warn", "failed", "timeout", "skipped") if s in counts
    )
    line = f"slot {completed.get('session')} {completed.get('date')}: {summary or 'no components'}"
    problems = sorted(
        name
        for name, record in components.items()
        if (record or {}).get("status") in ("failed", "timeout")
    )
    if problems:
        line += "; failures: " + ", ".join(problems)
    return line


@app.get("/api/pipeline/status")
def get_pipeline_status():
    config = _pipeline_config()
    now = _now_utc()
    sessions: dict[str, dict] = {}
    for session in sorted(SESSIONS):
        status = _read_json(config.state_dir / f"pipeline_status.{session}.json")
        slot_time = config.sessions[session].slot_time
        stale, stale_reason = _session_staleness(session, slot_time, status, now)
        sessions[session] = {
            "present": status is not None,  # R1: missing file renders "not yet run"
            "slot_time": slot_time,
            "timezone": SESSION_TZ[session],
            "stale": stale,
            "stale_reason": stale_reason,
            "summary_line": _summary_line(_latest_completed_slot(status)),
            "status": status,
        }
    return {"sessions": sessions, "halted": _halted(config)}


# -- page 2: pool view -------------------------------------------------------

@app.get("/api/pipeline/pool/{session}")
def get_pipeline_pool(session: str):
    if session not in SESSIONS:
        raise HTTPException(status_code=404, detail=f"unknown session {session!r}")
    config = _pipeline_config()
    pool_dir = config.pool_dir / session
    files = (
        sorted(p for p in pool_dir.glob("*.json") if _DATE_RE.match(p.stem))
        if pool_dir.is_dir()
        else []
    )
    if not files:
        return {"present": False, "message": "not yet run", "halted": _halted(config)}
    latest = files[-1]  # names are ISO dates, so lexicographic = chronological
    pool = _read_json(latest)
    if pool is None:
        return {
            "present": False,
            "message": f"pool file unreadable: {latest.name}",
            "halted": _halted(config),
        }
    return {
        "present": True,
        "file": latest.name,
        "carried_forward": bool(pool.get("carried_forward")),
        "pool": pool,
        "halted": _halted(config),
    }


# -- page 3: briefs view -----------------------------------------------------

@app.get("/api/pipeline/briefs")
def list_pipeline_briefs():
    config = _pipeline_config()
    # Lazy import: the hash-binding verdict reader lives in tradingagents
    # (read-only import — revision binding included, so a re-collected brief
    # never inherits its predecessor's verdict).
    from tradingagents.dataflows.brief_evals import get_eval_verdict

    macro: list[dict] = []
    if config.macro_brief_dir.is_dir():
        for path in sorted(config.macro_brief_dir.glob("*.md"), reverse=True):
            match = _MACRO_BRIEF_RE.match(path.name)
            if not match:
                continue
            macro.append(
                {
                    "kind": "macro",
                    "name": path.name,
                    "date": match.group(1),
                    "session": match.group(2),  # None for legacy v1 files
                    "verdict": get_eval_verdict(str(path)),
                }
            )

    ticker: list[dict] = []
    if config.ticker_brief_dir.is_dir():
        for ticker_dir in sorted(config.ticker_brief_dir.iterdir()):
            if not ticker_dir.is_dir() or ticker_dir.name == "archive":
                continue
            for path in sorted(ticker_dir.glob("*.md"), reverse=True):
                if not _DATE_RE.match(path.stem):
                    continue
                ticker.append(
                    {
                        "kind": "ticker",
                        "ticker": ticker_dir.name,
                        "name": f"{ticker_dir.name}/{path.name}",
                        "date": path.stem,
                        "session": session_for_ticker(ticker_dir.name),
                        "verdict": get_eval_verdict(str(path)),
                    }
                )
    ticker.sort(key=lambda entry: (entry["date"], entry["ticker"]), reverse=True)
    return {"macro": macro, "ticker": ticker, "halted": _halted(config)}


@app.get("/api/pipeline/brief")
def get_pipeline_brief(kind: str, name: str):
    config = _pipeline_config()
    bases = {"macro": config.macro_brief_dir, "ticker": config.ticker_brief_dir}
    base = bases.get(kind)
    if base is None:
        raise HTTPException(status_code=404, detail=f"unknown brief kind {kind!r}")
    target = (base / name).resolve()
    if base.resolve() not in target.parents or target.suffix != ".md" or not target.is_file():
        raise HTTPException(status_code=404, detail="unknown brief")
    from tradingagents.dataflows.brief_evals import get_eval_verdict

    eval_data = _read_json(target.with_name(target.stem + ".eval.json"))
    return {
        "kind": kind,
        "name": name,
        "markdown": target.read_text(encoding="utf-8"),
        # Hash-bound verdict — 'missing' when the eval is absent, corrupt, or
        # bound to a different brief revision.
        "verdict": get_eval_verdict(str(target)),
        "eval": eval_data,
        "flagged_claims": (eval_data or {}).get("flagged_claims") or [],
        "halted": _halted(config),
    }


# -- page 4: A/B aggregates (computed on demand — NEVER persisted) -----------

@app.get("/api/pipeline/ab")
def get_pipeline_ab(preset: str | None = None, paired_only: bool = False):
    config = _pipeline_config()
    decisions = ledger_aggregates.read_jsonl(config.ledger_dir / "decisions.jsonl")
    result = ledger_aggregates.compute_ab_aggregates(
        decisions,
        ledger_aggregates.read_jsonl(config.ledger_dir / "orders.jsonl"),
        ledger_aggregates.read_jsonl(config.ledger_dir / "outcomes.jsonl"),
        preset=preset or None,
        paired_only=paired_only,
    )
    result["present"] = bool(decisions)  # R1: empty ledger renders "not yet run"
    result["halted"] = _halted(config)
    return result


# -- page 5: decisions & orders ----------------------------------------------

@app.get("/api/pipeline/decisions")
def get_pipeline_decisions(
    date: str | None = None,
    session: str | None = None,
    ticker: str | None = None,
    arm: str | None = None,
):
    config = _pipeline_config()
    decisions = ledger_aggregates.read_jsonl(config.ledger_dir / "decisions.jsonl")
    outcomes = ledger_aggregates.latest_outcomes(
        ledger_aggregates.read_jsonl(config.ledger_dir / "outcomes.jsonl")
    )
    exec_states = ledger_aggregates.derive_execution_states(
        ledger_aggregates.read_jsonl(config.ledger_dir / "orders.jsonl")
    )
    rows = []
    for record in decisions:
        if date and str(record.get("date")) != date:
            continue
        if session and str(record.get("session")) != session:
            continue
        if ticker and str(record.get("ticker")).upper() != ticker.upper():
            continue
        if arm and str(record.get("arm")) != arm:
            continue
        run_id = str(record.get("run_id"))
        rows.append(
            {
                **record,
                # Derived per the ledger-contract join rule (submitted =
                # live-submitted row; filled = plus a later fill refresh).
                "execution": exec_states.get(run_id) or {"state": "none"},
                "outcome": outcomes.get(run_id),
            }
        )
    rows.sort(key=lambda row: str(row.get("decided_at") or ""), reverse=True)
    return {
        "present": bool(decisions),
        "rows": rows[:500],
        "manual_note": MANUAL_TRIGGER_NOTE,
        "halted": _halted(config),
    }


# -- write action 1: EXECUTION_HALT toggle -----------------------------------

class HaltPayload(BaseModel):
    halted: bool


@app.get("/api/pipeline/halt")
def get_pipeline_halt():
    config = _pipeline_config()
    return {"halted": _halted(config), "path": str(_halt_path(config))}


@app.post("/api/pipeline/halt")
def post_pipeline_halt(payload: HaltPayload):
    config = _pipeline_config()
    path = _halt_path(config)
    if payload.halted:
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_text(f"halted via webui at {_now_utc().isoformat()}\n", encoding="utf-8")
    else:
        path.unlink(missing_ok=True)
    return {"halted": _halted(config), "path": str(path)}


# -- write action 2: manual analysis trigger ---------------------------------

class PipelineTriggerPayload(BaseModel):
    ticker: str
    session: str | None = None
    date: str | None = None


def _run_trigger_subprocess(argv: list[str]) -> tuple[int, str, str]:
    """Run the analysis-runner subprocess (module-level so tests fake it)."""
    proc = subprocess.run(argv, capture_output=True, text=True, timeout=7200)
    return proc.returncode, proc.stdout[-4000:], proc.stderr[-4000:]


def _trigger_worker(trigger_id: str, argv: list[str]) -> None:
    run = _trigger_runs[trigger_id]
    try:
        returncode, stdout, stderr = _run_trigger_subprocess(argv)
    except Exception as exc:  # surface, don't die silently
        logging.getLogger(__name__).exception("manual trigger failed")
        with _trigger_lock:
            run.update(status="error", error=f"{type(exc).__name__}: {exc}")
        return
    with _trigger_lock:
        run.update(
            status="done" if returncode == 0 else "error",
            returncode=returncode,
            stdout_tail=stdout,
            stderr_tail=stderr,
        )


@app.post("/api/pipeline/trigger")
def post_pipeline_trigger(payload: PipelineTriggerPayload):
    ticker = payload.ticker.strip().upper()
    if not ticker:
        raise HTTPException(status_code=400, detail="ticker required")
    session = payload.session or session_for_ticker(ticker)
    if session not in SESSIONS:
        raise HTTPException(status_code=400, detail=f"unknown session {session!r}")
    if payload.date:
        try:
            date_iso = datetime.strptime(payload.date, "%Y-%m-%d").date().isoformat()
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="date must be YYYY-MM-DD") from exc
    else:
        date_iso = session_date(session, _now_utc()).isoformat()
    config = _pipeline_config()
    argv = [
        str(config.python_executable),
        "-m",
        "pipeline.analysis_runner",
        "--session",
        session,
        "--date",
        date_iso,
        "--ticker",
        ticker,
    ]
    with _trigger_lock:
        if any(run["status"] == "running" for run in _trigger_runs.values()):
            raise HTTPException(status_code=409, detail="a manual trigger is already running")
        trigger_id = uuid.uuid4().hex[:12]
        _trigger_runs[trigger_id] = {
            "id": trigger_id,
            "ticker": ticker,
            "session": session,
            "date": date_iso,
            "argv": argv,
            "status": "running",
            "started_at": _now_utc().isoformat(),
        }
    threading.Thread(target=_trigger_worker, args=(trigger_id, argv), daemon=True).start()
    return {"trigger_id": trigger_id, "argv": argv, "note": MANUAL_TRIGGER_NOTE}


@app.get("/api/pipeline/trigger/{trigger_id}")
def get_pipeline_trigger(trigger_id: str):
    with _trigger_lock:
        run = _trigger_runs.get(trigger_id)
        if not run:
            raise HTTPException(status_code=404, detail="unknown trigger")
        return {**run, "note": MANUAL_TRIGGER_NOTE}


app.mount("/", StaticFiles(directory=Path(__file__).resolve().parent / "static", html=True), name="static")

# ---------------------------------------------------------------- entrypoint

#: R4 — the server binds loopback only: the write actions (manual trigger,
#: halt toggle) must not be reachable from the network.
HOST = "127.0.0.1"


def main(argv: list[str] | None = None) -> None:
    import argparse

    import uvicorn

    parser = argparse.ArgumentParser(
        prog="python -m webui.server",
        description="TradingAgents local web UI (binds 127.0.0.1 only)",
    )
    parser.add_argument("--port", type=int, default=8321)
    args = parser.parse_args(argv)
    uvicorn.run(app, host=HOST, port=args.port)


if __name__ == "__main__":
    main()
