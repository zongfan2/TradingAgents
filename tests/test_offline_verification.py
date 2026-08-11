import subprocess
from pathlib import Path

import pytest

from devtools.verification import offline


@pytest.mark.unit
def test_all_steps_are_offline_and_ordered(tmp_path):
    steps = offline.build_steps(tmp_path, Path("/venv/python"))
    assert [step.name for step in steps] == ["tests", "lint", "diff"]
    assert steps[0].argv == (
        "/venv/python",
        "-m",
        "pytest",
        "tests/",
        "-q",
        "-m",
        "not integration",
    )
    assert steps[1].argv == ("/venv/python", "-m", "ruff", "check", ".")
    assert steps[2].argv == ("git", "diff", "--check")


@pytest.mark.unit
@pytest.mark.parametrize("only", ["tests", "lint", "diff"])
def test_only_selects_one_step(tmp_path, only):
    assert [step.name for step in offline.build_steps(tmp_path, Path("python"), only)] == [
        only
    ]


@pytest.mark.unit
def test_gate_stops_at_first_failure(tmp_path):
    calls = []

    def runner(argv, **kwargs):
        calls.append(tuple(argv))
        code = 7 if "pytest" in argv else 0
        return subprocess.CompletedProcess(argv, code)

    assert offline.run_gate(tmp_path, Path("python"), runner=runner) == 7
    assert len(calls) == 1


@pytest.mark.unit
def test_gate_runs_from_repo_without_a_shell_and_announces_steps(tmp_path, capsys):
    calls = []

    def runner(argv, **kwargs):
        calls.append((tuple(argv), kwargs))
        return subprocess.CompletedProcess(argv, 0)

    assert offline.run_gate(tmp_path, Path("python"), runner=runner) == 0
    assert [call[1] for call in calls] == [{"cwd": tmp_path}] * 3
    assert "==> tests: python -m pytest tests/ -q -m not integration" in capsys.readouterr().out


@pytest.mark.unit
def test_unknown_selection_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="unknown verification selection"):
        offline.build_steps(tmp_path, Path("python"), "network")


@pytest.mark.unit
def test_cli_reports_missing_repository_in_one_stderr_line(tmp_path, capsys):
    assert offline.main(["--repo", str(tmp_path / "missing")]) == 1
    assert capsys.readouterr().err.splitlines() == [
        f"error: repository does not exist: {tmp_path / 'missing'}"
    ]


@pytest.mark.unit
def test_cli_reports_missing_python_in_one_stderr_line(tmp_path, capsys):
    assert offline.main(["--repo", str(tmp_path), "--python", str(tmp_path / "missing")]) == 1
    assert capsys.readouterr().err.splitlines() == [
        f"error: Python executable does not exist: {tmp_path / 'missing'}"
    ]
