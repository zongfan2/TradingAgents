"""Claude → OpenAI → DeepSeek ladder — specs/llm-provider-ladder.md."""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from pipeline import analysis_runner as ar, claude_cli
from pipeline.llm_routing import CLAUDE, DEEPSEEK, OPENAI, choose_profile, cli_rung, openai_usable
from pipeline.llm_usage import (
    append_usage,
    call_cost,
    confirm_balance,
    confirmation_path,
    exhausted_marker,
    is_credit_exhausted_error,
    mark_exhausted,
    month_spend,
    price_for,
    provider_for_model,
    read_confirmation,
    remaining_balance,
)
from tests.test_analysis_runner import NOW, read_records, run
from tests.test_llm_cost import UsageChild, _slot, usage_row

MONTH = NOW.strftime("%Y-%m")
KEY_ENV = {"OPENAI_API_KEY": "sk-proj-test"}


def openai_run(state, cost, at):
    append_usage(state, run_id="r", date=at.date().isoformat(), session="us", ticker="A", arm="brief",
                 profile="openai", clock=lambda: at,
                 models={"gpt-6.1-sol": {"calls": 1, "cost_usd": cost, "priced": True}})


def cli_line(log, billing, billed, ts):
    with open(log, "a", encoding="utf-8") as handle:
        handle.write(json.dumps({"ts": ts.isoformat(), "billing": billing, "billed_usd": billed}) + "\n")


def over_claude_budget(state):
    mark_exhausted(state, MONTH, "credit balance too low")


# ---------------------------------------------------------------------------
# L4 — prices and per-provider spend
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_gpt_prices_and_provider():
    assert provider_for_model("gpt-6-luna") == "openai"
    assert call_cost("gpt-6-luna", {"input": 200_000, "cache_read": 100_000, "output": 1_000_000}) == (
        pytest.approx(100_000 * 0.10 / 1e6 + 100_000 * 0.01 / 1e6 + 0.50)
    )
    # Over 272K prompt tokens: the long-context tier, cache writes included.
    long = {"input": 300_000, "cache_read": 100_000, "cache_write": 100_000, "output": 1_000}
    assert call_cost("gpt-6.1-sol", long) == pytest.approx(
        (100_000 * 4.00 + 100_000 * 0.20 + 100_000 * 4.00 + 1_000 * 15.00) / 1e6)
    assert call_cost("gpt-6.1-terra", {"input": 200_000, "output": 200_000}) == pytest.approx(0.2 + 1.0)
    assert price_for("gpt-6-luna-2026-09-14") == (price_for("gpt-6-luna")[0], True)  # snapshot id
    ceiling, exact = price_for("gpt-9-unknown")
    assert (ceiling.output, exact) == (50.00, False)  # the highest listed rate (gpt-6-astra)
    assert price_for("gpt-6-astra") == (ceiling, True)
    # Per request, on the largest growing request's bound 2·sum/(n+1):
    short, long = (300_000 * 2 + 1_000 * 10) / 1e6, (300_000 * 4 + 1_000 * 15) / 1e6
    summed = {"input": 300_000, "output": 1_000}
    assert call_cost("gpt-6.1-sol", summed, growing_requests=10) == pytest.approx(short)  # bound 54.5K
    assert call_cost("gpt-6.1-sol", summed) == pytest.approx(long)  # one request
    # 100K + 200K + 300K: average 200K, but the last request is long.
    assert call_cost("gpt-6.1-sol", {"input": 600_000, "output": 1_000}, growing_requests=3) == pytest.approx(
        (600_000 * 4 + 1_000 * 15) / 1e6)


# ---------------------------------------------------------------------------
# L1 — balance confirmation
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_remaining_balance_deducts_only_spend_after_the_confirmation(tmp_path, monkeypatch):
    state = tmp_path / "state"
    log = tmp_path / "cli.jsonl"
    monkeypatch.setenv("TRADINGAGENTS_CLAUDE_USAGE_LOG", str(log))
    before, at = NOW - timedelta(hours=2), NOW - timedelta(hours=1)
    assert remaining_balance(state, MONTH) is None
    openai_run(state, 9.0, before)  # spent before the confirmation: already in the balance
    confirm_balance(state, MONTH, 71, at)
    openai_run(state, 2.5, NOW)
    cli_line(log, "openai-api", 1.25, NOW)
    cli_line(log, "openai-api", 7.0, before)
    cli_line(log, "anthropic-api", 3.0, NOW)
    assert remaining_balance(state, MONTH) == pytest.approx(71 - 2.5 - 1.25)
    assert month_spend(state, MONTH, "openai") == pytest.approx(9 + 2.5 + 1.25 + 7)


@pytest.mark.unit
@pytest.mark.parametrize("record", [
    "junk", {"month": "2026-07", "balance_usd": 71, "confirmed_at": "2026-08-07T10:00:00Z"},
    {"month": MONTH, "balance_usd": "71", "confirmed_at": "2026-08-07T10:00:00Z"},
    {"month": MONTH, "balance_usd": -1, "confirmed_at": "2026-08-07T10:00:00Z"},
    {"month": MONTH, "balance_usd": True, "confirmed_at": "2026-08-07T10:00:00Z"},
    {"month": MONTH, "balance_usd": 71, "confirmed_at": "yesterday"},
])
def test_a_bad_confirmation_reads_as_unconfirmed(tmp_path, record):
    path = confirmation_path(tmp_path, MONTH)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(record))
    assert read_confirmation(tmp_path, MONTH) is None
    assert openai_usable(tmp_path, MONTH, KEY_ENV)[0] is False


@pytest.mark.unit
@pytest.mark.parametrize("balance", [float("nan"), float("inf"), -5])
def test_confirm_rejects_junk(tmp_path, balance):
    with pytest.raises(ValueError):
        confirm_balance(tmp_path, MONTH, balance, NOW)


@pytest.mark.unit
def test_openai_usable_reasons(tmp_path):
    assert openai_usable(tmp_path, MONTH, {}) == (False, "OPENAI_API_KEY unset")
    assert openai_usable(tmp_path, MONTH, KEY_ENV) == (False, f"openai balance not confirmed for {MONTH}")
    confirm_balance(tmp_path, MONTH, 71, NOW)
    assert openai_usable(tmp_path, MONTH, KEY_ENV) == (True, "openai remaining $71.00")
    margin = {**KEY_ENV, "TRADINGAGENTS_OPENAI_BALANCE_MARGIN_USD": "71"}
    assert openai_usable(tmp_path, MONTH, margin)[0] is False
    mark_exhausted(tmp_path, MONTH, "insufficient_quota", "openai")
    assert exhausted_marker(tmp_path, MONTH, "openai").exists()
    assert not exhausted_marker(tmp_path, MONTH).exists()  # Anthropic's marker untouched
    assert openai_usable(tmp_path, MONTH, KEY_ENV) == (False, "openai credits exhausted this month")


# ---------------------------------------------------------------------------
# L1 — routing
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_ladder_order(tmp_path):
    assert choose_profile(tmp_path, "ladder", 190, NOW, KEY_ENV).profile is CLAUDE
    over_claude_budget(tmp_path)
    decision = choose_profile(tmp_path, "ladder", 190, NOW, KEY_ENV)
    assert decision.profile is DEEPSEEK and "not confirmed" in decision.reason
    confirm_balance(tmp_path, MONTH, 71, NOW)
    assert choose_profile(tmp_path, "ladder", 190, NOW, KEY_ENV).profile is OPENAI
    # claude-first never uses OpenAI.
    assert choose_profile(tmp_path, "claude-first", 190, NOW, KEY_ENV).profile is DEEPSEEK
    assert cli_rung(tmp_path, "ladder", 190, NOW, KEY_ENV)[0] == "openai"
    mark_exhausted(tmp_path, MONTH, "insufficient_quota", "openai")
    assert choose_profile(tmp_path, "ladder", 190, NOW, KEY_ENV).profile is DEEPSEEK


@pytest.mark.unit
def test_openai_profile_and_per_role_effort():
    from tradingagents.graph.trading_graph import TradingAgentsGraph

    assert OPENAI.env["TRADINGAGENTS_QUICK_THINK_LLM"] == "gpt-6-luna"
    assert OPENAI.env["TRADINGAGENTS_DEEP_THINK_LLM"] == "gpt-6.1-sol"
    assert OPENAI.env["TRADINGAGENTS_RESEARCH_MANAGER_LLM"] == ""  # RM on sol, like the PM
    graph = ar.effective_analysis_config(OPENAI.env)["graph"]
    assert (graph["llm_provider"], graph["openai_deep_reasoning_effort"],
            graph["openai_quick_reasoning_effort"], graph["anthropic_deep_effort"]) == (
        "openai", "medium", "low", None)
    for profile in (CLAUDE, DEEPSEEK):  # never inherit the OpenAI knobs
        assert profile.env["TRADINGAGENTS_OPENAI_DEEP_REASONING_EFFORT"] == ""
    g = TradingAgentsGraph.__new__(TradingAgentsGraph)
    g.config = {"llm_provider": "openai", "openai_reasoning_effort": "high",
                "openai_deep_reasoning_effort": "medium", "openai_quick_reasoning_effort": "low"}
    assert g._get_provider_kwargs("deep") == {"reasoning_effort": "medium"}
    assert g._get_provider_kwargs("quick") == {"reasoning_effort": "low"}
    g.config = {"llm_provider": "openai", "openai_reasoning_effort": "high"}
    assert g._get_provider_kwargs("quick") == {"reasoning_effort": "high"}


@pytest.mark.unit
def test_runner_steps_down_the_ladder_on_exhaustion(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-proj-test")
    config, resolvers = _slot(tmp_path)
    confirm_balance(config.state_dir, MONTH, 71, NOW - timedelta(hours=1))

    class LadderChild(UsageChild):
        def __call__(self, spec, timeout):
            env = dict(spec.env)
            provider = env.get("TRADINGAGENTS_LLM_PROVIDER")
            model = env.get("TRADINGAGENTS_DEEP_THINK_LLM")
            if provider == "anthropic":
                self.specs.append(spec)
                return ar.ChildResult(1, None, "Error code: 400 - Your credit balance is too low",
                                      usage=usage_row(model, cost=0.0))
            if provider == "openai" and len([s for s in self.specs if dict(s.env).get(
                    "TRADINGAGENTS_LLM_PROVIDER") == "openai"]) >= 1:
                self.specs.append(spec)
                return ar.ChildResult(1, None, "Error code: 429 - insufficient_quota",
                                      usage=usage_row(model, cost=0.0))
            result = super().__call__(spec, timeout)
            return ar.ChildResult(result.returncode, result.payload, result.error,
                                  usage=usage_row(model, cost=0.3))

    child = LadderChild()
    settings = ar.RunnerSettings(ab_pairing="off", llm_routing="ladder", analysis_job_concurrency=1)
    outcome, _ = run(config, resolvers, child, settings=settings)
    providers = [dict(s.env)["TRADINGAGENTS_LLM_PROVIDER"] for s in child.specs]
    # Job 1: claude out → openai runs. Job 2: openai out → deepseek reruns.
    assert providers == ["anthropic", "openai", "openai", "deepseek"]
    assert exhausted_marker(config.state_dir, MONTH).exists()
    assert exhausted_marker(config.state_dir, MONTH, "openai").exists()
    assert sorted(r.preset for r in read_records(config)) == ["deepseek", "openai"]
    assert outcome.summary["errors"] == 0
    assert outcome.summary["llm"]["openai_remaining_usd"] == pytest.approx(71 - 0.3)


@pytest.mark.unit
def test_claude_first_runner_never_uses_openai(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-proj-test")
    config, resolvers = _slot(tmp_path)
    confirm_balance(config.state_dir, MONTH, 71, NOW)
    child = UsageChild(credit_error=True)
    settings = ar.RunnerSettings(ab_pairing="off", llm_routing="claude-first", analysis_job_concurrency=1)
    outcome, _ = run(config, resolvers, child, settings=settings)
    assert {dict(s.env)["TRADINGAGENTS_LLM_PROVIDER"] for s in child.specs} == {"anthropic", "deepseek"}
    assert "openai_remaining_usd" not in outcome.summary["llm"]


@pytest.mark.unit
def test_lark_line_shows_the_openai_balance():
    from pipeline.orchestrator import RunResult, _analysis_llm_text

    def text(**llm):
        summary = {"llm": {"profiles": {"openai": 2}, "cost_usd": 0.6, "anthropic_month_usd": 190,
                           "anthropic_budget_usd": 190, **llm}}
        return _analysis_llm_text(RunResult(0, json.dumps(summary) + "\n", ""))

    assert text().endswith("Anthropic month $190.00/$190")
    assert text(openai_remaining_usd=None).endswith("; OpenAI balance unconfirmed")
    assert text(openai_remaining_usd=70.4).endswith("; OpenAI left $70.40")


@pytest.mark.unit
def test_openai_quota_error_detection():
    assert is_credit_exhausted_error("Error code: 429 - {'code': 'insufficient_quota'}", "openai")
    assert is_credit_exhausted_error("You exceeded your current quota, please check", "openai")
    assert is_credit_exhausted_error("Quota exceeded. Check your plan and billing details.", "openai")  # codex
    assert is_credit_exhausted_error("stream error: quota_exceeded", "openai")
    assert not is_credit_exhausted_error("Your credit balance is too low", "openai")
    assert not is_credit_exhausted_error("insufficient_quota")  # Anthropic markers only


# ---------------------------------------------------------------------------
# L3 — collection and evaluation via hermetic codex
# ---------------------------------------------------------------------------


@pytest.fixture
def ladder_cli(tmp_path, monkeypatch):
    state = tmp_path / "state"
    monkeypatch.setenv("TRADINGAGENTS_STATE_DIR", str(state))
    log = tmp_path / "claude_usage.jsonl"
    monkeypatch.setenv("TRADINGAGENTS_CLAUDE_USAGE_LOG", str(log))
    for name in ("ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "TRADINGAGENTS_OPENAI_COLLECT_MODEL",
                 "TRADINGAGENTS_OPENAI_EVAL_MODEL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-ds-test")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-proj-secret")
    monkeypatch.setenv("TRADINGAGENTS_LLM_ROUTING", "ladder")
    monkeypatch.setattr(claude_cli, "codex_executable", lambda: ("/opt/codex/bin/codex", Path("/opt/codex")))
    now = datetime.now(timezone.utc)
    month = now.strftime("%Y-%m")
    mark_exhausted(state, month, "credit balance too low")
    confirm_balance(state, month, 71, now - timedelta(minutes=1))
    claude_cli._reset_route_for_tests()
    yield state, log, month
    claude_cli._reset_route_for_tests()


def codex_stream(*, text="brief", fail=None, searches=("q1", "q2"), usage=None):
    events = [{"type": "thread.started", "thread_id": "t"}, {"type": "turn.started"}]
    for query in searches:
        events.append({"type": "item.completed", "item": {"type": "web_search",
                                                          "action": {"type": "search", "query": query}}})
    events.append({"type": "item.completed", "item": {"type": "web_search",
                                                      "action": {"type": "open_page", "url": "https://e.com/a"}}})
    if fail:
        events.append({"type": "error", "message": "Reconnecting... 1/5"})
        events.append({"type": "turn.failed", "error": {"message": fail}})
    else:
        events.append({"type": "item.completed", "item": {"type": "agent_message", "text": text}})
        events.append({"type": "turn.completed", "usage": usage or {
            "input_tokens": 25_000, "cached_input_tokens": 10_000, "cache_write_input_tokens": 14_000,
            "output_tokens": 500, "reasoning_output_tokens": 100}})
    return "\n".join(json.dumps(e) for e in events) + "\n"


@pytest.mark.unit
def test_openai_route_builds_a_hermetic_codex_call(ladder_cli):
    state, _log, _month = ladder_cli
    command = claude_cli.claude_command("claude-haiku-5-5", "low")
    assert claude_cli.cli_route() == "openai"
    # The native binary, never the node launcher (a timeout must kill it).
    assert command[:2] == ["/opt/codex/bin/codex", "exec"] and command[-1] == "-"
    for flag in ("--ignore-user-config", "--ephemeral", "--json", "--skip-git-repo-check"):
        assert flag in command
    assert command[command.index("--sandbox") + 1] == "read-only"
    assert command[command.index("-m") + 1] == "gpt-6-luna"
    assert "model_reasoning_effort=low" in command and "tools.web_search=true" in command
    assert not any("sk-proj-secret" in part for part in command)  # key never on argv
    env = claude_cli.call_env(command)
    assert env["CODEX_API_KEY"] == "sk-proj-secret"
    assert env["CODEX_HOME"] == env["HOME"] == str(state / "codex_home")
    assert (env["CODEX_MANAGED_BY_NPM"], env["CODEX_MANAGED_PACKAGE_ROOT"]) == ("1", "/opt/codex")
    # No other secret reaches codex: only the allow-listed parent env.
    assert set(env) <= {*claude_cli._CODEX_ENV_KEEP, "CODEX_API_KEY", "CODEX_HOME", "HOME",
                        "CODEX_MANAGED_BY_NPM", "CODEX_MANAGED_PACKAGE_ROOT"}
    assert not {"ANTHROPIC_API_KEY", "DEEPSEEK_API_KEY", "OPENAI_API_KEY"} & set(env)
    # Web search only: shell, browser, computer use, code mode ... disabled.
    disabled = {command[i + 1] for i, part in enumerate(command) if part == "--disable"}
    assert {"shell_tool", "unified_exec", "browser_use", "computer_use",
            "multi_agent", "plugins", "apps", "hooks"} <= disabled
    assert "code_mode_host" not in disabled  # web search runs through code mode
    assert command[command.index("--cd") + 1] == str(state / "codex_home" / "work")
    schema = json.dumps({"type": "object"})
    evaluate = claude_cli.claude_command("claude-opus-5-5", "xhigh", "--json-schema", schema, role="eval")
    assert evaluate[evaluate.index("-m") + 1] == "gpt-6.1-terra"
    assert "model_reasoning_effort=high" in evaluate
    path = evaluate[evaluate.index("--output-schema") + 1]
    assert json.loads(Path(path).read_text()) == {"type": "object"}
    with pytest.raises(ValueError):
        claude_cli.claude_command("claude-opus-5-5", "low", "--bogus")
    with pytest.raises(ValueError):
        claude_cli.claude_command("claude-opus-5-5", "low", role="nope")
    assert claude_cli.call_env(["codex", "exec", "-"]) is None  # the legacy codex backend inherits


@pytest.mark.unit
def test_codex_stream_becomes_a_priced_envelope(ladder_cli):
    _state, log, _month = ladder_cli
    claude_cli.claude_command("claude-haiku-5-5", "low")
    text = claude_cli.collect_output("claude", codex_stream(), component="ticker_collector")
    assert text == "brief"
    line = json.loads(log.read_text().splitlines()[-1])
    expected = (1_000 * 0.10 + 10_000 * 0.01 + 14_000 * 0.10 + 500 * 0.50) / 1e6 + 2 * 0.01
    assert (line["billing"], line["model"]) == ("openai-api", "gpt-6-luna")
    assert line["billed_usd"] == pytest.approx(expected)
    assert (line["web_search_requests"], line["web_search_queries"]) == (2, ["q1", "q2"])
    assert (line["web_fetch_requests"], line["web_fetch_urls"]) == (1, ["https://e.com/a"])
    assert (line["input_tokens"], line["cache_read_input_tokens"], line["cache_creation_input_tokens"]) == (
        1_000, 10_000, 14_000)


@pytest.mark.unit
def test_codex_structured_output_and_failures(ladder_cli):
    claude_cli.claude_command("claude-opus-5-5", "medium", "--json-schema", "{}", role="eval")
    envelope = claude_cli.parse_envelope(codex_stream(text='{"verdict": "pass"}'))
    assert envelope["structured_output"] == {"verdict": "pass"}
    with pytest.raises(claude_cli.EnvelopeError, match="boom"):
        claude_cli.parse_envelope(codex_stream(fail="boom"))
    assert json.loads(claude_cli.envelope_line(codex_stream(fail="boom")))["result"] == "boom"
    cut = "\n".join(json.dumps(e) for e in ({"type": "thread.started"}, {"type": "turn.started"}))
    with pytest.raises(claude_cli.EnvelopeError):
        claude_cli.parse_envelope(cut)


@pytest.mark.unit
def test_codex_quota_error_marks_openai_and_moves_to_deepseek(ladder_cli):
    import os

    state, log, month = ladder_cli
    claude_cli.claude_command("claude-haiku-5-5", "low")
    claude_cli.record_failed_call("ticker_collector", "claude-haiku-5-5", "low",
                                  codex_stream(fail="Quota exceeded. Check your plan and billing details."))
    assert exhausted_marker(state, month, "openai").exists()
    assert claude_cli.cli_route() == "deepseek"
    assert os.environ["ANTHROPIC_BASE_URL"] == "https://api.deepseek.com/anthropic"
    line = json.loads(log.read_text().splitlines()[-1])
    assert (line["outcome"], line["billing"]) == ("error", "openai-api")
    command = claude_cli.claude_command("claude-haiku-5-5", "low")
    assert command[0] == "claude"


@pytest.mark.unit
def test_missing_codex_binary_skips_the_openai_rung(ladder_cli, monkeypatch):
    monkeypatch.setattr(claude_cli, "codex_executable", lambda: None)
    assert claude_cli.cli_route() == "deepseek"


@pytest.mark.unit
def test_codex_executable_resolves_the_native_binary_behind_the_npm_launcher(tmp_path, monkeypatch):
    monkeypatch.setattr(claude_cli, "_codex_target",
                        lambda: ("x86_64-unknown-linux-musl", "@openai/codex-linux-x64"))
    root = tmp_path / "lib" / "node_modules" / "@openai" / "codex"
    (root / "bin").mkdir(parents=True)
    launcher = root / "bin" / "codex.js"
    launcher.write_text("#!/usr/bin/env node\n")
    native = root / "node_modules" / "@openai" / "codex-linux-x64" / "vendor" / "x86_64-unknown-linux-musl" / "bin" / "codex"
    native.parent.mkdir(parents=True)
    native.write_bytes(b"\x7fELF....")
    native.chmod(0o755)
    # Another target installed next to it (sorts first) is never picked.
    other = root / "vendor" / "aarch64-apple-darwin" / "bin" / "codex"
    other.parent.mkdir(parents=True)
    other.write_bytes(b"\xcf\xfa\xed\xfe")
    other.chmod(0o755)
    shim = tmp_path / "bin" / "codex"
    shim.parent.mkdir()
    shim.symlink_to(launcher)
    monkeypatch.setattr(claude_cli.shutil, "which", lambda cli: str(shim))
    assert claude_cli.codex_executable() == (str(native), root)
    native.chmod(0o644)  # not executable: no usable binary
    assert claude_cli.codex_executable() is None
    monkeypatch.setattr(claude_cli.shutil, "which", lambda cli: str(native))
    assert claude_cli.codex_executable() == (str(native), None)  # PATH already points at the binary
    macho = tmp_path / "codex-macho"
    macho.write_bytes(b"\xcf\xfa\xed\xfe....")  # a Homebrew-style native binary on macOS
    monkeypatch.setattr(claude_cli.shutil, "which", lambda cli: str(macho))
    assert claude_cli.codex_executable() == (str(macho), None)
    monkeypatch.setattr(claude_cli.shutil, "which", lambda cli: None)
    assert claude_cli.codex_executable() is None


@pytest.mark.unit
def test_cli_openai_spend_counts_in_the_openai_balance(ladder_cli):
    state, _log, month = ladder_cli
    claude_cli.claude_command("claude-haiku-5-5", "low")
    claude_cli.collect_output("claude", codex_stream(), component="ticker_collector")
    assert remaining_balance(state, month) == pytest.approx(71 - month_spend(state, month, "openai"))
    assert month_spend(state, month, "openai") > 0


# ---------------------------------------------------------------------------
# The confirmation CLI
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_llm_budget_cli(tmp_path, monkeypatch, capsys):
    from pipeline import llm_budget

    monkeypatch.setenv("TRADINGAGENTS_SKIP_DOTENV", "1")
    monkeypatch.setenv("TRADINGAGENTS_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("OPENAI_API_KEY", "sk-proj-test")
    monkeypatch.setenv("TRADINGAGENTS_ANTHROPIC_MONTHLY_BUDGET_USD", "150")
    assert llm_budget.main(["status"], now=NOW) == 0
    out = capsys.readouterr().out
    assert "NOT confirmed" in out and "/ budget $150" in out
    monkeypatch.setenv("TRADINGAGENTS_ANTHROPIC_MONTHLY_BUDGET_USD", "lots")
    assert llm_budget.main(["status"], now=NOW) == 0  # junk shows, never crashes
    assert "budget unreadable" in capsys.readouterr().out
    assert llm_budget.main(["confirm", "openai", "71"], now=NOW) == 0
    assert read_confirmation(tmp_path, MONTH)["balance_usd"] == 71
    assert llm_budget.main(["status"], now=NOW) == 0
    assert "left $71.00" in capsys.readouterr().out
    assert llm_budget.main(["confirm", "openai", "-3"], now=NOW) == 2
    assert llm_budget.main(["confirm", "openai", "71", "--month", "2026-1"], now=NOW) == 2


@pytest.mark.unit
def test_cli_lines_with_naive_or_junk_ts_still_count(tmp_path, monkeypatch):
    state = tmp_path / "state"
    log = tmp_path / "cli.jsonl"
    monkeypatch.setenv("TRADINGAGENTS_CLAUDE_USAGE_LOG", str(log))
    with open(log, "a", encoding="utf-8") as handle:
        for ts in (f"{MONTH}-07T10:00:00", f"{MONTH}-junk"):
            handle.write(json.dumps({"ts": ts, "billing": "openai-api", "billed_usd": 1.0}) + "\n")
    assert month_spend(state, MONTH, "openai") == pytest.approx(2.0)
    assert month_spend(state, MONTH, "openai", since=NOW.isoformat()) == pytest.approx(2.0)


@pytest.mark.unit
def test_eval_schema_is_sent_in_strict_form(ladder_cli):
    from pipeline.evaluator import _ModelJudgment

    schema = json.dumps(_ModelJudgment.model_json_schema())
    command = claude_cli.claude_command("claude-opus-5-5", "medium", "--json-schema", schema, role="eval")
    sent = json.loads(Path(command[command.index("--output-schema") + 1]).read_text())

    def objects(node):
        if isinstance(node, dict):
            if isinstance(node.get("properties"), dict):
                yield node
            for value in node.values():
                yield from objects(value)
        elif isinstance(node, list):
            for value in node:
                yield from objects(value)

    found = list(objects(sent))
    assert found and all(set(o["required"]) == set(o["properties"]) and o["additionalProperties"] is False
                         for o in found)
    assert "default" not in json.dumps(sent).replace('"default_', "")
    assert _ModelJudgment.model_json_schema() == json.loads(schema)  # caller's schema untouched


@pytest.mark.unit
def test_every_claude_command_caller_states_its_cli_role():
    """On the openai rung the role picks the GPT model (collect=luna,
    eval=sol): pin the callers so a new evaluation path cannot silently run
    on the collector's model, and none passes a flag codex cannot map."""
    root = Path(__file__).resolve().parent.parent
    calls = {}
    for path in sorted((root / "pipeline").glob("*.py")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if "claude_command(" in line and "def claude_command" not in line:
                calls.setdefault(path.name, []).append(line.strip())
    assert calls == {
        "claude_cli.py": ["return claude_command(*_collect_model_effort())"],
        "evaluator.py": ['command = claude_command(model, effort, "--json-schema", schema, role="eval")'],
    }


@pytest.mark.unit
def test_an_unpriced_codex_model_is_budgeted_at_the_openai_ceiling(ladder_cli, monkeypatch):
    _state, log, _month = ladder_cli
    monkeypatch.setenv("TRADINGAGENTS_OPENAI_COLLECT_MODEL", "o9-research")
    command = claude_cli.claude_command("claude-haiku-5-5", "low")
    assert command[command.index("-m") + 1] == "o9-research"
    claude_cli.collect_output("claude", codex_stream(searches=()), component="ticker_collector")
    line = json.loads(log.read_text().splitlines()[-1])
    assert line["billed_usd"] == pytest.approx((1_000 * 10 + 10_000 * 1 + 14_000 * 10 + 500 * 50) / 1e6)


@pytest.mark.unit
def test_unset_openai_knobs_leave_other_profiles_digests_alone():
    for env in (CLAUDE.env, DEEPSEEK.env, {}):
        graph = ar.effective_analysis_config(env)["graph"]
        assert "openai_deep_reasoning_effort" not in graph and "openai_quick_reasoning_effort" not in graph
    assert ar.effective_analysis_config(OPENAI.env)["graph"]["openai_quick_reasoning_effort"] == "low"


@pytest.mark.unit
def test_a_cut_codex_stream_is_billed_conservatively(ladder_cli):
    _state, log, _month = ladder_cli
    claude_cli.claude_command("claude-haiku-5-5", "low")
    events = [{"type": "thread.started"}, {"type": "turn.started"},
              {"type": "item.completed", "item": {"type": "web_search", "action": {"type": "search", "query": "q"}}}]
    cut = "\n".join(json.dumps(e) for e in events) + "\n"
    with pytest.raises(claude_cli.EnvelopeError):
        claude_cli.parse_envelope(cut)
    claude_cli.record_failed_call("ticker_collector", "claude-haiku-5-5", "low", cut)
    line = json.loads(log.read_text().splitlines()[-1])
    assert (line["outcome"], line["billing"]) == ("error", "openai-api")
    assert line["billed_usd"] == pytest.approx(0.01 + claude_cli.CODEX_CUT_CALL_ESTIMATE_USD)


@pytest.mark.unit
@pytest.mark.parametrize("site", ["ticker", "macro", "pool", "evaluator"])
@pytest.mark.parametrize("printed", [(json.dumps({"type": "thread.started"}) + "\n").encode(), b"", None],
                         ids=["one-event", "empty", "none"])
def test_a_timed_out_call_records_its_spend(ladder_cli, monkeypatch, site, printed):
    import subprocess

    import pipeline.evaluator as ev
    import pipeline.macro_collector as mc
    import pipeline.pool_builder as pb
    import pipeline.ticker_collector as tc

    _state, log, _month = ladder_cli
    module, run_it, error = {
        "ticker": (tc, lambda: tc.default_runner("claude", "P", 60), tc.CollectorError),
        "macro": (mc, lambda: mc.default_runner("claude", "P"), mc.CollectorError),
        "pool": (pb, lambda: pb.default_runner("claude", "P"), pb.NominationError),
        "evaluator": (ev, lambda: ev.claude_runner("P"), ev.BackendError),
    }[site]
    partial = printed  # bytes (or nothing), as TimeoutExpired carries

    def fake_run(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, 60, output=partial)

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    with pytest.raises(error, match="timed out"):
        run_it()
    line = json.loads(log.read_text().splitlines()[-1])
    assert line["billed_usd"] == pytest.approx(claude_cli.CODEX_CUT_CALL_ESTIMATE_USD)


@pytest.mark.unit
def test_strict_schema_keeps_a_property_named_default():
    schema = {"type": "object", "properties": {"default": {"type": "string", "default": "x"},
                                               "n": {"type": "integer"}}, "required": ["n"]}
    strict = claude_cli.strict_output_schema(schema)
    assert strict["required"] == ["default", "n"] and strict["additionalProperties"] is False
    assert strict["properties"]["default"] == {"type": "string"}


@pytest.mark.unit
@pytest.mark.parametrize("tail", [
    [{"type": "error", "message": "Reconnecting... 1/5"}],                      # killed while reconnecting
    [{"type": "turn.failed", "error": {"message": "stream disconnected"}}],    # failed mid-turn
])
def test_any_unfinished_codex_turn_adds_the_token_estimate(ladder_cli, tail):
    _state, log, _month = ladder_cli
    claude_cli.claude_command("claude-haiku-5-5", "low")
    events = [{"type": "thread.started"}, {"type": "turn.started"},
              {"type": "item.completed", "item": {"type": "web_search", "action": {"type": "search", "query": "q"}}},
              *tail]
    claude_cli.record_failed_call("ticker_collector", "claude-haiku-5-5", "low",
                                  "\n".join(json.dumps(e) for e in events))
    line = json.loads(log.read_text().splitlines()[-1])
    assert line["billed_usd"] == pytest.approx(0.01 + claude_cli.CODEX_CUT_CALL_ESTIMATE_USD)


@pytest.mark.unit
def test_openai_child_usage_is_captured_and_an_unnamed_model_still_counts(tmp_path):
    from langchain_core.messages import AIMessage

    from pipeline.llm_usage import UsageCallback
    from tests.test_llm_cost import _result

    # The shape langchain-openai returns (verified live 2026-11-01).
    message = AIMessage(content="x", response_metadata={"model_name": "gpt-6-luna-2026-09-14"},
                        usage_metadata={"input_tokens": 1000, "output_tokens": 100, "total_tokens": 1100,
                                        "input_token_details": {"cache_read": 400}})
    callback = UsageCallback()
    callback.on_llm_end(_result(message))
    row = callback.summary()["gpt-6-luna-2026-09-14"]
    assert row["priced"] is True
    assert row["cost_usd"] == pytest.approx((600 * 0.10 + 400 * 0.01 + 100 * 0.50) / 1e6, abs=1e-7)
    # No model name reported → "unknown", unpriced: an openai run still bills it, at the ceiling.
    unnamed = {"unknown": {"calls": 1, "input": 1000, "output": 100, "cache_read": 0, "cache_write": 0,
                           "reasoning": 0, "cost_usd": 0.0, "priced": False}}
    append_usage(tmp_path, run_id="r", date="2026-08-07", session="us", ticker="A", arm="brief",
                 profile="openai", clock=lambda: NOW, models=unnamed)
    assert month_spend(tmp_path, MONTH, "openai") == pytest.approx((1000 * 10 + 100 * 50) / 1e6)
    assert month_spend(tmp_path, MONTH, "anthropic") == 0


@pytest.mark.unit
@pytest.mark.parametrize(("plat", "machine", "triple"), [
    ("linux", "x86_64", "x86_64-unknown-linux-musl"), ("linux", "AMD64", "x86_64-unknown-linux-musl"),
    ("linux", "aarch64", "aarch64-unknown-linux-musl"), ("linux", "arm64", "aarch64-unknown-linux-musl"),
    ("darwin", "arm64", "aarch64-apple-darwin"), ("darwin", "x86_64", "x86_64-apple-darwin"),
    ("win32", "AMD64", None), ("linux", "riscv64", None),
])
def test_codex_target_follows_the_launcher(monkeypatch, plat, machine, triple):
    monkeypatch.setattr(claude_cli.sys, "platform", plat)
    monkeypatch.setattr(claude_cli.platform, "machine", lambda: machine)
    target = claude_cli._codex_target()
    assert (target[0] if target else None) == triple


@pytest.mark.unit
def test_failed_marker_writes_never_loop_between_rungs(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-proj-test")
    config, resolvers = _slot(tmp_path)
    confirm_balance(config.state_dir, MONTH, 71, NOW - timedelta(hours=1))
    monkeypatch.setattr(ar, "mark_exhausted", lambda *a, **k: None)  # e.g. a read-only state dir

    class BrokeEverywhere(UsageChild):
        def __call__(self, spec, timeout):
            env = dict(spec.env)
            provider = env.get("TRADINGAGENTS_LLM_PROVIDER")
            if provider in ("anthropic", "openai"):
                self.specs.append(spec)
                error = ("Your credit balance is too low" if provider == "anthropic"
                         else "Error code: 429 - insufficient_quota")
                return ar.ChildResult(1, None, error, usage=usage_row(env["TRADINGAGENTS_DEEP_THINK_LLM"], cost=0.0))
            result = super().__call__(spec, timeout)
            return ar.ChildResult(result.returncode, result.payload, result.error, usage=usage_row("deepseek-v4-pro"))

    child = BrokeEverywhere()
    settings = ar.RunnerSettings(ab_pairing="off", llm_routing="ladder", analysis_job_concurrency=1)
    run(config, resolvers, child, settings=settings)
    providers = [dict(s.env)["TRADINGAGENTS_LLM_PROVIDER"] for s in child.specs]
    # Each job: anthropic → openai → deepseek, never back to a spent rung.
    assert providers == ["anthropic", "openai", "deepseek"] * 2


@pytest.mark.unit
def test_codex_executable_finds_a_hoisted_platform_package(tmp_path, monkeypatch):
    monkeypatch.setattr(claude_cli, "_codex_target",
                        lambda: ("x86_64-unknown-linux-musl", "@openai/codex-linux-x64"))
    modules = tmp_path / "proj" / "node_modules"
    launcher = modules / "@openai" / "codex" / "bin" / "codex.js"
    launcher.parent.mkdir(parents=True)
    launcher.write_text("#!/usr/bin/env node\n")
    native = modules / "@openai" / "codex-linux-x64" / "vendor" / "x86_64-unknown-linux-musl" / "bin" / "codex"
    native.parent.mkdir(parents=True)
    native.write_bytes(b"\x7fELF")
    native.chmod(0o755)
    monkeypatch.setattr(claude_cli.shutil, "which", lambda cli: str(launcher))
    assert claude_cli.codex_executable() == (str(native), modules / "@openai" / "codex")


@pytest.mark.unit
@pytest.mark.parametrize(("input_tokens", "searches", "tier"), [
    (300_000, 9, "short"),   # 10 requests: largest ≥ 54.5K
    (300_000, 0, "long"),    # one request of 300K
    (600_000, 2, "long"),    # 3 growing requests: largest ≥ 300K
])
def test_codex_turn_long_tier_wiring(ladder_cli, input_tokens, searches, tier):
    _state, log, _month = ladder_cli
    claude_cli.claude_command("claude-haiku-5-5", "low")
    usage = {"input_tokens": input_tokens, "cached_input_tokens": 0, "cache_write_input_tokens": 0, "output_tokens": 1_000}
    claude_cli.collect_output("claude", codex_stream(searches=tuple(f"q{i}" for i in range(searches)), usage=usage),
                              component="ticker_collector")
    rate_in, rate_out = (0.10, 0.50) if tier == "short" else (0.20, 0.75)  # gpt-6-luna
    expected = (input_tokens * rate_in + 1_000 * rate_out) / 1e6 + searches * 0.01
    assert json.loads(log.read_text().splitlines()[-1])["billed_usd"] == pytest.approx(expected)


@pytest.mark.unit
def test_unnamed_ledger_rows_use_the_whole_sum_for_the_tier(tmp_path):
    big = {"unknown": {"calls": 20, "input": 400_000, "output": 1_000, "cache_read": 0, "cache_write": 0,
                       "reasoning": 0, "cost_usd": 0.0, "priced": False}}
    append_usage(tmp_path, run_id="r", date="2026-08-07", session="us", ticker="A", arm="brief",
                 profile="openai", clock=lambda: NOW, models=big)
    # Unrelated calls summed: long tier on the sum (over-count), at the astra ceiling.
    assert month_spend(tmp_path, MONTH, "openai") == pytest.approx((400_000 * 20 + 1_000 * 75) / 1e6)
