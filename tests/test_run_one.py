"""Per-run analysis child (``pipeline/run_one.py``) — offline, faked graph/LLM.

Covers the argv/env contract (arm bundle switches + per-arm results/memory
isolation applied before any ``tradingagents`` import), TradePlan block
extraction with the one repair-reprompt, decision normalization, the
one-JSON-line stdout discipline, and the crash exit code the parent maps to a
``decision: "ERROR"`` ledger row.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from pipeline import run_one

VALID_PLAN = {
    "action": "BUY",
    "conviction": 0.7,
    "add_intent": False,
    "add_rationale": None,
    "entry_zone": [280.0, 285.0],
    "stop": 268.0,
    "targets": [301.0, 315.0],
    "horizon_days": 10,
    "invalidation": "daily close below the weekly mid-band",
    "sizing": {"risk_pct": 1.0},
    "source_levels": "entry = daily BOLL mid, stop = below daily lower band",
}


def fenced(payload) -> str:
    body = payload if isinstance(payload, str) else json.dumps(payload)
    return f"```json\n{body}\n```"


def parse_result_line(line: str) -> dict:
    """The stdout result line must carry the sentinel tag (parent protocol)."""
    assert line.startswith(run_one.RESULT_SENTINEL), line
    return json.loads(line[len(run_one.RESULT_SENTINEL):])


class FakeLLM:
    """Repair LLM: returns a canned reply and records the prompts it saw."""

    def __init__(self, reply: str):
        self.reply = reply
        self.prompts: list[str] = []

    def invoke(self, prompt):
        self.prompts.append(prompt)
        return SimpleNamespace(content=self.reply)


# ---------------------------------------------------------------------------
# extract_trade_plan / extract_with_repair
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestExtractTradePlan:
    def test_valid_fenced_block(self):
        plan, error = run_one.extract_trade_plan(
            "Reasoning first.\nFINAL TRANSACTION PROPOSAL: **BUY**\n" + fenced(VALID_PLAN)
        )
        assert error is None
        assert plan["action"] == "BUY"
        assert plan["entry_zone"] == [280.0, 285.0]

    def test_last_fenced_block_wins(self):
        earlier = fenced({"not": "a plan"})
        text = earlier + "\n...\n" + fenced({**VALID_PLAN, "conviction": 0.9})
        plan, error = run_one.extract_trade_plan(text)
        assert error is None
        assert plan["conviction"] == 0.9

    def test_untagged_fence_is_accepted(self):
        plan, error = run_one.extract_trade_plan("```\n" + json.dumps(VALID_PLAN) + "\n```")
        assert error is None
        assert plan["action"] == "BUY"

    def test_bare_json_fallback(self):
        # Repair replies sometimes drop the fence entirely.
        plan, error = run_one.extract_trade_plan(json.dumps(VALID_PLAN))
        assert error is None
        assert plan["action"] == "BUY"

    def test_no_block_is_an_error(self):
        plan, error = run_one.extract_trade_plan("just prose, no JSON anywhere")
        assert plan is None
        assert "no fenced JSON block" in error

    def test_invalid_json_is_an_error(self):
        plan, error = run_one.extract_trade_plan(fenced("{not json"))
        assert plan is None
        assert "not valid JSON" in error

    def test_shape_violation_is_an_error(self):
        plan, error = run_one.extract_trade_plan(fenced({**VALID_PLAN, "action": "LONG"}))
        assert plan is None
        assert "TradePlan shape invalid" in error

    def test_unknown_field_is_a_shape_error(self):
        # Contract models are strict (extra="forbid") for writers.
        plan, error = run_one.extract_trade_plan(fenced({**VALID_PLAN, "surprise": 1}))
        assert plan is None
        assert "TradePlan shape invalid" in error


@pytest.mark.unit
class TestRepairPath:
    def test_no_repair_when_first_parse_succeeds(self):
        llm = FakeLLM("unused")
        plan, repair_used, error = run_one.extract_with_repair(fenced(VALID_PLAN), llm)
        assert (repair_used, error) == (False, None)
        assert plan["action"] == "BUY"
        assert llm.prompts == []

    def test_repair_reprompt_recovers(self):
        llm = FakeLLM(fenced(VALID_PLAN))
        plan, repair_used, error = run_one.extract_with_repair("no JSON here", llm)
        assert (repair_used, error) == (True, None)
        assert plan["action"] == "BUY"
        # The repair prompt carries the parse error and the previous output.
        assert len(llm.prompts) == 1
        assert "no fenced JSON block" in llm.prompts[0]
        assert "no JSON here" in llm.prompts[0]

    def test_repair_failure_is_final(self):
        llm = FakeLLM("still not json")
        plan, repair_used, error = run_one.extract_with_repair("prose only", llm)
        assert plan is None
        assert repair_used is True
        assert "after repair" in error

    def test_repair_llm_crash_never_raises(self):
        class Boom:
            def invoke(self, prompt):
                raise RuntimeError("provider down")

        plan, repair_used, error = run_one.extract_with_repair("prose only", Boom())
        assert plan is None
        assert repair_used is True
        assert "repair-reprompt failed" in error


@pytest.mark.unit
def test_normalize_decision():
    assert run_one.normalize_decision("FINAL: **BUY**", None) == "BUY"
    assert run_one.normalize_decision("sell", None) == "SELL"
    # Earliest token wins on ambiguity.
    assert run_one.normalize_decision("HOLD (not BUY)", None) == "HOLD"
    # Unrecognizable signal falls back to the plan action, then HOLD.
    assert run_one.normalize_decision("¯\\_(ツ)_/¯", {"action": "SELL"}) == "SELL"
    assert run_one.normalize_decision(None, None) == "HOLD"


# ---------------------------------------------------------------------------
# argv/env contract
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_apply_run_env_arm_bundle_and_isolation(tmp_path):
    env = {"TRADINGAGENTS_STATE_DIR": str(tmp_path / "state")}
    brief = run_one.apply_run_env("brief", env=env)
    assert env["TRADINGAGENTS_MACRO_SOURCE"] == "brief"
    assert env["TRADINGAGENTS_TICKER_SOURCE"] == "brief"
    assert "/analysis/brief/" in env["TRADINGAGENTS_RESULTS_DIR"]
    assert env["TRADINGAGENTS_MEMORY_LOG_PATH"].endswith("trading_memory.md")

    feeds_env = {"TRADINGAGENTS_STATE_DIR": str(tmp_path / "state")}
    feeds = run_one.apply_run_env("feeds", env=feeds_env)
    assert feeds_env["TRADINGAGENTS_MACRO_SOURCE"] == "feeds"
    assert feeds_env["TRADINGAGENTS_TICKER_SOURCE"] == "feeds"
    # R1 arm isolation: results and memory paths never collide across arms.
    assert brief["TRADINGAGENTS_RESULTS_DIR"] != feeds["TRADINGAGENTS_RESULTS_DIR"]
    assert brief["TRADINGAGENTS_MEMORY_LOG_PATH"] != feeds["TRADINGAGENTS_MEMORY_LOG_PATH"]


@pytest.mark.unit
def test_apply_run_env_overrides_inherited_values(tmp_path):
    # Arm identity is normative: a stray inherited switch cannot veto it.
    env = {
        "TRADINGAGENTS_STATE_DIR": str(tmp_path / "state"),
        "TRADINGAGENTS_MACRO_SOURCE": "feeds",
        "TRADINGAGENTS_TICKER_SOURCE": "feeds",
    }
    run_one.apply_run_env("brief", env=env)
    assert env["TRADINGAGENTS_MACRO_SOURCE"] == "brief"
    assert env["TRADINGAGENTS_TICKER_SOURCE"] == "brief"


@pytest.mark.unit
def test_apply_run_env_withhold_points_at_empty_dir(tmp_path):
    env = {"TRADINGAGENTS_TICKER_BRIEF_DIR": str(tmp_path / "real_briefs"),
           "TRADINGAGENTS_STATE_DIR": str(tmp_path / "state")}
    run_one.apply_run_env("brief", withhold_ticker_brief=True, env=env)
    withheld = env["TRADINGAGENTS_TICKER_BRIEF_DIR"]
    assert withheld != str(tmp_path / "real_briefs")
    import os

    assert os.path.isdir(withheld)
    assert os.listdir(withheld) == []  # DATA_UNAVAILABLE degrade, not a crash


# ---------------------------------------------------------------------------
# main(): stdout discipline + result payload + crash exit
# ---------------------------------------------------------------------------


class FakeGraph:
    def __init__(self, trader_text: str, signal: str = "BUY", crash: bool = False):
        self.trader_text = trader_text
        self.signal = signal
        self.crash = crash
        self.quick_thinking_llm = FakeLLM(fenced(VALID_PLAN))
        self.calls: list[tuple] = []

    def propagate(self, ticker, trade_date, asset_type="stock"):
        if self.crash:
            raise RuntimeError("LLM exploded after retries")
        print("graph noise on stdout")  # must NOT reach the parent's stdout
        self.calls.append((ticker, trade_date, asset_type))
        return {"trader_investment_plan": self.trader_text}, self.signal

    def save_reports(self, final_state, ticker):
        return f"/reports/{ticker}"


@pytest.fixture
def child_env(tmp_path, monkeypatch):
    """Pre-seed every env key main() writes so monkeypatch restores them, and
    keep the repo .env out of the test process."""
    monkeypatch.setenv("TRADINGAGENTS_STATE_DIR", str(tmp_path / "state"))
    for key in (
        "TRADINGAGENTS_MACRO_SOURCE",
        "TRADINGAGENTS_TICKER_SOURCE",
        "TRADINGAGENTS_RESULTS_DIR",
        "TRADINGAGENTS_MEMORY_LOG_PATH",
        "TRADINGAGENTS_TICKER_BRIEF_DIR",
    ):
        monkeypatch.setenv(key, "sentinel")
    monkeypatch.setattr(run_one, "load_env_file", lambda *a, **k: None)


@pytest.mark.unit
def test_main_emits_exactly_one_json_line(child_env, capsys):
    graph = FakeGraph("thinking...\n" + fenced(VALID_PLAN), signal="**BUY**")
    factories = []

    def factory(position_context, debug):
        factories.append((position_context, debug))
        return graph

    rc = run_one.main(
        [
            "--ticker", "NVDA", "--date", "2026-08-07", "--session", "us",
            "--arm", "brief", "--position-context", "- qty: 12",
        ],
        graph_factory=factory,
    )
    captured = capsys.readouterr()
    assert rc == 0
    out_lines = [line for line in captured.out.splitlines() if line.strip()]
    assert len(out_lines) == 1  # stdout discipline: ONE machine-readable line
    assert "graph noise" not in captured.out
    assert "graph noise" in captured.err
    result = parse_result_line(out_lines[0])
    assert result["ticker"] == "NVDA"
    assert result["arm"] == "brief"
    assert result["decision"] == "BUY"
    assert result["plan"]["action"] == "BUY"
    assert result["repair_used"] is False
    assert result["report_dir"] == "/reports/NVDA"
    # The graph factory received the position context; propagate saw the run.
    assert factories == [("- qty: 12", False)]
    assert graph.calls == [("NVDA", "2026-08-07", "stock")]


@pytest.mark.unit
def test_main_repair_path_reports_repair_used(child_env, capsys):
    graph = FakeGraph("prose without any JSON block", signal="hold")
    rc = run_one.main(
        ["--ticker", "AAA", "--date", "2026-08-07", "--session", "us", "--arm", "feeds"],
        graph_factory=lambda ctx, debug: graph,
    )
    assert rc == 0
    result = parse_result_line(capsys.readouterr().out.strip())
    # The repair LLM (the trader's own quick LLM) returned a valid plan.
    assert result["repair_used"] is True
    assert result["plan"]["action"] == "BUY"
    assert result["plan_error"] is None
    assert graph.quick_thinking_llm.prompts  # repair-reprompt actually fired


@pytest.mark.unit
def test_main_plan_failure_is_not_an_exit_failure(child_env, capsys):
    graph = FakeGraph("prose only", signal="BUY")
    graph.quick_thinking_llm = FakeLLM("still prose")  # repair fails too
    rc = run_one.main(
        ["--ticker", "AAA", "--date", "2026-08-07", "--session", "us", "--arm", "brief"],
        graph_factory=lambda ctx, debug: graph,
    )
    assert rc == 0  # the decision stands as a directional opinion
    result = parse_result_line(capsys.readouterr().out.strip())
    assert result["plan"] is None
    assert result["decision"] == "BUY"
    assert "after repair" in result["plan_error"]


@pytest.mark.unit
def test_main_crash_exits_nonzero_with_no_stdout_line(child_env, capsys):
    graph = FakeGraph("", crash=True)
    rc = run_one.main(
        ["--ticker", "AAA", "--date", "2026-08-07", "--session", "us", "--arm", "brief"],
        graph_factory=lambda ctx, debug: graph,
    )
    captured = capsys.readouterr()
    assert rc == 1  # parent records decision: "ERROR" (runner R3)
    assert captured.out.strip() == ""
    assert "LLM exploded" in captured.err


@pytest.mark.unit
def test_main_crypto_asset_type_detection(child_env, capsys):
    graph = FakeGraph(fenced(VALID_PLAN), signal="BUY")
    rc = run_one.main(
        ["--ticker", "BTC-USD", "--date", "2026-08-07", "--session", "us", "--arm", "brief"],
        graph_factory=lambda ctx, debug: graph,
    )
    assert rc == 0
    assert graph.calls == [("BTC-USD", "2026-08-07", "crypto")]
    capsys.readouterr()
