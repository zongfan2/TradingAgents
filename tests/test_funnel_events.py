"""Decision-funnel event log — specs/decision-funnel.md R1 (the emitter).

Component emitters are covered next to each component's own tests.
"""

import json
import logging

import pytest

from pipeline import funnel_events
from pipeline.funnel_events import (
    FunnelEvents,
    best_effort,
    current_outcomes,
    read_events,
    reason_code,
)


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


def _event(run, stage, ticker, outcome, code, **details):
    return {"run": run, "stage": stage, "ticker": ticker, "outcome": outcome,
            "code": code, "reason": code, "details": details}


@pytest.mark.unit
def test_current_outcomes_keeps_both_arms_and_the_strongest_execute_event():
    events = [
        _event("r1", "decide", "NVDA", "passed", "BUY", arm="brief"),
        _event("r1", "decide", "NVDA", "excluded", "HOLD", arm="feeds"),
        _event("x1", "execute", "NVDA", "info", "superseded", arm="brief"),
        _event("x1", "execute", "NVDA", "passed", "submitted", arm="brief"),
        _event("x1", "execute", "NVDA", "excluded", "feeds-arm", arm="feeds"),
        _event("x1", "execute", "TSLA", "info", "cancel", arm="brief"),
        _event("x1", "execute", "TSLA", "passed", "submitted", arm="brief"),
    ]
    current = current_outcomes(events)
    decide = current[("decide", "NVDA")]
    assert (decide["outcome"], decide["code"]) == ("passed", "BUY")
    assert [e["details"]["arm"] for e in decide["events"]] == ["brief", "feeds"]
    assert current[("execute", "NVDA")]["code"] == "submitted"
    assert current[("execute", "TSLA")]["code"] == "submitted"


@pytest.mark.unit
def test_current_outcomes_reruns_supersede_only_what_they_decided():
    events = [
        _event("r1", "trigger", "HUM", "passed", "catalyst"),
        _event("r1", "trigger", "CHRW", "excluded", "below-threshold"),
        # Rerun: HUM already done (info only), CHRW re-decided.
        _event("r2", "trigger", "HUM", "info", "already-done"),
        _event("r2", "trigger", "CHRW", "passed", "catalyst"),
        # Info-only key keeps its info outcome.
        _event("r2", "trigger", "AAA", "info", "already-done"),
    ]
    current = current_outcomes(events)
    hum = current[("trigger", "HUM")]
    assert (hum["outcome"], hum["code"], hum["run"]) == ("passed", "catalyst", "r1")
    assert [e["code"] for e in hum["notes"]] == ["already-done"]
    chrw = current[("trigger", "CHRW")]
    assert (chrw["outcome"], chrw["code"], chrw["run"]) == ("passed", "catalyst", "r2")
    assert [e["code"] for e in chrw["notes"]] == ["below-threshold"]
    assert current[("trigger", "AAA")]["outcome"] == "info"


# ---------------------------------------------------------------------------
# Contract model (specs/README.md rule 5)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_emitted_line_round_trips_through_the_contract_model(tmp_path):
    from pipeline.contracts.funnel import FunnelEvent

    events = FunnelEvents(tmp_path, "us", "2026-10-23", "execution_adapter", clock=fixed_clock)
    events.emit("execute", "NVDA", "passed", "submitted", "submitted (live)", arm="brief")
    (line,) = funnel_events.events_path(tmp_path, "us", "2026-10-23").read_text().splitlines()
    model = FunnelEvent.model_validate(json.loads(line))
    assert (model.stage, model.code, model.details) == ("execute", "submitted", {"arm": "brief"})
    assert model.model_dump(mode="json", by_alias=True)["schema"] == 1


@pytest.mark.unit
def test_contract_model_rejects_unknown_fields_and_codes():
    from pydantic import ValidationError

    from pipeline.contracts.funnel import FunnelEvent

    base = {
        "schema": 1, "written_at": "2026-10-23T13:45:00Z", "session": "us",
        "date": "2026-10-23", "component": "analysis_runner", "run": "r1",
        "stage": "trigger", "ticker": "HUM", "outcome": "excluded",
        "code": "below-threshold", "reason": "6 < 7", "details": {},
    }
    FunnelEvent.model_validate(base)
    with pytest.raises(ValidationError, match="extra"):
        FunnelEvent.model_validate({**base, "surprise": 1})
    with pytest.raises(ValidationError, match="unknown trigger code"):
        FunnelEvent.model_validate({**base, "code": "made-up"})
    with pytest.raises(ValidationError, match="written_at"):
        FunnelEvent.model_validate({**base, "written_at": "2026-10-23T13:45:00"})
    # Readers drop unknown fields instead of failing.
    lenient = FunnelEvent.parse_lenient({**base, "surprise": 1})
    assert lenient.code == "below-threshold"
