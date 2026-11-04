"""Verified price tape — specs/price-tape.md."""

from datetime import date

import pytest

from pipeline import price_tape as pt

BARS = [(date(2026, 10, d), c) for d, c in ((27, 93.74), (28, 95.53), (29, 92.07), (30, 65.76))] + [
    (date(2026, 11, 2), 59.97), (date(2026, 11, 3), 60.55)]  # INTC, measured


@pytest.mark.unit
def test_rows_stop_before_as_of_and_carry_daily_moves():
    rows = pt.tape_rows(BARS, date(2026, 11, 3))  # the 11-03 brief: no 11-03 bar (partial / look-ahead)
    assert [r[0] for r in rows][-1] == date(2026, 11, 2)
    assert rows[0][2] is None and rows[-1][2] == pytest.approx(-8.80, abs=0.01)


@pytest.mark.unit
def test_render_flags_large_recent_moves_and_states_the_rules():
    text = pt.render_tape("INTC", date(2026, 11, 3), fetch=lambda t: BARS)
    assert "| 2026-11-02 | 59.97 | -8.80% ⚠ |" in text and "| 2026-10-30 | 65.76 | -28.58% ⚠ |" in text
    assert "| 2026-10-28 | 95.53 | +1.91% |" in text
    assert "never contradict them" in text and "driver unverified" in text
    quiet = pt.render_tape("X", date(2026, 11, 3), fetch=lambda t: [(date(2026, 10, d), 100 + d / 10) for d in (27, 28, 29)])
    assert "⚠" not in quiet and "Large recent moves" not in quiet


@pytest.mark.unit
def test_only_the_last_three_sessions_are_flagged():
    bars = [(date(2026, 10, 1), 100.0), (date(2026, 10, 2), 110.0)] + [(date(2026, 10, d), 110.0) for d in range(5, 9)]
    assert "⚠" not in pt.render_tape("X", date(2026, 10, 9), fetch=lambda t: bars)  # +10% is 4 sessions back


@pytest.mark.unit
@pytest.mark.parametrize("fetch", [lambda t: (_ for _ in ()).throw(RuntimeError("down")), lambda t: [], lambda t: BARS[:1]])
def test_unavailable_tape_never_raises(fetch):
    assert pt.render_tape("INTC", date(2026, 11, 3), fetch=fetch) == pt.UNAVAILABLE


@pytest.mark.unit
def test_bad_closes_are_ignored():
    bars = [(date(2026, 10, 27), float("nan")), (date(2026, 10, 28), 0.0), (date(2026, 10, 29), 10.0), (date(2026, 10, 30), 11.0)]
    assert [r[0] for r in pt.tape_rows(bars, date(2026, 11, 2))] == [date(2026, 10, 29), date(2026, 10, 30)]


@pytest.mark.unit
def test_the_collector_prompt_carries_the_tape(monkeypatch):
    from pipeline.ticker_collector import render_ticker_prompt

    text = render_ticker_prompt(date(2026, 11, 3), "INTC", "us", "claude-deep-search", "seed",
                                price_tape=pt.render_tape("INTC", date(2026, 11, 3), fetch=lambda t: BARS))
    assert "# Verified price tape" in text and "-8.80% ⚠" in text and "{{" not in text
    assert pt.UNAVAILABLE in render_ticker_prompt(date(2026, 11, 3), "INTC", "us", "g", "seed")


@pytest.mark.unit
def test_the_evaluator_gets_the_tape_before_as_of_and_the_severity_rule(monkeypatch, tmp_path):
    from pipeline import evaluator as ev

    seen = {}
    monkeypatch.setattr(pt, "fetch_daily_closes", lambda ticker: seen.setdefault("ticker", ticker) and BARS)

    class Meta:
        ticker, as_of_date = "INTC", date(2026, 11, 3)

    from pipeline.contracts.briefs import TickerBriefMeta

    prompt = ev.build_eval_prompt("ticker", TickerBriefMeta.model_construct(
        ticker="INTC", as_of_date=date(2026, 11, 3), catalyst_score=3.0, session="us"),
        "BRIEF", price_tape=pt.render_tape("INTC", date(2026, 11, 3)))
    assert "VERIFIED PRICE TAPE" in prompt and "-8.80% ⚠" in prompt and "2026-11-03 |" not in prompt
    assert "'minor' flag when you verified the claim itself elsewhere" in prompt
    assert "contradicts the tape" in prompt
    assert "VERIFIED PRICE TAPE" not in ev.build_eval_prompt("ticker", TickerBriefMeta.model_construct(
        ticker="INTC", as_of_date=date(2026, 11, 3), catalyst_score=3.0, session="us"), "BRIEF")
