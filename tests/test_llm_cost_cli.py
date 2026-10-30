"""Claude CLI inside the Anthropic budget, DeepSeek Flash after — specs/llm-cost.md R7."""

import json
from datetime import datetime, timezone

import pytest

from pipeline import claude_cli
from pipeline.llm_routing import DEEPSEEK, choose_profile, cli_uses_deepseek
from pipeline.llm_usage import (
    append_usage,
    cli_month_spend,
    exhausted_marker,
    month_spend,
)

NOW = datetime(2026, 11, 3, 12, tzinfo=timezone.utc)
MONTH = NOW.strftime("%Y-%m")


@pytest.fixture
def cli_env(tmp_path, monkeypatch):
    """A state dir, a usage log, a clean CLI route and restorable env."""
    state = tmp_path / "state"
    monkeypatch.setenv("TRADINGAGENTS_STATE_DIR", str(state))
    log = tmp_path / "claude_usage.jsonl"
    monkeypatch.setenv("TRADINGAGENTS_CLAUDE_USAGE_LOG", str(log))
    for name in ("ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-ds-test")
    claude_cli._reset_route_for_tests()
    yield state, log
    claude_cli._reset_route_for_tests()


def cli_row(log, *, billing="anthropic-api", billed=1.0, ts="2026-11-02T10:00:00+00:00"):
    row = {"ts": ts, "component": "ticker_collector", "model": "claude-sonnet-5-5"}
    if billing is not None:
        row.update(billing=billing, billed_usd=billed)
    with open(log, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(row) + "\n")


def envelope(cost=0.5, *, error=False, text="ok"):
    return {"type": "result", "subtype": "error" if error else "success", "is_error": error,
            "result": text, "total_cost_usd": cost, "num_turns": 3, "duration_ms": 10,
            "usage": {"input_tokens": 100, "cache_creation_input_tokens": 1000,
                      "cache_read_input_tokens": 4000, "output_tokens": 500}}


@pytest.mark.unit
def test_only_api_billed_cli_lines_count_in_the_monthly_budget(cli_env):
    state, log = cli_env
    cli_row(log, billed=2.5)
    cli_row(log, billing="subscription", billed=0.0)
    cli_row(log, billing=None)  # history before R7: ran on the subscription
    cli_row(log, billing="deepseek", billed=0.3)
    cli_row(log, billed=9.0, ts="2026-10-30T10:00:00+00:00")  # another month
    with open(log, "a", encoding="utf-8") as handle:
        handle.write("junk\n")
    assert cli_month_spend(state, MONTH) == pytest.approx(2.5)
    append_usage(state, run_id="r", date="2026-11-03", session="us", ticker="A", arm="brief",
                 profile="claude", clock=lambda: NOW,
                 models={"claude-opus-5-5": {"calls": 1, "cost_usd": 1.0, "priced": True}})
    assert month_spend(state, MONTH, "anthropic") == pytest.approx(3.5)


@pytest.mark.unit
def test_analysis_routing_sees_the_cli_spend(cli_env):
    state, log = cli_env
    assert choose_profile(state, "claude-first", 190, NOW).profile.name == "claude"
    cli_row(log, billed=190.0)
    assert choose_profile(state, "claude-first", 190, NOW).profile is DEEPSEEK
    assert cli_uses_deepseek(state, "claude-first", 190, NOW)[0] is True
    assert cli_uses_deepseek(state, None, 190, NOW) == (False, "routing unset (legacy)")


@pytest.mark.unit
def test_legacy_and_under_budget_keep_the_anthropic_cli(cli_env, monkeypatch):
    command = claude_cli.claude_command("claude-opus-5-5", "medium")
    assert claude_cli.cli_route() == "anthropic"
    assert command[command.index("--model") + 1] == "claude-opus-5-5"
    claude_cli._reset_route_for_tests()
    monkeypatch.setenv("TRADINGAGENTS_LLM_ROUTING", "claude-first")
    assert claude_cli.cli_route() == "anthropic"
    import os

    assert "ANTHROPIC_BASE_URL" not in os.environ


@pytest.mark.unit
def test_over_budget_points_the_cli_at_deepseek_flash(cli_env, monkeypatch):
    import os

    state, log = cli_env
    cli_row(log, billed=200.0, ts=datetime.now(timezone.utc).isoformat())
    monkeypatch.setenv("TRADINGAGENTS_LLM_ROUTING", "claude-first")
    command = claude_cli.claude_command("claude-opus-5-5", "medium", "--json-schema", "{}")
    assert claude_cli.cli_route() == "deepseek"
    assert command[command.index("--model") + 1] == "claude-sonnet-5-5"  # -> deepseek-flash
    assert os.environ["ANTHROPIC_BASE_URL"] == "https://api.deepseek.com/anthropic"
    assert os.environ["ANTHROPIC_API_KEY"] == "sk-ds-test"
    assert not any("sk-ds-test" in part for part in command)  # never on argv


@pytest.mark.unit
def test_no_deepseek_key_keeps_anthropic(cli_env, monkeypatch):
    _state, log = cli_env
    cli_row(log, billed=200.0, ts=datetime.now(timezone.utc).isoformat())
    monkeypatch.setenv("TRADINGAGENTS_LLM_ROUTING", "deepseek")
    monkeypatch.delenv("DEEPSEEK_API_KEY")
    assert claude_cli.cli_route() == "anthropic"


@pytest.mark.unit
def test_usage_lines_record_billing(cli_env, monkeypatch):
    _state, log = cli_env
    claude_cli.record_usage("evaluator", "claude-opus-5-5", "medium", envelope(0.7))
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    claude_cli.record_usage("evaluator", "claude-opus-5-5", "medium", envelope(0.7))
    monkeypatch.setenv("TRADINGAGENTS_LLM_ROUTING", "deepseek")
    claude_cli.cli_route()
    claude_cli.record_usage("evaluator", "claude-opus-5-5", "medium", envelope(0.7))
    rows = [json.loads(line) for line in log.read_text().splitlines()]
    assert [(r["billing"], r["billed_usd"]) for r in rows[:2]] == [
        ("anthropic-api", 0.7), ("subscription", 0.0),
    ]
    assert rows[2]["billing"] == "deepseek"
    # Repriced at Flash: 1000 written + 100 fresh input, 4000 cache hits, 500 out.
    assert rows[2]["billed_usd"] == pytest.approx((1100 * 0.14 + 4000 * 0.0028 + 500 * 0.28) / 1e6, abs=1e-6)


@pytest.mark.unit
def test_credit_exhaustion_marks_the_month_and_switches(cli_env, monkeypatch):
    import os

    state, _log = cli_env
    monkeypatch.setenv("TRADINGAGENTS_LLM_ROUTING", "claude-first")
    assert claude_cli.cli_route() == "anthropic"
    claude_cli.record_failed_call(
        "ticker_collector", "claude-sonnet-5-5", "medium",
        json.dumps(envelope(0.0, error=True, text="Credit balance is too low")),
    )
    month = datetime.now(timezone.utc).strftime("%Y-%m")
    assert exhausted_marker(state, month).exists()
    assert claude_cli.cli_route() == "deepseek"
    assert os.environ["ANTHROPIC_BASE_URL"] == "https://api.deepseek.com/anthropic"
    command = claude_cli.claude_command("claude-sonnet-5-5", "medium")
    assert command[command.index("--model") + 1] == "claude-sonnet-5-5"


@pytest.mark.unit
def test_routing_failure_never_breaks_a_call(cli_env, monkeypatch):
    monkeypatch.setenv("TRADINGAGENTS_LLM_ROUTING", "claude-first")
    monkeypatch.setenv("TRADINGAGENTS_ANTHROPIC_MONTHLY_BUDGET_USD", "not-a-number")
    assert claude_cli.claude_command("claude-opus-5-5", "medium")[0] == "claude"
    assert claude_cli.cli_route() == "anthropic"


@pytest.mark.unit
def test_in_flight_anthropic_call_is_billed_to_anthropic_after_a_switch(cli_env, monkeypatch):
    """Review a36f753: billing follows the route the call was built for."""
    _state, log = cli_env
    monkeypatch.setenv("TRADINGAGENTS_LLM_ROUTING", "claude-first")
    claude_cli.claude_command("claude-sonnet-5-5", "medium")  # built on Anthropic
    # Another thread's call hits the credit error and switches the process.
    import threading

    def other():
        claude_cli.record_failed_call(
            "ticker_collector", "claude-sonnet-5-5", "medium",
            json.dumps(envelope(0.0, error=True, text="Credit balance is too low")),
        )

    thread = threading.Thread(target=other)
    thread.start()
    thread.join()
    assert claude_cli.cli_route() == "deepseek"
    claude_cli.record_usage("ticker_collector", "claude-sonnet-5-5", "medium", envelope(0.9))
    last = json.loads(log.read_text().splitlines()[-1])
    assert (last["billing"], last["billed_usd"]) == ("anthropic-api", 0.9)
    claude_cli.claude_command("claude-sonnet-5-5", "medium")  # next call on DeepSeek
    claude_cli.record_usage("ticker_collector", "claude-sonnet-5-5", "medium", envelope(0.9))
    assert json.loads(log.read_text().splitlines()[-1])["billing"] == "deepseek"


@pytest.mark.unit
def test_junk_usage_on_the_deepseek_route_never_raises(cli_env, monkeypatch):
    """Review a36f753: record_usage never raises, so a paid brief is kept."""
    _state, log = cli_env
    monkeypatch.setenv("TRADINGAGENTS_LLM_ROUTING", "deepseek")
    claude_cli.claude_command("claude-sonnet-5-5", "medium")
    bad = envelope(0.5)
    bad["usage"] = {"input_tokens": "n/a", "output_tokens": {"nested": 1},
                    "cache_read_input_tokens": None}
    claude_cli.record_usage("ticker_collector", "claude-sonnet-5-5", "medium", bad)
    line = json.loads(log.read_text().splitlines()[-1])
    assert line["billing"] == "deepseek" and line["billed_usd"] == 0.0
    assert claude_cli.collect_output("claude", json.dumps(bad), component="ticker_collector") == "ok"
