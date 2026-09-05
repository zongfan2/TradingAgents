"""Bounded, source-located context and harness-owned deterministic evidence."""

from __future__ import annotations

import ast
import re
import shlex
from collections.abc import Sequence
from pathlib import Path, PurePosixPath

from devtools.verification import offline
from devtools.verification.models import TestRun

_COUNTS = re.compile(
    r"(?:\d+ (?:passed|failed|skipped|deselected|xfailed|xpassed|warnings?|errors?|subtests passed)"
    r"(?:, )?)+(?: in \d+(?:\.\d+)?s)?(?: \(\d+:\d+(?::\d+)?\))?"
)
_SUFFIXES = {".py", ".md", ".toml", ".yaml", ".yml", ".json"}


def harness_test_evidence(output: str) -> list[TestRun]:
    """Called only after successful exact-head Layer 1, never on model output."""
    summaries = [_COUNTS.fullmatch(line.strip("= \n")) for line in output.splitlines()]
    counts = next((match.group() for match in reversed(summaries) if match), None)
    # Build the actual gate definition with safe display paths, not host paths.
    steps = offline.build_steps(Path("."), Path("python"), only="all")
    return [
        TestRun(
            command=shlex.join(step.argv),
            status="pass",
            summary="Executed by harness in detached exact-head checkout; exit 0."
            + (f" {counts}" if step.name == "tests" and counts else ""),
        )
        for step in steps
    ]


def gate_step_names() -> set[str]:
    """Trusted step labels for safe progress/failure attribution."""
    return {step.name for step in offline.build_steps(Path("."), Path("python"), only="all")}


def _safe_source(root: Path, value: str) -> Path:
    path = PurePosixPath(value)
    if (
        not value or path.is_absolute() or "\\" in value
        or any(ord(char) < 32 for char in value)
        or any(part.startswith(".") for part in path.parts)
        or path.suffix not in _SUFFIXES
        or str(path) != value
    ):
        raise ValueError("invalid context path")
    source = root / path
    if source.resolve() != root.resolve() / path or not source.is_file():
        raise ValueError("missing or symlinked context path")
    return source


def validate_gate_definition(root: Path) -> None:
    """Require exact-head gate source to match the harness issuing attestations.

    Historical gate definitions must use their matching revision's harness.
    """
    try:
        target = _safe_source(root, "devtools/verification/offline.py")
        matches = target.read_bytes() == Path(offline.__file__).read_bytes()
    except (OSError, ValueError, TypeError):
        raise ValueError("review gate definition is unavailable") from None
    if not matches:
        raise ValueError("review gate definition does not match harness")


def navigation_index(
    root: Path, paths: Sequence[str], *, explicit: Sequence[str] = ()
) -> str:
    """Index only supplied source paths; no workspace discovery or execution."""
    for value in explicit:
        _safe_source(root, value)
    rows: list[str] = []
    selected = list(dict.fromkeys([*explicit, *paths]))
    for value in selected[:40]:
        try:
            source = _safe_source(root, value)
        except (ValueError, OSError):
            rows.append(f"- {value!r}: unavailable or excluded; not inspected")
            continue
        if source.stat().st_size > 300_000:
            rows.append(f"- {value}:1 — large file; symbol index omitted, read source")
            continue
        content = source.read_text(encoding="utf-8", errors="replace")
        symbols: list[tuple[int, str]] = []
        if source.suffix == ".py":
            try:
                tree = ast.parse(content)
            except (SyntaxError, ValueError, RecursionError):
                rows.append(f"- {value}:1 — Python index unavailable, read source")
                continue
            symbols = sorted(
                (node.lineno, node.name) for node in ast.walk(tree)
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            )
        elif source.suffix == ".md":
            symbols = [
                (number, line.lstrip("# ")[:120])
                for number, line in enumerate(content.splitlines(), 1)
                if re.match(r"^#{1,6} ", line)
            ]
        rows.append(f"- {value}:1 — {len(content.splitlines())} lines")
        rows.extend(f"  {value}:{line} — {name}" for line, name in symbols[:32])
        if len(symbols) > 32:
            rows.append("  additional symbols omitted; source remains available")
    if len(selected) > 40:
        rows.append("Additional files omitted from navigation; changed-file list remains authoritative.")
    return "\n".join(rows) or "No indexed source files; read governing context directly."
