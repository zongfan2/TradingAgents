"""Install script + scheduling gate — specs/orchestrator.md acceptance 2.

America/Chicago host fixtures for BOTH sessions in both DST renderings.
Time is always frozen via injectable clocks and explicit ZoneInfo instants —
the host clock and host timezone never participate.
"""

import json
import plistlib
import re
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from pipeline import install_schedule, orchestrator
from pipeline.config import PipelineConfig, SessionSchedule

CHICAGO = ZoneInfo("America/Chicago")
HOST_TZ = "America/Chicago"
YEAR = 2026


def make_config(tmp_path, **overrides):
    return PipelineConfig(state_dir=tmp_path / "state", **overrides)


def chicago(*args):
    return datetime(*args, tzinfo=CHICAGO)


# ---------------------------------------------------------------------------
# Fire-time enumeration (host-DST × market-DST)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_cn_fire_times_on_chicago_host_cover_both_dst_renderings():
    # 08:30 Asia/Shanghai (no DST) is 18:30 CST / 19:30 CDT the previous
    # local day on an America/Chicago host.
    fire_times = install_schedule.session_fire_times("cn", "08:30", HOST_TZ, YEAR)
    assert fire_times == [(18, 30), (19, 30)]


@pytest.mark.unit
def test_us_fire_times_on_chicago_host_single_rendering():
    # Chicago and New York share DST transitions: 07:30 in both renderings.
    fire_times = install_schedule.session_fire_times("us", "08:30", HOST_TZ, YEAR)
    assert fire_times == [(7, 30)]


@pytest.mark.unit
def test_fire_times_respect_configured_slot_time():
    assert install_schedule.session_fire_times("us", "09:00", HOST_TZ, YEAR) == [(8, 0)]


# ---------------------------------------------------------------------------
# Plist generation
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_generated_plists_cover_sessions_and_watchdog(tmp_path):
    config = make_config(tmp_path)
    plists = install_schedule.generate_plists(config, HOST_TZ, YEAR)
    assert sorted(plists) == [
        "com.tradingagents.pipeline.cn.plist",
        "com.tradingagents.pipeline.us.plist",
        "com.tradingagents.pipeline.watchdog.plist",
    ]

    cn = plists["com.tradingagents.pipeline.cn.plist"]
    assert cn["ProgramArguments"][1:] == ["-m", "pipeline.orchestrator", "slot",
                                          "--session", "cn"]
    assert cn["StartCalendarInterval"] == [
        {"Hour": 18, "Minute": 30},
        {"Hour": 19, "Minute": 30},
    ]
    assert cn["RunAtLoad"] is False

    us = plists["com.tradingagents.pipeline.us.plist"]
    assert us["StartCalendarInterval"] == [{"Hour": 7, "Minute": 30}]

    watchdog = plists["com.tradingagents.pipeline.watchdog.plist"]
    assert watchdog["StartInterval"] == 3600
    assert watchdog["ProgramArguments"][1:] == ["-m", "pipeline.orchestrator", "watchdog"]


@pytest.mark.unit
def test_install_writes_parseable_plists_and_never_runs_launchctl(
    tmp_path, monkeypatch, capsys
):
    state_dir = tmp_path / "state"
    monkeypatch.setenv("TRADINGAGENTS_STATE_DIR", str(state_dir))
    output_dir = tmp_path / "LaunchAgents"

    launchctl_calls = []
    import subprocess as subprocess_module

    def forbidden(*args, **kwargs):  # pragma: no cover - failure path
        launchctl_calls.append(args)
        raise AssertionError("installer must never spawn subprocesses")

    monkeypatch.setattr(subprocess_module, "run", forbidden)
    monkeypatch.setattr(subprocess_module, "Popen", forbidden)

    exit_code = install_schedule.main(
        ["--output-dir", str(output_dir), "--host-tz", HOST_TZ, "--year", str(YEAR)]
    )
    assert exit_code == 0
    assert launchctl_calls == []

    written = sorted(p.name for p in output_dir.iterdir())
    assert written == [
        "com.tradingagents.pipeline.cn.plist",
        "com.tradingagents.pipeline.us.plist",
        "com.tradingagents.pipeline.watchdog.plist",
    ]
    with open(output_dir / "com.tradingagents.pipeline.cn.plist", "rb") as handle:
        cn = plistlib.load(handle)
    assert cn["StartCalendarInterval"] == [
        {"Hour": 18, "Minute": 30},
        {"Hour": 19, "Minute": 30},
    ]

    out = capsys.readouterr().out
    # Prints the load commands instead of running them, plus the re-run reminder.
    assert out.count("launchctl load") == 3
    assert "re-running this installer" in out


@pytest.mark.unit
def test_install_creates_state_dir_for_launchd_log_paths(tmp_path, monkeypatch, capsys):
    """launchd never creates StandardOut/ErrorPath directories: on a fresh
    machine the installer must create state_dir or job output is lost. Writes
    are atomic (temp + rename) — no truncated plists, no temp droppings."""
    state_dir = tmp_path / "fresh" / "state"
    monkeypatch.setenv("TRADINGAGENTS_STATE_DIR", str(state_dir))
    output_dir = tmp_path / "LaunchAgents"
    exit_code = install_schedule.main(
        ["--output-dir", str(output_dir), "--host-tz", HOST_TZ, "--year", str(YEAR)]
    )
    assert exit_code == 0
    assert state_dir.is_dir()
    names = sorted(p.name for p in output_dir.iterdir())
    assert names == [  # exactly the three plists — no leftover temp files
        "com.tradingagents.pipeline.cn.plist",
        "com.tradingagents.pipeline.us.plist",
        "com.tradingagents.pipeline.watchdog.plist",
    ]
    with open(output_dir / "com.tradingagents.pipeline.us.plist", "rb") as handle:
        us = plistlib.load(handle)
    assert us["StandardOutPath"].startswith(str(state_dir))


@pytest.mark.unit
def test_print_schedule_matches_generated_plists(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("TRADINGAGENTS_STATE_DIR", str(tmp_path / "state"))
    exit_code = install_schedule.main(
        ["--print-schedule", "--host-tz", HOST_TZ, "--year", str(YEAR)]
    )
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "launchctl" not in out  # print-only mode installs nothing

    config = make_config(tmp_path)
    for session in ("cn", "us"):
        fire_times = install_schedule.session_fire_times(session, "08:30", HOST_TZ, YEAR)
        printed = re.search(rf"^  {session}: .* -> host (.+)$", out, re.MULTILINE).group(1)
        assert printed == ", ".join(f"{h:02d}:{m:02d}" for h, m in fire_times)
        plist = install_schedule.generate_plists(config, HOST_TZ, YEAR)[
            f"com.tradingagents.pipeline.{session}.plist"
        ]
        assert plist["StartCalendarInterval"] == [
            {"Hour": h, "Minute": m} for h, m in fire_times
        ]


# ---------------------------------------------------------------------------
# Wrapper acceptance window — session tz, both DST renderings
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    ("session", "now", "accepted"),
    [
        # cn winter (host CST): 18:35 Chicago = 08:35 Shanghai next day.
        ("cn", chicago(2026, 1, 5, 18, 35), True),
        # cn summer (host CDT): the 19:30 rendering is the valid one...
        ("cn", chicago(2026, 7, 6, 19, 35), True),
        # ...and the winter rendering now lands 07:35 Shanghai — rejected.
        ("cn", chicago(2026, 7, 6, 18, 35), False),
        # The summer rendering fired in winter lands 09:35 Shanghai — rejected.
        ("cn", chicago(2026, 1, 5, 19, 35), False),
        ("cn", chicago(2026, 1, 5, 12, 0), False),
        # us: 07:35 Chicago = 08:35 New York in BOTH DST renderings.
        ("us", chicago(2026, 1, 5, 7, 35), True),
        ("us", chicago(2026, 7, 6, 7, 35), True),
        ("us", chicago(2026, 1, 5, 8, 35), False),
        ("us", chicago(2026, 7, 6, 8, 35), False),
        # Window edges: ±15 minutes exactly.
        ("us", chicago(2026, 1, 5, 7, 45), True),
        ("us", chicago(2026, 1, 5, 7, 46), False),
        ("us", chicago(2026, 1, 5, 7, 15), True),
        ("us", chicago(2026, 1, 5, 7, 14), False),
    ],
)
def test_wrapper_window_evaluated_in_session_tz(session, now, accepted):
    assert orchestrator.within_slot_window(session, "08:30", now) is accepted


# ---------------------------------------------------------------------------
# Session-local slot identity + gates end to end (run_slot_if_due)
# ---------------------------------------------------------------------------


def placeholder_registry():
    """Placeholder-only registry: gate tests never touch a subprocess."""
    return (orchestrator.Component(0, "settle", None),)


def forbidden_runner(argv, timeout):  # pragma: no cover - failure path
    raise AssertionError("gate should not have spawned a component subprocess")


def read_status(config, session):
    return json.loads(
        orchestrator.status_file_path(config, session).read_text(encoding="utf-8")
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    ("now", "expected_date"),
    [
        # Sunday 18:30 CST Chicago IS Monday's Shanghai session (winter).
        (chicago(2026, 1, 4, 18, 30), "2026-01-05"),
        # Sunday 19:30 CDT Chicago IS Monday's Shanghai session (summer).
        (chicago(2026, 7, 5, 19, 30), "2026-07-06"),
    ],
)
def test_sunday_evening_chicago_fire_is_monday_cn_slot(tmp_path, now, expected_date):
    config = make_config(tmp_path)
    notifier = orchestrator.Notifier(enabled=False)
    exit_code = orchestrator.run_slot_if_due(
        config,
        "cn",
        clock=lambda: now,
        registry=placeholder_registry(),
        runner=forbidden_runner,
        notifier=notifier,
    )
    assert exit_code == 0
    status = read_status(config, "cn")
    assert status["slot"]["date"] == expected_date
    assert status["slot"]["session"] == "cn"
    # The lock was keyed on the session-local date too.
    assert not orchestrator.slot_lock_path(
        config, "cn", date.fromisoformat(expected_date)
    ).exists()


@pytest.mark.unit
def test_friday_evening_chicago_is_saturday_cn_slot_and_skipped(tmp_path):
    config = make_config(tmp_path)
    # Friday 18:30 CST Chicago = Saturday 08:30 Shanghai — weekend in the
    # session tz, so the weekday gate (evaluated session-side) skips it.
    now = chicago(2026, 1, 9, 18, 30)
    exit_code = orchestrator.run_slot_if_due(
        config,
        "cn",
        clock=lambda: now,
        registry=placeholder_registry(),
        runner=forbidden_runner,
        notifier=orchestrator.Notifier(enabled=False),
    )
    assert exit_code == 0
    assert not orchestrator.status_file_path(config, "cn").exists()


@pytest.mark.unit
def test_outside_window_fire_exits_zero_without_side_effects(tmp_path):
    config = make_config(tmp_path)
    # A DST-shifted extra fire: 19:35 Chicago in winter is 09:35 Shanghai.
    now = chicago(2026, 1, 5, 19, 35)
    exit_code = orchestrator.run_slot_if_due(
        config,
        "cn",
        clock=lambda: now,
        registry=placeholder_registry(),
        runner=forbidden_runner,
        notifier=orchestrator.Notifier(enabled=False),
    )
    assert exit_code == 0
    assert not orchestrator.status_file_path(config, "cn").exists()
    assert not (config.state_dir / "locks").exists()


@pytest.mark.unit
def test_double_fire_deduped_by_lockfile(tmp_path):
    config = make_config(tmp_path)
    now = chicago(2026, 1, 5, 18, 30)  # Monday cn slot (Shanghai 2026-01-06? no: Jan 6 08:30)
    slot_day = date(2026, 1, 6)  # 18:30 CST Monday = 08:30 Tuesday Shanghai
    lock = orchestrator.acquire_slot_lock(config, "cn", slot_day)
    assert lock.exists()
    exit_code = orchestrator.run_slot_if_due(
        config,
        "cn",
        clock=lambda: now,
        registry=placeholder_registry(),
        runner=forbidden_runner,
        notifier=orchestrator.Notifier(enabled=False),
    )
    # Second fire exits 0 and leaves no status file — deduped, not an error.
    assert exit_code == 0
    assert not orchestrator.status_file_path(config, "cn").exists()
    assert lock.exists()


@pytest.mark.unit
def test_us_slot_window_gate_end_to_end_both_dst(tmp_path):
    for now, slot_iso in (
        (chicago(2026, 1, 5, 7, 30), "2026-01-05"),
        (chicago(2026, 7, 6, 7, 30), "2026-07-06"),
    ):
        state = make_config(tmp_path / slot_iso)
        exit_code = orchestrator.run_slot_if_due(
            state,
            "us",
            clock=lambda now=now: now,
            registry=placeholder_registry(),
            runner=forbidden_runner,
            notifier=orchestrator.Notifier(enabled=False),
        )
        assert exit_code == 0
        assert read_status(state, "us")["slot"]["date"] == slot_iso


@pytest.mark.unit
def test_custom_slot_time_flows_from_config_to_gate_and_plists(tmp_path):
    sessions = {"cn": SessionSchedule("09:00"), "us": SessionSchedule("09:00")}
    config = make_config(tmp_path, sessions=sessions)
    # Gate accepts 09:05 New York (08:05 Chicago) under the custom slot_time.
    assert orchestrator.within_slot_window("us", "09:00", chicago(2026, 1, 5, 8, 5))
    plists = install_schedule.generate_plists(config, HOST_TZ, YEAR)
    assert plists["com.tradingagents.pipeline.us.plist"]["StartCalendarInterval"] == [
        {"Hour": 8, "Minute": 0}
    ]


@pytest.mark.unit
def test_host_timezone_name_requires_iana_link(monkeypatch):
    monkeypatch.setattr(
        install_schedule.os.path, "realpath", lambda _: "/var/db/timezone/zoneinfo/America/Chicago"
    )
    assert install_schedule.host_timezone_name() == "America/Chicago"
    monkeypatch.setattr(install_schedule.os.path, "realpath", lambda _: "/etc/localtime")
    with pytest.raises(RuntimeError, match="--host-tz"):
        install_schedule.host_timezone_name()


@pytest.mark.unit
def test_session_date_derivation_never_uses_host_clock():
    # Same UTC instant, both sessions: date differs — proving session-side
    # derivation (18:35 Chicago winter = 00:35 UTC next day).
    instant = chicago(2026, 1, 5, 18, 35).astimezone(timezone.utc)
    from pipeline.common import session_date

    assert session_date("cn", instant) == date(2026, 1, 6)
    assert session_date("us", instant) == date(2026, 1, 5)


@pytest.mark.unit
def test_plists_set_working_directory_to_repo_root(tmp_path):
    # Regression: launchd's default cwd is "/" where `python -m pipeline.*`
    # cannot resolve the package — two weeks of scheduled fires died on
    # ModuleNotFoundError before WorkingDirectory was emitted (2026-08-24).
    from pipeline import install_schedule as mod
    from pipeline.config import load_config
    cfg = load_config()
    repo_root = str(mod.REPO_ROOT)
    assert repo_root.endswith("TradingAgents")
    session_plist = mod.build_session_plist(cfg, "us", [(8, 30)])
    watchdog_plist = mod.build_watchdog_plist(cfg)
    assert session_plist["WorkingDirectory"] == repo_root
    assert watchdog_plist["WorkingDirectory"] == repo_root
