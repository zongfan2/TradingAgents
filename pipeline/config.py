"""Pipeline-side configuration (specs/orchestrator.md + design-doc config appendix).

Small, typed, env-overridable. The orchestrator and installer read only this
module — they never import ``tradingagents`` (pipeline components must run
without the heavy dependencies). Env override names follow the repo's
``TRADINGAGENTS_*`` convention; the pipeline-only keys here are read directly
from the environment rather than through ``tradingagents.default_config``.
"""

from __future__ import annotations

import os
import re
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

#: Orchestrator R1 default per-component subprocess timeouts, in seconds.
#: The fan-out steps (ticker collectors/evaluators) get one aggregate budget.
#: ``pool_builder`` is not pinned by R1 — private default, flagged in review.
DEFAULT_COMPONENT_TIMEOUTS: dict[str, int] = {
    "settle": 900,
    "macro_collector": 1800,
    "macro_evaluator": 1200,
    "pool_builder": 900,
    "ticker_collectors": 2700,
    "ticker_evaluators": 3600,
    "analysis_runner": 7200,
    "execution_adapter": 900,
}

DEFAULT_SLOT_TIME = "08:30"

#: Subscription-backed CLI backends the pipeline can drive.
BACKEND_CHOICES = ("claude", "codex")
#: D19 (2026-08-10): codex performs all deep-search collection (macro, pool
#: nomination, ticker briefs) and claude performs evaluation — collection is
#: the heavier consumer and the Claude subscription's monthly cap was hit
#: once. Collector/evaluator backend independence is preserved, mirrored.
DEFAULT_COLLECT_BACKEND = "codex"
DEFAULT_EVAL_BACKEND = "claude"

_SLOT_TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


def parse_slot_time(slot_time: str) -> tuple[int, int]:
    """``"HH:MM"`` (24h, market-local) → ``(hour, minute)``; raises on junk."""
    match = _SLOT_TIME_RE.match(slot_time)
    if not match:
        raise ValueError(f"invalid slot_time {slot_time!r} — expected 'HH:MM' (24h)")
    return int(match.group(1)), int(match.group(2))


@dataclass(frozen=True)
class SessionSchedule:
    """Per-session scheduling knobs (``sessions.{cn,us}.slot_time``)."""

    slot_time: str = DEFAULT_SLOT_TIME

    def __post_init__(self) -> None:
        parse_slot_time(self.slot_time)  # validate eagerly


def _default_state_dir() -> Path:
    return Path("~/.tradingagents").expanduser()


def _default_sessions() -> dict[str, SessionSchedule]:
    return {"cn": SessionSchedule(), "us": SessionSchedule()}


@dataclass(frozen=True)
class PipelineConfig:
    """Everything the orchestrator/installer need; nothing more.

    Data directories default to subdirectories of ``state_dir`` (the runtime
    home, ``~/.tradingagents``) so tests get full isolation by overriding a
    single field.
    """

    state_dir: Path = field(default_factory=_default_state_dir)
    macro_brief_dir: Path | None = None
    ticker_brief_dir: Path | None = None
    pool_dir: Path | None = None
    ledger_dir: Path | None = None
    sessions: Mapping[str, SessionSchedule] = field(default_factory=_default_sessions)
    component_timeouts: Mapping[str, int] = field(
        default_factory=lambda: dict(DEFAULT_COMPONENT_TIMEOUTS)
    )
    notifications_enabled: bool = True
    pool_max_staleness_days: int = 3
    python_executable: Path = field(default_factory=lambda: Path(sys.executable))
    #: D19 backend roles: deep-search collection vs evaluation. Validated
    #: eagerly against :data:`BACKEND_CHOICES` — a typo'd backend must fail
    #: loudly at config time, never as a mid-slot CLI error.
    collect_backend: str = DEFAULT_COLLECT_BACKEND
    eval_backend: str = DEFAULT_EVAL_BACKEND

    def __post_init__(self) -> None:
        for name in ("collect_backend", "eval_backend"):
            value = getattr(self, name)
            if value not in BACKEND_CHOICES:
                raise ValueError(
                    f"invalid {name} {value!r} — expected one of {sorted(BACKEND_CHOICES)}"
                )
        object.__setattr__(self, "state_dir", Path(self.state_dir).expanduser())
        derived = {
            "macro_brief_dir": "macro_briefs",
            "ticker_brief_dir": "ticker_briefs",
            "pool_dir": "pools",
            "ledger_dir": "ledger",
        }
        for name, subdir in derived.items():
            value = getattr(self, name)
            value = self.state_dir / subdir if value is None else Path(value).expanduser()
            object.__setattr__(self, name, value)
        for session in ("cn", "us"):
            if session not in self.sessions:
                raise ValueError(f"sessions config missing required session {session!r}")


def _env_bool(raw: str | None, default: bool) -> bool:
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def load_config(env: Mapping[str, str] | None = None) -> PipelineConfig:
    """Build a :class:`PipelineConfig` from defaults + ``TRADINGAGENTS_*`` env.

    ``env`` is injectable for tests; ``None`` reads ``os.environ``.
    """
    if env is None:
        env = os.environ

    def path_or_none(key: str) -> Path | None:
        raw = env.get(key)
        return Path(raw).expanduser() if raw else None

    sessions = {
        session: SessionSchedule(
            env.get(f"TRADINGAGENTS_SLOT_TIME_{session.upper()}") or DEFAULT_SLOT_TIME
        )
        for session in ("cn", "us")
    }
    timeouts = dict(DEFAULT_COMPONENT_TIMEOUTS)
    for name in DEFAULT_COMPONENT_TIMEOUTS:
        raw = env.get(f"TRADINGAGENTS_TIMEOUT_{name.upper()}")
        if raw:
            timeouts[name] = int(raw)  # loud on junk — never silently mis-time a component

    def backend(key: str, default: str) -> str:
        # Junk values are rejected by PipelineConfig's eager validation.
        return (env.get(key) or "").strip() or default

    return PipelineConfig(
        state_dir=path_or_none("TRADINGAGENTS_STATE_DIR") or _default_state_dir(),
        macro_brief_dir=path_or_none("TRADINGAGENTS_MACRO_BRIEF_DIR"),
        ticker_brief_dir=path_or_none("TRADINGAGENTS_TICKER_BRIEF_DIR"),
        pool_dir=path_or_none("TRADINGAGENTS_POOL_DIR"),
        ledger_dir=path_or_none("TRADINGAGENTS_LEDGER_DIR"),
        sessions=sessions,
        component_timeouts=timeouts,
        notifications_enabled=_env_bool(env.get("TRADINGAGENTS_NOTIFICATIONS_ENABLED"), True),
        pool_max_staleness_days=int(env.get("TRADINGAGENTS_POOL_MAX_STALENESS_DAYS") or 3),
        python_executable=Path(env.get("TRADINGAGENTS_PIPELINE_PYTHON") or sys.executable),
        collect_backend=backend("TRADINGAGENTS_COLLECT_BACKEND", DEFAULT_COLLECT_BACKEND),
        eval_backend=backend("TRADINGAGENTS_EVAL_BACKEND", DEFAULT_EVAL_BACKEND),
    )
