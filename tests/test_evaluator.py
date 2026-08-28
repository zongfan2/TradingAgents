"""Brief evaluator (specs/macro-brief-evaluator.md) — offline, faked backend.

Covers the spec's acceptance criteria 1-4 plus: the ticker consistency
dimension's catalyst_score-inflation instruction, exit-code semantics
(0 completed / 3 backend failure / 4 structural refusal — 2 stays argparse's
usage error), hash-scoped idempotency with archive-on-replace, identity
fields never model-reported, the model-JSON retry path, the codex subprocess
boundary, the macro eval-json S3 mirror, and the R6 log line.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from pipeline import evaluator as evaluator_module
from pipeline.common import sha256_text
from pipeline.contracts.briefs import (
    MACRO_IMPACT_SECTIONS,
    MACRO_SECTIONS,
    TICKER_SECTIONS,
)
from pipeline.contracts.evals import SCORE_DIMENSIONS, MacroEvalReport, TickerEvalReport
from pipeline.evaluator import (
    CLAUDE_EVALUATOR,
    EVALUATOR_MODEL,
    EXIT_BACKEND_FAILURE,
    EXIT_ERROR,
    EXIT_OK,
    EXIT_REFUSED,
    BackendError,
    batch_tickers,
    build_eval_prompt,
    claude_runner,
    codex_runner,
    detect_kind,
    evaluate_brief,
    main,
)

REPO_ROOT = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------


def _fm(fields, omit=()):
    lines = [f"{key}: {value}" for key, value in fields.items() if key not in omit]
    return "---\n" + "\n".join(lines) + "\n---\n\n"


def _sections(titles, impact_titles, citations):
    blocks = []
    for i, title in enumerate(titles):
        content = ("verifiable claims with sources " * 5).strip()
        if i == 0:
            content += " " + citations
        block = f"## {title}\n\n{content}\n"
        if title in impact_titles:
            block += "\n**Impact**: neutral — no marginal change.\n"
        blocks.append(block)
    return "\n".join(blocks)


def macro_text(*, date_str="2026-08-03", session="us", omit=(), extra=""):
    fields = {
        "as_of_date": date_str,
        "session": session,
        "generated_at": "2026-08-03T12:35:00Z",
        "generator": "claude-deep-search",
        "sources_count": 2,
    }
    citations = " ".join(f"[S](https://example.com/m{i})" for i in range(2))
    return _fm(fields, omit) + _sections(MACRO_SECTIONS, MACRO_IMPACT_SECTIONS, citations) + extra


def ticker_text(*, date_str="2026-08-03", ticker="NVDA", session="us", catalyst_score=7.5):
    fields = {
        "as_of_date": date_str,
        "ticker": ticker,
        "session": session,
        "generated_at": "2026-08-03T12:40:00Z",
        "generator": "claude-deep-search",
        "sources_count": 5,
        "catalyst_score": catalyst_score,
        "catalyst_type": "earnings",
        "catalyst_window": "2026-08-27",
    }
    citations = " ".join(f"[S](https://example.com/t{i})" for i in range(5))
    body = _sections(TICKER_SECTIONS, (), citations)
    return _fm(fields) + body + "\n**Impact**: bullish — dated catalyst inside two weeks.\n"


def flag(severity, section="Risks", claim="planted claim", issue="not supported by source"):
    return {"section": section, "claim": claim, "issue": issue, "severity": severity}


def judgment(scores=None, flags=(), notes="ok", **extra):
    payload = {
        "scores": dict.fromkeys(SCORE_DIMENSIONS, 8.0) | (scores or {}),
        "flagged_claims": list(flags),
        "notes": notes,
    }
    payload.update(extra)
    return json.dumps(payload)


class FakeRunner:
    """Injectable backend fake: returns queued responses, records prompts."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.prompts = []

    def __call__(self, prompt):
        self.prompts.append(prompt)
        if not self.responses:
            raise AssertionError("backend called more times than the test queued responses for")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    @property
    def calls(self):
        return len(self.prompts)


@pytest.fixture()
def macro_dir(tmp_path, monkeypatch):
    directory = tmp_path / "macro_briefs"
    directory.mkdir()
    monkeypatch.setenv("TRADINGAGENTS_MACRO_BRIEF_DIR", str(directory))
    monkeypatch.delenv("MACRO_BRIEF_S3_URI", raising=False)
    return directory


def write_brief(directory, name, text):
    path = directory / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def read_eval(brief_path):
    eval_path = brief_path.parent / f"{brief_path.stem}.eval.json"
    return json.loads(eval_path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# AC1 — valid macro brief: schema-valid eval, hash-scoped idempotency
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_macro_eval_written_schema_valid_with_identity_from_brief(macro_dir):
    text = macro_text()
    brief = write_brief(macro_dir, "2026-08-03.us.md", text)
    runner = FakeRunner(judgment())

    assert evaluate_brief(brief, runner=runner) == EXIT_OK

    data = read_eval(brief)
    report = MacroEvalReport.model_validate(data)  # schema-valid per the contract model
    assert report.session == "us"
    assert report.as_of_date.isoformat() == "2026-08-03"
    assert report.brief_sha256 == sha256_text(text)
    assert data["brief_generated_at"] == "2026-08-03T12:35:00Z"
    # D19: the default eval backend is claude — the harness stamps its identity.
    assert report.evaluator == CLAUDE_EVALUATOR
    assert report.verdict == "pass"


@pytest.mark.unit
def test_second_run_same_revision_skips_without_respending_tokens(macro_dir):
    brief = write_brief(macro_dir, "2026-08-03.us.md", macro_text())
    runner = FakeRunner(judgment())
    assert evaluate_brief(brief, runner=runner) == EXIT_OK
    assert evaluate_brief(brief, runner=runner) == EXIT_OK
    assert runner.calls == 1  # second run never touched the backend


@pytest.mark.unit
def test_recollected_brief_reevaluates_and_archives_stale_eval(macro_dir):
    brief = write_brief(macro_dir, "2026-08-03.us.md", macro_text())
    runner = FakeRunner(judgment(), judgment(notes="second evaluation"))
    assert evaluate_brief(brief, runner=runner) == EXIT_OK
    stale_evaluated_at = read_eval(brief)["evaluated_at"]

    new_text = macro_text(extra="\nre-collected revision.\n")
    write_brief(macro_dir, "2026-08-03.us.md", new_text)
    assert evaluate_brief(brief, runner=runner) == EXIT_OK  # no --force needed

    assert runner.calls == 2
    data = read_eval(brief)
    assert data["brief_sha256"] == sha256_text(new_text)
    assert data["notes"] == "second evaluation"
    archived = list((macro_dir / "archive").glob("2026-08-03.us.eval.*.json"))
    assert len(archived) == 1
    assert stale_evaluated_at in archived[0].name


@pytest.mark.unit
def test_force_reevaluates_even_on_hash_match(macro_dir):
    brief = write_brief(macro_dir, "2026-08-03.us.md", macro_text())
    runner = FakeRunner(judgment(), judgment())
    assert evaluate_brief(brief, runner=runner) == EXIT_OK
    assert evaluate_brief(brief, force=True, runner=runner) == EXIT_OK
    assert runner.calls == 2
    assert len(list((macro_dir / "archive").glob("*.json"))) == 1


# ---------------------------------------------------------------------------
# AC2 — ticker brief: kind detection + ticker contract applied
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_ticker_kind_detected_and_ticker_eval_written(tmp_path, macro_dir):
    text = ticker_text()
    brief = write_brief(tmp_path / "ticker_briefs" / "NVDA", "2026-08-03.md", text)
    runner = FakeRunner(judgment())

    assert detect_kind(text) == "ticker"
    assert evaluate_brief(brief, runner=runner) == EXIT_OK

    report = TickerEvalReport.model_validate(read_eval(brief))
    assert report.ticker == "NVDA"
    assert report.session == "us"
    assert report.brief_sha256 == sha256_text(text)
    assert (brief.parent / "2026-08-03.eval.json").exists()  # next to the brief


@pytest.mark.unit
def test_ticker_broken_section_refused_with_ticker_contract(tmp_path):
    text = ticker_text().replace("## Risks", "## Hazards")
    brief = write_brief(tmp_path / "ticker_briefs" / "NVDA", "2026-08-03.md", text)
    runner = FakeRunner()
    assert evaluate_brief(brief, runner=runner) == EXIT_REFUSED
    assert runner.calls == 0


@pytest.mark.unit
def test_ticker_consistency_prompt_includes_catalyst_inflation_instruction(tmp_path, macro_dir):
    ticker_brief = write_brief(
        tmp_path / "ticker_briefs" / "NVDA", "2026-08-03.md", ticker_text(catalyst_score=8.5)
    )
    runner = FakeRunner(judgment())
    assert evaluate_brief(ticker_brief, runner=runner) == EXIT_OK
    prompt = runner.prompts[0]
    assert "catalyst_score=8.5" in prompt
    assert "catalyst_score >= 7 with no dated catalyst inside ~2 weeks" in prompt
    assert "'major'" in prompt

    macro_brief = write_brief(macro_dir, "2026-08-03.us.md", macro_text())
    macro_runner = FakeRunner(judgment())
    assert evaluate_brief(macro_brief, runner=macro_runner) == EXIT_OK
    assert "catalyst_score" not in macro_runner.prompts[0]


@pytest.mark.unit
def test_prompt_embeds_brief_and_requests_strict_json(macro_dir):
    text = macro_text()
    brief = write_brief(macro_dir, "2026-08-03.us.md", text)
    runner = FakeRunner(judgment())
    evaluate_brief(brief, runner=runner)
    prompt = runner.prompts[0]
    assert text in prompt  # brief embedded verbatim
    for dimension in SCORE_DIMENSIONS:
        assert dimension in prompt
    assert "STRICT JSON" in prompt
    assert "Do NOT include a verdict" in prompt


# ---------------------------------------------------------------------------
# AC3 — planted false claim ⇒ flag + non-pass verdict, still exit 0 (R5)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_planted_false_claim_fails_verdict_but_exits_zero(macro_dir, capsys):
    brief = write_brief(macro_dir, "2026-08-03.us.md", macro_text())
    fabricated = flag("fabrication", section="China & Asia", claim="GDP grew 19%")
    runner = FakeRunner(judgment(scores={"factual_accuracy": 3.0}, flags=[fabricated]))

    assert evaluate_brief(brief, runner=runner) == EXIT_OK  # R5: component worked

    assert read_eval(brief)["verdict"] == "fail"
    err = capsys.readouterr().err
    assert "verdict fail" in err
    assert "GDP grew 19%" in err  # flagged claims printed to stderr
    assert "[fabrication]" in err


@pytest.mark.unit
def test_major_flag_yields_warn_verdict_no_stderr_claim_dump(macro_dir, capsys):
    brief = write_brief(macro_dir, "2026-08-03.us.md", macro_text())
    runner = FakeRunner(judgment(flags=[flag("major")]))
    assert evaluate_brief(brief, runner=runner) == EXIT_OK
    assert read_eval(brief)["verdict"] == "warn"
    assert "verdict fail" not in capsys.readouterr().err


@pytest.mark.unit
def test_model_claimed_verdict_and_identity_fields_are_ignored(macro_dir):
    text = macro_text()
    brief = write_brief(macro_dir, "2026-08-03.us.md", text)
    runner = FakeRunner(
        judgment(
            flags=[flag("fabrication")],
            verdict="pass",  # model lies — recomputed via compute_verdict
            brief_sha256="deadbeef",  # never model-reported
            evaluator="gpt-2",
        )
    )
    assert evaluate_brief(brief, runner=runner) == EXIT_OK
    data = read_eval(brief)
    assert data["verdict"] == "fail"
    assert data["brief_sha256"] == sha256_text(text)
    # Harness-stamped identity (default backend claude per D19) — the model's
    # self-claimed "gpt-2" is discarded.
    assert data["evaluator"] == CLAUDE_EVALUATOR


# ---------------------------------------------------------------------------
# AC4 — structural refusal (distinct exit code), backend failure distinct
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_structurally_broken_macro_brief_refused_distinctly(macro_dir, capsys):
    text = macro_text().replace("## China & Asia", "## China")
    brief = write_brief(macro_dir, "2026-08-03.us.md", text)
    runner = FakeRunner()

    code = evaluate_brief(brief, runner=runner)

    assert code == EXIT_REFUSED
    assert code not in (EXIT_OK, EXIT_ERROR, EXIT_BACKEND_FAILURE)
    assert runner.calls == 0  # refused before any tokens were spent
    assert not (macro_dir / "2026-08-03.us.eval.json").exists()
    assert "missing section '## China & Asia'" in capsys.readouterr().err


@pytest.mark.unit
def test_filename_session_mismatch_refused(macro_dir):
    brief = write_brief(macro_dir, "2026-08-03.cn.md", macro_text(session="us"))
    assert evaluate_brief(brief, runner=FakeRunner()) == EXIT_REFUSED


@pytest.mark.unit
def test_backend_failure_exits_3(macro_dir):
    brief = write_brief(macro_dir, "2026-08-03.us.md", macro_text())
    runner = FakeRunner(BackendError("codex exec failed (exit 1): boom"))
    assert evaluate_brief(brief, runner=runner) == EXIT_BACKEND_FAILURE
    assert not (macro_dir / "2026-08-03.us.eval.json").exists()


@pytest.mark.unit
def test_missing_brief_file_exits_1(tmp_path):
    assert evaluate_brief(tmp_path / "nope.md", runner=FakeRunner()) == EXIT_ERROR


# ---------------------------------------------------------------------------
# Model-JSON validation retry (once, with the errors appended)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_invalid_model_json_retries_once_with_errors_appended(macro_dir):
    brief = write_brief(macro_dir, "2026-08-03.us.md", macro_text())
    runner = FakeRunner("sorry, I ate the JSON", judgment())
    assert evaluate_brief(brief, runner=runner) == EXIT_OK
    assert runner.calls == 2
    retry_prompt = runner.prompts[1]
    assert "rejected by schema validation" in retry_prompt
    assert "no JSON object found in model output" in retry_prompt


@pytest.mark.unit
def test_model_json_invalid_twice_exits_3(macro_dir, capsys):
    brief = write_brief(macro_dir, "2026-08-03.us.md", macro_text())
    bad_scores = judgment(scores={"factual_accuracy": 99.0})  # out of 0-10 range
    runner = FakeRunner("not json", bad_scores)
    assert evaluate_brief(brief, runner=runner) == EXIT_BACKEND_FAILURE
    assert runner.calls == 2
    assert "failed schema validation twice" in capsys.readouterr().err
    assert not (macro_dir / "2026-08-03.us.eval.json").exists()


@pytest.mark.unit
def test_fenced_json_output_accepted_without_retry(macro_dir):
    brief = write_brief(macro_dir, "2026-08-03.us.md", macro_text())
    runner = FakeRunner(f"Here you go:\n```json\n{judgment()}\n```\n")
    assert evaluate_brief(brief, runner=runner) == EXIT_OK
    assert runner.calls == 1


# ---------------------------------------------------------------------------
# codex_runner — the production subprocess boundary (subprocess.run faked)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_codex_runner_command_construction_and_stdin(monkeypatch):
    monkeypatch.delenv("TRADINGAGENTS_TIMEOUT_MACRO_EVALUATOR", raising=False)
    seen = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        seen["input"] = kwargs.get("input")
        seen["timeout"] = kwargs.get("timeout")
        return subprocess.CompletedProcess(cmd, 0, stdout="MODEL OUTPUT", stderr="")

    monkeypatch.setattr(evaluator_module.subprocess, "run", fake_run)
    assert codex_runner("EVAL PROMPT") == "MODEL OUTPUT"
    # Spec R2: gpt-5.6-terra via codex exec with web search, fresh process.
    assert seen["cmd"] == [
        "codex", "exec", "--model", "gpt-5.6-terra", "-c", "tools.web_search=true", "--skip-git-repo-check", "-",
    ]
    assert seen["input"] == "EVAL PROMPT"  # prompt over stdin, never argv
    assert seen["timeout"] == 540.0  # budget-derived: (1200 - 120) / 2 attempts


@pytest.mark.unit
def test_codex_runner_missing_cli_raises_backend_error(monkeypatch):
    def fake_run(cmd, **kwargs):
        raise FileNotFoundError(cmd[0])

    monkeypatch.setattr(evaluator_module.subprocess, "run", fake_run)
    with pytest.raises(BackendError, match="not found on PATH"):
        codex_runner("PROMPT")


@pytest.mark.unit
def test_codex_runner_timeout_raises_backend_error(monkeypatch):
    monkeypatch.delenv("TRADINGAGENTS_TIMEOUT_MACRO_EVALUATOR", raising=False)

    def fake_run(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=kwargs.get("timeout"))

    monkeypatch.setattr(evaluator_module.subprocess, "run", fake_run)
    with pytest.raises(BackendError, match="timed out after 540s"):
        codex_runner("PROMPT")


@pytest.mark.unit
def test_backend_timeout_fits_inside_the_component_budget(monkeypatch):
    monkeypatch.delenv("TRADINGAGENTS_TIMEOUT_MACRO_EVALUATOR", raising=False)
    timeout = evaluator_module.backend_timeout_seconds()
    assert timeout == 540.0  # (1200 - 120) / 2
    # The worst case (run + schema-errors retry) plus the parse/write headroom
    # fits the orchestrator's 1200s macro_evaluator budget — a hung first
    # backend can no longer eat the whole outer budget and turn an exit-3
    # backend failure into a bare component ``timeout``.
    assert (
        evaluator_module.BACKEND_ATTEMPTS * timeout + evaluator_module.HEADROOM_SECONDS
        <= 1200.0
    )
    # The env override that resizes the outer budget resizes the inner share.
    monkeypatch.setenv("TRADINGAGENTS_TIMEOUT_MACRO_EVALUATOR", "1920")
    assert evaluator_module.backend_timeout_seconds() == 900.0  # (1920 - 120) / 2
    # A pathologically small budget still leaves a usable attempt (floor).
    monkeypatch.setenv("TRADINGAGENTS_TIMEOUT_MACRO_EVALUATOR", "120")
    assert (
        evaluator_module.backend_timeout_seconds()
        == evaluator_module.MIN_BACKEND_TIMEOUT_SECONDS
    )


@pytest.mark.unit
def test_codex_runner_nonzero_exit_raises_backend_error_with_detail(monkeypatch):
    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 3, stdout="", stderr="quota exceeded\nmore")

    monkeypatch.setattr(evaluator_module.subprocess, "run", fake_run)
    with pytest.raises(BackendError, match=r"exit 3\): quota exceeded"):
        codex_runner("PROMPT")


# ---------------------------------------------------------------------------
# claude_runner + D19 backend selection (default claude, --backend codex,
# identity per backend, R2 independence warning)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_claude_runner_command_construction_and_stdin(monkeypatch):
    monkeypatch.delenv("TRADINGAGENTS_TIMEOUT_MACRO_EVALUATOR", raising=False)
    seen = {}
    envelope = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": "",
        "structured_output": json.loads(judgment()),
    }

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        seen["input"] = kwargs.get("input")
        seen["timeout"] = kwargs.get("timeout")
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(envelope), stderr="")

    monkeypatch.setattr(evaluator_module.subprocess, "run", fake_run)
    assert json.loads(claude_runner("EVAL PROMPT")) == envelope["structured_output"]
    # D19 claude backend: fresh `claude -p` with web search tools allowed.
    assert "--output-format" in seen["cmd"]
    assert "json" in seen["cmd"]
    assert "--json-schema" in seen["cmd"]
    schema = json.loads(seen["cmd"][seen["cmd"].index("--json-schema") + 1])
    assert "scores" in schema["required"]
    assert seen["input"] == "EVAL PROMPT"  # prompt over stdin, never argv
    assert seen["timeout"] == 540.0  # same budget-derived timeout as the codex backend


@pytest.mark.unit
def test_claude_runner_error_paths_raise_backend_error(monkeypatch):
    def missing(cmd, **kwargs):
        raise FileNotFoundError(cmd[0])

    monkeypatch.setattr(evaluator_module.subprocess, "run", missing)
    with pytest.raises(BackendError, match="claude CLI not found on PATH"):
        claude_runner("PROMPT")

    monkeypatch.delenv("TRADINGAGENTS_TIMEOUT_MACRO_EVALUATOR", raising=False)

    def timeout(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=kwargs.get("timeout"))

    monkeypatch.setattr(evaluator_module.subprocess, "run", timeout)
    with pytest.raises(BackendError, match="timed out after 540s"):
        claude_runner("PROMPT")

    def nonzero(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="not logged in\nmore")

    monkeypatch.setattr(evaluator_module.subprocess, "run", nonzero)
    with pytest.raises(BackendError, match=r"exit 1\): not logged in"):
        claude_runner("PROMPT")


@pytest.mark.unit
@pytest.mark.parametrize(
    ("stdout", "message"),
    [
        (
            json.dumps(
                {
                    "type": "result",
                    "subtype": "error",
                    "is_error": True,
                    "result": "quota exceeded\nretry later",
                }
            ),
            "quota exceeded retry later",
        ),
        ("not valid JSON", None),
        (
            json.dumps(
                {
                    "type": "result",
                    "subtype": "success",
                    "is_error": False,
                    "result": "",
                }
            ),
            None,
        ),
        (
            json.dumps(
                {
                    "subtype": "success",
                    "is_error": False,
                    "structured_output": json.loads(judgment()),
                }
            ),
            None,
        ),
        (
            json.dumps(
                {
                    "type": "result",
                    "subtype": "success",
                    "structured_output": json.loads(judgment()),
                }
            ),
            None,
        ),
        (
            json.dumps(
                {
                    "type": "result",
                    "subtype": "success",
                    "is_error": "false",
                    "structured_output": json.loads(judgment()),
                }
            ),
            None,
        ),
        (
            json.dumps(
                {
                    "type": "result",
                    "subtype": "complete",
                    "is_error": False,
                    "structured_output": json.loads(judgment()),
                }
            ),
            None,
        ),
        (json.dumps([json.loads(judgment())]), None),
    ],
    ids=[
        "is_error",
        "invalid_json",
        "missing_structured_output",
        "missing_type",
        "missing_is_error",
        "nonboolean_is_error",
        "invalid_subtype",
        "nonmapping_envelope",
    ],
)
def test_claude_runner_rejects_unusable_result_envelopes(monkeypatch, stdout, message):
    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(evaluator_module.subprocess, "run", fake_run)
    with pytest.raises(BackendError) as exc_info:
        claude_runner("PROMPT")
    if message:
        assert message in str(exc_info.value)


@pytest.mark.unit
def test_default_backend_is_claude_and_flag_selects_codex(macro_dir, monkeypatch):
    # No injected runner: the production runner selection itself is under
    # test, with subprocess.run faked so both paths stay offline.
    seen = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        stdout = judgment()
        if cmd[0] == "claude":
            stdout = json.dumps(
                {
                    "type": "result",
                    "subtype": "success",
                    "is_error": False,
                    "result": "",
                    "structured_output": json.loads(stdout),
                }
            )
        return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(evaluator_module.subprocess, "run", fake_run)

    brief = write_brief(macro_dir, "2026-08-03.us.md", macro_text())
    assert evaluate_brief(brief) == EXIT_OK
    assert seen["cmd"][0] == "claude"  # D19 default: eval_backend=claude
    assert read_eval(brief)["evaluator"] == CLAUDE_EVALUATOR

    # Explicit --backend codex wins and stamps the codex identity.
    assert main([str(brief), "--force", "--backend", "codex"]) == EXIT_OK
    assert seen["cmd"][0] == "codex"
    assert read_eval(brief)["evaluator"] == EVALUATOR_MODEL


@pytest.mark.unit
def test_eval_backend_env_override_resolves_the_default(macro_dir, monkeypatch):
    monkeypatch.setenv("TRADINGAGENTS_EVAL_BACKEND", "codex")
    brief = write_brief(macro_dir, "2026-08-03.us.md", macro_text())
    assert evaluate_brief(brief, runner=FakeRunner(judgment())) == EXIT_OK
    assert read_eval(brief)["evaluator"] == EVALUATOR_MODEL


@pytest.mark.unit
def test_matching_collect_and_eval_backends_warn_loudly_but_never_fail(
    macro_dir, monkeypatch, capsys
):
    # Default eval backend (claude) colliding with collect_backend=claude
    # degrades R2 independence: loud stderr warning, still a completed eval.
    monkeypatch.setenv("TRADINGAGENTS_COLLECT_BACKEND", "claude")
    brief = write_brief(macro_dir, "2026-08-03.us.md", macro_text())
    assert evaluate_brief(brief, runner=FakeRunner(judgment())) == EXIT_OK
    err = capsys.readouterr().err
    assert "matches collect_backend" in err
    assert "independence" in err
    assert (macro_dir / "2026-08-03.us.eval.json").exists()


@pytest.mark.unit
def test_distinct_backends_emit_no_independence_warning(macro_dir, capsys):
    # Out of the box (collect=codex, eval=claude) the warning must NOT fire.
    brief = write_brief(macro_dir, "2026-08-03.us.md", macro_text())
    assert evaluate_brief(brief, runner=FakeRunner(judgment())) == EXIT_OK
    assert "matches collect_backend" not in capsys.readouterr().err


# ---------------------------------------------------------------------------
# S3 mirror of the macro eval json (collector R7's "and later its eval json")
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_macro_eval_json_synced_to_s3_after_write(macro_dir, monkeypatch):
    monkeypatch.setenv("MACRO_BRIEF_S3_URI", "s3://bucket/briefs")
    brief = write_brief(macro_dir, "2026-08-03.us.md", macro_text())
    copies = []
    code = evaluate_brief(
        brief,
        runner=FakeRunner(judgment()),
        s3_copy=lambda path, uri: copies.append((path, uri)),
    )
    assert code == EXIT_OK
    assert copies == [(macro_dir / "2026-08-03.us.eval.json", "s3://bucket/briefs")]


@pytest.mark.unit
def test_eval_s3_sync_failure_is_warning_not_error(macro_dir, monkeypatch, capsys):
    monkeypatch.setenv("MACRO_BRIEF_S3_URI", "s3://bucket/briefs")
    brief = write_brief(macro_dir, "2026-08-03.us.md", macro_text())

    def boom(path, uri):
        raise RuntimeError("no credentials")

    assert evaluate_brief(brief, runner=FakeRunner(judgment()), s3_copy=boom) == EXIT_OK
    assert (macro_dir / "2026-08-03.us.eval.json").exists()  # local source of truth
    err = capsys.readouterr().err
    assert "S3 sync" in err and "no credentials" in err


@pytest.mark.unit
def test_eval_s3_sync_skipped_without_env_and_for_ticker_briefs(tmp_path, macro_dir, monkeypatch):
    copies = []
    brief = write_brief(macro_dir, "2026-08-03.us.md", macro_text())
    assert (
        evaluate_brief(
            brief,
            runner=FakeRunner(judgment()),
            s3_copy=lambda path, uri: copies.append((path, uri)),
        )
        == EXIT_OK
    )
    assert copies == []  # no env — no sync

    monkeypatch.setenv("MACRO_BRIEF_S3_URI", "s3://bucket/briefs")
    ticker_brief = write_brief(tmp_path / "ticker_briefs" / "NVDA", "2026-08-03.md", ticker_text())
    assert (
        evaluate_brief(
            ticker_brief,
            runner=FakeRunner(judgment()),
            s3_copy=lambda path, uri: copies.append((path, uri)),
        )
        == EXIT_OK
    )
    assert copies == []  # ticker evals are never mirrored (no ticker S3 contract)


@pytest.mark.unit
def test_default_s3_copy_invokes_aws_cli(monkeypatch, tmp_path):
    seen = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(evaluator_module.subprocess, "run", fake_run)
    target = tmp_path / "2026-08-03.us.eval.json"
    target.write_text("{}", encoding="utf-8")
    evaluator_module.default_s3_copy(target, "s3://bucket/briefs/")
    assert seen["cmd"] == [
        "aws", "s3", "cp", str(target), "s3://bucket/briefs/2026-08-03.us.eval.json",
    ]

    def failing_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="AccessDenied\ndetail")

    monkeypatch.setattr(evaluator_module.subprocess, "run", failing_run)
    with pytest.raises(RuntimeError, match="AccessDenied"):
        evaluator_module.default_s3_copy(target, "s3://bucket/briefs")


# ---------------------------------------------------------------------------
# Legacy v1 macro briefs (session-less)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_legacy_sessionless_macro_brief_evaluated(macro_dir):
    text = macro_text(omit=("session",))
    brief = write_brief(macro_dir, "2026-08-03.md", text)
    runner = FakeRunner(judgment())
    assert evaluate_brief(brief, runner=runner) == EXIT_OK
    data = read_eval(brief)
    assert "session" not in data  # legacy briefs carry no session anywhere
    assert data["brief_sha256"] == sha256_text(text)
    assert (macro_dir / "2026-08-03.eval.json").exists()


# ---------------------------------------------------------------------------
# R6 — evaluator.log line: date, session/ticker, verdict, min-score, flags
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_log_line_appended_for_macro_and_ticker(tmp_path, macro_dir):
    macro_brief = write_brief(macro_dir, "2026-08-03.us.md", macro_text())
    evaluate_brief(macro_brief, runner=FakeRunner(judgment(scores={"coverage": 6.5})))
    ticker_brief = write_brief(tmp_path / "ticker_briefs" / "NVDA", "2026-08-03.md", ticker_text())
    evaluate_brief(ticker_brief, runner=FakeRunner(judgment(flags=[flag("major")])))

    lines = (macro_dir / "evaluator.log").read_text(encoding="utf-8").splitlines()
    assert lines[0] == "2026-08-03 | us | pass | 6.5 | 0"
    assert lines[1] == "2026-08-03 | NVDA | warn | 8.0 | 1"


@pytest.mark.unit
def test_idempotent_skip_does_not_append_log_line(macro_dir):
    brief = write_brief(macro_dir, "2026-08-03.us.md", macro_text())
    evaluate_brief(brief, runner=FakeRunner(judgment()))
    evaluate_brief(brief, runner=FakeRunner())
    lines = (macro_dir / "evaluator.log").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1


# ---------------------------------------------------------------------------
# CLI (argparse main + `python -m pipeline.evaluator`)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_main_parses_args_and_injects_runner(macro_dir):
    brief = write_brief(macro_dir, "2026-08-03.us.md", macro_text())
    runner = FakeRunner(judgment(), judgment())
    assert main([str(brief)], runner=runner) == EXIT_OK
    assert main([str(brief), "--force"], runner=runner) == EXIT_OK
    assert runner.calls == 2


@pytest.mark.unit
def test_module_invocable_and_refuses_before_backend(tmp_path):
    # `python -m pipeline.evaluator` on a broken brief exits 2 without ever
    # reaching the codex subprocess — safe to run offline.
    broken = tmp_path / "2026-08-03.us.md"
    broken.write_text("no frontmatter here", encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, "-m", "pipeline.evaluator", str(broken)],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )
    assert proc.returncode == EXIT_REFUSED
    assert "refusing structurally invalid" in proc.stderr

    help_proc = subprocess.run(
        [sys.executable, "-m", "pipeline.evaluator", "--help"],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )
    assert help_proc.returncode == 0
    assert "--force" in help_proc.stdout


# ---------------------------------------------------------------------------
# Prompt unit checks (no file round-trip)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_build_eval_prompt_mentions_subject_and_date():
    from pipeline.contracts.briefs import parse_macro_brief, parse_ticker_brief

    macro_meta, _ = parse_macro_brief(macro_text(), structural_only=True)
    macro_prompt = build_eval_prompt("macro", macro_meta, "BODY")
    assert "global macro brief" in macro_prompt
    assert "2026-08-03" in macro_prompt

    ticker_meta, _ = parse_ticker_brief(ticker_text(), structural_only=True)
    ticker_prompt = build_eval_prompt("ticker", ticker_meta, "BODY")
    assert "NVDA" in ticker_prompt
    assert "what moved NVDA in the 48h before 2026-08-03" in ticker_prompt


# ---------------------------------------------------------------------------
# Batch ticker evaluation (orchestrator step 5) — quota policy + isolation
# ---------------------------------------------------------------------------


def _write_pool(pool_dir, session, day, core=(), opportunity=()):
    payload = {
        "as_of_date": day,
        "session": session,
        "generated_at": f"{day}T12:31:00Z",
        "generator": "codex-deep-search",
        "core": [{"ticker": t} for t in core],
        "opportunity": [
            {
                "ticker": t,
                "score": 8.0,
                "catalyst_type": "earnings",
                "rationale": "seed",
                "citations": ["https://example.com/a"],
                "technical": {"gate": "pass"},
                "entered_on": day,
                "low_score_streak": 0,
                "gate_fail_streak": 0,
            }
            for t in opportunity
        ],
        "watch": [],
        "removed": [],
    }
    path = pool_dir / session / f"{day}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


@pytest.fixture()
def batch_dirs(tmp_path, monkeypatch):
    pool_dir = tmp_path / "pools"
    brief_dir = tmp_path / "ticker_briefs"
    pool_dir.mkdir()
    brief_dir.mkdir()
    monkeypatch.setenv("TRADINGAGENTS_POOL_DIR", str(pool_dir))
    monkeypatch.setenv("TRADINGAGENTS_TICKER_BRIEF_DIR", str(brief_dir))
    monkeypatch.setenv("TRADINGAGENTS_MACRO_BRIEF_DIR", str(tmp_path / "macro_briefs"))
    monkeypatch.delenv("TRADINGAGENTS_TRIGGER_THRESHOLD", raising=False)
    monkeypatch.delenv("MACRO_BRIEF_S3_URI", raising=False)
    return pool_dir, brief_dir


@pytest.mark.unit
def test_batch_quota_policy_selection_and_summary(batch_dirs, capsys):
    pool_dir, brief_dir = batch_dirs
    day = "2026-08-03"
    # Core: NVDA has a brief (evaluated), MSFT has none (absent).
    # Opportunity: AMD >= threshold (evaluated), INTC below threshold
    # (skipped, no backend spend), TSLA has no brief (absent).
    _write_pool(pool_dir, "us", day, core=("NVDA", "MSFT"), opportunity=("AMD", "INTC", "TSLA"))
    write_brief(brief_dir / "NVDA", f"{day}.md", ticker_text(catalyst_score=2.0))
    write_brief(brief_dir / "AMD", f"{day}.md", ticker_text(ticker="AMD", catalyst_score=7.5))
    write_brief(brief_dir / "INTC", f"{day}.md", ticker_text(ticker="INTC", catalyst_score=3.0))
    runner = FakeRunner(judgment(), judgment())

    rc = batch_tickers("us", date_str=day, runner=runner)

    assert rc == EXIT_OK
    assert runner.calls == 2  # core NVDA (score irrelevant for core) + AMD
    summary = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert summary["entrypoint"] == "batch_tickers"
    assert summary["requested"] == 2
    assert summary["absent"] == 2
    assert summary["below_threshold"] == 1
    assert summary["ok"] == 2
    assert summary["verdicts"] == {"pass": 2}
    # Both eval jsons landed next to their briefs.
    assert (brief_dir / "NVDA" / f"{day}.eval.json").is_file()
    assert (brief_dir / "AMD" / f"{day}.eval.json").is_file()


@pytest.mark.unit
def test_batch_broken_brief_is_refused_and_isolated(batch_dirs, capsys):
    pool_dir, brief_dir = batch_dirs
    day = "2026-08-03"
    _write_pool(pool_dir, "us", day, core=("BAD", "NVDA"))
    write_brief(brief_dir / "BAD", f"{day}.md", ticker_text(ticker="BAD").replace("## Risks", "## Hazards"))
    write_brief(brief_dir / "NVDA", f"{day}.md", ticker_text())
    runner = FakeRunner(judgment())

    rc = batch_tickers("us", date_str=day, runner=runner)

    assert rc == EXIT_OK  # refusal is a collector bug, not a batch wipeout
    summary = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert summary["refused"] == 1
    assert summary["ok"] == 1
    assert runner.calls == 1  # the refusal never reached the backend


@pytest.mark.unit
def test_batch_wipeout_exits_error(batch_dirs, capsys):
    pool_dir, brief_dir = batch_dirs
    day = "2026-08-03"
    _write_pool(pool_dir, "us", day, core=("NVDA",))
    write_brief(brief_dir / "NVDA", f"{day}.md", ticker_text())
    runner = FakeRunner(BackendError("boom"), BackendError("boom"))

    rc = batch_tickers("us", date_str=day, runner=runner)

    assert rc == EXIT_ERROR
    summary = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert summary["backend_failed"] == 1
    assert summary["ok"] == 0


@pytest.mark.unit
def test_batch_absent_pool_falls_back_to_core_yaml(batch_dirs, capsys):
    pool_dir, brief_dir = batch_dirs
    day = "2026-08-03"
    (pool_dir / "core.us.yaml").write_text("- ticker: NVDA\n", encoding="utf-8")
    write_brief(brief_dir / "NVDA", f"{day}.md", ticker_text())
    runner = FakeRunner(judgment())

    rc = batch_tickers("us", date_str=day, runner=runner)

    assert rc == EXIT_OK
    captured = capsys.readouterr()
    assert "core coverage from core.us.yaml only" in captured.err
    summary = json.loads(captured.out.strip().splitlines()[-1])
    assert summary["requested"] == 1
    assert summary["ok"] == 1


@pytest.mark.unit
def test_batch_cli_dispatch_and_validation(batch_dirs, capsys):
    # --batch-tickers requires --session and forbids a brief path (argparse
    # usage errors exit 2); a valid invocation with an empty universe exits 0.
    with pytest.raises(SystemExit) as exc:
        main(["--batch-tickers"])
    assert exc.value.code == 2
    with pytest.raises(SystemExit) as exc:
        main(["--batch-tickers", "--session", "us", "some-brief.md"])
    assert exc.value.code == 2
    capsys.readouterr()
    assert main(["--batch-tickers", "--session", "us", "--date", "2026-08-03"]) == EXIT_OK
    summary = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert summary["requested"] == 0
