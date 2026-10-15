"""Schedule installer for the pipeline slots (specs/orchestrator.md): launchd
plists on macOS, Windows Task Scheduler tasks on a WSL host.

Config is the single source of truth: this script *generates* the scheduler
jobs and relies on the orchestrator's wrapper gate (``orchestrator slot``,
slot_time ± 15 min evaluated in the session tz) — nothing is hard-coded twice.
Changing ``slot_time`` requires re-running this script (it prints that
reminder).

For each session, every distinct host-local rendering of ``slot_time`` in the
session's IANA tz across host-DST × market-DST combinations is enumerated over
a full year (≤ 4 candidates, deduped) and emitted as one
``StartCalendarInterval`` entry. The wrapper gate makes the extra renderings
harmless: a fire that lands outside the session-tz window exits 0.

The installer writes plists to ``~/Library/LaunchAgents`` but **never** runs
``launchctl`` itself — it prints the load commands for the user to run.
"""

from __future__ import annotations

import argparse
import getpass
import os
import plistlib
import re
import shlex
import shutil
import sys
import xml.etree.ElementTree as ET
from collections.abc import Sequence
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from pipeline.common import SESSION_TZ, SESSIONS, atomic_write
from pipeline.config import BACKEND_CHOICES, PipelineConfig, load_config, parse_slot_time

LABEL_PREFIX = "com.tradingagents.pipeline"
WATCHDOG_INTERVAL_S = 3600
DEFAULT_LAUNCH_AGENTS_DIR = Path("~/Library/LaunchAgents").expanduser()


def host_timezone_name() -> str:
    """The host's IANA timezone from the ``/etc/localtime`` symlink (macOS).

    DST enumeration needs a real IANA zone (a fixed current-offset zone would
    collapse the winter/summer renderings); pass ``--host-tz`` when this
    cannot be resolved.
    """
    link = os.path.realpath("/etc/localtime")
    marker = "zoneinfo/"
    index = link.find(marker)
    if index == -1:
        raise RuntimeError(
            f"cannot derive an IANA timezone from {link!r} — pass --host-tz explicitly"
        )
    return link[index + len(marker) :]


def session_fire_times(
    session: str, slot_time: str, host_tz: str, year: int
) -> list[tuple[int, int]]:
    """All distinct host-local ``(hour, minute)`` renderings of ``slot_time``
    in the session tz, enumerated across every day of ``year`` (covers every
    host-DST × market-DST combination)."""
    hour, minute = parse_slot_time(slot_time)
    session_zone = ZoneInfo(SESSION_TZ[session])
    host_zone = ZoneInfo(host_tz)
    times: set[tuple[int, int]] = set()
    day = date(year, 1, 1)
    one_day = timedelta(days=1)
    while day.year == year:
        session_local = datetime(day.year, day.month, day.day, hour, minute, tzinfo=session_zone)
        fire = session_local.astimezone(host_zone)
        times.add((fire.hour, fire.minute))
        day += one_day
    return sorted(times)


def session_label(session: str) -> str:
    return f"{LABEL_PREFIX}.{session}"


WATCHDOG_LABEL = f"{LABEL_PREFIX}.watchdog"

#: The repo root — ``python -m pipeline.<module>`` resolves the package from
#: the working directory (pipeline/ is not an installed package), and launchd's
#: default cwd is ``/``. Omitting WorkingDirectory made every scheduled fire
#: die with ModuleNotFoundError for two weeks before anything else could run
#: (found 2026-08-24); the watchdog crashed identically, so nothing alerted.
REPO_ROOT = Path(__file__).resolve().parent.parent

#: Baseline PATH for scheduled jobs. launchd gives agents a minimal PATH
#: (/usr/bin:/bin:/usr/sbin:/sbin) that misses nvm/homebrew-installed CLIs —
#: the 2026-08-24 us slot lost its whole collection layer to "codex not found
#: on PATH" — and a wsl.exe-launched job gets no login profile, so
#: ~/.local/bin is missing there too. job_path() resolves the configured
#: backend CLIs' actual directories at install time and bakes them in.
_STANDARD_PATH_DIRS = ("/usr/local/bin", "/opt/homebrew/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin")


def backend_clis(config: PipelineConfig) -> list[str]:
    """The CLIs the configured D19/D20 backend roles invoke, deduped."""
    clis: list[str] = []
    for backend in (config.collect_backend, config.eval_backend):
        if backend not in clis:
            clis.append(backend)
    return clis


def job_path(config: PipelineConfig) -> str:
    """PATH for the generated jobs: backend-CLI dirs first, then standard dirs.

    Every selectable backend CLI that is installed goes in, so switching
    ``TRADINGAGENTS_COLLECT_BACKEND``/``EVAL_BACKEND`` in ``.env`` later needs
    no re-install; only a *configured* backend that is missing warns.
    """
    dirs: list[str] = []
    configured = backend_clis(config)
    for cli in [*configured, *(c for c in BACKEND_CHOICES if c not in configured)]:
        found = shutil.which(cli)
        if found:
            # Keep the symlink's own directory (e.g. nvm's bin/) — resolving
            # would land in the npm package dir where the entry is codex.js,
            # not an executable named ``codex``.
            parent = str(Path(found).parent)
            if parent not in dirs:
                dirs.append(parent)
        elif cli in configured:
            print(f"install-schedule: warning: {cli!r} not found on the current PATH — "
                  "scheduled runs will fail to invoke it", file=sys.stderr)
    for d in _STANDARD_PATH_DIRS:
        if d not in dirs:
            dirs.append(d)
    return ":".join(dirs)


def build_session_plist(
    config: PipelineConfig, session: str, fire_times: Sequence[tuple[int, int]]
) -> dict:
    return {
        "Label": session_label(session),
        "ProgramArguments": [
            str(config.python_executable),
            "-m",
            "pipeline.orchestrator",
            "slot",
            "--session",
            session,
        ],
        "WorkingDirectory": str(REPO_ROOT),
        "EnvironmentVariables": {"PATH": job_path(config)},
        "StartCalendarInterval": [{"Hour": h, "Minute": m} for h, m in fire_times],
        "RunAtLoad": False,
        "StandardOutPath": str(config.state_dir / f"launchd.{session}.out.log"),
        "StandardErrorPath": str(config.state_dir / f"launchd.{session}.err.log"),
    }


def build_watchdog_plist(config: PipelineConfig) -> dict:
    return {
        "Label": WATCHDOG_LABEL,
        "ProgramArguments": [
            str(config.python_executable),
            "-m",
            "pipeline.orchestrator",
            "watchdog",
        ],
        "WorkingDirectory": str(REPO_ROOT),
        "EnvironmentVariables": {"PATH": job_path(config)},
        "StartInterval": WATCHDOG_INTERVAL_S,
        "RunAtLoad": False,
        "StandardOutPath": str(config.state_dir / "launchd.watchdog.out.log"),
        "StandardErrorPath": str(config.state_dir / "launchd.watchdog.err.log"),
    }


def generate_plists(config: PipelineConfig, host_tz: str, year: int) -> dict[str, dict]:
    """``{filename: plist mapping}`` for both sessions + the hourly watchdog."""
    plists: dict[str, dict] = {}
    for session in SESSIONS:
        fire_times = session_fire_times(
            session, config.sessions[session].slot_time, host_tz, year
        )
        plist = build_session_plist(config, session, fire_times)
        plists[f"{plist['Label']}.plist"] = plist
    watchdog = build_watchdog_plist(config)
    plists[f"{watchdog['Label']}.plist"] = watchdog
    return plists


# ---------------------------------------------------------------------------
# Windows Task Scheduler target (WSL host)
# ---------------------------------------------------------------------------
#
# On a WSL host the Linux side cannot schedule itself: the distro only runs
# while something keeps it up, so a Linux timer misses every slot after a
# reboot or an idle shutdown. Windows Task Scheduler is the trigger instead —
# it starts WSL on demand. Each task runs
#   conhost.exe --headless wsl.exe -d <distro> -u <user> --cd <repo>
#       --exec /bin/sh -c "exec env PATH=... <python> -m pipeline.orchestrator ..."
# conhost --headless keeps a multi-hour slot from holding a console window on
# the desktop; the job's own redirection keeps stdout/stderr (wsl.exe output
# would otherwise vanish). Triggers are host-local wall times, so the Windows
# timezone must equal ``host_tz`` (the installer prints that reminder).

TASK_FOLDER = "\\TradingAgents\\"
TASK_NAME_PREFIX = "pipeline"
CONHOST_EXE = r"C:\Windows\System32\conhost.exe"
WSL_EXE = r"C:\Windows\System32\wsl.exe"
TASK_XML_NS = "http://schemas.microsoft.com/windows/2004/02/mit/task"
#: Headroom over the summed component timeouts: the slot-task limit only
#: bounds a hung wsl.exe and must never cut a legitimate slot short.
SLOT_TASK_TIME_MARGIN_S = 3600
WATCHDOG_TASK_TIME_LIMIT = "PT15M"
DEFAULT_WINDOWS_TASKS_SUBDIR = "windows-tasks"
_WSL_NAME_RE = re.compile(r"[A-Za-z0-9._-]+")


def default_target() -> str:
    """``windows-tasks`` under WSL (``WSL_DISTRO_NAME`` is set by wsl.exe), else launchd."""
    return "windows-tasks" if os.environ.get("WSL_DISTRO_NAME") else "launchd"


def slot_task_time_limit(config: PipelineConfig) -> str:
    """ISO-8601 Task Scheduler limit for a slot task: every configured
    component timeout run back to back, plus a margin. Derived from config so
    a raised ``TRADINGAGENTS_TIMEOUT_*`` can never let Task Scheduler kill a
    slot that is still within its own budgets (re-install after changing one)."""
    return f"PT{sum(config.component_timeouts.values()) + SLOT_TASK_TIME_MARGIN_S}S"


def windows_task_name(job: str) -> str:
    return f"{TASK_NAME_PREFIX}.{job}"


def wsl_job_arguments(
    config: PipelineConfig, orchestrator_args: Sequence[str], log_stem: str, distro: str, user: str
) -> str:
    """The conhost.exe argument string that runs one orchestrator command in WSL.

    The sh script travels as ONE double-quoted wsl.exe argument, so it must
    not contain a double quote itself; its own pieces are shell-quoted.
    """
    for label, value in (("WSL distro", distro), ("WSL user", user)):
        if not _WSL_NAME_RE.fullmatch(value):
            raise ValueError(f"unsafe {label} {value!r} — expected [A-Za-z0-9._-]+")
    out_log = config.state_dir / f"scheduler.{log_stem}.out.log"
    err_log = config.state_dir / f"scheduler.{log_stem}.err.log"
    command = [str(config.python_executable), "-m", "pipeline.orchestrator", *orchestrator_args]
    script = (
        f"exec env PATH={shlex.quote(job_path(config))} "
        + " ".join(shlex.quote(part) for part in command)
        + f" >>{shlex.quote(str(out_log))} 2>>{shlex.quote(str(err_log))}"
    )
    repo = str(REPO_ROOT)
    if '"' in script or '"' in repo:
        raise ValueError(
            "a path in the scheduled command contains a double quote — it cannot be "
            "passed through wsl.exe as one quoted argument"
        )
    return (
        f'--headless "{WSL_EXE}" -d {distro} -u {user} --cd "{repo}" '
        f'--exec /bin/sh -c "{script}"'
    )


def _calendar_trigger(parent: ET.Element, year: int, hour: int, minute: int) -> ET.Element:
    trigger = ET.SubElement(parent, "CalendarTrigger")
    ET.SubElement(trigger, "StartBoundary").text = f"{year}-01-01T{hour:02d}:{minute:02d}:00"
    ET.SubElement(trigger, "Enabled").text = "true"
    ET.SubElement(ET.SubElement(trigger, "ScheduleByDay"), "DaysInterval").text = "1"
    return trigger


def build_windows_task(
    description: str,
    arguments: str,
    fire_times: Sequence[tuple[int, int]],
    year: int,
    time_limit: str,
    repeat_hourly: bool = False,
) -> str:
    """One Task Scheduler 1.2 task definition (XML text, no declaration)."""
    task = ET.Element("Task", {"version": "1.2", "xmlns": TASK_XML_NS})
    ET.SubElement(ET.SubElement(task, "RegistrationInfo"), "Description").text = description
    triggers = ET.SubElement(task, "Triggers")
    for hour, minute in fire_times:
        trigger = _calendar_trigger(triggers, year, hour, minute)
        if repeat_hourly:
            repetition = ET.Element("Repetition")
            ET.SubElement(repetition, "Interval").text = "PT1H"
            ET.SubElement(repetition, "Duration").text = "P1D"
            ET.SubElement(repetition, "StopAtDurationEnd").text = "false"
            # Repetition must precede StartBoundary in the schema.
            trigger.insert(0, repetition)
    principal = ET.SubElement(ET.SubElement(task, "Principals"), "Principal", {"id": "Author"})
    # WSL distros are per-user and need the user's logon session.
    ET.SubElement(principal, "LogonType").text = "InteractiveToken"
    ET.SubElement(principal, "RunLevel").text = "LeastPrivilege"
    settings = ET.SubElement(task, "Settings")
    for tag, text in (
        ("MultipleInstancesPolicy", "IgnoreNew"),
        ("DisallowStartIfOnBatteries", "false"),
        ("StopIfGoingOnBatteries", "false"),
        # A late start would land outside the wrapper window anyway; the
        # watchdog reports the missed slot.
        ("StartWhenAvailable", "false"),
        ("WakeToRun", "true"),
        ("ExecutionTimeLimit", time_limit),
        ("Enabled", "true"),
    ):
        ET.SubElement(settings, tag).text = text
    exec_node = ET.SubElement(ET.SubElement(task, "Actions", {"Context": "Author"}), "Exec")
    ET.SubElement(exec_node, "Command").text = CONHOST_EXE
    ET.SubElement(exec_node, "Arguments").text = arguments
    ET.indent(task)
    return ET.tostring(task, encoding="unicode")


def generate_windows_tasks(
    config: PipelineConfig, host_tz: str, year: int, distro: str, user: str
) -> dict[str, str]:
    """``{filename: task XML}`` for both sessions + the hourly watchdog."""
    tasks: dict[str, str] = {}
    for session in SESSIONS:
        fire_times = session_fire_times(
            session, config.sessions[session].slot_time, host_tz, year
        )
        arguments = wsl_job_arguments(
            config, ["slot", "--session", session], session, distro, user
        )
        tasks[f"{windows_task_name(session)}.xml"] = build_windows_task(
            f"TradingAgents {session} slot (generated by pipeline.install_schedule)",
            arguments,
            fire_times,
            year,
            slot_task_time_limit(config),
        )
    arguments = wsl_job_arguments(config, ["watchdog"], "watchdog", distro, user)
    tasks[f"{windows_task_name('watchdog')}.xml"] = build_windows_task(
        "TradingAgents missed-slot watchdog (generated by pipeline.install_schedule)",
        arguments,
        [(0, 0)],
        year,
        WATCHDOG_TASK_TIME_LIMIT,
        repeat_hourly=True,
    )
    return tasks


def install_windows_tasks(
    config: PipelineConfig, host_tz: str, year: int, output_dir: Path, distro: str, user: str
) -> list[Path]:
    """Write the task XML atomically as UTF-16 (schtasks rejects UTF-8 task
    XML); never runs schtasks. Creates ``state_dir`` for the job logs.
    Everything is generated (and validated) before anything is written."""
    tasks = generate_windows_tasks(config, host_tz, year, distro, user)
    output_dir.mkdir(parents=True, exist_ok=True)
    config.state_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for filename, xml_text in tasks.items():
        path = output_dir / filename
        document = '\ufeff<?xml version="1.0" encoding="UTF-16"?>\n' + xml_text + "\n"
        atomic_write(path, document, encoding="utf-16-le")
        written.append(path)
    return written


def format_schedule(
    config: PipelineConfig, host_tz: str, year: int, target: str = "launchd"
) -> str:
    """Human-readable effective fire times (``--print-schedule``)."""
    lines = [f"Effective {target} fire times (host {host_tz}, enumerated over {year}):"]
    for session in SESSIONS:
        slot_time = config.sessions[session].slot_time
        fire_times = session_fire_times(session, slot_time, host_tz, year)
        rendered = ", ".join(f"{h:02d}:{m:02d}" for h, m in fire_times)
        lines.append(
            f"  {session}: slot_time {slot_time} {SESSION_TZ[session]} -> host {rendered}"
        )
    lines.append(f"  watchdog: every {WATCHDOG_INTERVAL_S}s")
    lines.append(
        "Wrapper gate: each fire runs only within slot_time ± 15 min in the session tz, "
        "weekdays (session-local); other fires exit 0."
    )
    return "\n".join(lines)


def install(config: PipelineConfig, host_tz: str, year: int, output_dir: Path) -> list[Path]:
    """Write the plists atomically; returns the written paths.

    Never touches launchctl. Also creates ``state_dir`` up front: the plists
    point ``StandardOutPath``/``StandardErrorPath`` there, and launchd does
    not create log directories — on a fresh machine the jobs would silently
    lose their stdout/stderr. Atomic writes (temp file + rename) mean a kill
    mid-install can never leave a truncated plist for launchctl to reject.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    config.state_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for filename, plist in generate_plists(config, host_tz, year).items():
        path = output_dir / filename
        atomic_write(path, plistlib.dumps(plist, sort_keys=True).decode("utf-8"))
        written.append(path)
    return written


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m pipeline.install_schedule",
        description=(
            "Generate launchd plists or Windows Task Scheduler tasks (WSL host) for the "
            "pipeline slots (never runs launchctl/schtasks)."
        ),
    )
    parser.add_argument(
        "--target",
        choices=("launchd", "windows-tasks"),
        default=None,
        help="scheduler to generate for (default: windows-tasks under WSL, else launchd)",
    )
    parser.add_argument(
        "--wsl-distro",
        default=None,
        help="windows-tasks: WSL distro to run in (default: $WSL_DISTRO_NAME)",
    )
    parser.add_argument(
        "--wsl-user",
        default=None,
        help="windows-tasks: WSL user to run as (default: the current user)",
    )
    parser.add_argument(
        "--print-schedule",
        action="store_true",
        help="print the effective fire times without installing anything",
    )
    parser.add_argument(
        "--host-tz",
        default=None,
        help="host IANA timezone (default: derived from /etc/localtime)",
    )
    parser.add_argument(
        "--year",
        type=int,
        default=None,
        help="year to enumerate DST combinations over (default: current year)",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help=(
            "where to write the jobs (default: ~/Library/LaunchAgents for launchd, "
            f"<state_dir>/{DEFAULT_WINDOWS_TASKS_SUBDIR} for windows-tasks)"
        ),
    )
    return parser


def _main_windows_tasks(args: argparse.Namespace, config: PipelineConfig, host_tz: str,
                        year: int) -> int:
    distro = args.wsl_distro or os.environ.get("WSL_DISTRO_NAME")
    if not distro:
        print("install-schedule: error: not under WSL — pass --wsl-distro", file=sys.stderr)
        return 2
    user = args.wsl_user or getpass.getuser()
    output_dir = args.output_dir or config.state_dir / DEFAULT_WINDOWS_TASKS_SUBDIR
    try:
        written = install_windows_tasks(config, host_tz, year, output_dir, distro, user)
    except ValueError as exc:
        print(f"install-schedule: error: {exc}", file=sys.stderr)
        return 2
    print("\nWrote:")
    for path in written:
        print(f"  {path}")
    print("\nNot registered automatically — run when ready (from a WSL shell):")
    for path in written:
        print(
            f"  schtasks.exe /Create /TN '{TASK_FOLDER}{path.stem}' "
            f'/XML "$(wslpath -w {shlex.quote(str(path))})" /F'
        )
    print(
        f"\nThe triggers are wall times in {host_tz}: the Windows timezone must match "
        "(check with `tzutil.exe /g`). Tasks run only while the Windows user is logged on "
        "(a locked screen is fine)."
    )
    print(
        "\nReminder: changing sessions.*.slot_time or a TRADINGAGENTS_TIMEOUT_* budget "
        "requires re-running this installer (the tasks, their time limit and the wrapper "
        "window are generated from config)."
    )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    from pipeline.orchestrator import _load_repo_dotenv
    _load_repo_dotenv()
    args = build_parser().parse_args(argv)
    config = load_config()
    host_tz = args.host_tz or host_timezone_name()
    year = args.year or datetime.now().year
    target = args.target or default_target()

    print(format_schedule(config, host_tz, year, target))
    if args.print_schedule:
        return 0
    if target == "windows-tasks":
        return _main_windows_tasks(args, config, host_tz, year)

    written = install(config, host_tz, year, args.output_dir or DEFAULT_LAUNCH_AGENTS_DIR)
    print("\nWrote:")
    for path in written:
        print(f"  {path}")
    print("\nNot loaded automatically — run when ready:")
    for path in written:
        print(f"  launchctl load {path}")
    print(
        "\nReminder: changing sessions.*.slot_time requires re-running this installer "
        "(the plists and the wrapper window are generated from config)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
