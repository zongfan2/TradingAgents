"""Slot orchestrator — scheduling gate, slot driver, status files, notifications.

Spec: specs/orchestrator.md. One launchd-fired invocation drives a whole
session slot: settle → macro collect/eval → pool build → ticker collect/eval →
analysis → execution → notify. Steps whose components have not landed yet
(settle 0, ticker evaluators 5, adapter 7) are registered placeholders
recorded as ``skipped`` — the driver, ordering, barrier, status, lock, and
notification machinery are fully live, and step 6 runs the real analysis
runner behind the macro-evaluator join barrier.

Design points implemented here:

- Slot identity is session-local (``session_date``): a cn slot firing Sunday
  18:30/19:30 America/Chicago IS Monday's Shanghai session.
- Steps 1 and 3 run in parallel; the macro evaluator (2) overlaps 3–5 but is
  joined — with its outcome recorded in the status file — before step 6.
- Component crash/timeout ⇒ status ``failed``/``timeout``, slot continues. An
  evaluator that completes with a ``fail`` *verdict* is ``warn`` (the
  component worked; the content failed). The same worked-but-degraded rule
  maps a pool builder that exits 0 with a carried-forward pool (its R4
  nomination-failure degrade, announced by its stderr warning) and a ticker
  collector whose exit-0 JSON summary line reports failed tickers (its R6
  partial success) to ``warn``, with the reason surfaced in ``error``.
- The subprocess boundary (component CLIs, ``osascript``) is injectable so
  tests run fully offline.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from pipeline.common import (
    SESSION_TZ,
    SESSIONS,
    append_log_line,
    atomic_write,
    session_date,
    to_utc_iso,
)
from pipeline.config import PipelineConfig, load_config, parse_slot_time

logger = logging.getLogger("pipeline.orchestrator")

#: Wrapper acceptance window around slot_time, evaluated in the session tz.
SLOT_WINDOW_MINUTES = 15
#: R2: locks older than this are broken with a warning.
STALE_LOCK_HOURS = 12
#: R4: size-based rotation — keep the last 5 files of ≤ 10 MB.
LOG_MAX_BYTES = 10 * 1024 * 1024
LOG_KEEP = 5
#: Status files retain the last 20 completed slots.
HISTORY_LIMIT = 20
#: Watchdog: alert when a weekday slot has not started by slot_time + 1h.
WATCHDOG_GRACE = timedelta(hours=1)

Clock = Callable[[], datetime]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Subprocess boundary (injectable)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RunResult:
    """What the orchestrator needs back from a component subprocess."""

    returncode: int
    stdout: str = ""
    stderr: str = ""


Runner = Callable[[Sequence[str], float], RunResult]

#: Data-directory env vars the components resolve themselves, mapped to the
#: :class:`PipelineConfig` field carrying the orchestrator's resolved path.
_DATA_DIR_ENV_KEYS = {
    "TRADINGAGENTS_STATE_DIR": "state_dir",
    "TRADINGAGENTS_MACRO_BRIEF_DIR": "macro_brief_dir",
    "TRADINGAGENTS_TICKER_BRIEF_DIR": "ticker_brief_dir",
    "TRADINGAGENTS_POOL_DIR": "pool_dir",
    "TRADINGAGENTS_LEDGER_DIR": "ledger_dir",
}


def component_env(config: PipelineConfig) -> dict[str, str]:
    """Subprocess environment carrying the orchestrator's *resolved* data dirs.

    Split-brain guard: components resolve their directories from their own
    ``TRADINGAGENTS_*_DIR`` env vars only (the macro collector/evaluator never
    read ``TRADINGAGENTS_STATE_DIR``), while the orchestrator derives unset
    dirs from ``state_dir``. Exporting every resolved path makes each
    component read and write exactly the directories the orchestrator uses —
    a ``TRADINGAGENTS_STATE_DIR``-only override otherwise sends the collector
    to ``~/.tradingagents`` while the evaluator argv points elsewhere.
    """
    env = dict(os.environ)
    for key, attr in _DATA_DIR_ENV_KEYS.items():
        env[key] = str(getattr(config, attr))
    return env


def make_subprocess_runner(env: Mapping[str, str] | None = None) -> Runner:
    """Default-runner factory: one component per subprocess (R1).

    The component is started in its own session so a timeout can kill the
    whole process group — ``subprocess.run(timeout=...)`` would kill only the
    direct child, orphaning the component's backend CLI grandchild
    (``claude -p`` / ``codex exec``), which would keep burning quota. Raises
    :class:`subprocess.TimeoutExpired` on timeout.
    """
    frozen_env = dict(env) if env is not None else None

    def run(argv: Sequence[str], timeout: float) -> RunResult:
        proc = subprocess.Popen(
            list(argv),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=frozen_env,
            start_new_session=True,
        )
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()
            raise
        return RunResult(proc.returncode, stdout, stderr)

    return run


#: Module-level default runner (inherits this process's environment).
subprocess_runner: Runner = make_subprocess_runner()


# ---------------------------------------------------------------------------
# Component registry (slot-sequence table)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SlotContext:
    session: str
    slot_date: date
    config: PipelineConfig


ArgvBuilder = Callable[[SlotContext], "list[str]"]


@dataclass(frozen=True)
class Component:
    """One row of the slot-sequence table.

    ``build_argv`` ``None`` marks a registered placeholder: the step exists in
    every status file as ``skipped`` until its component lands. ``on_failure``
    documents the table's policy — every current row continues the slot.
    """

    step: int
    name: str
    build_argv: ArgvBuilder | None
    us_only: bool = False
    on_failure: str = "continue"
    placeholder_reason: str = "component not yet implemented"


def macro_brief_path(config: PipelineConfig, session: str, slot_date: date) -> Path:
    return config.macro_brief_dir / f"{slot_date.isoformat()}.{session}.md"


def macro_eval_path(config: PipelineConfig, session: str, slot_date: date) -> Path:
    return config.macro_brief_dir / f"{slot_date.isoformat()}.{session}.eval.json"


def build_default_registry(config: PipelineConfig) -> tuple[Component, ...]:
    """The slot-sequence table with today's real components wired in."""
    python = str(config.python_executable)

    def collector_argv(ctx: SlotContext) -> list[str]:
        return [
            python,
            "-m",
            "pipeline.macro_collector",
            "--session",
            ctx.session,
            "--date",
            ctx.slot_date.isoformat(),
        ]

    def evaluator_argv(ctx: SlotContext) -> list[str]:
        return [
            python,
            "-m",
            "pipeline.evaluator",
            str(macro_brief_path(ctx.config, ctx.session, ctx.slot_date)),
        ]

    def pool_builder_argv(ctx: SlotContext) -> list[str]:
        return [
            python,
            "-m",
            "pipeline.pool_builder",
            "--session",
            ctx.session,
            "--date",
            ctx.slot_date.isoformat(),
        ]

    def ticker_collector_argv(ctx: SlotContext) -> list[str]:
        return [
            python,
            "-m",
            "pipeline.ticker_collector",
            "--session",
            ctx.session,
            "--date",
            ctx.slot_date.isoformat(),
        ]

    def analysis_runner_argv(ctx: SlotContext) -> list[str]:
        return [
            python,
            "-m",
            "pipeline.analysis_runner",
            "--session",
            ctx.session,
            "--date",
            ctx.slot_date.isoformat(),
        ]

    return (
        # Settle (22v.10) is explicitly out of scope — registered no-op placeholder.
        Component(0, "settle", None, placeholder_reason="settle step (22v.10) not in scope yet"),
        Component(1, "macro_collector", collector_argv),
        Component(2, "macro_evaluator", evaluator_argv),
        Component(3, "pool_builder", pool_builder_argv),
        Component(4, "ticker_collectors", ticker_collector_argv),
        Component(5, "ticker_evaluators", None),
        # Step 6 runs strictly after the macro-evaluator join barrier (the
        # _POST_BARRIER_STEPS split below) — a late verdict can never be
        # bypassed by timing (specs/orchestrator.md slot sequence).
        Component(6, "analysis_runner", analysis_runner_argv),
        Component(7, "execution_adapter", None, us_only=True),
    )


# ---------------------------------------------------------------------------
# R2 — slot lock
# ---------------------------------------------------------------------------


class SlotLockHeld(RuntimeError):
    """Another invocation holds this slot's lock (double-fire dedupe)."""


def slot_lock_path(config: PipelineConfig, session: str, slot_date: date) -> Path:
    return config.state_dir / "locks" / f"{session}-{slot_date.isoformat()}.lock"


def acquire_slot_lock(
    config: PipelineConfig,
    session: str,
    slot_date: date,
    *,
    stale_after_hours: float = STALE_LOCK_HOURS,
) -> Path:
    """``O_CREAT|O_EXCL`` lockfile; stale locks (> 12h mtime) broken with a
    warning. Atomic — no status-file TOCTOU."""
    path = slot_lock_path(config, session, slot_date)
    path.parent.mkdir(parents=True, exist_ok=True)
    for attempt in (0, 1):
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            try:
                age_s = time.time() - path.stat().st_mtime
            except FileNotFoundError:
                continue  # raced away between open and stat — retry the create
            if attempt == 0 and age_s > stale_after_hours * 3600:
                logger.warning(
                    "breaking stale slot lock %s (age %.1fh > %.0fh)",
                    path,
                    age_s / 3600,
                    stale_after_hours,
                )
                # Rename-aside instead of unlink-by-path: only the process
                # whose rename succeeds owns the break. A plain unlink could
                # delete the FRESH lock a concurrent breaker created between
                # our stat and unlink, letting two orchestrators run the slot.
                aside = path.with_name(f"{path.name}.stale.{os.getpid()}.{time.time_ns()}")
                try:
                    os.rename(path, aside)
                except FileNotFoundError:
                    continue  # a concurrent breaker won — retry the create
                aside.unlink(missing_ok=True)
                continue
            raise SlotLockHeld(f"slot lock held: {path}") from None
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(f"pid={os.getpid()} acquired={to_utc_iso(_utc_now())}\n")
        return path
    raise SlotLockHeld(f"slot lock held: {path}")


def release_slot_lock(path: Path) -> None:
    path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# R4 — orchestrator log with size rotation
# ---------------------------------------------------------------------------


#: Serializes in-process rotation (main thread + evaluator thread).
_ROTATE_LOCK = threading.Lock()


def rotate_log(path: Path, *, max_bytes: int = LOG_MAX_BYTES, keep: int = LOG_KEEP) -> None:
    """Rotate ``path`` → ``path.1`` … keeping ``keep`` files total.

    Concurrency-safe: the module lock serializes the two in-process writers,
    and a rename chain raced away by the separate watchdog process (stat says
    the file exists, another rotator moves it first) is treated as
    already-rotated instead of raising into the caller's thread.
    """
    with _ROTATE_LOCK:
        try:
            if path.stat().st_size < max_bytes:
                return
            path.with_name(f"{path.name}.{keep - 1}").unlink(missing_ok=True)
            for i in range(keep - 2, 0, -1):
                src = path.with_name(f"{path.name}.{i}")
                if src.exists():
                    src.rename(path.with_name(f"{path.name}.{i + 1}"))
            path.rename(path.with_name(f"{path.name}.1"))
        except FileNotFoundError:
            return  # another rotator got there first — nothing left to do


def orchestrator_log_path(config: PipelineConfig) -> Path:
    return config.state_dir / "orchestrator.log"


# ---------------------------------------------------------------------------
# Notifications (osascript via injectable runner)
# ---------------------------------------------------------------------------

OsascriptRunner = Callable[[Sequence[str]], int]


def default_osascript_runner(argv: Sequence[str]) -> int:
    proc = subprocess.run(list(argv), capture_output=True, timeout=15)
    return proc.returncode


def _applescript_str(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


class Notifier:
    """macOS ``display notification`` with per-(component, slot, reason) dedup.

    An osascript failure degrades to log-only — notifications can never fail a
    slot. ``enabled=False`` (config ``notifications_enabled``) is log-only too.
    """

    def __init__(
        self,
        enabled: bool = True,
        runner: OsascriptRunner | None = None,
    ) -> None:
        self.enabled = enabled
        self._runner = runner or default_osascript_runner
        self._sent: set[tuple] = set()
        self.delivered: list[tuple[str, str]] = []

    def notify(self, title: str, message: str, dedup_key: tuple | None = None) -> bool:
        if dedup_key is not None:
            if dedup_key in self._sent:
                return False
            self._sent.add(dedup_key)
        logger.info("notification [%s] %s", title, message)
        if not self.enabled:
            return False
        script = (
            f"display notification {_applescript_str(message)} "
            f"with title {_applescript_str(title)}"
        )
        try:
            returncode = self._runner(["osascript", "-e", script])
        except Exception as exc:  # osascript failure degrades to log-only
            logger.warning("osascript failed (%s) — notification degraded to log-only", exc)
            return False
        if returncode != 0:
            logger.warning("osascript exited %d — notification degraded to log-only", returncode)
            return False
        self.delivered.append((title, message))
        return True


# ---------------------------------------------------------------------------
# Status file (~/.tradingagents/pipeline_status.<session>.json)
# ---------------------------------------------------------------------------


class StatusFile:
    """Atomic write-as-you-go status: the file is rewritten after every
    component update, so a kill mid-slot always leaves a readable file showing
    the in-flight component. Thread-safe (the evaluator thread writes too)."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()
        self._data: dict[str, Any] | None = None

    def start_slot(self, session: str, slot_date: date, started_at: str) -> None:
        with self._lock:
            self._data = {
                "slot": {
                    "date": slot_date.isoformat(),
                    "session": session,
                    "started_at": started_at,
                    "finished_at": None,
                },
                "components": {},
                "history": self._read_existing_history(),
            }
            self._write()

    def _read_existing_history(self) -> list[dict[str, Any]]:
        try:
            prior = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, UnicodeDecodeError):
            return []
        history = prior.get("history")
        if not isinstance(history, list):
            return []
        return history[-HISTORY_LIMIT:]

    def update_component(self, name: str, record: Mapping[str, Any]) -> None:
        with self._lock:
            assert self._data is not None, "start_slot() before update_component()"
            self._data["components"][name] = dict(record)
            self._write()

    def components_snapshot(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            if self._data is None:
                return {}
            return {name: dict(rec) for name, rec in self._data["components"].items()}

    def finish_slot(self, finished_at: str) -> None:
        with self._lock:
            assert self._data is not None, "start_slot() before finish_slot()"
            self._data["slot"]["finished_at"] = finished_at
            completed = {**self._data["slot"], "components": dict(self._data["components"])}
            self._data["history"] = (self._data["history"] + [completed])[-HISTORY_LIMIT:]
            self._write()

    def _write(self) -> None:
        atomic_write(self.path, json.dumps(self._data, indent=2) + "\n")


def status_file_path(config: PipelineConfig, session: str) -> Path:
    return config.state_dir / f"pipeline_status.{session}.json"


# ---------------------------------------------------------------------------
# The slot driver
# ---------------------------------------------------------------------------

_PRE_PARALLEL_STEPS = (0,)
_EVAL_THREAD_STEPS = (1, 2)  # macro collector then evaluator, off the main thread
_MAIN_STEPS = (3, 4, 5)  # pool builder + ticker fan-outs, parallel to 1–2
_POST_BARRIER_STEPS = (6, 7)  # analysis + execution — after the evaluator join


class Orchestrator:
    def __init__(
        self,
        config: PipelineConfig,
        session: str,
        *,
        slot_date: date | None = None,
        registry: Iterable[Component] | None = None,
        runner: Runner | None = None,
        notifier: Notifier | None = None,
        clock: Clock | None = None,
    ) -> None:
        if session not in SESSIONS:
            raise ValueError(f"unknown session {session!r} — expected one of {sorted(SESSIONS)}")
        self.config = config
        self.session = session
        self.clock = clock or _utc_now
        self.slot_date = slot_date or session_date(session, self.clock())
        components = tuple(registry) if registry is not None else build_default_registry(config)
        self.registry = tuple(sorted(components, key=lambda c: c.step))
        # The default runner exports the resolved data dirs so component
        # subprocesses resolve the SAME directories as this orchestrator.
        self.runner = runner or make_subprocess_runner(component_env(config))
        self.notifier = notifier or Notifier(enabled=config.notifications_enabled)
        self.status = StatusFile(status_file_path(config, session))
        self.ctx = SlotContext(session, self.slot_date, config)
        #: Step 6's parsed slot-summary counts, carried into the end-of-slot
        #: notification (spec: "plans produced, orders submitted/dry-run,
        #: pairs run, failures"). ``None`` until the runner reports a summary.
        self._analysis_counts: dict[str, int] | None = None

    # -- selection (R3) -----------------------------------------------------

    def _select(self, only: str | None, from_step: int | None) -> tuple[Component, ...]:
        if only is not None and from_step is not None:
            raise ValueError("--only and --from are mutually exclusive")
        if only is not None:
            selected = tuple(c for c in self.registry if c.name == only)
            if not selected:
                names = ", ".join(c.name for c in self.registry)
                raise ValueError(f"unknown component {only!r} — expected one of: {names}")
            return selected
        if from_step is not None:
            selected = tuple(c for c in self.registry if c.step >= from_step)
            if not selected:
                raise ValueError(f"--from {from_step} selects no steps")
            return selected
        return self.registry

    # -- driver -------------------------------------------------------------

    def run_slot(self, *, only: str | None = None, from_step: int | None = None) -> int:
        """Run the slot; returns 0 unless a component failed or timed out.

        Raises :class:`SlotLockHeld` when another invocation owns the slot.
        """
        selected = self._select(only, from_step)
        by_step = {c.step: c for c in selected}
        lock = acquire_slot_lock(self.config, self.session, self.slot_date)
        try:
            self.status.start_slot(self.session, self.slot_date, to_utc_iso(self.clock()))

            def run_steps(steps: Sequence[int]) -> None:
                for step in steps:
                    component = by_step.get(step)
                    if component is None:
                        continue
                    try:
                        self._run_component(component)
                    except Exception as exc:
                        # Belt-and-suspenders: _run_component already maps
                        # runner failures to statuses; anything escaping here
                        # would kill this thread and leave the component with
                        # no recorded outcome — breaking the join barrier's
                        # "macro eval outcome recorded before step 7" rule.
                        logger.exception("driver error in component %s", component.name)
                        with contextlib.suppress(Exception):
                            prior = self.status.components_snapshot().get(component.name, {})
                            now_iso = to_utc_iso(self.clock())
                            self.status.update_component(
                                component.name,
                                {
                                    "status": "failed",
                                    "started_at": prior.get("started_at") or now_iso,
                                    "finished_at": now_iso,
                                    "duration_s": prior.get("duration_s"),
                                    "error": f"orchestrator error: {exc}",
                                },
                            )

            run_steps(_PRE_PARALLEL_STEPS)
            eval_thread = threading.Thread(
                target=run_steps,
                args=(_EVAL_THREAD_STEPS,),
                name=f"macro-{self.session}-{self.slot_date.isoformat()}",
            )
            eval_thread.start()
            run_steps(_MAIN_STEPS)
            # Join barrier: the macro evaluator must have terminated — and its
            # outcome must be recorded in the status file — before step 6.
            eval_thread.join()
            run_steps(_POST_BARRIER_STEPS)
            return self._finalize()
        finally:
            release_slot_lock(lock)

    # -- one component ------------------------------------------------------

    def _run_component(self, component: Component) -> None:
        name = component.name
        started = self.clock()
        started_iso = to_utc_iso(started)

        if component.us_only and self.session != "us":
            self._record_skip(name, started_iso, "us slot only")
            return
        if component.build_argv is None:
            self._record_skip(name, started_iso, component.placeholder_reason)
            return

        argv = component.build_argv(self.ctx)
        timeout = float(self.config.component_timeouts.get(name, 900))
        # Write-as-you-go: the in-flight component is visible on disk, so a
        # kill mid-component leaves a readable status file naming it.
        self.status.update_component(
            name,
            {
                "status": "running",
                "started_at": started_iso,
                "finished_at": None,
                "duration_s": None,
                "error": None,
            },
        )
        error: str | None = None
        try:
            result = self.runner(argv, timeout)
        except subprocess.TimeoutExpired:
            status = "timeout"
            error = f"timeout after {timeout:.0f}s"
        except Exception as exc:  # component crash — the slot continues
            status = "failed"
            error = f"runner error: {exc}"
        else:
            if result.returncode != 0:
                status = "failed"
                error = _one_line_reason(result) or f"exit {result.returncode}"
            elif name == "macro_evaluator":
                status, error = self._evaluator_outcome()
            elif name == "pool_builder":
                status, error = _pool_builder_outcome(result)
            elif name == "ticker_collectors":
                status, error = _ticker_collector_outcome(result)
            elif name == "analysis_runner":
                status, error = _analysis_runner_outcome(result)
                self._analysis_counts = _analysis_summary_counts(result)
            else:
                status = "ok"
        finished = self.clock()
        record = {
            "status": status,
            "started_at": started_iso,
            "finished_at": to_utc_iso(finished),
            "duration_s": round((finished - started).total_seconds(), 1),
            "error": error,
        }
        self.status.update_component(name, record)
        self._log_component(name, status, record["duration_s"], error)
        slot_key = (self.session, self.slot_date.isoformat())
        if status in ("failed", "timeout"):
            self.notifier.notify(
                "TradingAgents pipeline",
                f"{name} {status} ({self.session} {self.slot_date.isoformat()}): {error}",
                dedup_key=(name, slot_key, status),
            )
        elif error is not None and error.startswith("verdict=fail"):
            self.notifier.notify(
                "TradingAgents pipeline",
                f"{name} ({self.session} {self.slot_date.isoformat()}): {error}",
                dedup_key=(name, slot_key, "verdict=fail"),
            )

    def _record_skip(self, name: str, at_iso: str, reason: str) -> None:
        self.status.update_component(
            name,
            {
                "status": "skipped",
                "started_at": at_iso,
                "finished_at": at_iso,
                "duration_s": 0.0,
                "error": reason,
            },
        )
        self._log_component(name, "skipped", 0.0, reason)

    def _evaluator_outcome(self) -> tuple[str, str | None]:
        """Map a completed evaluation to component status.

        Completed-with-``fail``-verdict is ``warn`` — the component worked;
        the content failed (spec status rules + evaluator R5)."""
        path = macro_eval_path(self.config, self.session, self.slot_date)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, UnicodeDecodeError):
            return "warn", f"evaluator exited 0 but eval json unreadable: {path.name}"
        if data.get("verdict") == "fail":
            flags = len(data.get("flagged_claims") or [])
            return "warn", f"verdict=fail: {flags} flagged claims"
        return "ok", None

    def _log_component(self, name: str, status: str, duration_s: float, error: str | None) -> None:
        try:
            log_path = orchestrator_log_path(self.config)
            rotate_log(log_path)
            append_log_line(
                log_path,
                to_utc_iso(self.clock()),
                self.session,
                self.slot_date.isoformat(),
                name,
                status,
                f"{duration_s:.1f}s",
                error or "-",
            )
        except Exception as exc:  # logging can never fail a slot or kill a thread
            logger.warning("component log write failed for %s: %s", name, exc)

    # -- end of slot --------------------------------------------------------

    def _finalize(self) -> int:
        components = self.status.components_snapshot()
        counts: dict[str, int] = {}
        for record in components.values():
            counts[record["status"]] = counts.get(record["status"], 0) + 1
        summary = ", ".join(
            f"{counts[s]} {s}" for s in ("ok", "warn", "failed", "timeout", "skipped") if s in counts
        )
        problems = sorted(
            name for name, rec in components.items() if rec["status"] in ("failed", "timeout")
        )
        message = f"slot {self.session} {self.slot_date.isoformat()}: {summary or 'no components'}"
        if self._analysis_counts is not None:
            # Spec (Notifications): the end-of-slot summary reports plans
            # produced / pairs run / failures. Orders stay n/a until the
            # execution adapter lands (step 7 placeholder).
            counts = self._analysis_counts
            message += (
                f"; plans {counts['completed']}/{counts['planned']}, "
                f"pairs {counts['pairs']} run, "
                f"invalid plans {counts['invalid_plans']}, "
                f"run errors {counts['errors']}, orders n/a"
            )
        if problems:
            message += "; failures: " + ", ".join(problems)
        # End-of-slot summary is always sent (dedup key still guards retries).
        self.notifier.notify(
            "TradingAgents pipeline",
            message,
            dedup_key=("summary", (self.session, self.slot_date.isoformat()), "end-of-slot"),
        )
        self.status.finish_slot(to_utc_iso(self.clock()))
        return 1 if problems else 0


def _one_line_reason(result: RunResult, limit: int = 300) -> str:
    """Last non-empty stderr line (collectors put the one-line reason there)."""
    for line in reversed(result.stderr.splitlines()):
        if line.strip():
            return line.strip()[:limit]
    return ""


def _last_nonempty_line(text: str) -> str:
    for line in reversed(text.splitlines()):
        if line.strip():
            return line.strip()
    return ""


def _pool_builder_outcome(result: RunResult) -> tuple[str, str | None]:
    """Map a pool-builder exit 0 to component status (pool-builder R4).

    A nomination failure degrades *inside* the builder: it writes a
    carried-forward pool, warns on stderr, and exits 0 — surfaced here as
    ``warn`` per the failure table ("builder self-handles nomination
    failure"): the component worked, the content degraded. Its CLI announces
    the outcome as the final stdout line (``written: <path>`` |
    ``carried_forward: <path>``); the error text prefers the builder's own
    stderr warning (the one-line nomination-failure reason). Hard crashes
    exit non-zero and never reach this mapper — they map to ``failed`` and
    the slot continues (downstream reads the most recent prior pool file).

    Defensive symmetry with :func:`_ticker_collector_outcome`: the mapper
    cross-checks the builder's carried-forward stderr marker (so stdout drift
    can never hide a degraded pool as ``ok``), and an exit 0 whose final
    stdout line matches neither outcome format is a *reporting* gap —
    ``warn``, never a silent ``ok``.
    """
    outcome_line = _last_nonempty_line(result.stdout)
    carried_warning = ""
    for line in result.stderr.splitlines():
        stripped = line.strip()
        if stripped.startswith("pool-builder: warning:") and (
            "carrying opportunity/watch forward" in stripped
        ):
            carried_warning = stripped  # keep the last (most specific) warning
    if outcome_line.startswith("carried_forward:") or carried_warning:
        return "warn", (carried_warning or outcome_line)[:300]
    if outcome_line.startswith("written:"):
        return "ok", None
    return "warn", "exited 0 but stdout has no 'written:'/'carried_forward:' outcome line"


def _ticker_collector_outcome(result: RunResult) -> tuple[str, str | None]:
    """Map a ticker-collector exit 0 to component status via its R6 summary.

    The collector's stdout ends with one JSON summary line; partial success
    (exit 0 with a non-empty ``failed`` list) is ``warn`` with the summary
    surfaced in ``error``. Defensive by design: a missing or unparseable
    summary is a *reporting* gap on a successful exit — ``warn``, never a
    crash. Non-zero exits (all-failed 1, no-tickers 3) never reach this
    mapper — they map to ``failed`` with the stderr one-liner.
    """
    line = _last_nonempty_line(result.stdout)
    try:
        summary = json.loads(line) if line else None
    except json.JSONDecodeError:
        summary = None
    if not isinstance(summary, dict):
        return "warn", "exited 0 but stdout has no parseable JSON summary line"
    failed = summary.get("failed")
    if not isinstance(failed, list) or not failed:
        return "ok", None
    details = "; ".join(
        f"{item.get('ticker', '?')}: {item.get('reason') or '?'}"
        for item in failed
        if isinstance(item, dict)
    )
    text = (
        f"{len(failed)} ticker(s) failed "
        f"({summary.get('written')}/{summary.get('requested')} written): {details}"
    )
    return "warn", text[:300]


def _analysis_summary_counts(result: RunResult) -> dict[str, int] | None:
    """Runner slot-summary counts for the end-of-slot notification.

    ``None`` when the final stdout line is not a JSON summary (the outcome
    mapper already surfaces that as ``warn``); missing/non-int fields read as
    0 so a partial summary still yields a message.
    """
    line = _last_nonempty_line(result.stdout)
    try:
        summary = json.loads(line) if line else None
    except json.JSONDecodeError:
        return None
    if not isinstance(summary, dict):
        return None

    def count(key: str) -> int:
        value = summary.get(key)
        return value if isinstance(value, int) else 0

    return {key: count(key) for key in ("planned", "completed", "errors", "invalid_plans", "pairs")}


def _analysis_runner_outcome(result: RunResult) -> tuple[str, str | None]:
    """Map an analysis-runner exit 0 to component status via its summary line.

    The runner's stdout ends with one JSON slot-summary line (runner R4). Any
    ``decision: "ERROR"`` rows or invalid plans are a degraded-but-working
    slot — ``warn`` with the counts surfaced in ``error`` (per-run isolation
    is the runner's R3; the component itself worked). Same defensive posture
    as the other mappers: an exit 0 without a parseable summary is a
    *reporting* gap — ``warn``, never a silent ``ok``. Hard exits never reach
    this mapper — they map to ``failed`` with the stderr one-liner.
    """
    line = _last_nonempty_line(result.stdout)
    try:
        summary = json.loads(line) if line else None
    except json.JSONDecodeError:
        summary = None
    if not isinstance(summary, dict):
        return "warn", "exited 0 but stdout has no parseable JSON summary line"

    def count(key: str) -> int:
        value = summary.get(key)
        return value if isinstance(value, int) else 0

    errors = count("errors")
    invalid = count("invalid_plans")
    if errors or invalid:
        return (
            "warn",
            f"{errors} ERROR row(s), {invalid} invalid plan(s) "
            f"({count('completed')}/{count('planned')} runs completed)",
        )
    return "ok", None


# ---------------------------------------------------------------------------
# Scheduling gate (wrapper) + watchdog
# ---------------------------------------------------------------------------


def within_slot_window(
    session: str,
    slot_time: str,
    now: datetime,
    *,
    tolerance_minutes: int = SLOT_WINDOW_MINUTES,
) -> bool:
    """True when ``now`` is within slot_time ± tolerance **in the session tz**.

    The host timezone never participates: launchd fires in host-local time,
    and this gate re-derives validity purely session-side.
    """
    hour, minute = parse_slot_time(slot_time)
    local = now.astimezone(ZoneInfo(SESSION_TZ[session]))
    base = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    tolerance = timedelta(minutes=tolerance_minutes)
    return any(abs(local - (base + timedelta(days=d))) <= tolerance for d in (-1, 0, 1))


def run_slot_if_due(
    config: PipelineConfig,
    session: str,
    *,
    clock: Clock | None = None,
    registry: Iterable[Component] | None = None,
    runner: Runner | None = None,
    notifier: Notifier | None = None,
) -> int:
    """The launchd wrapper entry: window gate + weekday gate, then the slot.

    Exits 0 without side effects outside the window (a DST-shifted fire), on a
    session-tz weekend, or when the slot lock is already held (double fire).
    """
    clock = clock or _utc_now
    now = clock()
    slot_time = config.sessions[session].slot_time
    if not within_slot_window(session, slot_time, now):
        logger.info(
            "outside %s acceptance window (%s ± %dmin in %s) — exiting",
            session,
            slot_time,
            SLOT_WINDOW_MINUTES,
            SESSION_TZ[session],
        )
        return 0
    slot_day = session_date(session, now)
    if slot_day.weekday() >= 5:
        logger.info("%s %s is a weekend in %s — exiting", session, slot_day, SESSION_TZ[session])
        return 0
    orchestrator = Orchestrator(
        config,
        session,
        slot_date=slot_day,
        registry=registry,
        runner=runner,
        notifier=notifier,
        clock=clock,
    )
    try:
        return orchestrator.run_slot()
    except SlotLockHeld as exc:
        logger.info("%s — double fire deduped, exiting 0", exc)
        return 0


def _slot_started(config: PipelineConfig, session: str, date_iso: str) -> bool:
    try:
        data = json.loads(status_file_path(config, session).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, UnicodeDecodeError):
        return False
    slot = data.get("slot") or {}
    return slot.get("date") == date_iso and bool(slot.get("started_at"))


def watchdog_marker_path(config: PipelineConfig, session: str, date_iso: str) -> Path:
    return config.state_dir / "watchdog" / f"{session}-{date_iso}.notified"


def run_watchdog(
    config: PipelineConfig,
    *,
    clock: Clock | None = None,
    notifier: Notifier | None = None,
) -> int:
    """Hourly: if a weekday slot has not started by slot_time + 1h (session
    tz), notify — at most once per session/day (marker file, since each
    watchdog fire is a fresh process). A missed slot is never silent."""
    clock = clock or _utc_now
    notifier = notifier or Notifier(enabled=config.notifications_enabled)
    now = clock()
    for session in SESSIONS:
        zone = ZoneInfo(SESSION_TZ[session])
        local = now.astimezone(zone)
        slot_time = config.sessions[session].slot_time
        hour, minute = parse_slot_time(slot_time)
        # A slot within WATCHDOG_GRACE of midnight has its deadline on the
        # NEXT calendar day, so yesterday's slot must be checked too: a slot
        # day is alertable from its deadline until the end of the deadline's
        # own calendar day (for normal slots that is the slot day itself,
        # matching the original all-day alert window).
        for day_offset in (0, -1):
            slot_day = local.date() + timedelta(days=day_offset)
            if slot_day.weekday() >= 5:  # weekday of the SLOT day, session-local
                continue
            deadline = (
                datetime(slot_day.year, slot_day.month, slot_day.day, hour, minute, tzinfo=zone)
                + WATCHDOG_GRACE
            )
            if local < deadline or local.date() != deadline.date():
                continue
            date_iso = slot_day.isoformat()
            if _slot_started(config, session, date_iso):
                continue
            marker = watchdog_marker_path(config, session, date_iso)
            if marker.exists():
                continue
            notifier.notify(
                "TradingAgents pipeline",
                f"{session} slot for {date_iso} has not started by {slot_time}+1h "
                f"({SESSION_TZ[session]})",
                dedup_key=("watchdog", (session, date_iso), "missed-slot"),
            )
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text(to_utc_iso(now), encoding="utf-8")
            rotate_log(orchestrator_log_path(config))
            append_log_line(
                orchestrator_log_path(config),
                to_utc_iso(now),
                session,
                date_iso,
                "watchdog",
                "missed-slot",
                "-",
                f"not started by {slot_time}+1h",
            )
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m pipeline.orchestrator",
        description="TradingAgents pipeline slot orchestrator (specs/orchestrator.md)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="run a slot now (manual; no window gate)")
    run.add_argument("--session", required=True, choices=sorted(SESSIONS))
    run.add_argument("--date", type=date.fromisoformat, default=None, help="slot date override")
    run.add_argument("--only", default=None, metavar="COMPONENT", help="run one component (R3)")
    run.add_argument(
        "--from",
        dest="from_step",
        type=int,
        default=None,
        choices=range(8),
        metavar="STEP",
        help="start from step N (R3)",
    )

    slot = sub.add_parser("slot", help="launchd wrapper: window + weekday gate, then the slot")
    slot.add_argument("--session", required=True, choices=sorted(SESSIONS))

    sub.add_parser("watchdog", help="hourly missed-slot check")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    args = build_parser().parse_args(argv)
    config = load_config()
    if args.command == "run":
        orchestrator = Orchestrator(config, args.session, slot_date=args.date)
        try:
            return orchestrator.run_slot(only=args.only, from_step=args.from_step)
        except SlotLockHeld as exc:
            print(str(exc), file=sys.stderr)
            return 0
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
    if args.command == "slot":
        return run_slot_if_due(config, args.session)
    return run_watchdog(config)


if __name__ == "__main__":
    raise SystemExit(main())
