"""Decision-funnel event log — specs/decision-funnel.md R1 (the emitter).

Component emitters are covered next to each component's own tests.
"""

import json
import logging

import pytest

from pipeline import funnel_events
from pipeline.funnel_events import FunnelEvents, best_effort, read_events, reason_code


def fixed_clock():
    from datetime import datetime, timezone

    return datetime(2026, 10, 23, 13, 45, tzinfo=timezone.utc)


@pytest.mark.unit
def test_emit_appends_one_schema_line_per_event(tmp_path):
    events = FunnelEvents(tmp_path, "us", "2026-10-23", "analysis_runner", clock=fixed_clock)
    events.emit("trigger", "HUM", "excluded", "below-threshold", "6 < 7", score=6.0)
    events.emit("trigger", "CHRW", "passed", "catalyst", "catalyst_score 7")
    path = tmp_path / "funnel" / "us" / "2026-10-23.events.jsonl"
    lines = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert [line["ticker"] for line in lines] == ["HUM", "CHRW"]
    first = lines[0]
    assert first == {
        "schema": 1,
        "written_at": "2026-10-23T13:45:00Z",
        "session": "us",
        "date": "2026-10-23",
        "component": "analysis_runner",
        "run": events.run,
        "stage": "trigger",
        "ticker": "HUM",
        "outcome": "excluded",
        "code": "below-threshold",
        "reason": "6 < 7",
        "details": {"score": 6.0},
    }
    assert read_events(tmp_path, "us", "2026-10-23") == lines
    # One run id per emitter instance; a rerun gets a new one.
    other = FunnelEvents(tmp_path, "us", "2026-10-23", "analysis_runner", clock=fixed_clock)
    assert other.run != events.run


@pytest.mark.unit
@pytest.mark.parametrize(
    "stage, outcome, code",
    [("bogus", "passed", "core"), ("pool", "maybe", "core"), ("pool", "passed", "nope")],
)
def test_invalid_events_are_dropped_with_a_warning_never_raised(tmp_path, caplog, stage, outcome, code):
    caplog.set_level(logging.WARNING, logger="pipeline.funnel_events")
    events = FunnelEvents(tmp_path, "us", "2026-10-23", "pool_builder")
    events.emit(stage, "AAA", outcome, code)
    assert events.emitted == []
    assert not funnel_events.events_path(tmp_path, "us", "2026-10-23").exists()
    assert "funnel event not recorded" in caplog.text


@pytest.mark.unit
def test_execute_codes_are_open_ended(tmp_path):
    events = FunnelEvents(tmp_path, "us", "2026-10-23", "execution_adapter")
    events.emit("execute", "AAPL", "excluded", "stale-quote", "stale-quote")
    assert [e["code"] for e in events.emitted] == ["stale-quote"]


@pytest.mark.unit
def test_write_failure_is_log_only(tmp_path, caplog):
    caplog.set_level(logging.WARNING, logger="pipeline.funnel_events")
    # The events file path is a directory: the append fails.
    funnel_events.events_path(tmp_path, "us", "2026-10-23").mkdir(parents=True)
    events = FunnelEvents(tmp_path, "us", "2026-10-23", "pool_builder")
    events.emit("pool", "AAA", "passed", "core")  # does not raise
    assert events.emitted == []
    assert "funnel event not recorded" in caplog.text


@pytest.mark.unit
def test_best_effort_swallows_call_site_errors(caplog):
    caplog.set_level(logging.WARNING, logger="pipeline.funnel_events")

    @best_effort
    def broken(events):
        events.emit("pool", "AAA", "passed", "core", reason="x", **{"reason": "clash"})

    assert broken(object()) is None
    assert "funnel events skipped in broken" in caplog.text


@pytest.mark.unit
def test_reason_code_and_unreadable_lines(tmp_path):
    assert reason_code("submit-error: HTTP 422 qty") == "submit-error"
    assert reason_code("stale-quote") == "stale-quote"
    path = funnel_events.events_path(tmp_path, "cn", "2026-10-23")
    path.parent.mkdir(parents=True)
    path.write_text('{"ticker": "A"}\nnot json\n[1]\n', encoding="utf-8")
    assert read_events(tmp_path, "cn", "2026-10-23") == [{"ticker": "A"}]
    assert read_events(tmp_path, "cn", "2026-10-24") == []
