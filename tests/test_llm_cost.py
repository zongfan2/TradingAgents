"""LLM usage ledger, Claude-first routing and per-role knobs — specs/llm-cost.md."""

import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage

from pipeline import analysis_runner as ar, llm_usage
from pipeline.llm_routing import CLAUDE, DEEPSEEK, choose_profile
from pipeline.llm_usage import (
    UsageCallback,
    append_usage,
    call_cost,
    exhausted_marker,
    is_credit_exhausted_error,
    mark_exhausted,
    month_spend,
    read_usage,
)
from tests.test_analysis_runner import (
    NOW,
    SLOT,
    FakeChild,
    make_config,
    make_resolvers,
    read_records,
    run,
    write_brief,
    write_eval,
    write_pool,
)

OCT = datetime(2026, 10, 29, 12, tzinfo=timezone.utc)


def usage_row(model, *, calls=1, inp=1000, out=100, cost=0.01):
    return {model: {"calls": calls, "input": inp, "output": out, "cache_read": 0,
                    "cache_write": 0, "reasoning": 0, "cost_usd": cost, "priced": True}}


# ---------------------------------------------------------------------------
# R1 — pricing, callback, ledger
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_call_cost_tiers_and_cache():
    assert call_cost("claude-opus-5-5", {"input": 1_000_000, "output": 1_000_000}) == pytest.approx(24.0)
    # Cached input bills at the cache-read rate; writes at 1.25x input.
    tokens = {"input": 1_000_000, "cache_read": 600_000, "cache_write": 100_000, "output": 0}
    assert call_cost("claude-opus-5-5", tokens) == pytest.approx(
        (300_000 * 4 + 600_000 * 0.2 + 100_000 * 5) / 1_000_000
    )
    # Haiku 5.5 switches to the long-prompt tier above 100K prompt tokens.
    short = call_cost("claude-haiku-5-5", {"input": 100_000, "output": 1000})
    long = call_cost("claude-haiku-5-5", {"input": 100_001, "output": 1000})
    assert short == pytest.approx((100_000 * 0.10 + 1000 * 0.50) / 1e6)
    assert long == pytest.approx((100_001 * 0.50 + 1000 * 2.50) / 1e6)
    assert call_cost("gpt-unknown", {"input": 5}) is None


def _result(*messages):
    return SimpleNamespace(generations=[[SimpleNamespace(message=m)] for m in messages])


@pytest.mark.unit
def test_usage_callback_sums_per_model_and_reads_deepseek_cache_hits():
    cb = UsageCallback()
    claude = AIMessage(
        content="x",
        usage_metadata={"input_tokens": 1200, "output_tokens": 300, "total_tokens": 1500,
                        "input_token_details": {"cache_read": 1000, "cache_creation": 100},
                        "output_token_details": {"reasoning": 50}},
        response_metadata={"model_name": "claude-opus-5-5"},
    )
    deepseek = AIMessage(
        content="y",
        usage_metadata={"input_tokens": 500, "output_tokens": 20, "total_tokens": 520},
        response_metadata={"model_name": "deepseek-flash",
                           "token_usage": {"prompt_cache_hit_tokens": 400}},
    )
    cb.on_llm_end(_result(claude, deepseek))
    cb.on_llm_end(_result(claude))
    cb.on_llm_end(_result(AIMessage(content="no usage")))  # skipped
    cb.on_llm_end(object())  # malformed: logged, never raised
    summary = cb.summary()
    assert summary["claude-opus-5-5"]["calls"] == 2
    assert summary["claude-opus-5-5"]["cache_read"] == 2000
    assert summary["claude-opus-5-5"]["reasoning"] == 100
    assert summary["deepseek-flash"]["cache_read"] == 400
    assert summary["claude-opus-5-5"]["cost_usd"] == pytest.approx(
        2 * call_cost("claude-opus-5-5", {"input": 1200, "output": 300, "cache_read": 1000,
                                         "cache_write": 100}), abs=1e-6)


@pytest.mark.unit
def test_month_spend_is_a_fold_over_the_ledger(tmp_path):
    clock = lambda: OCT  # noqa: E731
    append_usage(tmp_path, run_id="r1", date="2026-10-29", session="us", ticker="A", arm="brief",
                 profile="claude", models=usage_row("claude-opus-5-5", cost=1.5), clock=clock)
    mixed = {**usage_row("claude-haiku-5-5", cost=0.25), **usage_row("deepseek-flash", cost=9.0)}
    line = append_usage(tmp_path, run_id="r2", date="2026-10-29", session="us", ticker="B",
                        arm="brief", profile="mixed", models=mixed, clock=clock)
    assert line["provider"] == "mixed" and line["cost_usd"] == pytest.approx(9.25)
    append_usage(tmp_path, run_id="r3", date="2026-09-30", session="us", ticker="C", arm="brief",
                 profile="claude", models=usage_row("claude-opus-5-5", cost=50.0),
                 clock=lambda: datetime(2026, 9, 30, tzinfo=timezone.utc))
    assert append_usage(tmp_path, run_id="r4", date="2026-10-29", session="us", ticker="D",
                        arm="brief", profile="claude", models={}, clock=clock) is None
    with open(tmp_path / "llm_usage.jsonl", "a", encoding="utf-8") as handle:
        handle.write("not json\n")
    assert month_spend(tmp_path, "2026-10", "anthropic") == pytest.approx(1.75)
    assert month_spend(tmp_path, "2026-10", "deepseek") == pytest.approx(9.0)
    assert month_spend(tmp_path, "2026-09", "anthropic") == pytest.approx(50.0)
    assert len(read_usage(tmp_path)) == 3


# ---------------------------------------------------------------------------
# R2 — routing
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_claude_first_routing_rules(tmp_path):
    assert choose_profile(tmp_path, "claude-first", 190, OCT).profile is CLAUDE
    append_usage(tmp_path, run_id="r", date="2026-10-29", session="us", ticker="A", arm="brief",
                 profile="claude", models=usage_row("claude-opus-5-5", cost=190.0), clock=lambda: OCT)
    at_cap = choose_profile(tmp_path, "claude-first", 190, OCT)
    assert at_cap.profile is DEEPSEEK and "≥ budget" in at_cap.reason
    # A new month starts over.
    assert choose_profile(tmp_path, "claude-first", 190, datetime(2026, 11, 1, tzinfo=timezone.utc)).profile is CLAUDE
    assert choose_profile(tmp_path, "claude", 190, OCT).profile is CLAUDE
    assert choose_profile(tmp_path, "deepseek", 190, OCT).profile is DEEPSEEK
    mark_exhausted(tmp_path, "2026-11", "Your credit balance is too low")
    nov = choose_profile(tmp_path, "claude-first", 190, datetime(2026, 11, 2, tzinfo=timezone.utc))
    assert nov.profile is DEEPSEEK and "exhausted" in nov.reason
    with pytest.raises(ValueError):
        choose_profile(tmp_path, "openai", 190, OCT)


@pytest.mark.unit
def test_profiles_match_the_user_decision():
    assert CLAUDE.env["TRADINGAGENTS_DEEP_THINK_LLM"] == "claude-opus-5-5"
    assert CLAUDE.env["TRADINGAGENTS_QUICK_THINK_LLM"] == "claude-haiku-5-5"
    assert CLAUDE.env["TRADINGAGENTS_ANTHROPIC_DEEP_EFFORT"] == "medium"
    assert CLAUDE.env["TRADINGAGENTS_ANTHROPIC_QUICK_EFFORT"] == "low"
    assert DEEPSEEK.env["TRADINGAGENTS_QUICK_THINK_LLM"] == "deepseek-flash"
    assert DEEPSEEK.env["TRADINGAGENTS_DEEPSEEK_QUICK_THINKING"] == "disabled"
    assert is_credit_exhausted_error(
        "run-one: Error code: 400 - Your credit balance is too low to access the Anthropic API."
    )
    assert not is_credit_exhausted_error("rate limited")


@pytest.mark.unit
def test_runner_settings_routing_env():
    settings = ar.load_settings({"TRADINGAGENTS_LLM_ROUTING": "Claude-First",
                                 "TRADINGAGENTS_ANTHROPIC_MONTHLY_BUDGET_USD": "150"})
    assert (settings.llm_routing, settings.anthropic_monthly_budget_usd) == ("claude-first", 150.0)
    assert ar.load_settings({}).llm_routing is None
    with pytest.raises(ValueError):
        ar.load_settings({"TRADINGAGENTS_LLM_ROUTING": "gpt"})


class UsageChild(FakeChild):
    """FakeChild reporting usage; optionally fails Claude runs with the
    credit error."""

    def __init__(self, *, credit_error=False, **kwargs):
        super().__init__(**kwargs)
        self.credit_error = credit_error

    def __call__(self, spec, timeout):
        env = dict(spec.env)
        model = env.get("TRADINGAGENTS_DEEP_THINK_LLM", "legacy-model")
        if self.credit_error and env.get("TRADINGAGENTS_LLM_PROVIDER") == "anthropic":
            self.specs.append(spec)
            return ar.ChildResult(
                1, None, "run-one: Error code: 400 - Your credit balance is too low",
                usage=usage_row(model, cost=0.0, inp=10, out=0),
            )
        result = super().__call__(spec, timeout)
        cost = 0.4 if model.startswith("claude") else 0.05
        return ar.ChildResult(result.returncode, result.payload, result.error,
                              usage=usage_row(model, cost=cost))


def _slot(tmp_path):
    config = make_config(tmp_path)
    write_pool(config, "us", SLOT, core=("AAA", "BBB"))
    macro = write_brief(config.macro_brief_dir / "2026-08-07.us.md")
    write_eval(macro, "pass")
    return config, make_resolvers(macro)


@pytest.mark.unit
def test_routed_slot_records_profile_digest_usage_and_summary(tmp_path):
    config, resolvers = _slot(tmp_path)
    child = UsageChild()
    settings = ar.RunnerSettings(ab_pairing="off", llm_routing="claude-first")
    outcome, emitted = run(config, resolvers, child, settings=settings)
    assert all(dict(spec.env)["TRADINGAGENTS_LLM_PROVIDER"] == "anthropic" for spec in child.specs)
    records = read_records(config)
    assert {r.preset for r in records} == {"claude"}
    snapshot = json.loads(
        (config.ledger_dir / "config_snapshots" / f"{records[0].inputs.config_digest}.json").read_text()
    )
    assert snapshot["preset"] == "claude"
    assert snapshot["graph"]["deep_think_llm"] == "claude-opus-5-5"
    assert snapshot["graph"]["anthropic_quick_effort"] == "low"
    usage = read_usage(config.state_dir)
    assert sorted(u["run_id"] for u in usage) == sorted(r.run_id for r in records)
    assert all(u["profile"] == "claude" for u in usage)
    assert {line["profile"] for line in emitted} == {"claude"}
    llm = outcome.summary["llm"]
    assert llm["profiles"] == {"claude": 2} and llm["cost_usd"] == pytest.approx(0.8)
    assert llm["anthropic_month_usd"] == pytest.approx(0.8)


@pytest.mark.unit
def test_credit_exhaustion_marks_the_month_and_reruns_on_deepseek(tmp_path):
    config, resolvers = _slot(tmp_path)
    child = UsageChild(credit_error=True)
    settings = ar.RunnerSettings(ab_pairing="off", llm_routing="claude-first",
                                 analysis_job_concurrency=1)
    outcome, _ = run(config, resolvers, child, settings=settings)
    month = NOW.strftime("%Y-%m")
    assert exhausted_marker(config.state_dir, month).exists()
    records = read_records(config)
    assert {r.preset for r in records} == {"deepseek"} and {r.decision for r in records} == {"BUY"}
    providers = [dict(s.env)["TRADINGAGENTS_LLM_PROVIDER"] for s in child.specs]
    # First job: claude fails, deepseek reruns; second job goes straight to deepseek.
    assert providers == ["anthropic", "deepseek", "deepseek"]
    usage = read_usage(config.state_dir)
    assert [u["run_id"] is None for u in usage].count(True) == 1  # the failed Claude attempt
    assert outcome.summary["errors"] == 0
    snapshot = json.loads(
        (config.ledger_dir / "config_snapshots" / f"{records[0].inputs.config_digest}.json").read_text()
    )
    assert snapshot["graph"]["quick_think_llm"] == "deepseek-flash"
    assert snapshot["graph"]["deepseek_quick_thinking"] == "disabled"


@pytest.mark.unit
def test_unrouted_slot_keeps_the_legacy_behavior(tmp_path):
    config, resolvers = _slot(tmp_path)
    child = UsageChild()
    outcome, _ = run(config, resolvers, child, settings=ar.RunnerSettings(ab_pairing="off", preset="deepseek"))
    assert all(spec.env == () for spec in child.specs)
    assert {r.preset for r in read_records(config)} == {"deepseek"}
    assert "llm" not in outcome.summary
    assert {u["profile"] for u in read_usage(config.state_dir)} == {"deepseek"}


@pytest.mark.unit
def test_child_usage_line_is_parsed_from_a_failed_child():
    stdout = 'noise\nTRADINGAGENTS_RUN_ONE_USAGE {"claude-opus-5-5": {"calls": 1}}\n'
    assert ar._extract_usage(stdout) == {"claude-opus-5-5": {"calls": 1}}
    assert ar._extract_usage("nothing here") is None
    assert ar._extract_usage("TRADINGAGENTS_RUN_ONE_USAGE {broken") is None


@pytest.mark.unit
def test_run_one_failure_prints_the_spend(monkeypatch, capsys, tmp_path):
    from pipeline import run_one

    monkeypatch.setenv("TRADINGAGENTS_STATE_DIR", str(tmp_path / "state"))
    # Keep the repo .env out of the test process (test_run_one convention).
    monkeypatch.setattr(run_one, "load_env_file", lambda *a, **k: None)

    def factory(position_context, debug, callbacks=None):
        callbacks[0].on_llm_end(_result(AIMessage(
            content="x", usage_metadata={"input_tokens": 10, "output_tokens": 2, "total_tokens": 12},
            response_metadata={"model_name": "claude-haiku-5-5"})))
        raise RuntimeError("graph blew up")

    code = run_one.main(["--ticker", "NVDA", "--date", "2026-10-29", "--session", "us",
                         "--arm", "brief"], graph_factory=factory)
    assert code == 1
    out = capsys.readouterr().out
    assert run_one.USAGE_SENTINEL in out
    assert ar._extract_usage(out)["claude-haiku-5-5"]["calls"] == 1


@pytest.mark.unit
def test_orchestrator_notification_llm_text():
    from pipeline.orchestrator import RunResult, _analysis_llm_text

    summary = {"llm": {"profiles": {"claude": 12, "deepseek": 3}, "cost_usd": 1.84,
                       "anthropic_month_usd": 42.1, "anthropic_budget_usd": 190.0}}
    text = _analysis_llm_text(RunResult(0, json.dumps(summary) + "\n", ""))
    assert text == "LLM claude 12/deepseek 3 $1.84; Anthropic month $42.10/$190"
    assert _analysis_llm_text(RunResult(0, json.dumps({"planned": 1}), "")) is None
    assert _analysis_llm_text(RunResult(0, "not json", "")) is None


@pytest.mark.unit
def test_usage_failure_never_fails_the_run(tmp_path, monkeypatch):
    config, resolvers = _slot(tmp_path)

    def boom(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(llm_usage, "locked_append", boom)
    outcome, _ = run(config, resolvers, UsageChild(),
                     settings=ar.RunnerSettings(ab_pairing="off", llm_routing="claude"))
    assert outcome.summary["completed"] == 2 and outcome.summary["errors"] == 0
