"""Report parity: the shared writer produces the report tree for the CLI and the
programmatic API alike (#1037)."""

import hashlib
import json
from types import SimpleNamespace

import pytest

from tradingagents.dataflows.config import set_config
from tradingagents.graph.trading_graph import TradingAgentsGraph
from tradingagents.reporting import write_report_tree


def _state():
    return {
        "market_report": "MKT",
        "news_report": "NEWS",
        "investment_debate_state": {"judge_decision": "RM PLAN"},
        "trader_investment_plan": "TRADE",
        "risk_debate_state": {"judge_decision": "PM DECISION"},
    }


@pytest.mark.unit
def test_write_report_tree_creates_files(tmp_path):
    out = write_report_tree(_state(), "AAPL", tmp_path)
    assert out.name == "complete_report.md"
    assert (tmp_path / "1_analysts" / "market.md").read_text() == "MKT"
    assert (tmp_path / "1_analysts" / "news.md").read_text() == "NEWS"
    assert (tmp_path / "2_research" / "manager.md").read_text() == "RM PLAN"
    assert (tmp_path / "3_trading" / "trader.md").read_text() == "TRADE"
    assert (tmp_path / "5_portfolio" / "decision.md").read_text() == "PM DECISION"
    complete = out.read_text()
    assert "Trading Analysis Report: AAPL" in complete
    assert "MKT" in complete and "PM DECISION" in complete


@pytest.mark.unit
def test_save_reports_explicit_path(tmp_path):
    # Unbound: with an explicit save_path, the method doesn't touch self/config.
    out = TradingAgentsGraph.save_reports(None, _state(), "AAPL", save_path=tmp_path)
    assert (tmp_path / "complete_report.md").exists()
    assert out == tmp_path / "complete_report.md"


@pytest.mark.unit
def test_save_reports_defaults_under_results_dir(tmp_path):
    mock_self = SimpleNamespace(config={"results_dir": str(tmp_path)})
    out = TradingAgentsGraph.save_reports(mock_self, _state(), "AAPL")
    assert out.exists()
    assert out.parent.parent.name == "reports"  # results_dir/reports/AAPL_<stamp>/...
    assert out.parent.name.startswith("AAPL_")


@pytest.mark.unit
def test_report_header_surfaces_brief_eval_verdicts(tmp_path):
    # specs/pipeline-consumption-v2.md §5: with a brief arm active, the
    # consolidated report header states the served brief's eval verdict,
    # rendered by this existing saving path.
    macro_dir = tmp_path / "macro"
    macro_dir.mkdir()
    body = "macro body"
    (macro_dir / "2026-08-03.us.md").write_text(body, encoding="utf-8")
    (macro_dir / "2026-08-03.us.eval.json").write_text(
        json.dumps({
            "brief_sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
            "verdict": "pass",
        }),
        encoding="utf-8",
    )
    set_config({"macro_source": "brief", "macro_brief_dir": str(macro_dir)})

    state = _state() | {"trade_date": "2026-08-03"}
    out = write_report_tree(state, "AAPL", tmp_path / "reports")
    complete = out.read_text()
    assert "Macro brief eval: pass (2026-08-03.us.md)" in complete
    # The ticker arm stays feeds: no ticker eval line.
    assert "Ticker brief eval" not in complete
    # The verdict lines live in the header, before the first section.
    assert complete.index("Macro brief eval") < complete.index("## I.")


@pytest.mark.unit
def test_report_header_unchanged_on_default_arms(tmp_path):
    # feeds/feeds (the default) consumed no brief: no eval lines appear.
    state = _state() | {"trade_date": "2026-08-03"}
    out = write_report_tree(state, "AAPL", tmp_path)
    assert "brief eval" not in out.read_text()
