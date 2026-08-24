"""launchd schedule installer for the pipeline slots (specs/orchestrator.md).

Config is the single source of truth: this script *generates* the launchd
plists and relies on the orchestrator's wrapper gate (``orchestrator slot``,
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
import os
import plistlib
import shutil
import sys
from collections.abc import Sequence
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from pipeline.common import SESSION_TZ, SESSIONS, atomic_write
from pipeline.config import PipelineConfig, load_config, parse_slot_time

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

#: Baseline PATH for launchd jobs. launchd gives agents a minimal PATH
#: (/usr/bin:/bin:/usr/sbin:/sbin) that misses nvm/homebrew-installed CLIs —
#: the 2026-08-24 us slot lost its whole collection layer to "codex not found
#: on PATH". launchd_path() resolves the backend CLIs' actual directories at
#: install time and bakes them into the plist's EnvironmentVariables.
_STANDARD_PATH_DIRS = ("/usr/local/bin", "/opt/homebrew/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin")
_REQUIRED_CLIS = ("codex", "claude")


def launchd_path() -> str:
    """PATH for the generated plists: backend-CLI dirs first, then standard dirs."""
    dirs: list[str] = []
    for cli in _REQUIRED_CLIS:
        found = shutil.which(cli)
        if found:
            # Keep the symlink's own directory (e.g. nvm's bin/) — resolving
            # would land in the npm package dir where the entry is codex.js,
            # not an executable named ``codex``.
            parent = str(Path(found).parent)
            if parent not in dirs:
                dirs.append(parent)
        else:
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
        "EnvironmentVariables": {"PATH": launchd_path()},
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
        "EnvironmentVariables": {"PATH": launchd_path()},
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


def format_schedule(config: PipelineConfig, host_tz: str, year: int) -> str:
    """Human-readable effective fire times (``--print-schedule``)."""
    lines = [f"Effective launchd fire times (host {host_tz}, enumerated over {year}):"]
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
        description="Generate launchd plists for the pipeline slots (never runs launchctl).",
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
        default=DEFAULT_LAUNCH_AGENTS_DIR,
        help="where to write the plists (default: ~/Library/LaunchAgents)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = load_config()
    host_tz = args.host_tz or host_timezone_name()
    year = args.year or datetime.now().year

    print(format_schedule(config, host_tz, year))
    if args.print_schedule:
        return 0

    written = install(config, host_tz, year, args.output_dir)
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
