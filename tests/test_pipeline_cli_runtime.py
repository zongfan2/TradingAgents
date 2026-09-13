"""Regressions for the scheduled CLI/default-model compatibility incident.

Only subprocess boundaries are faked; no real model, auth, cache or runtime
state is accessed. The observable command and surfaced failure are the API.
"""

import errno
import json
import subprocess
from pathlib import Path

import pytest

from pipeline import evaluator, macro_collector, orchestrator, pool_builder, ticker_collector
from pipeline.cli_errors import cli_failure_reason
from pipeline.config import PipelineConfig, load_config

COLLECTORS = (macro_collector, pool_builder, ticker_collector)
CACHE_WARNING = (
    "2026-09-11T09:30:31Z ERROR codex_models_manager::manager: "
    "failed to load models cache: missing field supports_parallel_tool_calls"
)
FATAL = "The 'gpt-6-astra' model requires a newer version of Codex."
INCIDENT_FIXTURE_PATH = (
    Path(__file__).parent / "fixtures" / "codex_cli_2026_09_11_error.json"
)


def incident_error_json() -> str:
    fixture = json.loads(INCIDENT_FIXTURE_PATH.read_text(encoding="utf-8"))
    assert "not a raw stdout/stderr transcript" in fixture["provenance"]
    return json.dumps(fixture["error_object"])


@pytest.fixture
def cli(tmp_path):
    path = tmp_path / "Trading Tools" / "codex"
    path.parent.mkdir()
    path.write_text("#!/bin/sh\nexit 0\n")
    path.chmod(0o700)
    return str(path)


@pytest.mark.unit
def test_cli_override_survives_config_loading(cli):
    assert load_config({"TRADINGAGENTS_CODEX_CLI": cli}).codex_cli == cli
    assert load_config({}).codex_cli == "codex"


@pytest.mark.unit
@pytest.mark.parametrize("wrapper", ['"{cli}"', "'{cli}'"])
def test_load_config_unwraps_only_the_cli_selector(wrapper, cli):
    config = load_config(
        {
            "TRADINGAGENTS_CODEX_CLI": wrapper.format(cli=cli),
            "TRADINGAGENTS_STATE_DIR": '"/fixture/quoted-state"',
        }
    )

    assert config.codex_cli == cli
    assert str(config.state_dir) == '"/fixture/quoted-state"'


@pytest.mark.unit
@pytest.mark.parametrize("wrapper", ["  {cli}\t", '  "{cli}"  ', "\t'{cli}' "])
@pytest.mark.parametrize(
    ("runner", "expected_tail", "expected_output"),
    [
        (
            lambda prompt: macro_collector.default_runner("codex", prompt),
            ["exec", "-c", "tools.web_search=true", "--skip-git-repo-check", "-"],
            "BRIEF",
        ),
        (
            evaluator.codex_runner,
            [
                "exec",
                "--model",
                "gpt-5.6-terra",
                "-c",
                "tools.web_search=true",
                "--skip-git-repo-check",
                "-",
            ],
            "BRIEF",
        ),
    ],
)
def test_env_selector_normalizes_one_outer_quote_pair_and_padding_for_exact_argv(
    monkeypatch, cli, wrapper, runner, expected_tail, expected_output
):
    monkeypatch.setenv("TRADINGAGENTS_CODEX_CLI", wrapper.format(cli=cli))
    commands = []

    def fake_run(command, **kwargs):
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, stdout=expected_output, stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    assert runner("PRIVATE PROMPT") == expected_output
    assert commands == [[cli, *expected_tail]]


@pytest.mark.unit
@pytest.mark.parametrize(
    "malformed",
    ['"/absolute/path/codex', "/absolute/path/codex'", '"/absolute/path/codex\''],
)
def test_env_selector_rejects_malformed_outer_quote_wrappers(malformed):
    with pytest.raises(ValueError, match="codex_cli"):
        load_config({"TRADINGAGENTS_CODEX_CLI": malformed})


@pytest.mark.unit
@pytest.mark.parametrize("blank", ["", " ", "\t "])
def test_blank_cli_environment_uses_default_command(monkeypatch, blank):
    monkeypatch.setenv("TRADINGAGENTS_CODEX_CLI", blank)
    commands = []

    def fake_run(command, **kwargs):
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="BRIEF", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert macro_collector.default_runner("codex", "PROMPT") == "BRIEF"
    assert commands[0][0] == "codex"


@pytest.mark.unit
@pytest.mark.parametrize(
    "bad",
    [
        "./codex",
        "codex --model x",
        "~/bin/codex",
        "/bin/co\ndex",
        " ",
        3,
        '"/absolute/path/codex"',
        "'/absolute/path/codex'",
    ],
)
def test_cli_configuration_rejects_ambiguous_or_unsafe_command(bad):
    with pytest.raises(ValueError, match="codex_cli"):
        PipelineConfig(codex_cli=bad)


@pytest.mark.unit
@pytest.mark.parametrize("module", COLLECTORS)
def test_collector_uses_exact_configured_cli_and_preserves_stdin_model_policy(monkeypatch, module, cli):
    monkeypatch.setenv("TRADINGAGENTS_CODEX_CLI", cli)
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, stdout="BRIEF", stderr=CACHE_WARNING)

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert module.default_runner("codex", "PRIVATE PROMPT") == "BRIEF"
    assert len(calls) == 1
    command, options = calls[0]
    assert command == [cli, "exec", "-c", "tools.web_search=true", "--skip-git-repo-check", "-"]
    assert "PRIVATE PROMPT" not in command
    assert options["input"] == "PRIVATE PROMPT"
    assert options.get("shell", False) is False


@pytest.mark.unit
def test_evaluator_uses_configured_cli_without_changing_pinned_model(monkeypatch, cli):
    monkeypatch.setenv("TRADINGAGENTS_CODEX_CLI", cli)
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, stdout="JUDGMENT", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert evaluator.codex_runner("PRIVATE PROMPT") == "JUDGMENT"
    command, options = calls[0]
    assert command == [cli, "exec", "--model", "gpt-5.6-terra", "-c",
                       "tools.web_search=true", "--skip-git-repo-check", "-"]
    assert options["input"] == "PRIVATE PROMPT"


@pytest.mark.unit
@pytest.mark.parametrize("module", COLLECTORS)
@pytest.mark.parametrize("terminal_stream", ["stdout", "stderr"])
def test_collectors_surface_persisted_error_envelope_from_synthetic_stream(
    monkeypatch, module, terminal_stream
):
    # Stream placement is synthetic: only the task-complete error object was
    # retained. The fixture records that provenance instead of claiming an
    # exact stdout/stderr rendering that no retained artifact establishes.
    terminal = incident_error_json()
    streams = {"stderr": CACHE_WARNING + "\n", "stdout": ""}
    streams[terminal_stream] += terminal + "\n"
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 1, **streams)

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(RuntimeError, match="requires a newer version") as exc:
        module.default_runner("codex", "PROMPT")
    assert "models cache" not in str(exc.value)
    assert "\n" not in str(exc.value)
    assert len(calls) == 1  # a backend failure must not spend a retry


@pytest.mark.unit
def test_codex_evaluator_surfaces_persisted_error_envelope_from_synthetic_stdout(
    monkeypatch,
):
    def fake_run(command, **kwargs):
        return subprocess.CompletedProcess(
            command, 1, stderr=CACHE_WARNING, stdout=incident_error_json() + "\n"
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(evaluator.BackendError, match="requires a newer version"):
        evaluator.codex_runner("PROMPT")


@pytest.mark.unit
@pytest.mark.parametrize("module", COLLECTORS)
def test_codex_override_does_not_change_claude_command(monkeypatch, module, cli):
    monkeypatch.setenv("TRADINGAGENTS_CODEX_CLI", cli)
    commands = []

    def fake_run(command, **kwargs):
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="BRIEF", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert module.default_runner("claude", "PROMPT") == "BRIEF"
    assert commands == [["claude", "-p", "--allowedTools", "WebSearch,WebFetch"]]


@pytest.mark.unit
@pytest.mark.parametrize("kind", ["missing", "directory", "not-executable"])
@pytest.mark.parametrize("module", COLLECTORS)
def test_bad_cli_path_fails_before_any_subprocess(tmp_path, monkeypatch, kind, module):
    path = tmp_path / "bad-codex"
    if kind == "directory":
        path.mkdir()
    elif kind == "not-executable":
        path.write_text("not an executable")
        path.chmod(0o600)
    monkeypatch.setenv("TRADINGAGENTS_CODEX_CLI", str(path))

    def forbidden(*args, **kwargs):
        pytest.fail("invalid CLI path reached subprocess.run")

    monkeypatch.setattr(subprocess, "run", forbidden)
    with pytest.raises(RuntimeError, match="configured codex CLI"):
        module.default_runner("codex", "PROMPT")


@pytest.mark.unit
def test_missing_explicit_codex_does_not_launch_evaluator(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADINGAGENTS_CODEX_CLI", str(tmp_path / "missing"))
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: pytest.fail("launched missing CLI"))
    with pytest.raises(evaluator.BackendError, match="configured codex CLI"):
        evaluator.codex_runner("PROMPT")


@pytest.mark.unit
@pytest.mark.parametrize("module", COLLECTORS)
@pytest.mark.parametrize("backend", ["codex", "claude"])
@pytest.mark.parametrize("error_number", [errno.EACCES, errno.ENOEXEC])
def test_collector_os_launch_failure_is_controlled_without_retry(
    monkeypatch, cli, module, backend, error_number
):
    monkeypatch.setenv("TRADINGAGENTS_CODEX_CLI", cli)
    commands = []

    def fail_launch(command, **kwargs):
        commands.append(command)
        raise OSError(error_number, "private OS detail", "/private/executable")

    monkeypatch.setattr(subprocess, "run", fail_launch)
    with pytest.raises(RuntimeError, match="could not be launched") as exc:
        module.default_runner(backend, "PROMPT")
    assert "private" not in str(exc.value)
    assert len(commands) == 1
    assert commands[0][0] == (cli if backend == "codex" else "claude")


@pytest.mark.unit
@pytest.mark.parametrize("backend", ["codex", "claude"])
@pytest.mark.parametrize("error_number", [errno.EACCES, errno.ENOEXEC])
def test_evaluator_os_launch_failure_is_backend_error_without_retry(
    monkeypatch, cli, backend, error_number
):
    monkeypatch.setenv("TRADINGAGENTS_CODEX_CLI", cli)
    commands = []

    def fail_launch(command, **kwargs):
        commands.append(command)
        raise OSError(error_number, "private OS detail", "/private/executable")

    monkeypatch.setattr(subprocess, "run", fail_launch)
    runner = evaluator.codex_runner if backend == "codex" else evaluator.claude_runner
    with pytest.raises(evaluator.BackendError, match="could not be launched") as exc:
        runner("PROMPT")
    assert "private" not in str(exc.value)
    assert len(commands) == 1
    assert commands[0][0] == (cli if backend == "codex" else "claude")


@pytest.mark.unit
@pytest.mark.parametrize("envelope_error", [False, True])
def test_claude_evaluator_failure_is_bounded_one_line_and_redacted(monkeypatch, envelope_error):
    diagnostic = "\x1b[31mERROR: rejected api_key=private-value\n" + "x" * 900
    if envelope_error:
        proc = subprocess.CompletedProcess(
            ["claude"], 0, stderr="",
            stdout=json.dumps({"type": "result", "is_error": True, "result": diagnostic}),
        )
    else:
        proc = subprocess.CompletedProcess(["claude"], 1, stderr=diagnostic, stdout="")
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: proc)
    with pytest.raises(evaluator.BackendError) as exc:
        evaluator.claude_runner("PROMPT")
    text = str(exc.value)
    assert "private-value" not in text
    assert "rejected" in text
    assert all(char.isprintable() for char in text)
    assert len(text) <= 550  # <=500 extracted detail plus fixed backend context


@pytest.mark.unit
def test_orchestrator_exports_resolved_cli_over_ambient_environment(monkeypatch, cli):
    monkeypatch.setenv("TRADINGAGENTS_CODEX_CLI", "/wrong/ambient/codex")
    config = PipelineConfig(codex_cli=cli)
    child_env = orchestrator.component_env(config)
    assert child_env["TRADINGAGENTS_CODEX_CLI"] == cli
    assert load_config(child_env).codex_cli == cli


@pytest.mark.unit
def test_executable_symlink_is_kept_as_the_configured_command(tmp_path, cli):
    link = tmp_path / "codex-link"
    link.symlink_to(cli)
    assert PipelineConfig(codex_cli=str(link)).codex_cli == str(link)


@pytest.mark.unit
def test_synthetic_terminal_prefix_survives_ansi_and_control_characters():
    assert cli_failure_reason(CACHE_WARNING, "\x1b[31m\x00ERROR: " + FATAL + "\x1b[0m") == "ERROR: " + FATAL


@pytest.mark.unit
@pytest.mark.parametrize(
    ("terminal", "expected"),
    [
        ("2026-09-11T09:30:32Z ERROR codex_core::client: " + FATAL, "ERROR codex_core::client: " + FATAL),
        ("2026-09-11T09:30:32.123Z FATAL codex_core::client: " + FATAL, "FATAL codex_core::client: " + FATAL),
        ("2026-09-11T09:30:32.123456+05:30 ERROR codex_core::client: " + FATAL, "ERROR codex_core::client: " + FATAL),
    ],
)
def test_timestamped_terminal_diagnostic_beats_cache_warning_in_each_stream(terminal, expected):
    assert cli_failure_reason(CACHE_WARNING + "\n" + terminal + "\nINFO trailing mention ERROR", "") == expected
    assert cli_failure_reason("", CACHE_WARNING + "\n" + terminal + "\nINFO trailing mention ERROR") == expected


@pytest.mark.unit
def test_timestamped_terminal_diagnostic_prefers_non_cache_across_streams_and_keeps_cache_only():
    terminal = "2026-09-11T09:30:32Z ERROR codex_core::client: " + FATAL
    assert cli_failure_reason(CACHE_WARNING, terminal) == "ERROR codex_core::client: " + FATAL
    assert cli_failure_reason(CACHE_WARNING, "") == (
        "ERROR codex_models_manager::manager: failed to load models cache: "
        "missing field supports_parallel_tool_calls"
    )
    assert cli_failure_reason("INFO body mentions ERROR but is not terminal", "") == "INFO body mentions ERROR but is not terminal"
    terminal = "2026-09-11T09:30:32Z ERROR codex_core::client: terminal"
    assert cli_failure_reason(terminal, CACHE_WARNING) == "ERROR codex_core::client: terminal"
    assert cli_failure_reason(terminal, "") == "ERROR codex_core::client: terminal"


@pytest.mark.unit
def test_retained_startup_status_diagnostics_are_exact_cache_candidates_and_demoted():
    fixture = json.loads(INCIDENT_FIXTURE_PATH.read_text(encoding="utf-8"))
    evidence = fixture["startup_status_evidence"]
    assert [
        (
            item["source_basename"],
            item["json_pointer"],
            item["slot"]["session"],
            item["slot"]["date"],
            item["startup_diagnostic"],
        )
        for item in evidence
    ] == [
        (
            "pipeline_status.cn.json",
            "/components/macro_collector/error",
            "cn",
            "2026-09-11",
            "2026-09-11T00:30:26.749363Z ERROR "
            "codex_models_manager::manager: failed to load models cache: "
            "missing field `supports_parallel_tool_calls` at line 132 column 5",
        ),
        (
            "pipeline_status.us.json",
            "/components/macro_collector/error",
            "us",
            "2026-09-11",
            "2026-09-11T09:30:31.186247Z ERROR "
            "codex_models_manager::manager: failed to load models cache: "
            "missing field `supports_parallel_tool_calls` at line 132 column 5",
        ),
    ]
    terminal = "2026-09-11T09:30:32Z ERROR codex_core::client: " + FATAL
    for item in evidence:
        assert item["status_error"].endswith(item["startup_diagnostic"])
        assert cli_failure_reason(item["startup_diagnostic"], terminal) == (
            "ERROR codex_core::client: " + FATAL
        )


@pytest.mark.unit
def test_timestamped_candidates_keep_stream_and_newest_precedence_without_cross_stream_ordering():
    old = "2026-09-11T09:30:32Z ERROR codex_core::old: old terminal"
    new = "2026-09-11T09:30:33Z FATAL codex_core::new: newest terminal"
    assert cli_failure_reason(old + "\n" + new, "") == "FATAL codex_core::new: newest terminal"
    assert cli_failure_reason(old, new) == "ERROR codex_core::old: old terminal"
    assert cli_failure_reason(
        "2026-09-11T09:30:32Z INFO codex_core::info: body says ERROR",
        new,
    ) == "FATAL codex_core::new: newest terminal"
    warn_decoy = "2026-09-11T09:30:32Z WARN codex_core::warn: body says FATAL"
    assert cli_failure_reason(
        warn_decoy,
        new,
    ) == "FATAL codex_core::new: newest terminal"
    assert cli_failure_reason(warn_decoy, "answer") == warn_decoy


@pytest.mark.unit
def test_timestamped_tail_bound_and_bare_prefix_priority():
    outside = "2026-09-11T09:30:32Z ERROR codex_core::old: outside"
    inside = "2026-09-11T09:30:33Z ERROR codex_core::new: inside"
    assert cli_failure_reason(outside + "\n" + "x" * 32768, inside) == "ERROR codex_core::new: inside"
    assert cli_failure_reason("ERROR: bare wins", inside) == "ERROR: bare wins"


@pytest.mark.unit
def test_collector_surfaces_timestamped_terminal_with_one_backend_call(monkeypatch):
    calls = []
    terminal = "2026-09-11T09:30:32+05:30 ERROR codex_core::client: " + FATAL

    def fake_run(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 1, stdout=terminal, stderr=CACHE_WARNING)

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(RuntimeError, match="requires a newer version"):
        macro_collector.default_runner("codex", "PROMPT")
    assert len(calls) == 1


@pytest.mark.unit
def test_timestamped_terminal_diagnostic_preserves_json_priority_redaction_and_bound():
    payload = {"type": "error", "message": "json wins"}
    terminal = (
        "2026-09-11T09:30:32Z ERROR codex_core::client: rejected "
        "api_key=private-value https://example.test/private " + "x" * 900
    )
    assert cli_failure_reason(terminal, json.dumps(payload)) == "json wins"
    reason = cli_failure_reason(terminal, "")
    assert "private-value" not in reason
    assert "[url redacted]" in reason
    assert len(reason) == 500


@pytest.mark.unit
def test_untyped_model_json_does_not_override_explicit_terminal_error():
    stdout = json.dumps({"answer": "private answer", "error": {"message": "model content"}})
    assert cli_failure_reason("ERROR: permission denied", stdout) == "ERROR: permission denied"


@pytest.mark.unit
def test_reason_is_one_bounded_line_with_no_terminal_controls():
    payload = {"type": "error", "message": "bad\n\r\x1b[31mrequest " + "x" * 900}
    reason = cli_failure_reason("", json.dumps(payload))
    assert reason.startswith("bad request ")
    assert len(reason) == 500
    assert all(c.isprintable() for c in reason)


@pytest.mark.unit
def test_unknown_plain_error_keeps_existing_fallback_and_empty_is_explicit():
    assert cli_failure_reason("not logged in\nmore detail", "answer") == "not logged in"
    assert cli_failure_reason(None, None) == "no output"


@pytest.mark.unit
@pytest.mark.parametrize(
    ("stderr", "stdout"),
    [
        ("not logged in\n" + "filler line\n" * 5000, "stdout answer"),
        ("", "not logged in\n" + "filler line\n" * 5000),
    ],
    ids=["stderr", "stdout"],
)
def test_plain_fallback_reads_true_stream_start_beyond_recognized_tail(stderr, stdout):
    assert cli_failure_reason(stderr, stdout) == "not logged in"


@pytest.mark.unit
def test_plain_fallback_finds_first_meaningful_line_after_long_leading_whitespace():
    stderr = (" \r\n" * 12_000) + "  first useful diagnostic  \r\nlater"
    assert cli_failure_reason(stderr, "stdout answer") == "first useful diagnostic"


@pytest.mark.unit
@pytest.mark.parametrize("separator", ["\r\n", "\v", "\x1c", "\x85", "\u2028"])
def test_plain_fallback_skips_cleaned_empty_lines_across_splitlines_boundaries(separator):
    stderr = f" \t{separator}\x1b[31m\x00\x1b[0m{separator}  useful diagnostic  {separator}later"
    assert cli_failure_reason(stderr, "stdout answer") == "useful diagnostic"


@pytest.mark.unit
def test_plain_fallback_redacts_complete_long_line_before_output_bound():
    secret = "private-value-" + "x" * 32_768
    stderr = f'  rejected api_key="{secret}" final reason ' + "y" * 900 + "\nlater"

    reason = cli_failure_reason(stderr, "")

    assert reason.startswith("rejected api_key=[redacted] final reason ")
    assert secret[:32] not in reason
    assert len(reason) == 500


@pytest.mark.unit
def test_collector_long_plain_failure_surfaces_initial_diagnostic_once(monkeypatch):
    calls = []
    stderr = "not logged in\n" + "filler line\n" * 5000

    def fake_run(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 1, stdout="", stderr=stderr)

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(RuntimeError, match="not logged in"):
        macro_collector.default_runner("claude", "PROMPT")
    assert len(calls) == 1


@pytest.mark.unit
def test_terminal_error_redacts_common_credentials_and_url_secrets():
    secret_error = (
        "ERROR: Incorrect API key sk-private-test; Authorization: Bearer abc.def.ghi "
        "api_key=private-value https://example.test/hook/private-token"
    )
    reason = cli_failure_reason(CACHE_WARNING, secret_error)
    for secret in ("sk-private-test", "abc.def.ghi", "private-value", "private-token"):
        assert secret not in reason
    assert "Incorrect API key" in reason


@pytest.mark.unit
@pytest.mark.parametrize("assignment", [
    "OPENAI_API_KEY=private-value",
    "AWS_SECRET_ACCESS_KEY=private-value",
    "ALPACA_SECRET_KEY=private-value",
    "client_secret=private-value",
    "password=private-value",
    '"access_token": "private-value with spaces"',
    "LARK_APP_SECRET='private-value with spaces'",
])
def test_credential_assignments_in_terminal_diagnostics_are_redacted(assignment):
    reason = cli_failure_reason("ERROR: rejected " + assignment, "")
    assert "private-value" not in reason
    assert "with spaces" not in reason
    assert "rejected" in reason


@pytest.mark.unit
@pytest.mark.parametrize("header", [
    "Authorization: Basic private-value",
    "Proxy-Authorization: Digest username=user, response=private-value",
    "Cookie: session=private-value; other=also-private",
    "Set-Cookie: session=private-value; HttpOnly",
])
def test_auth_and_cookie_headers_redact_the_complete_remaining_value(header):
    reason = cli_failure_reason("ERROR: rejected " + header, "")
    assert "private-value" not in reason
    assert "also-private" not in reason
    assert "rejected" in reason


@pytest.mark.unit
@pytest.mark.parametrize("module", COLLECTORS)
def test_actual_subprocess_uses_isolated_configured_executable(tmp_path, monkeypatch, module):
    # A local fake CLI exercises exec with a spaced path and stdin. It has no
    # model transport and neither reads config/credentials nor touches runtime.
    import sys
    executable = tmp_path / "Space In Path" / "codex"
    executable.parent.mkdir()
    executable.write_text(
        f"#!{sys.executable}\n"
        "import json, sys\n"
        "print(json.dumps({'argv': sys.argv[1:], 'stdin': sys.stdin.read()}))\n"
    )
    executable.chmod(0o700)
    monkeypatch.setenv("TRADINGAGENTS_CODEX_CLI", str(executable))
    result = json.loads(module.default_runner("codex", "FIXTURE INPUT"))
    assert result == {
        "argv": ["exec", "-c", "tools.web_search=true", "--skip-git-repo-check", "-"],
        "stdin": "FIXTURE INPUT",
    }
