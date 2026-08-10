"""Macro collector behavior (specs/macro-brief-collector.md R1–R8, AC1–AC5).

Fully offline: the backend subprocess boundary (``claude -p`` / ``codex exec``)
and the aws CLI are exercised only through injected fakes or a monkeypatched
``subprocess.run``.
"""

import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest

from pipeline import macro_collector
from pipeline.contracts import briefs
from pipeline.macro_collector import CollectorError, collect, main
from pipeline.prompts import SESSION_BLOCKS

DATE = "2026-08-08"
AS_OF = date(2026, 8, 8)

# ---------------------------------------------------------------------------
# Fixtures & fakes
# ---------------------------------------------------------------------------


def make_brief(
    *,
    date_str=DATE,
    session="us",
    generator="claude-deep-search",
    generated_at="2026-08-08T12:30:00Z",
    n_urls=9,
    sources_count=None,
    words_per_section=150,
    titles=briefs.MACRO_SECTIONS,
):
    """A contract-valid macro brief (or an invalid one via the knobs)."""
    if sources_count is None:
        sources_count = n_urls
    citations = " ".join(f"[Src](https://example.com/src{i})" for i in range(n_urls))
    blocks = []
    for i, title in enumerate(titles):
        content = ("filler " * words_per_section).strip()
        if i == 0:
            content += " " + citations
        block = f"## {title}\n\n{content}\n"
        if title in briefs.MACRO_IMPACT_SECTIONS:
            block += "\n**Impact**: neutral — no marginal change.\n"
        blocks.append(block)
    return (
        "---\n"
        f"as_of_date: {date_str}\n"
        f"session: {session}\n"
        f"generated_at: {generated_at}\n"
        f"generator: {generator}\n"
        f"sources_count: {sources_count}\n"
        "---\n\n" + "\n".join(blocks)
    )


def missing_section_brief(**kwargs):
    titles = tuple(t for t in briefs.MACRO_SECTIONS if t != "China & Asia")
    return make_brief(titles=titles, **kwargs)


class FakeRunner:
    """Injected in place of the backend subprocess; replays queued outputs."""

    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.calls = []  # list of (backend, prompt)

    def __call__(self, backend, prompt):
        self.calls.append((backend, prompt))
        result = self.outputs.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


@pytest.fixture()
def brief_dir(tmp_path, monkeypatch):
    directory = tmp_path / "macro_briefs"
    monkeypatch.setenv(macro_collector.MACRO_BRIEF_DIR_ENV, str(directory))
    monkeypatch.delenv(macro_collector.S3_URI_ENV, raising=False)
    # These tests' fixtures are claude-flavored; the shipped default backend
    # is codex per D19, so pin the config env (the resolution itself is
    # covered by the dedicated default-backend tests below).
    monkeypatch.setenv("TRADINGAGENTS_COLLECT_BACKEND", "claude")
    return directory


def collector_log(brief_dir):
    return (brief_dir / "collector.log").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Fresh run (R1/R2/R4/R8) and idempotence (R5, AC1)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_fresh_run_writes_valid_brief_and_logs(brief_dir):
    runner = FakeRunner([make_brief()])
    result = collect("us", as_of=AS_OF, runner=runner)
    assert result.outcome == "written"
    assert result.attempts == 1
    assert result.sources_count == 9
    assert result.path == brief_dir / f"{DATE}.us.md"
    assert result.path.read_text(encoding="utf-8") == make_brief()
    assert f"{DATE} | us | claude | 9 | written" in collector_log(brief_dir)
    backend, prompt = runner.calls[0]
    assert backend == "claude"
    assert f"as_of_date: {DATE}" in prompt  # {{DATE}} rendered (R1)
    assert "generator: claude-deep-search" in prompt


@pytest.mark.unit
def test_second_run_skips_without_invoking_backend(brief_dir):
    collect("us", as_of=AS_OF, runner=FakeRunner([make_brief()]))
    before = (brief_dir / f"{DATE}.us.md").read_text(encoding="utf-8")

    second = FakeRunner([])
    result = collect("us", as_of=AS_OF, runner=second)
    assert result.outcome == "skipped"
    assert second.calls == []  # backend never invoked (R5)
    assert (brief_dir / f"{DATE}.us.md").read_text(encoding="utf-8") == before
    assert f"{DATE} | us | claude | - | skipped" in collector_log(brief_dir)


@pytest.mark.unit
def test_force_rewrites_and_archives_previous_revision(brief_dir):
    first = make_brief(generated_at="2026-08-08T11:00:00Z")
    second = make_brief(generated_at="2026-08-08T12:30:00Z")
    collect("us", as_of=AS_OF, runner=FakeRunner([first]))

    result = collect("us", as_of=AS_OF, force=True, runner=FakeRunner([second]))
    assert result.outcome == "written"
    assert result.path.read_text(encoding="utf-8") == second
    archived = brief_dir / "archive" / f"{DATE}.us.2026-08-08T11:00:00Z.md"
    assert archived.read_text(encoding="utf-8") == first  # revision preserved


@pytest.mark.unit
def test_default_date_resolves_in_session_timezone(brief_dir, monkeypatch):
    seen = {}

    def fake_session_date(session):
        seen["session"] = session
        return AS_OF

    monkeypatch.setattr(macro_collector, "session_date", fake_session_date)
    result = collect("us", runner=FakeRunner([make_brief()]))
    assert seen["session"] == "us"
    assert result.path.name == f"{DATE}.us.md"


# ---------------------------------------------------------------------------
# Failure never leaves a partial file (R6, AC2)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_runner_crash_leaves_no_partial_file(brief_dir):
    runner = FakeRunner([RuntimeError("killed mid-search")])
    with pytest.raises(RuntimeError, match="killed mid-search"):
        collect("us", as_of=AS_OF, runner=runner)
    assert not (brief_dir / f"{DATE}.us.md").exists()
    stray = sorted(p.name for p in brief_dir.iterdir())
    assert stray == ["collector.log"]  # no temp files, no partial brief
    assert f"{DATE} | us | claude | - | failed" in collector_log(brief_dir)


@pytest.mark.unit
def test_invalid_brief_never_written(brief_dir):
    bad = missing_section_brief()
    with pytest.raises(CollectorError):
        collect("us", as_of=AS_OF, runner=FakeRunner([bad, bad]))
    assert not (brief_dir / f"{DATE}.us.md").exists()
    assert f"{DATE} | us | claude | - | failed" in collector_log(brief_dir)


# ---------------------------------------------------------------------------
# Validation + single retry with errors appended (R3, AC3)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_validation_rejection_retries_exactly_once_then_fails(brief_dir):
    bad = missing_section_brief()
    runner = FakeRunner([bad, bad])
    with pytest.raises(CollectorError, match="validation failed after retry"):
        collect("us", as_of=AS_OF, runner=runner)
    assert len(runner.calls) == 2  # exactly one retry
    first_prompt, retry_prompt = runner.calls[0][1], runner.calls[1][1]
    assert retry_prompt.startswith(first_prompt)
    assert "missing section '## China & Asia'" in retry_prompt  # errors fed back


@pytest.mark.unit
def test_retry_with_valid_brief_recovers(brief_dir):
    runner = FakeRunner([missing_section_brief(), make_brief()])
    result = collect("us", as_of=AS_OF, runner=runner)
    assert result.outcome == "written"
    assert result.attempts == 2
    assert result.path.read_text(encoding="utf-8") == make_brief()


@pytest.mark.unit
def test_generator_mismatch_is_a_validation_error(brief_dir):
    wrong = make_brief(generator="codex-deep-search")  # backend is claude
    runner = FakeRunner([wrong, make_brief()])
    result = collect("us", as_of=AS_OF, runner=runner)
    assert result.attempts == 2
    assert "does not match the invoked backend 'claude-deep-search'" in runner.calls[1][1]


@pytest.mark.unit
def test_frontmatter_filename_mismatch_is_a_validation_error(brief_dir):
    # Internally consistent brief for the wrong date: parse passes, the
    # filename cross-check must still reject it (R3).
    wrong_date = make_brief(date_str="2026-08-07")
    runner = FakeRunner([wrong_date, make_brief()])
    result = collect("us", as_of=AS_OF, runner=runner)
    assert result.attempts == 2
    assert "does not match frontmatter" in runner.calls[1][1]


@pytest.mark.unit
def test_word_count_outside_target_warns_but_writes(brief_dir, caplog):
    # ~610 words: inside the 500-2500 hard bounds, below the 800-1500 target.
    text = make_brief(words_per_section=80)
    with caplog.at_level("WARNING", logger="pipeline.contracts.briefs"):
        result = collect("us", as_of=AS_OF, runner=FakeRunner([text]))
    assert result.outcome == "written"
    assert result.attempts == 1
    assert "outside the 800-1500 target" in caplog.text


# ---------------------------------------------------------------------------
# Backfill (--date, AC4) and session renders (AC5)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_date_backfill_renders_date_and_marks_generated_at_later(brief_dir):
    text = make_brief(date_str="2026-07-25", generated_at="2026-08-09T01:00:00Z")
    runner = FakeRunner([text])
    result = collect("us", as_of="2026-07-25", runner=runner)
    assert result.path.name == "2026-07-25.us.md"
    prompt = runner.calls[0][1]
    assert "as_of_date: 2026-07-25" in prompt
    assert "nothing published after 2026-07-25" in prompt  # the backfill cutoff
    meta, _body = briefs.parse_macro_brief(result.path.read_text(encoding="utf-8"))
    assert meta.generated_at.date() > meta.as_of_date  # backfill marker


@pytest.mark.unit
def test_cn_and_us_prompts_differ_for_same_date(brief_dir):
    cn_runner = FakeRunner([make_brief(session="cn")])
    us_runner = FakeRunner([make_brief(session="us")])
    collect("cn", as_of=AS_OF, runner=cn_runner)
    collect("us", as_of=AS_OF, runner=us_runner)
    cn_prompt, us_prompt = cn_runner.calls[0][1], us_runner.calls[0][1]
    assert cn_prompt != us_prompt
    assert SESSION_BLOCKS["cn"] in cn_prompt and SESSION_BLOCKS["us"] not in cn_prompt
    assert SESSION_BLOCKS["us"] in us_prompt and SESSION_BLOCKS["cn"] not in us_prompt
    assert (brief_dir / f"{DATE}.cn.md").exists()
    assert (brief_dir / f"{DATE}.us.md").exists()


# ---------------------------------------------------------------------------
# Optional S3 sync (R7)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_s3_sync_copies_after_write(brief_dir, monkeypatch):
    monkeypatch.setenv(macro_collector.S3_URI_ENV, "s3://bucket/briefs")
    copies = []
    result = collect(
        "us",
        as_of=AS_OF,
        runner=FakeRunner([make_brief()]),
        s3_copy=lambda path, uri: copies.append((path, uri)),
    )
    assert copies == [(result.path, "s3://bucket/briefs")]


@pytest.mark.unit
def test_s3_sync_failure_is_warning_not_error(brief_dir, monkeypatch, caplog):
    monkeypatch.setenv(macro_collector.S3_URI_ENV, "s3://bucket/briefs")

    def boom(path, uri):
        raise RuntimeError("no credentials")

    with caplog.at_level("WARNING", logger="pipeline.macro_collector"):
        result = collect("us", as_of=AS_OF, runner=FakeRunner([make_brief()]), s3_copy=boom)
    assert result.outcome == "written"  # local write is the source of truth
    assert "S3 sync" in caplog.text and "no credentials" in caplog.text
    assert f"{DATE} | us | claude | 9 | written" in collector_log(brief_dir)


@pytest.mark.unit
def test_s3_sync_skipped_without_env(brief_dir):
    copies = []
    collect(
        "us",
        as_of=AS_OF,
        runner=FakeRunner([make_brief()]),
        s3_copy=lambda path, uri: copies.append((path, uri)),
    )
    assert copies == []


@pytest.mark.unit
def test_default_s3_copy_invokes_aws_cli(monkeypatch, tmp_path):
    seen = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(macro_collector.subprocess, "run", fake_run)
    target = tmp_path / f"{DATE}.us.md"
    target.write_text("brief", encoding="utf-8")
    macro_collector.default_s3_copy(target, "s3://bucket/briefs/")
    assert seen["cmd"] == ["aws", "s3", "cp", str(target), f"s3://bucket/briefs/{DATE}.us.md"]


# ---------------------------------------------------------------------------
# Default backend runner (subprocess boundary, faked)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_default_runner_claude_command(monkeypatch):
    seen = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        seen["input"] = kwargs.get("input")
        return subprocess.CompletedProcess(cmd, 0, stdout="BRIEF TEXT", stderr="")

    monkeypatch.setattr(macro_collector.subprocess, "run", fake_run)
    assert macro_collector.default_runner("claude", "PROMPT") == "BRIEF TEXT"
    assert seen["cmd"] == ["claude", "-p", "--allowedTools", "WebSearch,WebFetch"]
    assert seen["input"] == "PROMPT"  # prompt goes over stdin


@pytest.mark.unit
def test_default_runner_codex_command(monkeypatch):
    seen = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, stdout="BRIEF", stderr="")

    monkeypatch.setattr(macro_collector.subprocess, "run", fake_run)
    macro_collector.default_runner("codex", "PROMPT")
    # D19 (codex is the collection default): a working codex exec invocation —
    # web search on, git-repo trust check skipped (components inherit an
    # arbitrary cwd), prompt over stdin via '-'.
    assert seen["cmd"] == ["codex", "exec", "--search", "--skip-git-repo-check", "-"]


@pytest.mark.unit
def test_default_runner_nonzero_exit_raises_one_line_reason(monkeypatch):
    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 2, stdout="", stderr="not logged in\nmore detail")

    monkeypatch.setattr(macro_collector.subprocess, "run", fake_run)
    with pytest.raises(CollectorError, match="exited 2: not logged in"):
        macro_collector.default_runner("claude", "PROMPT")


@pytest.mark.unit
def test_default_runner_missing_cli_raises(monkeypatch):
    def fake_run(cmd, **kwargs):
        raise FileNotFoundError(cmd[0])

    monkeypatch.setattr(macro_collector.subprocess, "run", fake_run)
    with pytest.raises(CollectorError, match="not found on PATH"):
        macro_collector.default_runner("claude", "PROMPT")


@pytest.mark.unit
def test_default_runner_timeout_raises_collector_error(monkeypatch):
    monkeypatch.delenv("TRADINGAGENTS_TIMEOUT_MACRO_COLLECTOR", raising=False)

    def fake_run(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=kwargs.get("timeout"))

    monkeypatch.setattr(macro_collector.subprocess, "run", fake_run)
    with pytest.raises(CollectorError, match="timed out after 840s"):
        macro_collector.default_runner("claude", "PROMPT")


@pytest.mark.unit
def test_backend_timeout_fits_inside_the_component_budget(monkeypatch):
    monkeypatch.delenv("TRADINGAGENTS_TIMEOUT_MACRO_COLLECTOR", raising=False)
    timeout = macro_collector.backend_timeout_seconds()
    assert timeout == 840.0  # (1800 - 120) / 2
    # The R3 worst case (attempt + errors-appended retry) plus the
    # render/validate/write headroom fits the orchestrator's 1800s
    # macro_collector budget — a hung first backend can no longer eat the
    # whole outer budget and get the process group SIGKILLed before the R6
    # one-line stderr reason (or the retry) happens.
    assert (
        macro_collector.BACKEND_ATTEMPTS * timeout + macro_collector.HEADROOM_SECONDS
        <= 1800.0
    )
    # The env override that resizes the outer budget resizes the inner share.
    monkeypatch.setenv("TRADINGAGENTS_TIMEOUT_MACRO_COLLECTOR", "2520")
    assert macro_collector.backend_timeout_seconds() == 1200.0  # (2520 - 120) / 2
    # A pathologically small budget still leaves a usable attempt (floor).
    monkeypatch.setenv("TRADINGAGENTS_TIMEOUT_MACRO_COLLECTOR", "120")
    assert (
        macro_collector.backend_timeout_seconds()
        == macro_collector.MIN_BACKEND_TIMEOUT_SECONDS
    )


@pytest.mark.unit
def test_default_runner_uses_the_budget_sized_timeout(monkeypatch):
    monkeypatch.delenv("TRADINGAGENTS_TIMEOUT_MACRO_COLLECTOR", raising=False)
    seen = {}

    def fake_run(cmd, **kwargs):
        seen["timeout"] = kwargs.get("timeout")
        return subprocess.CompletedProcess(cmd, 0, stdout="BRIEF", stderr="")

    monkeypatch.setattr(macro_collector.subprocess, "run", fake_run)
    macro_collector.default_runner("claude", "PROMPT")
    assert seen["timeout"] == 840.0  # budget-derived, not the outer 1800s


# ---------------------------------------------------------------------------
# CLI (argparse + exit codes, R6)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_cli_writes_then_skips_with_exit_zero(brief_dir, capsys):
    assert main(["--session", "us", "--date", DATE], runner=FakeRunner([make_brief()])) == 0
    assert "written" in capsys.readouterr().out

    second = FakeRunner([])
    assert main(["--session", "us", "--date", DATE], runner=second) == 0
    assert second.calls == []
    assert "skipped" in capsys.readouterr().out


@pytest.mark.unit
def test_cli_force_recollects(brief_dir, capsys):
    assert main(["--session", "us", "--date", DATE], runner=FakeRunner([make_brief()])) == 0
    forced = FakeRunner([make_brief(generated_at="2026-08-08T13:00:00Z")])
    assert main(["--session", "us", "--date", DATE, "--force"], runner=forced) == 0
    assert len(forced.calls) == 1
    assert "written" in capsys.readouterr().out


@pytest.mark.unit
def test_cli_backend_flag_selects_generator(brief_dir):
    # Explicit --backend beats the fixture's TRADINGAGENTS_COLLECT_BACKEND=claude.
    runner = FakeRunner([make_brief(generator="codex-deep-search")])
    rc = main(["--session", "us", "--date", DATE, "--backend", "codex"], runner=runner)
    assert rc == 0
    backend, prompt = runner.calls[0]
    assert backend == "codex"
    assert "generator: codex-deep-search" in prompt


# ---------------------------------------------------------------------------
# D19 — default backend resolves from config (env-aware), explicit flag wins
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_default_backend_is_codex_when_env_unset(tmp_path, monkeypatch):
    directory = tmp_path / "briefs-codex-default"
    monkeypatch.setenv(macro_collector.MACRO_BRIEF_DIR_ENV, str(directory))
    monkeypatch.delenv(macro_collector.S3_URI_ENV, raising=False)
    monkeypatch.delenv("TRADINGAGENTS_COLLECT_BACKEND", raising=False)
    runner = FakeRunner([make_brief(generator="codex-deep-search")])
    result = collect("us", as_of=AS_OF, runner=runner)
    assert result.outcome == "written"
    backend, prompt = runner.calls[0]
    assert backend == "codex"  # D19 shipped default
    assert "generator: codex-deep-search" in prompt
    assert f"{DATE} | us | codex | 9 | written" in collector_log(directory)


@pytest.mark.unit
def test_cli_backend_default_defers_to_env_aware_config(brief_dir):
    args = macro_collector.build_parser().parse_args(["--session", "us"])
    assert args.backend is None  # resolution happens in collect(), env-aware
    # brief_dir pins TRADINGAGENTS_COLLECT_BACKEND=claude — the CLI default
    # resolves to it.
    runner = FakeRunner([make_brief()])
    assert main(["--session", "us", "--date", DATE], runner=runner) == 0
    assert runner.calls[0][0] == "claude"


@pytest.mark.unit
def test_cli_failure_exits_nonzero_with_one_line_stderr(brief_dir, capsys):
    bad = missing_section_brief()
    rc = main(["--session", "us", "--date", DATE], runner=FakeRunner([bad, bad]))
    assert rc == 1
    reasons = [
        line for line in capsys.readouterr().err.splitlines()
        if line.startswith("macro-collector:")
    ]
    assert len(reasons) == 1  # exactly one reason line (R6)
    assert "validation failed after retry" in reasons[0]
    assert not (brief_dir / f"{DATE}.us.md").exists()


@pytest.mark.unit
def test_cli_requires_session(capsys):
    with pytest.raises(SystemExit) as excinfo:
        main([])
    assert excinfo.value.code == 2


@pytest.mark.unit
@pytest.mark.parametrize(
    "argv",
    [
        ["--session", "eu"],
        ["--session", "us", "--date", "08/08/2026"],
        ["--session", "us", "--backend", "gemini"],
    ],
)
def test_cli_rejects_invalid_arguments(argv, capsys):
    with pytest.raises(SystemExit) as excinfo:
        main(argv)
    assert excinfo.value.code == 2


@pytest.mark.unit
def test_module_invocable_via_python_dash_m():
    repo_root = Path(__file__).resolve().parents[1]
    proc = subprocess.run(
        [sys.executable, "-m", "pipeline.macro_collector", "--help"],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=repo_root,
    )
    assert proc.returncode == 0
    assert "--session" in proc.stdout
    assert "--backend" in proc.stdout
