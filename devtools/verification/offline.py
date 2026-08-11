"""Deterministic, offline repository verification commands."""

from __future__ import annotations

import argparse
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

Runner = Callable[..., subprocess.CompletedProcess[Any]]


@dataclass(frozen=True)
class GateStep:
    """A named command that belongs to the offline verification gate."""

    name: str
    argv: tuple[str, ...]


def build_steps(
    repo_root: Path, python_executable: Path, only: str = "all"
) -> tuple[GateStep, ...]:
    """Build the selected offline verification commands in their required order."""
    del repo_root
    steps = (
        GateStep(
            "tests",
            (
                str(python_executable),
                "-m",
                "pytest",
                "tests/",
                "-q",
                "-m",
                "not integration",
            ),
        ),
        GateStep("lint", (str(python_executable), "-m", "ruff", "check", ".")),
        GateStep("diff", ("git", "diff", "--check")),
    )
    if only == "all":
        return steps
    selected = tuple(step for step in steps if step.name == only)
    if not selected:
        raise ValueError(f"unknown verification selection: {only}")
    return selected


def run_gate(
    repo_root: Path,
    python_executable: Path,
    only: str = "all",
    runner: Runner = subprocess.run,
) -> int:
    """Run selected checks, returning as soon as one command fails."""
    for step in build_steps(repo_root, python_executable, only):
        print(f"==> {step.name}: {' '.join(step.argv)}", flush=True)
        result = runner(step.argv, cwd=repo_root)
        if result.returncode:
            return result.returncode
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--only", choices=("all", "tests", "lint", "diff"), default="all")
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--python", dest="python_executable", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the offline verification gate from the command line."""
    args = _parser().parse_args(argv)
    repo_root = args.repo.resolve()
    if not repo_root.is_dir():
        print(f"error: repository does not exist: {repo_root}", file=sys.stderr)
        return 1

    python_executable = (
        args.python_executable or repo_root / ".venv/bin/python"
    ).resolve()
    if not python_executable.is_file():
        print(f"error: Python executable does not exist: {python_executable}", file=sys.stderr)
        return 1

    return run_gate(repo_root, python_executable, args.only)


if __name__ == "__main__":
    raise SystemExit(main())
