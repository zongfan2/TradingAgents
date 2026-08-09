"""Orchestrator slot driver — specs/orchestrator.md acceptance criteria 1, 3, 5.

Fully offline: the subprocess boundary (component CLIs, osascript) is faked.
Covers ordering, 1∥3 parallelism, the evaluator join barrier before step 6,
the failure-continuation matrix, timeouts, lockfile exclusivity + stale
breaking, --only/--from, watchdog once-only, mid-slot kill survivability,
concurrent cn+us slots, and the steps 3–4 wiring (pool builder + ticker
collector argvs against the real CLIs, warn mappings, aggregate timeouts).
"""

import json
import os
import signal
import subprocess
import sys
import threading
import time
from datetime import date, datetime, timedelta, timezone

import pytest

from pipeline import orchestrator, pool_builder, ticker_collector
from pipeline.config import (
    DEFAULT_COMPONENT_TIMEOUTS,
    PipelineConfig,
    SessionSchedule,
    load_config,
)

SLOT_DATE = date(2026, 1, 5)  # a Monday
NOW = datetime(2026, 1, 5, 13, 40, tzinfo=timezone.utc)


def clock():
    return NOW


def make_config(tmp_path, **overrides):
    return PipelineConfig(state_dir=tmp_path / "state", **overrides)


def fake_component(step, name, us_only=False):
    return orchestrator.Component(
        step=step,
        name=name,
        build_argv=lambda ctx, name=name: ["fake-cli", name],
        us_only=us_only,
    )


STEP_NAMES = (
    "settle",
    "macro_collector",
    "macro_evaluator",
    "pool_builder",
    "ticker_collectors",
    "ticker_evaluators",
    "analysis_runner",
    "execution_adapter",
)


def full_fake_registry():
    """Every step of the slot-sequence table backed by a fake CLI."""
    return tuple(
        fake_component(step, name, us_only=(name == "execution_adapter"))
        for step, name in enumerate(STEP_NAMES)
    )


class FakeRunner:
    """Injectable subprocess boundary. ``behaviors[name]`` may be a RunResult,
    an exception instance to raise, or a callable(argv) -> RunResult."""

    def __init__(self, behaviors=None):
        self.behaviors = behaviors or {}
        self._lock = threading.Lock()
        self.calls = []  # (name, argv, timeout)
        self.events = []  # (name, "start"|"end")

    def _mark(self, name, phase):
        with self._lock:
            self.events.append((name, phase))

    @staticmethod
    def _component_name(argv):
        if argv[0] == "fake-cli":
            return argv[1]
        if "-m" in argv:  # default-registry argvs: python -m pipeline.<module> ...
            module = argv[argv.index("-m") + 1]
            return {
                "pipeline.macro_collector": "macro_collector",
                "pipeline.evaluator": "macro_evaluator",
                "pipeline.pool_builder": "pool_builder",
                "pipeline.ticker_collector": "ticker_collectors",
            }.get(module, module)
        return argv[0]

    def __call__(self, argv, timeout):
        name = self._component_name(argv)
        with self._lock:
            self.calls.append((name, list(argv), timeout))
        self._mark(name, "start")
        try:
            behavior = self.behaviors.get(name)
            if behavior is None:
                return orchestrator.RunResult(0)
            if isinstance(behavior, BaseException):
                raise behavior
            if callable(behavior):
                return behavior(argv)
            return behavior
        finally:
            self._mark(name, "end")

    def started(self, name):
        with self._lock:
            return (name, "start") in self.events

    def order_index(self, name, phase):
        with self._lock:
            return self.events.index((name, phase))


def recording_notifier():
    sent = []

    def osascript(argv):
        sent.append(argv)
        return 0

    return orchestrator.Notifier(enabled=True, runner=osascript), sent


def write_eval(config, session, slot_date, verdict="pass", flags=0):
    path = orchestrator.macro_eval_path(config, session, slot_date)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"verdict": verdict, "flagged_claims": [{"claim": str(i)} for i in range(flags)]}),
        encoding="utf-8",
    )


def read_status(config, session):
    return json.loads(
        orchestrator.status_file_path(config, session).read_text(encoding="utf-8")
    )


def run_slot(config, session, runner, *, registry=None, notifier=None, **kwargs):
    orch = orchestrator.Orchestrator(
        config,
        session,
        slot_date=SLOT_DATE,
        registry=registry if registry is not None else full_fake_registry(),
        runner=runner,
        notifier=notifier or recording_notifier()[0],
        clock=clock,
    )
    return orch.run_slot(**kwargs)


# ---------------------------------------------------------------------------
# Default registry shape
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_default_registry_wires_collector_and_evaluator_and_marks_placeholders(tmp_path):
    config = make_config(tmp_path)
    registry = {c.name: c for c in orchestrator.build_default_registry(config)}
    assert [c.step for c in sorted(registry.values(), key=lambda c: c.step)] == list(range(8))

    ctx = orchestrator.SlotContext("cn", SLOT_DATE, config)
    collector_argv = registry["macro_collector"].build_argv(ctx)
    assert collector_argv[1:] == [
        "-m", "pipeline.macro_collector", "--session", "cn", "--date", "2026-01-05",
    ]
    evaluator_argv = registry["macro_evaluator"].build_argv(ctx)
    assert evaluator_argv[1:3] == ["-m", "pipeline.evaluator"]
    assert evaluator_argv[3].endswith("macro_briefs/2026-01-05.cn.md")

    # Settle (22v.10) and the not-yet-landed components are placeholders.
    for name in ("settle", "ticker_evaluators", "analysis_runner", "execution_adapter"):
        assert registry[name].build_argv is None
    assert "22v.10" in registry["settle"].placeholder_reason
    assert registry["execution_adapter"].us_only


@pytest.mark.unit
def test_default_registry_slot_records_placeholders_as_skipped(tmp_path):
    config = make_config(tmp_path)
    runner = FakeRunner()
    write_eval(config, "cn", SLOT_DATE)
    orch = orchestrator.Orchestrator(
        config, "cn", slot_date=SLOT_DATE, runner=runner,
        notifier=recording_notifier()[0], clock=clock,
    )
    assert orch.run_slot() == 0
    status = read_status(config, "cn")
    components = status["components"]
    assert components["settle"]["status"] == "skipped"
    assert components["macro_collector"]["status"] == "ok"
    assert components["macro_evaluator"]["status"] == "ok"
    assert components["execution_adapter"]["status"] == "skipped"
    assert components["execution_adapter"]["error"] == "us slot only"
    # Only the four real components spawned subprocesses.
    assert sorted(name for name, _, _ in runner.calls) == [
        "macro_collector", "macro_evaluator", "pool_builder", "ticker_collectors",
    ]


# ---------------------------------------------------------------------------
# Subprocess boundary — resolved-dir env export, process-group timeout kill
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_component_env_exports_resolved_data_dirs(tmp_path):
    # Split-brain regression: a TRADINGAGENTS_STATE_DIR-only override must
    # reach the components (which read only the *_DIR vars) as resolved paths.
    base = tmp_path / "custom-state"
    config = load_config({"TRADINGAGENTS_STATE_DIR": str(base)})
    env = orchestrator.component_env(config)
    assert env["TRADINGAGENTS_STATE_DIR"] == str(base)
    assert env["TRADINGAGENTS_MACRO_BRIEF_DIR"] == str(base / "macro_briefs")
    assert env["TRADINGAGENTS_TICKER_BRIEF_DIR"] == str(base / "ticker_briefs")
    assert env["TRADINGAGENTS_POOL_DIR"] == str(base / "pools")
    assert env["TRADINGAGENTS_LEDGER_DIR"] == str(base / "ledger")

    # An explicit per-dir override still wins over the state_dir derivation.
    config = load_config(
        {
            "TRADINGAGENTS_STATE_DIR": str(base),
            "TRADINGAGENTS_MACRO_BRIEF_DIR": str(tmp_path / "elsewhere"),
        }
    )
    assert orchestrator.component_env(config)["TRADINGAGENTS_MACRO_BRIEF_DIR"] == str(
        tmp_path / "elsewhere"
    )


@pytest.mark.unit
def test_default_runner_spawns_components_with_resolved_dir_env(tmp_path, monkeypatch):
    """The collector subprocess must resolve the SAME brief dir the
    orchestrator hands the evaluator — the env travels through Popen."""
    config = make_config(tmp_path)
    captured = {}

    class FakeProc:
        pid = 1234
        returncode = 0

        def communicate(self, timeout=None):
            captured["timeout"] = timeout
            return ("", "")

    def fake_popen(argv, **kwargs):
        captured["argv"] = list(argv)
        captured["kwargs"] = kwargs
        return FakeProc()

    monkeypatch.setattr(orchestrator.subprocess, "Popen", fake_popen)
    orch = orchestrator.Orchestrator(
        config, "cn", slot_date=SLOT_DATE, notifier=recording_notifier()[0], clock=clock
    )  # no injected runner: the default subprocess runner is under test
    assert orch.run_slot(only="macro_collector") == 0
    assert captured["argv"][1:] == [
        "-m", "pipeline.macro_collector", "--session", "cn", "--date", "2026-01-05",
    ]
    env = captured["kwargs"]["env"]
    assert env["TRADINGAGENTS_MACRO_BRIEF_DIR"] == str(config.macro_brief_dir)
    assert env["TRADINGAGENTS_STATE_DIR"] == str(config.state_dir)
    assert captured["kwargs"]["start_new_session"] is True
    assert captured["timeout"] == 1800.0


@pytest.mark.unit
def test_subprocess_runner_passes_env_and_captures_output():
    env = dict(os.environ) | {"TA_TEST_MARKER": "42"}
    runner = orchestrator.make_subprocess_runner(env)
    result = runner(
        [sys.executable, "-c", "import os; print(os.environ['TA_TEST_MARKER'])"], 60
    )
    assert result.returncode == 0
    assert result.stdout.strip() == "42"


@pytest.mark.unit
def test_timeout_kills_the_whole_process_group(monkeypatch):
    """On timeout the orchestrator must kill the component's process GROUP —
    otherwise the backend CLI grandchild keeps running orphaned."""
    events = []

    class HungProc:
        pid = 4242
        returncode = None

        def communicate(self, timeout=None):
            raise subprocess.TimeoutExpired(cmd="component", timeout=timeout)

        def wait(self):
            events.append("reaped")
            return -9

    monkeypatch.setattr(
        orchestrator.subprocess, "Popen", lambda argv, **kwargs: HungProc()
    )
    monkeypatch.setattr(
        orchestrator.os, "killpg", lambda pgid, sig: events.append((pgid, sig))
    )
    runner = orchestrator.make_subprocess_runner()
    with pytest.raises(subprocess.TimeoutExpired):
        runner(["component"], 5.0)
    assert (4242, signal.SIGKILL) in events
    assert "reaped" in events


# ---------------------------------------------------------------------------
# Acceptance 1 — ordering, parallelism, barrier
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_steps_1_and_3_run_in_parallel(tmp_path):
    config = make_config(tmp_path)
    write_eval(config, "us", SLOT_DATE)
    collector_started = threading.Event()
    pool_started = threading.Event()

    # Mirrored waits: each side blocks until the OTHER has started, so ANY
    # sequential driver — 1-before-3 or 3-before-1 — fails loudly.
    def collector(argv):
        collector_started.set()
        assert pool_started.wait(timeout=10), "pool_builder never started while collector ran"
        return orchestrator.RunResult(0)

    def pool(argv):
        pool_started.set()
        assert collector_started.wait(timeout=10), "collector never started while pool_builder ran"
        return orchestrator.RunResult(0, stdout="written: pools/us/2026-01-05.json\n")

    runner = FakeRunner({"macro_collector": collector, "pool_builder": pool})
    assert run_slot(config, "us", runner) == 0
    status = read_status(config, "us")
    assert status["components"]["macro_collector"]["status"] == "ok"
    assert status["components"]["pool_builder"]["status"] == "ok"


@pytest.mark.unit
def test_evaluator_overlaps_3_to_5_and_joins_before_step_6(tmp_path):
    config = make_config(tmp_path)
    write_eval(config, "us", SLOT_DATE)
    evaluator_started = threading.Event()
    step5_started = threading.Event()
    barrier_check = {}

    # Mirrored waits (see test_steps_1_and_3_run_in_parallel): genuine 2∥3–5
    # overlap in both directions, not just one launch order.
    def evaluator(argv):
        evaluator_started.set()
        # Overlap: the evaluator finishes only after step 5 has started.
        assert step5_started.wait(timeout=10), "step 5 never started while evaluator ran"
        return orchestrator.RunResult(0)

    def ticker_evals(argv):
        step5_started.set()
        assert evaluator_started.wait(timeout=10), "evaluator never started while step 5 ran"
        return orchestrator.RunResult(0)

    def analysis(argv):
        # Barrier: by step 6 the evaluator outcome is already in the status file.
        record = read_status(config, "us")["components"].get("macro_evaluator")
        barrier_check["record"] = record
        return orchestrator.RunResult(0)

    runner = FakeRunner(
        {"macro_evaluator": evaluator, "ticker_evaluators": ticker_evals,
         "analysis_runner": analysis}
    )
    assert run_slot(config, "us", runner) == 0
    assert runner.order_index("macro_evaluator", "end") < runner.order_index(
        "analysis_runner", "start"
    )
    assert barrier_check["record"]["status"] == "ok"
    assert barrier_check["record"]["finished_at"] is not None


@pytest.mark.unit
def test_main_steps_run_in_table_order(tmp_path):
    config = make_config(tmp_path)
    write_eval(config, "us", SLOT_DATE)
    runner = FakeRunner()
    assert run_slot(config, "us", runner) == 0
    main_thread_order = [
        name for name, phase in runner.events
        if phase == "start" and name in ("settle", "pool_builder", "ticker_collectors",
                                         "ticker_evaluators", "analysis_runner",
                                         "execution_adapter")
    ]
    assert main_thread_order == ["settle", "pool_builder", "ticker_collectors",
                                 "ticker_evaluators", "analysis_runner", "execution_adapter"]
    # Collector before evaluator on the parallel thread.
    assert runner.order_index("macro_collector", "end") < runner.order_index(
        "macro_evaluator", "start"
    )


# ---------------------------------------------------------------------------
# Acceptance 1 — failure continuation matrix
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_component_failure_marks_failed_and_slot_continues(tmp_path):
    config = make_config(tmp_path)
    write_eval(config, "us", SLOT_DATE)
    runner = FakeRunner(
        {"macro_collector": orchestrator.RunResult(1, stderr="boom: no network\n")}
    )
    notifier, sent = recording_notifier()
    assert run_slot(config, "us", runner, notifier=notifier) == 1
    status = read_status(config, "us")
    collector = status["components"]["macro_collector"]
    assert collector["status"] == "failed"
    assert collector["error"] == "boom: no network"
    # Every later step still ran/was recorded — the slot continued.
    for name in STEP_NAMES:
        assert name in status["components"]
    assert status["components"]["analysis_runner"]["status"] == "ok"
    # failure notification + end-of-slot summary
    assert any("macro_collector failed" in " ".join(argv) for argv in sent)
    assert any("failures: macro_collector" in " ".join(argv) for argv in sent)


@pytest.mark.unit
def test_unexpected_runner_exception_is_failed_not_fatal(tmp_path):
    config = make_config(tmp_path)
    write_eval(config, "us", SLOT_DATE)
    runner = FakeRunner({"pool_builder": OSError("exec format error")})
    assert run_slot(config, "us", runner) == 1
    record = read_status(config, "us")["components"]["pool_builder"]
    assert record["status"] == "failed"
    assert "exec format error" in record["error"]


@pytest.mark.unit
def test_component_timeout_marks_timeout_and_slot_continues(tmp_path):
    config = make_config(
        tmp_path, component_timeouts={"macro_collector": 7, "macro_evaluator": 60}
    )
    write_eval(config, "us", SLOT_DATE)
    runner = FakeRunner(
        {"macro_collector": subprocess.TimeoutExpired(cmd="fake-cli", timeout=7)}
    )
    assert run_slot(config, "us", runner) == 1
    status = read_status(config, "us")
    collector = status["components"]["macro_collector"]
    assert collector["status"] == "timeout"
    assert collector["error"] == "timeout after 7s"
    # Downstream still ran, including the evaluator on the same thread.
    assert status["components"]["macro_evaluator"]["status"] == "ok"
    assert status["components"]["analysis_runner"]["status"] == "ok"
    # Configured timeout was passed to the runner.
    assert [t for name, _, t in runner.calls if name == "macro_collector"] == [7.0]


@pytest.mark.unit
def test_evaluator_fail_verdict_is_warn_not_failed(tmp_path):
    config = make_config(tmp_path)
    write_eval(config, "us", SLOT_DATE, verdict="fail", flags=2)
    runner = FakeRunner()
    notifier, sent = recording_notifier()
    # warn is not a slot failure — exit 0.
    assert run_slot(config, "us", runner, notifier=notifier) == 0
    record = read_status(config, "us")["components"]["macro_evaluator"]
    assert record["status"] == "warn"
    assert record["error"] == "verdict=fail: 2 flagged claims"
    assert any("verdict=fail" in " ".join(argv) for argv in sent)


@pytest.mark.unit
def test_evaluator_pass_verdict_is_ok_and_crash_is_failed(tmp_path):
    config = make_config(tmp_path)
    write_eval(config, "us", SLOT_DATE, verdict="pass")
    runner = FakeRunner()
    assert run_slot(config, "us", runner) == 0
    assert read_status(config, "us")["components"]["macro_evaluator"]["status"] == "ok"

    # Crash (non-zero exit): component failed; gating treats the verdict as missing.
    runner = FakeRunner({"macro_evaluator": orchestrator.RunResult(4, stderr="refused\n")})
    assert run_slot(config, "us", runner) == 1
    record = read_status(config, "us")["components"]["macro_evaluator"]
    assert record["status"] == "failed"
    assert record["error"] == "refused"


@pytest.mark.unit
def test_evaluator_exit_zero_without_eval_json_is_warn(tmp_path):
    config = make_config(tmp_path)  # no eval json written
    runner = FakeRunner()
    assert run_slot(config, "cn", runner) == 0
    record = read_status(config, "cn")["components"]["macro_evaluator"]
    assert record["status"] == "warn"
    assert "eval json unreadable" in record["error"]


@pytest.mark.unit
def test_unexpected_driver_error_still_records_component_outcome(tmp_path, monkeypatch):
    """An exception escaping _run_component (outside the runner try) must not
    kill the eval thread silently: the outcome is recorded as failed and the
    post-barrier steps still run — the join barrier's guarantee."""
    config = make_config(tmp_path)
    write_eval(config, "us", SLOT_DATE)

    def boom(self):
        raise RuntimeError("status machinery broke")

    monkeypatch.setattr(orchestrator.Orchestrator, "_evaluator_outcome", boom)
    assert run_slot(config, "us", FakeRunner()) == 1
    status = read_status(config, "us")
    record = status["components"]["macro_evaluator"]
    assert record["status"] == "failed"
    assert "status machinery broke" in record["error"]
    assert record["finished_at"] is not None
    # Steps 6–7 ran after the join with the eval outcome recorded.
    assert status["components"]["analysis_runner"]["status"] == "ok"
    assert status["components"]["execution_adapter"]["status"] == "ok"


@pytest.mark.unit
def test_logging_failure_never_kills_the_slot_or_eval_thread(tmp_path, monkeypatch):
    """A rotate/append crash (e.g. the watchdog process racing the rename
    chain) is swallowed: every component still completes and is recorded."""
    config = make_config(tmp_path)
    write_eval(config, "us", SLOT_DATE)

    def broken_rotate(path, **kwargs):
        raise FileNotFoundError(path)

    monkeypatch.setattr(orchestrator, "rotate_log", broken_rotate)
    assert run_slot(config, "us", FakeRunner()) == 0
    components = read_status(config, "us")["components"]
    assert components["macro_evaluator"]["status"] == "ok"
    assert components["analysis_runner"]["status"] == "ok"


# ---------------------------------------------------------------------------
# Acceptance 1 — lockfile
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_lockfile_exclusivity_blocks_second_run(tmp_path):
    config = make_config(tmp_path)
    lock = orchestrator.acquire_slot_lock(config, "cn", SLOT_DATE)
    assert lock.exists()
    with pytest.raises(orchestrator.SlotLockHeld):
        run_slot(config, "cn", FakeRunner())
    # The blocked run never touched the status file.
    assert not orchestrator.status_file_path(config, "cn").exists()


@pytest.mark.unit
def test_stale_lock_is_broken_with_warning(tmp_path, caplog):
    config = make_config(tmp_path)
    lock_path = orchestrator.slot_lock_path(config, "cn", SLOT_DATE)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text("pid=999 dead\n", encoding="utf-8")
    stale = time.time() - 13 * 3600
    os.utime(lock_path, (stale, stale))
    write_eval(config, "cn", SLOT_DATE)
    with caplog.at_level("WARNING", logger="pipeline.orchestrator"):
        assert run_slot(config, "cn", FakeRunner()) == 0
    assert any("stale" in message for message in caplog.messages)
    # Slot completed and released its own (fresh) lock.
    assert not lock_path.exists()
    assert read_status(config, "cn")["slot"]["finished_at"] is not None


@pytest.mark.unit
def test_stale_lock_break_loser_defers_to_concurrent_breaker(tmp_path, monkeypatch):
    """Two processes racing to break the same stale lock: the one whose
    rename-aside fails must NOT unlink the winner's fresh lock — it backs off
    with SlotLockHeld, so the slot can never run twice."""
    config = make_config(tmp_path)
    lock_path = orchestrator.slot_lock_path(config, "cn", SLOT_DATE)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text("pid=999 dead\n", encoding="utf-8")
    stale = time.time() - 13 * 3600
    os.utime(lock_path, (stale, stale))

    def racing_rename(src, dst):
        # Concurrent breaker: renamed the stale lock away and O_EXCL-created
        # a fresh one between our stat and rename.
        lock_path.write_text("pid=1234 fresh\n", encoding="utf-8")
        raise FileNotFoundError(src)

    monkeypatch.setattr(orchestrator.os, "rename", racing_rename)
    with pytest.raises(orchestrator.SlotLockHeld):
        orchestrator.acquire_slot_lock(config, "cn", SLOT_DATE)
    # The winner's fresh lock survived untouched.
    assert lock_path.read_text(encoding="utf-8") == "pid=1234 fresh\n"


@pytest.mark.unit
def test_fresh_lock_is_not_broken(tmp_path):
    config = make_config(tmp_path)
    lock_path = orchestrator.slot_lock_path(config, "cn", SLOT_DATE)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text("pid=999 alive\n", encoding="utf-8")  # fresh mtime
    with pytest.raises(orchestrator.SlotLockHeld):
        orchestrator.acquire_slot_lock(config, "cn", SLOT_DATE)


@pytest.mark.unit
def test_lock_released_after_slot(tmp_path):
    config = make_config(tmp_path)
    write_eval(config, "cn", SLOT_DATE)
    run_slot(config, "cn", FakeRunner())
    assert not orchestrator.slot_lock_path(config, "cn", SLOT_DATE).exists()


# ---------------------------------------------------------------------------
# Acceptance 1 — --only / --from (R3)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_only_runs_a_single_component(tmp_path):
    config = make_config(tmp_path)
    runner = FakeRunner()
    assert run_slot(config, "us", runner, only="pool_builder") == 0
    status = read_status(config, "us")
    assert list(status["components"]) == ["pool_builder"]
    assert [name for name, _, _ in runner.calls] == ["pool_builder"]


@pytest.mark.unit
def test_from_step_runs_only_later_steps(tmp_path):
    config = make_config(tmp_path)
    runner = FakeRunner()
    assert run_slot(config, "us", runner, from_step=4) == 0
    status = read_status(config, "us")
    assert sorted(status["components"]) == sorted(
        ["ticker_collectors", "ticker_evaluators", "analysis_runner", "execution_adapter"]
    )
    assert "macro_collector" not in status["components"]


@pytest.mark.unit
def test_only_unknown_component_and_conflicting_flags_raise(tmp_path):
    config = make_config(tmp_path)
    orch = orchestrator.Orchestrator(
        config, "us", slot_date=SLOT_DATE, registry=full_fake_registry(),
        runner=FakeRunner(), notifier=recording_notifier()[0], clock=clock,
    )
    with pytest.raises(ValueError, match="unknown component"):
        orch.run_slot(only="nope")
    with pytest.raises(ValueError, match="mutually exclusive"):
        orch.run_slot(only="settle", from_step=3)
    # Neither error consumed the lock.
    assert not orchestrator.slot_lock_path(config, "us", SLOT_DATE).exists()


# ---------------------------------------------------------------------------
# Acceptance 5 — status file durability
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_status_file_survives_kill_mid_slot(tmp_path):
    config = make_config(tmp_path)
    write_eval(config, "us", SLOT_DATE)

    def pool(argv):
        # Wait (via the on-disk status) until the parallel thread's components
        # are fully recorded, then die mid-component.
        deadline = time.time() + 10
        while time.time() < deadline:
            try:
                components = read_status(config, "us")["components"]
            except FileNotFoundError:
                components = {}
            collector = components.get("macro_collector")
            evaluator = components.get("macro_evaluator")
            if (
                collector
                and collector["status"] == "ok"
                and evaluator
                and evaluator["finished_at"]
            ):
                raise KeyboardInterrupt
            time.sleep(0.005)
        raise AssertionError("thread components never finished")

    runner = FakeRunner({"pool_builder": pool})
    with pytest.raises(KeyboardInterrupt):
        run_slot(config, "us", runner)
    # Write-as-you-go: the file on disk is readable and shows the in-flight
    # component; the slot never finished.
    status = read_status(config, "us")
    assert status["slot"]["finished_at"] is None
    assert status["components"]["macro_collector"]["status"] == "ok"
    assert status["components"]["pool_builder"]["status"] == "running"
    assert status["components"]["pool_builder"]["finished_at"] is None


@pytest.mark.unit
def test_concurrent_cn_and_us_slots_leave_both_files_intact(tmp_path):
    config = make_config(tmp_path)
    write_eval(config, "cn", SLOT_DATE)
    write_eval(config, "us", SLOT_DATE)

    def slow(argv):
        time.sleep(0.01)
        return orchestrator.RunResult(0)

    results = {}

    def run(session):
        runner = FakeRunner(dict.fromkeys(STEP_NAMES, slow))
        results[session] = run_slot(config, session, runner)

    threads = [threading.Thread(target=run, args=(s,)) for s in ("cn", "us")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert results == {"cn": 0, "us": 0}
    for session in ("cn", "us"):
        status = read_status(config, session)
        assert status["slot"]["session"] == session
        assert status["slot"]["date"] == "2026-01-05"
        assert status["slot"]["finished_at"] is not None
        assert len(status["history"]) == 1
    # us-only step ran on us, skipped on cn.
    assert read_status(config, "cn")["components"]["execution_adapter"]["status"] == "skipped"
    assert read_status(config, "us")["components"]["execution_adapter"]["status"] == "ok"


@pytest.mark.unit
def test_history_keeps_last_20_completed_slots(tmp_path):
    config = make_config(tmp_path)
    registry = (fake_component(0, "settle"),)
    for offset in range(22):
        slot_day = date(2026, 1, 1) + timedelta(days=offset)
        orch = orchestrator.Orchestrator(
            config, "cn", slot_date=slot_day, registry=registry,
            runner=FakeRunner(), notifier=recording_notifier()[0], clock=clock,
        )
        assert orch.run_slot() == 0
    history = read_status(config, "cn")["history"]
    assert len(history) == 20
    assert history[-1]["date"] == "2026-01-22"
    assert history[0]["date"] == "2026-01-03"
    assert all("components" in entry for entry in history)


# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_notification_dedup_per_component_slot_reason():
    notifier, sent = recording_notifier()
    key = ("macro_collector", ("us", "2026-01-05"), "failed")
    assert notifier.notify("t", "first", dedup_key=key) is True
    assert notifier.notify("t", "second", dedup_key=key) is False
    assert len(sent) == 1


@pytest.mark.unit
def test_osascript_failure_degrades_to_log_only():
    def broken(argv):
        raise OSError("no osascript")

    notifier = orchestrator.Notifier(enabled=True, runner=broken)
    assert notifier.notify("t", "m") is False  # no exception escapes

    notifier = orchestrator.Notifier(enabled=True, runner=lambda argv: 1)
    assert notifier.notify("t", "m") is False


@pytest.mark.unit
def test_notifications_disabled_never_invokes_osascript(tmp_path):
    calls = []
    notifier = orchestrator.Notifier(enabled=False, runner=lambda argv: calls.append(argv) or 0)
    config = make_config(tmp_path)
    write_eval(config, "us", SLOT_DATE)
    runner = FakeRunner({"macro_collector": orchestrator.RunResult(1, stderr="x\n")})
    run_slot(config, "us", runner, notifier=notifier)
    assert calls == []


@pytest.mark.unit
def test_end_of_slot_summary_always_sent(tmp_path):
    config = make_config(tmp_path)
    write_eval(config, "cn", SLOT_DATE)
    notifier, sent = recording_notifier()
    assert run_slot(config, "cn", FakeRunner(), notifier=notifier) == 0
    assert any("slot cn 2026-01-05" in " ".join(argv) for argv in sent)


# ---------------------------------------------------------------------------
# Acceptance 3 — watchdog
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_watchdog_notifies_once_for_missed_slot(tmp_path):
    config = make_config(tmp_path)
    # Monday 18:00 UTC: NY 13:00 (past 09:30 deadline), Shanghai Tue 02:00
    # (before its deadline) — only us is due and missing.
    now = datetime(2026, 1, 5, 18, 0, tzinfo=timezone.utc)
    notifier, sent = recording_notifier()
    assert orchestrator.run_watchdog(config, clock=lambda: now, notifier=notifier) == 0
    assert len(sent) == 1
    assert "us slot for 2026-01-05 has not started" in " ".join(sent[0])
    # Second hourly fire (fresh notifier = fresh process): marker suppresses it.
    notifier2, sent2 = recording_notifier()
    assert orchestrator.run_watchdog(config, clock=lambda: now, notifier=notifier2) == 0
    assert sent2 == []


@pytest.mark.unit
def test_watchdog_suppressed_when_slot_started(tmp_path):
    config = make_config(tmp_path)
    now = datetime(2026, 1, 5, 18, 0, tzinfo=timezone.utc)
    status = orchestrator.StatusFile(orchestrator.status_file_path(config, "us"))
    status.start_slot("us", date(2026, 1, 5), "2026-01-05T13:31:00Z")
    notifier, sent = recording_notifier()
    orchestrator.run_watchdog(config, clock=lambda: now, notifier=notifier)
    assert sent == []


@pytest.mark.unit
def test_watchdog_silent_on_session_weekend_and_before_deadline(tmp_path):
    config = make_config(tmp_path)
    notifier, sent = recording_notifier()
    # Saturday in both session timezones.
    saturday = datetime(2026, 1, 10, 15, 0, tzinfo=timezone.utc)
    orchestrator.run_watchdog(config, clock=lambda: saturday, notifier=notifier)
    assert sent == []
    # Monday 13:00 UTC: NY 08:00 — before the 09:30 deadline, so us is silent.
    # Shanghai is Mon 21:00 (past its deadline), so mark the cn slot started
    # to isolate the pre-deadline us case.
    monday_early = datetime(2026, 1, 5, 13, 0, tzinfo=timezone.utc)
    status = orchestrator.StatusFile(orchestrator.status_file_path(config, "cn"))
    status.start_slot("cn", date(2026, 1, 5), "2026-01-05T00:31:00Z")
    orchestrator.run_watchdog(config, clock=lambda: monday_early, notifier=notifier)
    assert sent == []


@pytest.mark.unit
def test_watchdog_catches_slot_time_within_grace_of_midnight(tmp_path):
    """A slot_time whose +1h deadline lands past midnight (e.g. 23:30) must
    still alert on the NEXT calendar day — a missed slot is never silent."""
    sessions = {"cn": SessionSchedule("23:30"), "us": SessionSchedule("08:30")}
    config = make_config(tmp_path, sessions=sessions)
    # us Friday slot started — isolates the cn midnight boundary.
    status = orchestrator.StatusFile(orchestrator.status_file_path(config, "us"))
    status.start_slot("us", date(2026, 1, 9), "2026-01-09T13:31:00Z")

    # Friday 23:45 Shanghai: before Friday's 23:30+1h deadline — no alert yet.
    early = datetime(2026, 1, 9, 15, 45, tzinfo=timezone.utc)
    notifier, sent = recording_notifier()
    assert orchestrator.run_watchdog(config, clock=lambda: early, notifier=notifier) == 0
    assert not any("cn slot for 2026-01-09" in " ".join(argv) for argv in sent)

    # Saturday 00:40 Shanghai: Friday's deadline (Sat 00:30) has passed on the
    # next calendar day — the miss is alerted exactly once...
    late = datetime(2026, 1, 9, 16, 40, tzinfo=timezone.utc)
    notifier2, sent2 = recording_notifier()
    assert orchestrator.run_watchdog(config, clock=lambda: late, notifier=notifier2) == 0
    cn_alerts = [argv for argv in sent2 if "cn slot for 2026-01-09" in " ".join(argv)]
    assert len(cn_alerts) == 1
    # ...and the marker suppresses the next hourly fire (fresh process).
    notifier3, sent3 = recording_notifier()
    assert orchestrator.run_watchdog(config, clock=lambda: late, notifier=notifier3) == 0
    assert not any("cn slot for 2026-01-09" in " ".join(argv) for argv in sent3)


# ---------------------------------------------------------------------------
# Log rotation (R4)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_log_rotation_keeps_five_files(tmp_path):
    log = tmp_path / "orchestrator.log"
    for generation in range(7):
        log.write_text(f"generation {generation}\n" * 10, encoding="utf-8")
        orchestrator.rotate_log(log, max_bytes=1, keep=5)
    rotated = sorted(p.name for p in tmp_path.iterdir())
    assert rotated == [
        "orchestrator.log.1",
        "orchestrator.log.2",
        "orchestrator.log.3",
        "orchestrator.log.4",
    ]
    assert (tmp_path / "orchestrator.log.1").read_text(encoding="utf-8").startswith(
        "generation 6"
    )


@pytest.mark.unit
def test_rotate_log_swallows_concurrent_rotation_race(tmp_path, monkeypatch):
    """stat says the file is big, but another rotator (the watchdog process)
    moves it before our rename — treated as already-rotated, never raised."""
    log = tmp_path / "orchestrator.log"
    log.write_text("x" * 64, encoding="utf-8")

    def vanished(self, target):
        raise FileNotFoundError(self)

    monkeypatch.setattr(type(log), "rename", vanished)
    orchestrator.rotate_log(log, max_bytes=1, keep=5)  # must not raise


@pytest.mark.unit
def test_component_lines_logged_one_per_component(tmp_path):
    config = make_config(tmp_path)
    write_eval(config, "cn", SLOT_DATE)
    run_slot(config, "cn", FakeRunner())
    lines = orchestrator.orchestrator_log_path(config).read_text(encoding="utf-8").splitlines()
    logged = [line.split(" | ")[3] for line in lines]
    assert logged.count("macro_collector") == 1
    assert logged.count("settle") == 1
    # machine-parseable prefix: ts | session | date | component | status | dur | tail
    first = lines[0].split(" | ")
    assert first[1] == "cn"
    assert first[2] == "2026-01-05"


# ---------------------------------------------------------------------------
# CLI (offline: placeholder-only paths)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_cli_run_only_settle_offline(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADINGAGENTS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("TRADINGAGENTS_NOTIFICATIONS_ENABLED", "0")
    exit_code = orchestrator.main(
        ["run", "--session", "cn", "--date", "2026-01-05", "--only", "settle"]
    )
    assert exit_code == 0
    status = json.loads(
        (tmp_path / "state" / "pipeline_status.cn.json").read_text(encoding="utf-8")
    )
    assert status["slot"]["date"] == "2026-01-05"
    assert status["components"]["settle"]["status"] == "skipped"


@pytest.mark.unit
def test_cli_rejects_unknown_component(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("TRADINGAGENTS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("TRADINGAGENTS_NOTIFICATIONS_ENABLED", "0")
    exit_code = orchestrator.main(
        ["run", "--session", "cn", "--date", "2026-01-05", "--only", "bogus"]
    )
    assert exit_code == 2
    assert "unknown component" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Steps 3–4 wiring — pool builder + ticker collector (default registry)
# ---------------------------------------------------------------------------


def ticker_summary(failed=(), written=2, requested=3, skipped=0, session="us"):
    """A ticker-collector R6 stdout JSON summary line."""
    return json.dumps(
        {
            "date": SLOT_DATE.isoformat(),
            "session": session,
            "requested": requested,
            "written": written,
            "skipped": skipped,
            "failed": [{"ticker": t, "reason": r} for t, r in failed],
        }
    )


def run_default_slot(config, session, runner, *, notifier=None):
    """Run a slot on the REAL default registry (not the fake one)."""
    orch = orchestrator.Orchestrator(
        config,
        session,
        slot_date=SLOT_DATE,
        runner=runner,
        notifier=notifier or recording_notifier()[0],
        clock=clock,
    )
    return orch.run_slot()


@pytest.mark.unit
def test_default_registry_step_3_and_4_argvs_match_the_real_clis(tmp_path):
    config = make_config(tmp_path)
    registry = {c.name: c for c in orchestrator.build_default_registry(config)}
    ctx = orchestrator.SlotContext("cn", SLOT_DATE, config)

    pool_argv = registry["pool_builder"].build_argv(ctx)
    assert pool_argv[0] == str(config.python_executable)
    assert pool_argv[1:] == [
        "-m", "pipeline.pool_builder", "--session", "cn", "--date", "2026-01-05",
    ]
    # The argv parses against the REAL pool-builder CLI, with default
    # backend/force (the orchestrator never overrides them).
    args = pool_builder.build_parser().parse_args(pool_argv[3:])
    assert (args.session, args.date) == ("cn", SLOT_DATE)
    assert args.backend == "claude"
    assert args.force is False

    ticker_argv = registry["ticker_collectors"].build_argv(ctx)
    assert ticker_argv[0] == str(config.python_executable)
    assert ticker_argv[1:] == [
        "-m", "pipeline.ticker_collector", "--session", "cn", "--date", "2026-01-05",
    ]
    args = ticker_collector.build_parser().parse_args(ticker_argv[3:])
    assert (args.session, args.date) == ("cn", SLOT_DATE)
    assert args.tickers is None
    assert args.backend == "claude"
    assert args.force is False

    # Steps 5–7 remain placeholders.
    for name in ("ticker_evaluators", "analysis_runner", "execution_adapter"):
        assert registry[name].build_argv is None


@pytest.mark.unit
def test_ticker_collector_runs_after_pool_builder(tmp_path):
    config = make_config(tmp_path)
    write_eval(config, "us", SLOT_DATE)
    runner = FakeRunner(
        {
            "pool_builder": orchestrator.RunResult(
                0, stdout=f"written: pools/us/{SLOT_DATE.isoformat()}.json\n"
            ),
            "ticker_collectors": orchestrator.RunResult(0, stdout=ticker_summary() + "\n"),
        }
    )
    assert run_default_slot(config, "us", runner) == 0
    # 4 strictly after 3 (same main thread, table order).
    assert runner.order_index("pool_builder", "end") < runner.order_index(
        "ticker_collectors", "start"
    )
    components = read_status(config, "us")["components"]
    assert components["pool_builder"]["status"] == "ok"
    assert components["pool_builder"]["error"] is None
    assert components["ticker_collectors"]["status"] == "ok"
    assert components["ticker_collectors"]["error"] is None


@pytest.mark.unit
def test_pool_builder_carried_forward_exit_zero_is_warn(tmp_path):
    """Failure-table row: the builder self-handles nomination failure — exit 0
    with a carried-forward pool + stderr warning maps to warn, not failed."""
    config = make_config(tmp_path)
    write_eval(config, "us", SLOT_DATE)
    stderr = (
        "pool-builder: warning: backend 'claude' exited 1: no network — carrying "
        "opportunity/watch forward from the prior pool (streaks untouched)\n"
        "INFO pipeline.pool_builder: wrote pools/us/2026-01-05.json (carried_forward: ...)\n"
    )
    runner = FakeRunner(
        {
            "pool_builder": orchestrator.RunResult(
                0, stdout="carried_forward: pools/us/2026-01-05.json\n", stderr=stderr
            ),
            "ticker_collectors": orchestrator.RunResult(0, stdout=ticker_summary() + "\n"),
        }
    )
    notifier, sent = recording_notifier()
    # warn is not a slot failure — exit 0, and the slot continued into step 4.
    assert run_default_slot(config, "us", runner, notifier=notifier) == 0
    components = read_status(config, "us")["components"]
    record = components["pool_builder"]
    assert record["status"] == "warn"
    assert record["error"].startswith("pool-builder: warning:")
    assert "carrying opportunity/watch forward" in record["error"]
    assert components["ticker_collectors"]["status"] == "ok"
    # No failure notification for a warn — only the end-of-slot summary.
    assert not any("pool_builder" in " ".join(argv) for argv in sent)
    assert any("1 warn" in " ".join(argv) for argv in sent)


@pytest.mark.unit
def test_pool_builder_hard_crash_is_failed_and_slot_continues(tmp_path):
    """Failure-table row: a hard crash (non-zero exit) is failed; the slot
    continues — downstream reads the most recent prior pool file."""
    config = make_config(tmp_path)
    write_eval(config, "us", SLOT_DATE)
    runner = FakeRunner(
        {
            "pool_builder": orchestrator.RunResult(
                1, stderr="pool-builder: unreadable core file core.us.yaml: bad yaml\n"
            ),
            "ticker_collectors": orchestrator.RunResult(0, stdout=ticker_summary() + "\n"),
        }
    )
    notifier, sent = recording_notifier()
    assert run_default_slot(config, "us", runner, notifier=notifier) == 1
    components = read_status(config, "us")["components"]
    assert components["pool_builder"]["status"] == "failed"
    assert components["pool_builder"]["error"].startswith("pool-builder: unreadable core file")
    assert components["ticker_collectors"]["status"] == "ok"
    assert any("pool_builder failed" in " ".join(argv) for argv in sent)


@pytest.mark.unit
def test_pool_builder_outcome_mapper_is_defensive_about_stdout_drift():
    """Symmetry with the ticker mapper: stdout drift on a zero exit can hide a
    degraded (carried-forward) pool, so it maps to warn — never silently ok."""
    R = orchestrator.RunResult
    # Normal written pool -> ok, even alongside a benign builder warning
    # (missing core file) that is NOT the carried-forward marker.
    assert orchestrator._pool_builder_outcome(
        R(
            0,
            stdout="written: pools/us/2026-01-05.json\n",
            stderr="pool-builder: warning: core.us.yaml not found — core layer is empty\n",
        )
    ) == ("ok", None)
    # An exit 0 whose final line matches neither outcome format is a
    # reporting gap -> warn.
    status, error = orchestrator._pool_builder_outcome(R(0, stdout="all done\n"))
    assert status == "warn"
    assert "no 'written:'/'carried_forward:' outcome line" in error
    status, error = orchestrator._pool_builder_outcome(R(0, stdout=""))
    assert status == "warn"
    # The stderr carried-forward marker wins even when stdout drifted (a
    # stray print after the outcome line must not hide the degraded pool).
    status, error = orchestrator._pool_builder_outcome(
        R(
            0,
            stdout="written: pools/us/2026-01-05.json\nsome stray trailing print\n",
            stderr=(
                "pool-builder: warning: backend 'claude' timed out after 360s — "
                "carrying opportunity/watch forward from the prior pool "
                "(streaks untouched)\n"
            ),
        )
    )
    assert status == "warn"
    assert "carrying opportunity/watch forward" in error


@pytest.mark.unit
def test_ticker_collector_partial_failure_summary_maps_to_warn(tmp_path):
    """Collector R6: partial success is exit 0 with the failed list in the
    final-line JSON summary — warn, with the summary surfaced in error."""
    config = make_config(tmp_path)
    write_eval(config, "us", SLOT_DATE)
    summary = ticker_summary(
        failed=[("NVDA", "validation failed after retry: sources_count 3 < 5")],
        written=2,
        requested=3,
    )
    runner = FakeRunner(
        {
            # Preceding stdout noise: only the FINAL line is the summary.
            "ticker_collectors": orchestrator.RunResult(
                0, stdout="collector chatter\n" + summary + "\n"
            ),
        }
    )
    assert run_default_slot(config, "us", runner) == 0  # warn is not a slot failure
    record = read_status(config, "us")["components"]["ticker_collectors"]
    assert record["status"] == "warn"
    assert "2/3 written" in record["error"]
    assert "NVDA: validation failed after retry" in record["error"]


@pytest.mark.unit
def test_ticker_collector_missing_summary_is_warn_not_crash(tmp_path):
    config = make_config(tmp_path)
    for stdout in ("", "not json at all\n"):
        write_eval(config, "us", SLOT_DATE)
        runner = FakeRunner(
            {"ticker_collectors": orchestrator.RunResult(0, stdout=stdout)}
        )
        assert run_default_slot(config, "us", runner) == 0
        record = read_status(config, "us")["components"]["ticker_collectors"]
        assert record["status"] == "warn"
        assert "no parseable JSON summary" in record["error"]


@pytest.mark.unit
def test_ticker_collector_nonzero_exits_are_failed_with_stderr_reason(tmp_path):
    config = make_config(tmp_path)
    write_eval(config, "us", SLOT_DATE)
    # Exit 1: every ticker failed, none written (summary still printed).
    runner = FakeRunner(
        {
            "ticker_collectors": orchestrator.RunResult(
                1,
                stdout=ticker_summary(failed=[("NVDA", "backend down")], written=0,
                                      requested=1) + "\n",
                stderr="ticker-collector: all 1 requested ticker(s) failed; none written\n",
            ),
        }
    )
    assert run_default_slot(config, "us", runner) == 1
    record = read_status(config, "us")["components"]["ticker_collectors"]
    assert record["status"] == "failed"
    assert record["error"] == "ticker-collector: all 1 requested ticker(s) failed; none written"

    # Exit 3: R1's distinct no-tickers exit — failed too, with its reason.
    runner = FakeRunner(
        {
            "ticker_collectors": orchestrator.RunResult(
                3, stderr="ticker-collector: no tickers to collect for session 'us'\n"
            ),
        }
    )
    assert run_default_slot(config, "us", runner) == 1
    record = read_status(config, "us")["components"]["ticker_collectors"]
    assert record["status"] == "failed"
    assert record["error"].startswith("ticker-collector: no tickers")


@pytest.mark.unit
def test_steps_3_and_4_get_configured_timeouts(tmp_path):
    # R1 classes: ticker fan-out 45 min aggregate; the pool builder's default
    # sits inside the collectors' class (config-owned, overridable).
    assert DEFAULT_COMPONENT_TIMEOUTS["ticker_collectors"] == 45 * 60
    assert DEFAULT_COMPONENT_TIMEOUTS["pool_builder"] <= DEFAULT_COMPONENT_TIMEOUTS[
        "macro_collector"
    ]

    config = make_config(tmp_path / "defaults")
    write_eval(config, "us", SLOT_DATE)
    runner = FakeRunner(
        {"ticker_collectors": orchestrator.RunResult(0, stdout=ticker_summary() + "\n")}
    )
    assert run_default_slot(config, "us", runner) == 0
    assert [t for name, _, t in runner.calls if name == "pool_builder"] == [
        float(DEFAULT_COMPONENT_TIMEOUTS["pool_builder"])
    ]
    assert [t for name, _, t in runner.calls if name == "ticker_collectors"] == [2700.0]

    # Config overrides win — the timeouts come from config, not code.
    config = make_config(
        tmp_path / "overridden",
        component_timeouts={"pool_builder": 111, "ticker_collectors": 222},
    )
    write_eval(config, "us", SLOT_DATE)
    runner = FakeRunner(
        {"ticker_collectors": orchestrator.RunResult(0, stdout=ticker_summary() + "\n")}
    )
    assert run_default_slot(config, "us", runner) == 0
    assert [t for name, _, t in runner.calls if name == "pool_builder"] == [111.0]
    assert [t for name, _, t in runner.calls if name == "ticker_collectors"] == [222.0]
