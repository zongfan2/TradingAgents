"""Slot orchestrator — scheduling gate, slot driver, status files, notifications.

Spec: specs/orchestrator.md. One launchd-fired invocation drives a whole
session slot: settle → macro collect/eval → pool build → ticker collect/eval →
analysis → execution → notify. Steps whose components have not landed yet
(ticker evaluators 5) are registered placeholders recorded as ``skipped`` —
the driver, ordering, barrier, status, lock, and notification machinery are
fully live; step 0 runs the real settle step (specs/orchestrator.md "Settle
step (0)"), step 6 runs the real analysis runner behind the macro-evaluator
join barrier and step 7 runs the real execution adapter (``us`` slots only —
specs/execution-adapter.md).

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
import base64
import contextlib
import hashlib
import hmac
import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
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
from pipeline.funnel_events import best_effort, funnel_dir

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
#: Slot field present while a deferred execution has the slot re-opened
#: (set by reopen_finished_slot, removed by finish_slot). Its presence on an
#: unfinished slot marks an execute that died before re-finishing.
REOPENED_KEY = "reopened_for_execution"

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

#: Backend roles (D19/D20), exported like the data dirs: the collectors and the
#: evaluator resolve their default ``--backend`` from these env vars via the
#: shared config, so orchestrated subprocesses must see the orchestrator's
#: *resolved* values (D20: claude collects on Sonnet, evaluates on Opus).
_BACKEND_ENV_KEYS = {
    "TRADINGAGENTS_COLLECT_BACKEND": "collect_backend",
    "TRADINGAGENTS_EVAL_BACKEND": "eval_backend",
    "TRADINGAGENTS_CLAUDE_COLLECT_MODEL": "claude_collect_model",
    "TRADINGAGENTS_CLAUDE_COLLECT_EFFORT": "claude_collect_effort",
    "TRADINGAGENTS_CLAUDE_EVAL_MODEL": "claude_eval_model",
    "TRADINGAGENTS_CLAUDE_EVAL_EFFORT": "claude_eval_effort",
}


def component_env(config: PipelineConfig) -> dict[str, str]:
    """Subprocess environment carrying the orchestrator's *resolved* data dirs
    and backend roles.

    Split-brain guard: components resolve their directories from their own
    ``TRADINGAGENTS_*_DIR`` env vars only (the macro collector/evaluator never
    read ``TRADINGAGENTS_STATE_DIR``), while the orchestrator derives unset
    dirs from ``state_dir``. Exporting every resolved path makes each
    component read and write exactly the directories the orchestrator uses —
    a ``TRADINGAGENTS_STATE_DIR``-only override otherwise sends the collector
    to ``~/.tradingagents`` while the evaluator argv points elsewhere. The
    same rule covers the backend split (``collect_backend`` /
    ``eval_backend``) and the D20 pinned claude models/efforts: every
    component defaults its backend and model from the exact values this
    orchestrator resolved.
    """
    env = dict(os.environ)
    for key, attr in _DATA_DIR_ENV_KEYS.items():
        env[key] = str(getattr(config, attr))
    for key, attr in _BACKEND_ENV_KEYS.items():
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

    def settle_argv(ctx: SlotContext) -> list[str]:
        return [
            python,
            "-m",
            "pipeline.settle",
            "--session",
            ctx.session,
            "--date",
            ctx.slot_date.isoformat(),
        ]

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

    def ticker_evaluators_argv(ctx: SlotContext) -> list[str]:
        return [
            python,
            "-m",
            "pipeline.evaluator",
            "--batch-tickers",
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

    def execution_adapter_argv(ctx: SlotContext) -> list[str]:
        return [
            python,
            "-m",
            "pipeline.execution_adapter",
            "submit",
            "--session",
            ctx.session,
            "--date",
            ctx.slot_date.isoformat(),
        ]

    return (
        # Step 0 is the real settle step (specs/orchestrator.md "Settle step
        # (0)"): adapter refresh + outcome backfill; it never talks to the
        # broker itself, so it runs on BOTH sessions.
        Component(0, "settle", settle_argv),
        Component(1, "macro_collector", collector_argv),
        Component(2, "macro_evaluator", evaluator_argv),
        Component(3, "pool_builder", pool_builder_argv),
        Component(4, "ticker_collectors", ticker_collector_argv),
        # Step 5 evaluates the day's ticker briefs (core ∪ would-trigger
        # opportunity, evaluator spec "Scope") so step 6's gating sees real
        # verdicts instead of blanket ``missing``.
        Component(5, "ticker_evaluators", ticker_evaluators_argv),
        # Step 6 runs strictly after the macro-evaluator join barrier (the
        # _POST_BARRIER_STEPS split below) — a late verdict can never be
        # bypassed by timing (specs/orchestrator.md slot sequence).
        Component(6, "analysis_runner", analysis_runner_argv),
        # Step 7 is the real execution adapter (specs/execution-adapter.md):
        # us slots only — Alpaca is US-only (analysis-runner R5); cn slots
        # record it as ``skipped``.
        Component(7, "execution_adapter", execution_adapter_argv, us_only=True),
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
# Notifications (osascript / Windows toast via injectable runner)
# ---------------------------------------------------------------------------

OsascriptRunner = Callable[[Sequence[str]], int]

#: Windows PowerShell through WSL interop; the scheduled job's PATH carries no
#: Windows directories, so the path is absolute.
WSL_POWERSHELL = "/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"
#: PowerShell's own AppUserModelID: toasts need a registered app id, and this
#: one exists on every Windows 10/11 install.
_POWERSHELL_APP_ID = r"{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe"


def default_osascript_runner(argv: Sequence[str]) -> int:
    proc = subprocess.run(list(argv), capture_output=True, timeout=15)
    return proc.returncode


def _applescript_str(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _powershell_str(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def default_notification_platform() -> str:
    """``wsl`` when running inside WSL (wsl.exe sets ``WSL_DISTRO_NAME`` for
    every process it starts, scheduled jobs included), else ``macos``."""
    return "wsl" if os.environ.get("WSL_DISTRO_NAME") else "macos"


def notification_argv(platform: str, title: str, message: str) -> list[str]:
    """The command that shows one desktop notification on ``platform``.

    WSL: a Windows toast via PowerShell. The script travels base64-encoded
    (``-EncodedCommand``) so no command-line quoting is involved, and the text
    is inserted as XML text nodes, never parsed as markup.
    """
    if platform == "macos":
        script = (
            f"display notification {_applescript_str(message)} "
            f"with title {_applescript_str(title)}"
        )
        return ["osascript", "-e", script]
    if platform == "wsl":
        script = "\n".join(
            [
                "$ErrorActionPreference = 'Stop'",
                "$null = [Windows.UI.Notifications.ToastNotificationManager, "
                "Windows.UI.Notifications, ContentType = WindowsRuntime]",
                "$xml = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent("
                "[Windows.UI.Notifications.ToastTemplateType]::ToastText02)",
                "$nodes = $xml.GetElementsByTagName('text')",
                f"$null = $nodes.Item(0).AppendChild($xml.CreateTextNode({_powershell_str(title)}))",
                f"$null = $nodes.Item(1).AppendChild($xml.CreateTextNode({_powershell_str(message)}))",
                f"[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("
                f"{_powershell_str(_POWERSHELL_APP_ID)}).Show("
                "[Windows.UI.Notifications.ToastNotification]::new($xml))",
            ]
        )
        encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
        return [WSL_POWERSHELL, "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded]
    raise ValueError(f"unknown notification platform {platform!r}")


#: ``(url, body, timeout) -> (http status, response body)``.
HttpPost = Callable[[str, bytes, float], tuple[int, bytes]]
#: Per-socket-operation timeout handed to the HTTP stack.
LARK_TIMEOUT_S = 10.0
#: Hard bound on the whole delivery (DNS included) as seen by the slot: the
#: POST runs on a daemon thread that is abandoned past this.
LARK_DEADLINE_S = 15.0


class LarkError(RuntimeError):
    """A Lark webhook delivery failure; its text never contains the URL."""


def default_http_post(url: str, body: bytes, timeout: float) -> tuple[int, bytes]:
    request = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - https enforced by config
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        # A 4xx/5xx is a response, not a transport failure: hand back its
        # status so the caller reports "HTTP <code>" (404 = revoked/mistyped
        # hook), and release the connection it holds.
        with contextlib.closing(exc):
            return exc.code, exc.read()


def lark_sign(timestamp: str, secret: str) -> str:
    """Lark/Feishu custom-bot signature: HMAC-SHA256 keyed by
    ``"<timestamp>\\n<secret>"`` over an empty message, base64-encoded."""
    key = f"{timestamp}\n{secret}".encode()
    return base64.b64encode(hmac.new(key, b"", hashlib.sha256).digest()).decode("ascii")


class LarkWebhook:
    """Second notification channel: a Lark/Feishu custom-bot ``text`` message.

    ``__call__`` raises :class:`LarkError` on any failure. The URL is a
    credential, so it is scrubbed from every error text.
    """

    def __init__(
        self,
        url: str,
        secret: str | None = None,
        *,
        post: HttpPost | None = None,
        clock: Callable[[], float] | None = None,
        deadline: float = LARK_DEADLINE_S,
    ) -> None:
        self._url = url
        self._secret = secret
        self._post = post or default_http_post
        self._clock = clock or time.time
        self._deadline = deadline

    def __repr__(self) -> str:
        return f"LarkWebhook(signed={self._secret is not None})"

    def _scrub(self, text: str) -> str:
        for credential in (self._url, self._secret):
            if credential:
                text = text.replace(credential, "<redacted>")
        return text

    def __call__(self, title: str, message: str) -> None:
        body: dict[str, Any] = {"msg_type": "text", "content": {"text": f"{title}\n{message}"}}
        if self._secret:
            timestamp = str(int(self._clock()))
            body["timestamp"] = timestamp
            body["sign"] = lark_sign(timestamp, self._secret)
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        outcome: dict[str, Any] = {}

        def deliver() -> None:
            try:
                outcome["result"] = self._post(self._url, data, LARK_TIMEOUT_S)
            except BaseException as exc:  # noqa: BLE001 - reported by type only
                outcome["error"] = exc

        worker = threading.Thread(target=deliver, name="lark-webhook", daemon=True)
        worker.start()
        worker.join(self._deadline)
        if worker.is_alive():
            raise LarkError(f"no response within {self._deadline:.0f}s")
        if "error" in outcome:
            # Type only: transport errors can quote parts of the
            # token-bearing URL that no substring scrub can anticipate.
            raise LarkError(type(outcome["error"]).__name__)
        status, raw = outcome["result"]
        if status != 200:
            raise LarkError(f"HTTP {status}")
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise LarkError("response is not JSON") from None
        code = payload.get("code", payload.get("StatusCode")) if isinstance(payload, dict) else None
        if code != 0:
            detail = payload.get("msg") or payload.get("StatusMessage") if isinstance(payload, dict) else None
            raise LarkError(self._scrub(f"code {code}: {detail}"))


class Notifier:
    """Desktop notification — macOS ``display notification`` or, under WSL, a
    Windows toast — plus the optional Lark webhook, with per-(component,
    slot, reason) dedup across both channels.

    A channel failure degrades that channel to log-only; the other channel is
    still tried, and notifications can never fail a slot. ``enabled=False``
    (config ``notifications_enabled``) is log-only for every channel.
    """

    def __init__(
        self,
        enabled: bool = True,
        runner: OsascriptRunner | None = None,
        platform: str | None = None,
        webhook: Callable[[str, str], None] | None = None,
    ) -> None:
        self.enabled = enabled
        self._runner = runner or default_osascript_runner
        self.platform = platform or default_notification_platform()
        self._webhook = webhook
        self._sent: set[tuple] = set()
        self.delivered: list[tuple[str, str]] = []
        #: Messages the webhook channel accepted.
        self.webhook_delivered: list[tuple[str, str]] = []

    def notify(
        self,
        title: str,
        message: str,
        dedup_key: tuple | None = None,
        details: str | None = None,
    ) -> bool:
        """``details`` (extra lines, e.g. one per order) go to the webhook
        only: a desktop toast stays the one-line ``message``."""
        if dedup_key is not None:
            if dedup_key in self._sent:
                return False
            self._sent.add(dedup_key)
        logger.info("notification [%s] %s", title, message)
        if details:
            logger.info("notification details:\n%s", details)
        if not self.enabled:
            return False
        desktop = self._notify_desktop(title, message)
        webhook = self._notify_webhook(title, f"{message}\n{details}" if details else message)
        return desktop or webhook

    def _notify_desktop(self, title: str, message: str) -> bool:
        try:
            returncode = self._runner(notification_argv(self.platform, title, message))
        except Exception as exc:  # notifier failure degrades to log-only
            logger.warning(
                "%s notifier failed (%s) — notification degraded to log-only", self.platform, exc
            )
            return False
        if returncode != 0:
            logger.warning(
                "%s notifier exited %d — notification degraded to log-only",
                self.platform,
                returncode,
            )
            return False
        self.delivered.append((title, message))
        return True

    def _notify_webhook(self, title: str, message: str) -> bool:
        if self._webhook is None:
            return False
        try:
            self._webhook(title, message)
        except Exception as exc:  # LarkError text is already credential-free
            reason = str(exc) if isinstance(exc, LarkError) else type(exc).__name__
            logger.warning("lark notifier failed (%s) — channel degraded to log-only", reason)
            return False
        self.webhook_delivered.append((title, message))
        return True


def make_notifier(config: PipelineConfig) -> Notifier:
    """The configured notifier: desktop, plus Lark when its webhook is set."""
    webhook = (
        LarkWebhook(config.lark_webhook_url, config.lark_webhook_secret)
        if config.lark_webhook_url
        else None
    )
    return Notifier(enabled=config.notifications_enabled, webhook=webhook)


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

    def reopen_finished_slot(self, session: str, slot_date: date) -> bool:
        """Load the on-disk slot and clear its ``finished_at`` so the deferred
        execution step can run inside it. ``False`` (file untouched) unless
        the file holds this session and date's slot that is either finished
        or still carries :data:`REOPENED_KEY` from an execute that died at
        any point before re-finishing (the caller holds the slot lock, so
        no execute is alive)."""
        with self._lock:
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
            except (FileNotFoundError, json.JSONDecodeError, UnicodeDecodeError):
                return False
            slot = data.get("slot") if isinstance(data, dict) else None
            if (
                not isinstance(slot, dict)
                or slot.get("session") != session
                or slot.get("date") != slot_date.isoformat()
                or not isinstance(data.get("components"), dict)
            ):
                return False
            if not slot.get("finished_at") and not slot.get(REOPENED_KEY):
                return False
            history = data.get("history")
            data["history"] = history[-HISTORY_LIMIT:] if isinstance(history, list) else []
            slot["finished_at"] = None
            slot[REOPENED_KEY] = True
            self._data = data
            self._write()
            return True

    def finish_slot(self, finished_at: str) -> None:
        with self._lock:
            assert self._data is not None, "start_slot() before finish_slot()"
            self._data["slot"].pop(REOPENED_KEY, None)
            self._data["slot"]["finished_at"] = finished_at
            completed = {**self._data["slot"], "components": dict(self._data["components"])}
            history = list(self._data["history"])
            # A re-opened slot (deferred execution) replaces its own entry.
            if history and all(
                history[-1].get(key) == completed[key] for key in ("date", "session", "started_at")
            ):
                history[-1] = completed
            else:
                history.append(completed)
            self._data["history"] = history[-HISTORY_LIMIT:]
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
        self.notifier = notifier or make_notifier(config)
        self.status = StatusFile(status_file_path(config, session))
        self.ctx = SlotContext(session, self.slot_date, config)
        #: Step 6's parsed slot-summary counts, carried into the end-of-slot
        #: notification (spec: "plans produced, orders submitted/dry-run,
        #: pairs run, failures"). ``None`` until the runner reports a summary.
        self._analysis_counts: dict[str, int] | None = None
        #: Step 7's parsed adapter-summary counts for the same notification.
        self._execution_counts: dict[str, int] | None = None
        #: Step 7's per-order lines for the Lark summary (None: no summary).
        self._execution_orders: str | None = None
        #: Set by run_slot: step 7 is left to the ``execute`` job.
        self._defer_execution = False
        #: The execute job's pre-submit refresh counts (run_execution only).
        self._refresh_counts: dict[str, int] | None = None

    @property
    def execution_time(self) -> str | None:
        """us ``execution_time`` (deferred step 7), else ``None``."""
        if self.session != "us":
            return None
        return self.config.sessions[self.session].execution_time

    def _deferral_reason(self) -> str:
        return f"deferred to the execution task ({self.execution_time} {SESSION_TZ['us']})"

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
        # Only an explicit --only execution_adapter runs step 7 inline when a
        # deferred execution job is configured (specs/orchestrator.md).
        self._defer_execution = self.execution_time is not None and only != "execution_adapter"
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
        if name == "execution_adapter" and self._defer_execution:
            self._record_skip(name, started_iso, self._deferral_reason())
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
        result: RunResult | None = None
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
            elif name == "settle":
                status, error = _settle_outcome(result)
            elif name == "macro_evaluator":
                status, error = self._evaluator_outcome()
            elif name == "pool_builder":
                status, error = _pool_builder_outcome(result)
            elif name == "ticker_collectors":
                status, error = _ticker_collector_outcome(result)
            elif name == "ticker_evaluators":
                status, error = _ticker_evaluators_outcome(result)
            elif name == "analysis_runner":
                status, error = _analysis_runner_outcome(result)
                self._analysis_counts = _analysis_summary_counts(result)
            elif name == "execution_adapter":
                status, error = _execution_adapter_outcome(result)
                self._execution_counts = _execution_summary_counts(result)
                self._execution_orders = _execution_order_lines(result)
            elif name == "execution_refresh":
                status, error = _execution_refresh_outcome(result)
                self._refresh_counts = _refresh_summary_counts(result)
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
        archive_component_output(
            self.config, self.session, self.slot_date, name, record, result
        )
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
            # produced / pairs run / orders submitted or dry-run / failures.
            # Orders read "n/a" when step 7 did not report a summary (cn
            # slots skip it; a hard-failed adapter is named in failures).
            if self._execution_counts is not None:
                orders_text = (
                    f"orders {self._execution_counts['submitted']} live"
                    f"/{self._execution_counts['dry_run']} dry-run"
                )
            elif self._defer_execution:
                orders_text = f"orders deferred to {self.execution_time}"
            else:
                orders_text = "orders n/a"
            counts = self._analysis_counts
            message += (
                f"; plans {counts['completed']}/{counts['planned']}, "
                f"pairs {counts['pairs']} run, "
                f"invalid plans {counts['invalid_plans']}, "
                f"run errors {counts['errors']}, {orders_text}"
            )
        if problems:
            message += "; failures: " + ", ".join(problems)
        # End-of-slot summary is always sent (dedup key still guards retries).
        self.notifier.notify(
            "TradingAgents pipeline",
            message,
            dedup_key=("summary", (self.session, self.slot_date.isoformat()), "end-of-slot"),
            details=self._execution_orders,
        )
        self.status.finish_slot(to_utc_iso(self.clock()))
        return 1 if problems else 0


    # -- deferred execution (specs/orchestrator.md "Deferred us execution") --

    def run_execution(self) -> int:
        """Run step 7 inside the day's already-finished us slot.

        First a full ``execution_adapter refresh`` (recorded as component
        ``execution_refresh``): with fresh post-open quotes it owns the
        once-only close re-submission that settle's pre-market refresh skips.
        Its failure never blocks step 7 (submit reads live broker state).

        Raises :class:`SlotLockHeld` when the slot (or another ``execute``)
        holds the lock. Returns 1 when the slot has not finished (nothing is
        run and the status file is untouched) or either step failed/timed out.
        """
        if self.session != "us":
            raise ValueError("execute is us-only — the adapter never runs for cn")
        component = next((c for c in self.registry if c.name == "execution_adapter"), None)
        if component is None:
            raise ValueError("registry has no execution_adapter component")
        refresh = next(
            (c for c in self.registry if c.name == "execution_refresh"),
            execution_refresh_component(self.config),
        )
        self._defer_execution = False
        lock = acquire_slot_lock(self.config, self.session, self.slot_date)
        try:
            if not self.status.reopen_finished_slot(self.session, self.slot_date):
                self._report_unfinished_slot()
                return 1
            self._run_component(refresh)
            self._run_component(component)
            snapshot = self.status.components_snapshot()
            refresh_record = snapshot.get(refresh.name, {})
            refresh_status = refresh_record.get("status", "failed")
            record = snapshot.get(component.name, {})
            status = record.get("status", "failed")
            message = f"execution {self.session} {self.slot_date.isoformat()}: {status}"
            if self._refresh_counts is not None:
                message += (
                    f"; refresh {refresh_status}, closes re-submitted "
                    f"{self._refresh_counts['close_resubmitted']}"
                )
            else:
                message += f"; refresh {refresh_status}"
            if refresh_record.get("error"):
                # A warn sends no notification of its own: the reason (e.g. a
                # close still unfilled after its one retry) must ride here.
                message += f" ({refresh_record['error']})"
            if self._execution_counts is not None:
                counts = self._execution_counts
                message += (
                    f"; orders {counts['submitted']} live/{counts['dry_run']} dry-run, "
                    f"{counts['skipped']} skipped"
                )
            if record.get("error"):
                message += f"; {record['error']}"
            # Re-finish before the (up to 15 s) notifier subprocess: the
            # status file must not depend on surviving it.
            self.status.finish_slot(to_utc_iso(self.clock()))
            self.notifier.notify(
                "TradingAgents pipeline",
                message,
                dedup_key=("summary", (self.session, self.slot_date.isoformat()), "execution"),
                details=self._execution_orders,
            )
            failed = {"failed", "timeout"}
            return 1 if status in failed or refresh_status in failed else 0
        finally:
            release_slot_lock(lock)

    def _report_unfinished_slot(self) -> None:
        """Execution time arrived but the day's slot is missing or still
        running: never run the adapter, never write the status file."""
        reason = (
            f"us slot for {self.slot_date.isoformat()} not finished at execution time "
            "— execution skipped"
        )
        logger.warning(reason)
        self._log_component("execution_adapter", "skipped", 0.0, reason)
        self.notifier.notify(
            "TradingAgents pipeline",
            reason,
            dedup_key=("execution_adapter", (self.session, self.slot_date.isoformat()), "unfinished"),
        )


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


def _settle_outcome(result: RunResult) -> tuple[str, str | None]:
    """Map a settle exit 0 to component status via its summary line.

    The settle CLI's stdout ends with one JSON summary line carrying the
    settled/skipped counts and the adapter-refresh outcome. A refresh failure
    inside a successful settle is degraded-but-working — the settle spec says
    outcome computation proceeds regardless — so it maps to ``warn`` with the
    refresh reason passed through and the counts surfaced. Same defensive
    posture as the other mappers: an exit 0 without a parseable summary is a
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
    if summary.get("refresh_ok") is False:
        reason = summary.get("refresh_error") or "adapter refresh failed"
        message = (
            f"{reason} — {summary.get('settled', 0)} settled, "
            f"{summary.get('skipped', 0)} skipped"
        )
        return "warn", message[:300]
    return "ok", None


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


def _ticker_evaluators_outcome(result: RunResult) -> tuple[str, str | None]:
    """Map a batch-ticker-evaluation exit 0 via its JSON summary line.

    ``fail`` verdicts, structural refusals, backend failures, and errors are
    degraded-but-working states — the slot continues, gating treats the
    affected briefs per the state table — so they map to ``warn`` with
    counts. Same defensive posture as :func:`_ticker_collector_outcome`:
    a successful exit without a parseable summary is a reporting gap
    (``warn``), never a crash. Non-zero exits (batch wipeout) never reach
    this mapper.
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

    verdicts = summary.get("verdicts")
    fails = verdicts.get("fail", 0) if isinstance(verdicts, dict) else 0
    fails = fails if isinstance(fails, int) else 0
    problems: list[str] = []
    if fails:
        problems.append(f"{fails} fail verdict(s)")
    for key, label in (
        ("refused", "structural refusal(s)"),
        ("backend_failed", "backend failure(s)"),
        ("errors", "error(s)"),
    ):
        if count(key):
            problems.append(f"{count(key)} {label}")
    if not problems:
        return "ok", None
    text = (
        f"{'; '.join(problems)} "
        f"({count('ok')}/{count('requested')} evaluated)"
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


#: Bytes of each stream kept by the component output archive (from the end).
ARCHIVE_TAIL_BYTES = 64 * 1024


def _tail(text: str, limit: int = ARCHIVE_TAIL_BYTES) -> str:
    data = text.encode("utf-8", errors="replace")
    if len(data) <= limit:
        return text
    return data[-limit:].decode("utf-8", errors="ignore")


@best_effort
def archive_component_output(
    config: PipelineConfig,
    session: str,
    slot_date: date,
    name: str,
    record: dict,
    result: RunResult | None,
) -> None:
    """Spec decision-funnel R2: keep each component's final stdout summary
    line and output tails, so a reason no funnel emitter covers survives
    the slot. Overwritten on a rerun; failures are log-only."""
    stdout = result.stdout if result is not None else ""
    stderr = result.stderr if result is not None else ""
    summary_line = _last_nonempty_line(stdout)
    try:
        summary = json.loads(summary_line) if summary_line else None
    except json.JSONDecodeError:
        summary = None
    payload = {
        "schema": 1,
        "component": name,
        "session": session,
        "date": slot_date.isoformat(),
        "status": record.get("status"),
        "started_at": record.get("started_at"),
        "finished_at": record.get("finished_at"),
        "error": record.get("error"),
        "returncode": result.returncode if result is not None else None,
        "summary": summary if isinstance(summary, dict) else None,
        "stdout_tail": _tail(stdout),
        "stderr_tail": _tail(stderr),
    }
    path = funnel_dir(config.state_dir, session) / slot_date.isoformat() / f"{name}.out.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(path, json.dumps(payload, indent=2, ensure_ascii=False) + "\n")


_EXECUTION_SUMMARY_KEYS = ("submitted", "dry_run", "skipped", "canceled", "errors", "rejected")


def _execution_summary_counts(result: RunResult) -> dict[str, int] | None:
    """Adapter submission/skip counts for the end-of-slot notification.

    ``None`` when the final stdout line is not a JSON summary (the outcome
    mapper already surfaces that as ``warn``); missing/non-int fields read
    as 0 so a partial summary still yields a message.
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

    return {key: count(key) for key in _EXECUTION_SUMMARY_KEYS}


#: Per-order lines shown in the Lark summary; the rest are counted.
MAX_ORDER_LINES = 15


def _num(value: object) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return "?"
    return f"{value:g}"


def _order_line(entry: dict) -> str:
    ticker = str(entry.get("ticker") or "?")
    kind = entry.get("kind")
    mode = "dry-run" if entry.get("dry_run") else "live"
    reason = entry.get("reason")
    if kind == "skip":
        return f"• {ticker} skipped: {reason or '?'}"
    order_kind = entry.get("order_kind")
    side = {"bracket": "BUY", "close": "SELL"}.get(str(order_kind), str(order_kind or "order"))
    line = f"• {ticker} {side} {_num(entry.get('qty'))} @{_num(entry.get('limit_price'))}"
    if entry.get("stop_price") is not None:
        line += f" stop {_num(entry.get('stop_price'))}"
    if entry.get("target_price") is not None:
        line += f" target {_num(entry.get('target_price'))}"
    if kind == "cancel":
        return f"{line} canceled ({mode})"
    status = entry.get("broker_status")
    note = f"{mode}, {status}" if status and mode == "live" else mode
    if reason:
        note += f", {reason}"
    return f"{line} ({note})"


def _execution_order_lines(result: RunResult) -> str | None:
    """One line per order from the submit summary's ``orders`` list (spec
    execution-adapter R4) — the Lark-only details of the step 7 summary.
    ``None`` when there is no parseable summary or no order rows."""
    line = _last_nonempty_line(result.stdout)
    try:
        summary = json.loads(line) if line else None
    except json.JSONDecodeError:
        return None
    orders = summary.get("orders") if isinstance(summary, dict) else None
    if not isinstance(orders, list):
        return None
    entries = [entry for entry in orders if isinstance(entry, dict)]
    if not entries:
        return None
    lines = [_order_line(entry) for entry in entries[:MAX_ORDER_LINES]]
    if len(entries) > MAX_ORDER_LINES:
        lines.append(f"… {len(entries) - MAX_ORDER_LINES} more in ledger/orders.jsonl")
    return "\n".join(lines)


_REFRESH_SUMMARY_KEYS = (
    "refreshed", "canceled_stale", "close_resubmitted", "rejected", "canceled", "skipped", "errors",
)


def _refresh_summary(result: RunResult) -> dict | None:
    line = _last_nonempty_line(result.stdout)
    try:
        summary = json.loads(line) if line else None
    except json.JSONDecodeError:
        return None
    return summary if isinstance(summary, dict) else None


def _refresh_summary_counts(result: RunResult) -> dict[str, int] | None:
    summary = _refresh_summary(result)
    if summary is None:
        return None
    return {
        key: summary[key] if isinstance(summary.get(key), int) else 0
        for key in _REFRESH_SUMMARY_KEYS
    }


def _execution_refresh_outcome(result: RunResult) -> tuple[str, str | None]:
    """Map the execute job's ``refresh`` exit 0 to component status.

    Broker errors, rejected/canceled orders and adapter alerts (e.g. a close
    still unfilled after its one re-submission) are ``warn`` with the counts
    surfaced — the step worked, the broker state needs a look. No parseable
    summary is a reporting gap: ``warn``, never a silent ``ok``.
    """
    summary = _refresh_summary(result)
    counts = _refresh_summary_counts(result)
    if summary is None or counts is None:
        return "warn", "exited 0 but stdout has no parseable JSON summary line"
    alerts = summary.get("alerts") if isinstance(summary.get("alerts"), list) else []
    if counts["errors"] or counts["rejected"] or counts["canceled"] or alerts:
        # Name each alert (kind: ticker) — this text is what reaches the
        # operator's execution summary notification.
        named = sorted(
            {
                f"{a.get('kind')}: {a.get('ticker')}" if a.get("ticker") else str(a.get("kind"))
                for a in alerts
                if isinstance(a, dict)
            }
        )
        return (
            "warn",
            (
                f"{counts['errors']} broker error(s), {counts['rejected']} rejected, "
                f"{counts['canceled']} canceled, {len(alerts)} alert(s)"
                + (f" ({', '.join(named)})" if named else "")
                + f" — {counts['close_resubmitted']} close(s) re-submitted"
            )[:300],
        )
    return "ok", None


def execution_refresh_component(config: PipelineConfig) -> Component:
    """The execute job's pre-submit ``execution_adapter refresh`` (full —
    close re-submission included). Not a slot step: run_execution only."""

    def argv(ctx: SlotContext) -> list[str]:
        return [
            str(config.python_executable),
            "-m",
            "pipeline.execution_adapter",
            "refresh",
            "--date",
            ctx.slot_date.isoformat(),
        ]

    return Component(7, "execution_refresh", argv)


def _execution_adapter_outcome(result: RunResult) -> tuple[str, str | None]:
    """Map an adapter exit 0 to component status via its summary line.

    Any broker error or rejected order is a degraded-but-working slot —
    ``warn`` with the submission/skip counts surfaced in ``error`` (per-order
    isolation is the adapter's R4; the component itself worked). Same
    defensive posture as the other mappers: an exit 0 without a parseable
    summary is a *reporting* gap — ``warn``, never a silent ``ok``. Hard
    exits (incl. the S1 paper-account refusal) never reach this mapper —
    they map to ``failed`` with the stderr one-liner.
    """
    counts = _execution_summary_counts(result)
    if counts is None:
        return "warn", "exited 0 but stdout has no parseable JSON summary line"
    errors = counts["errors"]
    rejected = counts["rejected"]
    if errors or rejected:
        return (
            "warn",
            f"{errors} broker error(s), {rejected} rejected — "
            f"{counts['submitted']} submitted, {counts['dry_run']} dry-run, "
            f"{counts['skipped']} skipped",
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


def run_execution_if_due(
    config: PipelineConfig,
    *,
    clock: Clock | None = None,
    registry: Iterable[Component] | None = None,
    runner: Runner | None = None,
    notifier: Notifier | None = None,
) -> int:
    """The ``execute`` job wrapper: us ``execution_time`` ± 15 min window and
    weekday gate (New York), then step 7 in the day's finished slot."""
    clock = clock or _utc_now
    now = clock()
    execution_time = config.sessions["us"].execution_time
    if execution_time is None:
        logger.info("no us execution_time configured — step 7 runs inline; exiting")
        return 0
    if not within_slot_window("us", execution_time, now):
        logger.info(
            "outside us execution window (%s ± %dmin in %s) — exiting",
            execution_time,
            SLOT_WINDOW_MINUTES,
            SESSION_TZ["us"],
        )
        return 0
    slot_day = session_date("us", now)
    if slot_day.weekday() >= 5:
        logger.info("us %s is a weekend in %s — exiting", slot_day, SESSION_TZ["us"])
        return 0
    return execute_slot(
        config, slot_day, clock=clock, registry=registry, runner=runner, notifier=notifier
    )


def execute_slot(
    config: PipelineConfig,
    slot_day: date,
    *,
    clock: Clock | None = None,
    registry: Iterable[Component] | None = None,
    runner: Runner | None = None,
    notifier: Notifier | None = None,
    manual: bool = False,
) -> int:
    """Run deferred step 7 for ``slot_day``; a held lock never touches the
    status file another process owns.

    The held lock is classified by the slot record, never by step 7: a slot
    that is finished or marked re-opened can only be held by an execute
    (alive, or killed at any point — a real kill leaves the lock behind) ⇒
    warning naming the lock, and ``manual`` (``execute --now``) exits 1 with
    it; anything else is the slot itself still running ⇒ the unfinished-slot
    report."""
    orchestrator = Orchestrator(
        config,
        "us",
        slot_date=slot_day,
        registry=registry,
        runner=runner,
        notifier=notifier,
        clock=clock,
    )
    try:
        return orchestrator.run_execution()
    except SlotLockHeld as exc:
        slot = _slot_data(config, "us", slot_day.isoformat()).get("slot") or {}
        if slot.get("finished_at") or slot.get(REOPENED_KEY):
            lock_path = slot_lock_path(config, "us", slot_day)
            message = (
                f"{exc} — held by an execute run; if no `pipeline.orchestrator execute` "
                f"process is alive it was killed: remove {lock_path} and run "
                "`execute --now` again"
            )
            if manual:
                print(message, file=sys.stderr)
                return 1
            logger.warning(message)
            return 0
        logger.info("%s — the us slot is still running", exc)
        orchestrator._report_unfinished_slot()
        return 0


def _slot_data(config: PipelineConfig, session: str, date_iso: str) -> dict[str, Any]:
    """The status file's content when it holds ``date_iso``'s slot, else ``{}``."""
    try:
        data = json.loads(status_file_path(config, session).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, UnicodeDecodeError):
        return {}
    if not isinstance(data, dict) or not isinstance(data.get("slot"), dict):
        return {}
    return data if data["slot"].get("date") == date_iso else {}


def _slot_components(config: PipelineConfig, session: str, date_iso: str) -> dict[str, Any]:
    """The status file's components when it holds ``date_iso``'s slot, else ``{}``."""
    components = _slot_data(config, session, date_iso).get("components")
    return components if isinstance(components, dict) else {}


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
    notifier = notifier or make_notifier(config)
    now = clock()
    for session in SESSIONS:
        slot_time = config.sessions[session].slot_time
        for slot_day in _alertable_days(session, slot_time, now):
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
    execution_time = config.sessions["us"].execution_time
    if execution_time is not None:
        _watch_deferred_execution(config, execution_time, now, notifier)
    return 0


def _alertable_days(session: str, at_time: str, now: datetime) -> list[date]:
    """Weekday session-local days whose ``at_time`` + grace deadline has passed.

    A time within WATCHDOG_GRACE of midnight has its deadline on the NEXT
    calendar day, so yesterday must be checked too: a day is alertable from
    its deadline until the end of the deadline's own calendar day (for normal
    times that is the day itself, matching the original all-day alert window).
    """
    zone = ZoneInfo(SESSION_TZ[session])
    local = now.astimezone(zone)
    hour, minute = parse_slot_time(at_time)
    days: list[date] = []
    for day_offset in (0, -1):
        day = local.date() + timedelta(days=day_offset)
        if day.weekday() >= 5:  # weekday of the SLOT day, session-local
            continue
        deadline = (
            datetime(day.year, day.month, day.day, hour, minute, tzinfo=zone) + WATCHDOG_GRACE
        )
        if local < deadline or local.date() != deadline.date():
            continue
        days.append(day)
    return days


def _watch_deferred_execution(
    config: PipelineConfig, execution_time: str, now: datetime, notifier: Notifier
) -> None:
    """Alert once when, at ``execution_time`` + 1h, a us slot's step 7 is
    still the deferral skip — the execute job never ran (or never got past
    its gates) — or the slot is still re-opened for execution: far past the
    adapter's own timeout, so the execute process died before re-finishing.
    A slot that never started is the missed-slot alert's job."""
    for slot_day in _alertable_days("us", execution_time, now):
        date_iso = slot_day.isoformat()
        slot = _slot_data(config, "us", date_iso)
        step7 = (slot.get("components") or {}).get("execution_adapter", {})
        deferred = step7.get("status") == "skipped" and str(step7.get("error") or "").startswith(
            "deferred"
        )
        stuck = bool((slot.get("slot") or {}).get(REOPENED_KEY))
        if not deferred and not stuck:
            continue
        marker = config.state_dir / "watchdog" / f"us-{date_iso}.execution.notified"
        if marker.exists():
            continue
        notifier.notify(
            "TradingAgents pipeline",
            f"us execution for {date_iso} has not completed by {execution_time}+1h "
            f"({SESSION_TZ['us']})",
            dedup_key=("watchdog", ("us", date_iso), "missed-execution"),
        )
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(to_utc_iso(now), encoding="utf-8")
        rotate_log(orchestrator_log_path(config))
        append_log_line(
            orchestrator_log_path(config),
            to_utc_iso(now),
            "us",
            date_iso,
            "watchdog",
            "missed-execution",
            "-",
            ("step 7 still deferred" if deferred else "slot still re-opened for execution")
            + f" at {execution_time}+1h",
        )


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

    execute = sub.add_parser(
        "execute",
        help="us execution job: execution_time window + weekday gate, then the "
        "deferred step 7 in the day's finished slot",
    )
    execute.add_argument(
        "--now",
        action="store_true",
        help="manual: skip the window/weekday gate (every other rule still applies)",
    )
    execute.add_argument(
        "--date", type=date.fromisoformat, default=None,
        help="with --now: slot date (default: today in New York)",
    )
    return parser


def _load_repo_dotenv() -> None:
    """Fill missing env keys from the repo-root ``.env`` (setdefault — never
    overrides launchd/shell-provided values). Under launchd the process env is
    only the plist's PATH, so orchestrator-level knobs (timeouts, slot times)
    and component credentials all live in ``.env``; ``run_one`` already does
    the same for its children (2026-08-24). ``TRADINGAGENTS_SKIP_DOTENV``
    disables the load (tests set it — the real .env must not leak into
    fixtures)."""
    if os.environ.get("TRADINGAGENTS_SKIP_DOTENV"):
        return
    root_env = Path(__file__).resolve().parent.parent / ".env"
    if not root_env.exists():
        return
    for raw in root_env.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if value.strip():
            os.environ.setdefault(key.strip(), value.strip())


def main(argv: Sequence[str] | None = None) -> int:
    _load_repo_dotenv()
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
    if args.command == "execute":
        if args.date is not None and not args.now:
            print("error: --date requires --now", file=sys.stderr)
            return 2
        if args.now:
            return execute_slot(
                config, args.date or session_date("us", _utc_now()), manual=True
            )
        return run_execution_if_due(config)
    return run_watchdog(config)


if __name__ == "__main__":
    raise SystemExit(main())
