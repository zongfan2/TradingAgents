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
    assert "never contradict them" in text and "driver unverified" in text and "[price tape]" in text
    judge = pt.render_tape("INTC", date(2026, 11, 3), fetch=lambda t: BARS, for_writer=False)
    assert "-8.80% ⚠" in judge and "Large recent moves" in judge
    assert "FIRST searches" not in judge and "never contradict" not in judge  # facts only for the judge
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


@pytest.mark.unit
def test_collect_one_builds_the_tape_for_the_jobs_ticker_and_as_of(monkeypatch, tmp_path):
    import pipeline.ticker_collector as tc

    calls, prompts = [], []
    monkeypatch.setattr(tc.price_tape_module, "render_tape", lambda t, d: calls.append((t, d)) or f"TAPE {t} {d}")
    monkeypatch.setattr(tc, "render_ticker_prompt",
                        lambda *a, price_tape=None, **k: prompts.append(price_tape) or "prompt")
    monkeypatch.setattr(tc, "_generate_validated", lambda *a, **k: (_ for _ in ()).throw(tc.CollectorError("stop")))
    monkeypatch.setattr(tc, "resolve_jobs", lambda *a, **k: ([tc.TickerJob("INTC")], None))
    monkeypatch.setattr(tc, "_emit_collect_funnel", lambda *a, **k: None)
    tc.collect_all("us", "2026-11-03", backend="claude", runner=lambda *a, **k: "",
                   brief_dir=tmp_path / "b", pool_dir=tmp_path / "p")
    assert calls == [("INTC", date(2026, 11, 3))] and prompts == ["TAPE INTC 2026-11-03"]


@pytest.mark.unit
def test_an_unavailable_tape_leaves_the_evaluator_to_its_own_checks(tmp_path, capsys):
    from pipeline import evaluator as ev
    from tests.test_evaluator import FakeRunner, judgment, ticker_text, write_brief

    brief = write_brief(tmp_path / "ticker_briefs" / "NVDA", "2026-08-03.md", ticker_text())
    runner = FakeRunner(judgment())
    assert ev.evaluate_brief(brief, runner=runner) == ev.EXIT_OK  # the offline fixture: no tape
    assert "VERIFIED PRICE TAPE" not in runner.prompts[0] and "do not\nsearch for them" not in runner.prompts[0]
    assert "price tape unavailable for NVDA" in capsys.readouterr().err


@pytest.mark.unit
@pytest.mark.parametrize(("as_of", "available"), [
    (date(2026, 11, 3), True),    # newest 11-02 (Mon): 1 day
    (date(2026, 11, 6), True),    # 4 days after 11-02 (e.g. sessions missing over a holiday)
    (date(2026, 11, 7), False),   # 5 days: the latest sessions are missing
])
def test_without_a_benchmark_the_calendar_rule_applies(as_of, available):
    def fetch(t):
        if t == "SPY":
            raise RuntimeError("benchmark down")
        return BARS[:-1]  # through 11-02

    text = pt.render_tape("INTC", as_of, fetch=fetch)
    assert (text != pt.UNAVAILABLE) is available


@pytest.mark.unit
def test_a_tape_one_session_behind_its_market_is_unavailable():
    spy = BARS + [(date(2026, 11, 4), 600.0)]

    def fetch(t):
        return spy if t == "SPY" else BARS  # INTC's 11-04 bar not published yet

    assert pt.render_tape("INTC", date(2026, 11, 5), fetch=fetch) == pt.UNAVAILABLE
    assert pt.render_tape("INTC", date(2026, 11, 4), fetch=fetch) != pt.UNAVAILABLE  # up to date


@pytest.mark.unit
def test_a_session_listed_without_a_close_is_still_missing_from_the_tape():
    # Measured 2026-11-05 00:28 Shanghai: 000001.SS and 600256.SS both listed
    # 11-04 with a NaN close — the market and the ticker lagging together.
    nan_last = BARS + [(date(2026, 11, 4), float("nan"))]
    assert pt.render_tape("600256.SS", date(2026, 11, 5), fetch=lambda t: nan_last) == pt.UNAVAILABLE
    assert pt.render_tape("SPY", date(2026, 11, 5), fetch=lambda t: nan_last) == pt.UNAVAILABLE  # no benchmark of its own
    assert pt.latest_market_session("INTC", date(2026, 11, 5), fetch=lambda t: nan_last) == date(2026, 11, 4)


@pytest.mark.unit
def test_no_change_is_computed_across_a_gap_inside_the_window():
    bars = [(date(2026, 10, 27), 100.0), (date(2026, 10, 28), float("nan")), (date(2026, 10, 29), 110.0),
            (date(2026, 10, 30), 111.0)]
    text = pt.render_tape("X", date(2026, 11, 2), fetch=lambda t: bars)
    assert "| 2026-10-29 | 110.00 | — |" in text and "+10.00%" not in text and "⚠" not in text
    assert "| 2026-10-30 | 111.00 | +0.91% |" in text


@pytest.mark.unit
def test_benchmarks_follow_the_market():
    assert [pt.benchmark_for(t) for t in ("AAPL", "600256.SS", "300750.SZ", "0700.HK")] == [
        "SPY", "000001.SS", "000001.SS", "^HSI"]


@pytest.mark.unit
def test_the_evaluator_always_accepts_price_tape_citations():
    from pipeline import evaluator as ev
    from pipeline.contracts.briefs import TickerBriefMeta

    prompt = ev.build_eval_prompt("ticker", TickerBriefMeta.model_construct(
        ticker="INTC", as_of_date=date(2026, 11, 3), catalyst_score=3.0, session="us"), "BRIEF")  # no tape
    assert "`[price tape]` is market data" in prompt and "never a missing-URL or fabrication flag" in prompt


@pytest.mark.unit
def test_macro_evaluations_keep_their_citation_rules():
    from pipeline import evaluator as ev
    from pipeline.contracts.briefs import MacroBriefMeta

    prompt = ev.build_eval_prompt("macro", MacroBriefMeta.model_construct(
        as_of_date=date(2026, 11, 3), session="us"), "BRIEF")
    assert "[price tape]" not in prompt and "PRICE TAPE" not in prompt
    assert "a URL that contradicts or does not contain the\n   claim is a 'major' flag" in prompt


@pytest.mark.unit
def test_a_failed_benchmark_read_is_retried_by_the_next_ticker():
    calls = []
    spy = [(date(2026, 10, d), 600.0 + d) for d in (27, 28, 29, 30)]
    intc = [(date(2026, 10, d), 90.0 + d) for d in (27, 28, 29)]  # Friday 10-30 missing

    def fetch(t):
        if t == "SPY":
            calls.append(t)
            if len(calls) == 1:
                raise TimeoutError("transient")
            return spy
        return intc

    monday = date(2026, 11, 2)
    assert pt.render_tape("INTC", monday, fetch=fetch) != pt.UNAVAILABLE  # 4-day fallback lets it through
    assert pt.render_tape("INTC", monday, fetch=fetch) == pt.UNAVAILABLE  # benchmark re-read: Friday missing
    pt.render_tape("INTC", monday, fetch=fetch)
    assert len(calls) == 2  # a session read is cached
