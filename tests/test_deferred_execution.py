"""Deferred us execution — specs/orchestrator.md "Deferred us execution",
acceptance 3 (deferred-step watchdog) and 6.

Fully offline: the same fake subprocess boundary and notifier as
tests/test_orchestrator.py. SLOT_DATE 2026-01-05 is a Monday in EST, so
09:45 New York is 14:45 UTC.
"""

import json
from datetime import datetime, timezone

import pytest

from pipeline import install_schedule, orchestrator
from pipeline.config import PipelineConfig, SessionSchedule, load_config
from tests.test_orchestrator import (
    SLOT_DATE,
    FakeRunner,
    execution_summary,
    full_fake_registry,
    read_status,
    recording_notifier,
)

EXEC_TIME = "09:45"
IN_WINDOW = datetime(2026, 1, 5, 14, 45, tzinfo=timezone.utc)  # 09:45 New York
DEFERRED = "deferred to the execution task (09:45 America/New_York)"


def deferred_config(tmp_path, execution_time=EXEC_TIME):
    sessions = {
        "cn": SessionSchedule(),
        "us": SessionSchedule("05:30", execution_time=execution_time),
    }
    return PipelineConfig(state_dir=tmp_path / "state", sessions=sessions)


def make_orch(config, runner, notifier=None, session="us", now=IN_WINDOW):
    return orchestrator.Orchestrator(
        config,
        session,
        slot_date=SLOT_DATE,
        registry=full_fake_registry(),
        runner=runner,
        notifier=notifier or recording_notifier()[0],
        clock=lambda: now,
    )


def names(runner):
    return [name for name, _argv, _timeout in runner.calls]


def finished_slot(config):
    """Run the day's us slot to completion (step 7 deferred)."""
    runner = FakeRunner()
    assert make_orch(config, runner).run_slot() == 0
    assert "execution_adapter" not in names(runner)
    return read_status(config, "us")


def status_bytes(config):
    return orchestrator.status_file_path(config, "us").read_bytes()


def joined(sent):
    return [" ".join(argv) for argv in sent]


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_execution_time_env_is_us_only_and_unset_by_default():
    assert load_config({}).sessions["us"].execution_time is None
    config = load_config({"TRADINGAGENTS_EXECUTION_TIME_US": " 09:45 "})
    assert config.sessions["us"].execution_time == "09:45"
    assert config.sessions["cn"].execution_time is None
    assert load_config({"TRADINGAGENTS_EXECUTION_TIME_US": "  "}).sessions["us"].execution_time is None


@pytest.mark.unit
def test_invalid_or_cn_execution_time_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="invalid slot_time"):
        load_config({"TRADINGAGENTS_EXECUTION_TIME_US": "9:45am"})
    with pytest.raises(ValueError, match="us-only"):
        PipelineConfig(
            state_dir=tmp_path,
            sessions={"cn": SessionSchedule(execution_time="09:45"), "us": SessionSchedule()},
        )


# ---------------------------------------------------------------------------
# The slot defers step 7
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_slot_defers_step7_and_says_so_in_the_summary(tmp_path):
    config = deferred_config(tmp_path)
    runner = FakeRunner()
    notifier, sent = recording_notifier()
    assert make_orch(config, runner, notifier).run_slot() == 0
    assert "execution_adapter" not in names(runner)
    status = read_status(config, "us")
    step7 = status["components"]["execution_adapter"]
    assert step7["status"] == "skipped" and step7["error"] == DEFERRED
    assert status["slot"]["finished_at"] is not None
    assert any("orders deferred to 09:45" in text for text in joined(sent))


@pytest.mark.unit
def test_from_step7_defers_but_only_execution_adapter_runs_inline(tmp_path):
    config = deferred_config(tmp_path)
    runner = FakeRunner()
    make_orch(config, runner).run_slot(from_step=7)
    assert names(runner) == []
    assert read_status(config, "us")["components"]["execution_adapter"]["error"] == DEFERRED

    runner = FakeRunner()
    make_orch(config, runner).run_slot(only="execution_adapter")
    assert names(runner) == ["execution_adapter"]
    assert read_status(config, "us")["components"]["execution_adapter"]["status"] == "ok"


@pytest.mark.unit
def test_unset_execution_time_keeps_step7_inline(tmp_path):
    config = deferred_config(tmp_path, execution_time=None)
    runner = FakeRunner()
    make_orch(config, runner).run_slot()
    assert "execution_adapter" in names(runner)


@pytest.mark.unit
def test_cn_slot_still_records_us_only_skip(tmp_path):
    config = deferred_config(tmp_path)
    make_orch(config, FakeRunner(), session="cn").run_slot()
    step7 = read_status(config, "cn")["components"]["execution_adapter"]
    assert step7 == {**step7, "status": "skipped", "error": "us slot only"}


# ---------------------------------------------------------------------------
# execute: runs step 7 inside the finished slot
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_execute_runs_step7_in_the_finished_slot_with_one_history_entry(tmp_path):
    config = deferred_config(tmp_path)
    before = finished_slot(config)
    runner = FakeRunner(
        {"execution_adapter": orchestrator.RunResult(0, stdout=execution_summary(
            submitted=1, dry_run=0, skipped=2) + "\n")}
    )
    notifier, sent = recording_notifier()
    assert make_orch(config, runner, notifier).run_execution() == 0
    assert names(runner) == ["execution_adapter"]

    status = read_status(config, "us")
    assert status["slot"]["started_at"] == before["slot"]["started_at"]
    assert status["slot"]["finished_at"] is not None
    assert status["components"]["execution_adapter"]["status"] == "ok"
    # Everything the slot recorded is kept.
    for name, record in before["components"].items():
        if name != "execution_adapter":
            assert status["components"][name] == record
    # The re-finish replaced the slot's history entry instead of appending.
    assert len(status["history"]) == len(before["history"]) == 1
    assert status["history"][-1]["components"]["execution_adapter"]["status"] == "ok"
    assert any(
        "execution us 2026-01-05: ok; orders 1 live/0 dry-run, 2 skipped" in text
        for text in joined(sent)
    )
    # The lock is released for a later manual run.
    assert not orchestrator.slot_lock_path(config, "us", SLOT_DATE).exists()


@pytest.mark.unit
def test_execute_keeps_older_history_entries(tmp_path):
    config = deferred_config(tmp_path)
    path = orchestrator.status_file_path(config, "us")
    path.parent.mkdir(parents=True, exist_ok=True)
    older = {"date": "2026-01-02", "session": "us", "started_at": "2026-01-02T10:30:00Z"}
    path.write_text(json.dumps({"slot": older, "components": {}, "history": [older]}))
    finished_slot(config)
    make_orch(config, FakeRunner()).run_execution()
    history = read_status(config, "us")["history"]
    assert [entry["date"] for entry in history] == ["2026-01-02", "2026-01-05"]


@pytest.mark.unit
def test_execute_failure_is_reported_and_still_finishes_the_slot(tmp_path):
    config = deferred_config(tmp_path)
    finished_slot(config)
    runner = FakeRunner({"execution_adapter": orchestrator.RunResult(1, stderr="S1 refused\n")})
    notifier, sent = recording_notifier()
    assert make_orch(config, runner, notifier).run_execution() == 1
    status = read_status(config, "us")
    assert status["components"]["execution_adapter"]["status"] == "failed"
    assert status["slot"]["finished_at"] is not None
    assert any("execution_adapter failed" in text for text in joined(sent))


@pytest.mark.unit
@pytest.mark.parametrize("state", ["missing", "other-date", "unfinished"])
def test_execute_on_an_unfinished_slot_never_runs_or_writes(tmp_path, state):
    config = deferred_config(tmp_path)
    path = orchestrator.status_file_path(config, "us")
    if state != "missing":
        status = orchestrator.StatusFile(path)
        day = SLOT_DATE if state == "unfinished" else datetime(2026, 1, 2).date()
        status.start_slot("us", day, "2026-01-05T10:30:00Z")
        if state == "other-date":
            status.finish_slot("2026-01-02T12:00:00Z")
    before = path.read_bytes() if path.exists() else None
    runner = FakeRunner()
    notifier, sent = recording_notifier()
    assert make_orch(config, runner, notifier).run_execution() == 1
    assert names(runner) == []
    assert (path.read_bytes() if path.exists() else None) == before
    assert any("not finished at execution time" in text for text in joined(sent))
    log = (config.state_dir / "orchestrator.log").read_text(encoding="utf-8")
    assert "execution_adapter | skipped" in log


@pytest.mark.unit
def test_execute_while_the_slot_holds_the_lock_notifies_without_writing(tmp_path):
    config = deferred_config(tmp_path)
    status = orchestrator.StatusFile(orchestrator.status_file_path(config, "us"))
    status.start_slot("us", SLOT_DATE, "2026-01-05T10:30:00Z")  # slot still running
    lock = orchestrator.acquire_slot_lock(config, "us", SLOT_DATE)
    try:
        before = status_bytes(config)
        runner = FakeRunner()
        notifier, sent = recording_notifier()
        assert orchestrator.execute_slot(
            config, SLOT_DATE, clock=lambda: IN_WINDOW,
            registry=full_fake_registry(), runner=runner, notifier=notifier,
        ) == 0
        assert names(runner) == []
        assert status_bytes(config) == before
        assert any("not finished at execution time" in text for text in joined(sent))
    finally:
        orchestrator.release_slot_lock(lock)


@pytest.mark.unit
def test_execute_double_fire_is_silent(tmp_path):
    config = deferred_config(tmp_path)
    finished_slot(config)
    status = orchestrator.StatusFile(orchestrator.status_file_path(config, "us"))
    assert status.reopen_finished_slot("us", SLOT_DATE)
    status.update_component("execution_adapter", {"status": "running", "started_at": "x",
                                                  "finished_at": None, "duration_s": None,
                                                  "error": None})
    lock = orchestrator.acquire_slot_lock(config, "us", SLOT_DATE)
    try:
        runner = FakeRunner()
        notifier, sent = recording_notifier()
        assert orchestrator.execute_slot(
            config, SLOT_DATE, clock=lambda: IN_WINDOW,
            registry=full_fake_registry(), runner=runner, notifier=notifier,
        ) == 0
        assert names(runner) == [] and sent == []
    finally:
        orchestrator.release_slot_lock(lock)


@pytest.mark.unit
def test_execute_is_us_only(tmp_path):
    config = deferred_config(tmp_path)
    with pytest.raises(ValueError, match="us-only"):
        make_orch(config, FakeRunner(), session="cn").run_execution()


# ---------------------------------------------------------------------------
# execute wrapper gate
# ---------------------------------------------------------------------------


def run_if_due(config, now, runner):
    return orchestrator.run_execution_if_due(
        config, clock=lambda: now, registry=full_fake_registry(), runner=runner,
        notifier=recording_notifier()[0],
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    ("now", "runs"),
    [
        (IN_WINDOW, True),
        (datetime(2026, 1, 5, 14, 59, tzinfo=timezone.utc), True),  # 09:59 NY, edge
        (datetime(2026, 1, 5, 15, 1, tzinfo=timezone.utc), False),  # 10:01 NY
        (datetime(2026, 1, 5, 13, 45, tzinfo=timezone.utc), False),  # 08:45 NY (EDT rendering)
    ],
)
def test_execute_window_is_evaluated_in_new_york(tmp_path, now, runs):
    config = deferred_config(tmp_path)
    finished_slot(config)
    runner = FakeRunner()
    assert run_if_due(config, now, runner) == 0
    assert (names(runner) == ["execution_adapter"]) is runs


@pytest.mark.unit
def test_execute_wrapper_exits_on_weekend_or_without_execution_time(tmp_path):
    saturday = datetime(2026, 1, 10, 14, 45, tzinfo=timezone.utc)
    runner = FakeRunner()
    assert run_if_due(deferred_config(tmp_path), saturday, runner) == 0
    assert run_if_due(deferred_config(tmp_path, execution_time=None), IN_WINDOW, runner) == 0
    assert names(runner) == []


@pytest.mark.unit
def test_execute_cli_date_requires_now(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("TRADINGAGENTS_SKIP_DOTENV", "1")
    monkeypatch.setenv("TRADINGAGENTS_STATE_DIR", str(tmp_path / "state"))
    assert orchestrator.main(["execute", "--date", "2026-01-05"]) == 2
    assert "--date requires --now" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Watchdog: a deferred step 7 that never ran
# ---------------------------------------------------------------------------

LATE = datetime(2026, 1, 5, 16, 0, tzinfo=timezone.utc)  # 11:00 NY, past 09:45+1h


def us_execution_alerts(sent):
    return [text for text in joined(sent) if "us execution for 2026-01-05" in text]


@pytest.mark.unit
def test_watchdog_alerts_once_when_execution_never_ran(tmp_path):
    config = deferred_config(tmp_path)
    finished_slot(config)
    # Keep the cn session quiet: its slot is "started".
    orchestrator.StatusFile(orchestrator.status_file_path(config, "cn")).start_slot(
        "cn", SLOT_DATE, "2026-01-05T00:30:00Z")
    notifier, sent = recording_notifier()
    orchestrator.run_watchdog(config, clock=lambda: LATE, notifier=notifier)
    assert len(us_execution_alerts(sent)) == 1
    notifier2, sent2 = recording_notifier()
    orchestrator.run_watchdog(config, clock=lambda: LATE, notifier=notifier2)
    assert us_execution_alerts(sent2) == []


@pytest.mark.unit
def test_watchdog_silent_before_deadline_or_after_execution(tmp_path):
    config = deferred_config(tmp_path)
    finished_slot(config)
    early = datetime(2026, 1, 5, 15, 30, tzinfo=timezone.utc)  # 10:30 NY < 10:45
    notifier, sent = recording_notifier()
    orchestrator.run_watchdog(config, clock=lambda: early, notifier=notifier)
    assert us_execution_alerts(sent) == []

    make_orch(config, FakeRunner()).run_execution()
    orchestrator.run_watchdog(config, clock=lambda: LATE, notifier=notifier)
    assert us_execution_alerts(sent) == []


@pytest.mark.unit
def test_watchdog_has_no_execution_check_when_unset(tmp_path):
    config = deferred_config(tmp_path, execution_time=None)
    status = orchestrator.StatusFile(orchestrator.status_file_path(config, "us"))
    status.start_slot("us", SLOT_DATE, "2026-01-05T10:30:00Z")
    status.update_component("execution_adapter", {"status": "skipped", "error": DEFERRED})
    status.finish_slot("2026-01-05T12:00:00Z")
    notifier, sent = recording_notifier()
    orchestrator.run_watchdog(config, clock=lambda: LATE, notifier=notifier)
    assert us_execution_alerts(sent) == []


# ---------------------------------------------------------------------------
# Install script
# ---------------------------------------------------------------------------

TASK_NS = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}


def trigger_times(xml_text):
    import xml.etree.ElementTree as ET

    root = ET.fromstring(xml_text)
    return root, [
        node.text.split("T", 1)[1]
        for node in root.findall("t:Triggers/t:CalendarTrigger/t:StartBoundary", TASK_NS)
    ]


@pytest.mark.unit
def test_windows_execution_task_on_shanghai_host(tmp_path):
    config = deferred_config(tmp_path)
    tasks = install_schedule.generate_windows_tasks(
        config, "Asia/Shanghai", 2026, distro="Ubuntu-24.04", user="zongfan"
    )
    assert "pipeline.us-execution.xml" in tasks
    root, times = trigger_times(tasks["pipeline.us-execution.xml"])
    # 09:45 New York = 21:45 (EDT) / 22:45 (EST) Shanghai.
    assert times == ["21:45:00", "22:45:00"]
    args = root.find("t:Actions/t:Exec/t:Arguments", TASK_NS).text
    assert "pipeline.orchestrator execute >>" in args
    assert f"{config.state_dir}/scheduler.us-execution.out.log" in args
    limit = root.find("t:Settings/t:ExecutionTimeLimit", TASK_NS).text
    assert limit == f"PT{900 + install_schedule.SLOT_TASK_TIME_MARGIN_S}S"


@pytest.mark.unit
def test_no_execution_job_when_unset(tmp_path):
    config = deferred_config(tmp_path, execution_time=None)
    tasks = install_schedule.generate_windows_tasks(
        config, "Asia/Shanghai", 2026, distro="Ubuntu-24.04", user="zongfan"
    )
    assert "pipeline.us-execution.xml" not in tasks
    plists = install_schedule.generate_plists(config, "America/Chicago", 2026)
    assert f"{install_schedule.EXECUTION_LABEL}.plist" not in plists
    assert "us-execution" not in install_schedule.format_schedule(config, "Asia/Shanghai", 2026)


@pytest.mark.unit
def test_launchd_execution_plist_and_printed_schedule(tmp_path):
    config = deferred_config(tmp_path)
    plists = install_schedule.generate_plists(config, "America/Chicago", 2026)
    plist = plists[f"{install_schedule.EXECUTION_LABEL}.plist"]
    assert plist["ProgramArguments"][1:] == ["-m", "pipeline.orchestrator", "execute"]
    # 09:45 New York is 08:45 Chicago in both DST seasons... except the
    # mismatched transition weeks, which render 07:45/09:45.
    hours = sorted({(entry["Hour"], entry["Minute"]) for entry in plist["StartCalendarInterval"]})
    assert (8, 45) in hours
    text = install_schedule.format_schedule(config, "Asia/Shanghai", 2026, "windows-tasks")
    assert "us-execution: execution_time 09:45 America/New_York -> host 21:45, 22:45" in text


# ---------------------------------------------------------------------------
# An execute run killed mid-step-7 (review 98e9d6c, low)
# ---------------------------------------------------------------------------


def killed_execute(config):
    """A finished slot, re-opened by an execute that died with step 7
    'running' and its lock file left behind."""
    finished_slot(config)
    status = orchestrator.StatusFile(orchestrator.status_file_path(config, "us"))
    assert status.reopen_finished_slot("us", SLOT_DATE)
    status.update_component("execution_adapter", {"status": "running", "started_at": "x",
                                                  "finished_at": None, "duration_s": None,
                                                  "error": None})
    return orchestrator.acquire_slot_lock(config, "us", SLOT_DATE)


@pytest.mark.unit
def test_watchdog_alerts_once_on_a_killed_execution(tmp_path):
    config = deferred_config(tmp_path)
    killed_execute(config)
    notifier, sent = recording_notifier()
    orchestrator.run_watchdog(config, clock=lambda: LATE, notifier=notifier)
    assert len(us_execution_alerts(sent)) == 1
    assert "has not completed" in us_execution_alerts(sent)[0]
    notifier2, sent2 = recording_notifier()
    orchestrator.run_watchdog(config, clock=lambda: LATE, notifier=notifier2)
    assert us_execution_alerts(sent2) == []


@pytest.mark.unit
def test_manual_execute_on_a_stale_running_lock_says_how_to_recover(tmp_path, capsys):
    config = deferred_config(tmp_path)
    lock = killed_execute(config)
    runner = FakeRunner()
    notifier, sent = recording_notifier()
    kwargs = {"clock": lambda: IN_WINDOW, "registry": full_fake_registry(), "runner": runner,
              "notifier": notifier}
    assert orchestrator.execute_slot(config, SLOT_DATE, manual=True, **kwargs) == 1
    assert f"remove {lock}" in capsys.readouterr().err
    # The scheduled path stays a silent double-fire exit.
    assert orchestrator.execute_slot(config, SLOT_DATE, **kwargs) == 0
    assert names(runner) == [] and sent == []


@pytest.mark.unit
def test_execute_recovers_a_killed_execution_once_the_lock_is_gone(tmp_path):
    config = deferred_config(tmp_path)
    lock = killed_execute(config)
    orchestrator.release_slot_lock(lock)
    runner = FakeRunner()
    assert make_orch(config, runner).run_execution() == 0
    assert names(runner) == ["execution_adapter"]
    status = read_status(config, "us")
    assert status["components"]["execution_adapter"]["status"] == "ok"
    assert status["slot"]["finished_at"] is not None
    assert len(status["history"]) == 1


@pytest.mark.unit
def test_reopen_refuses_a_killed_slot_without_a_running_step7(tmp_path):
    config = deferred_config(tmp_path)
    status = orchestrator.StatusFile(orchestrator.status_file_path(config, "us"))
    status.start_slot("us", SLOT_DATE, "2026-01-05T10:30:00Z")
    status.update_component("analysis_runner", {"status": "running"})  # slot killed here
    assert not orchestrator.StatusFile(status.path).reopen_finished_slot("us", SLOT_DATE)
