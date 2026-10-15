"""Shared ``claude -p`` plumbing (D20): test isolation of the usage log and
the best-effort failed-call logging."""

import json
import os
from pathlib import Path

import pytest

from pipeline import claude_cli

#: The developer's real home, captured at import — before any fixture
#: sandboxes HOME.
REAL_STATE_DIR = Path(os.path.realpath(Path.home() / ".tradingagents"))


@pytest.mark.unit
def test_suite_routes_the_usage_log_to_the_test_tmp_dir(tmp_path):
    # Review F1 (6187ff7): the redirect must survive the TRADINGAGENTS_* strip
    # regardless of autouse fixture order.
    assert os.environ["TRADINGAGENTS_CLAUDE_USAGE_LOG"] == str(tmp_path / "claude_usage.jsonl")
    resolved = Path(os.path.realpath(claude_cli.usage_log_path()))
    assert resolved == Path(os.path.realpath(tmp_path / "claude_usage.jsonl"))
    assert not resolved.is_relative_to(REAL_STATE_DIR)


@pytest.mark.unit
def test_record_failed_call_ignores_output_that_is_not_an_envelope(tmp_path, monkeypatch):
    log = tmp_path / "usage.jsonl"
    monkeypatch.setenv(claude_cli.USAGE_LOG_ENV, str(log))
    for stdout in ("", "plain text", "[1, 2]", "null"):
        claude_cli.record_failed_call("evaluator", "m", "medium", stdout)
    assert not log.exists()
    claude_cli.record_failed_call("evaluator", "m", "medium", json.dumps({"usage": {}}))
    record = json.loads(log.read_text())
    assert (record["outcome"], record["model"]) == ("error", "m")
