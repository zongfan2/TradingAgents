"""Research Manager model and ticker search cap — specs/llm-cost.md R9/R10."""

import json
from datetime import date

import pytest

# ---------------------------------------------------------------------------
# R9 — the Research Manager judges on its own model; the PM keeps the deep one
# ---------------------------------------------------------------------------


class _FakeClient:
    def __init__(self, model, kwargs):
        self.llm = ("llm", model, tuple(sorted(kwargs.items())))

    def get_llm(self):
        return self.llm


def _build_graph(monkeypatch, tmp_path, **overrides):
    import tradingagents.graph.setup as setup_mod
    import tradingagents.graph.trading_graph as tg
    from tradingagents.default_config import DEFAULT_CONFIG

    monkeypatch.setattr(tg, "create_llm_client",
                        lambda provider, model, base_url=None, **kw: _FakeClient(model, kw))
    judges = {}
    real_rm, real_pm = setup_mod.create_research_manager, setup_mod.create_portfolio_manager
    monkeypatch.setattr(setup_mod, "create_research_manager",
                        lambda llm: judges.setdefault("rm", llm) and real_rm(llm))
    monkeypatch.setattr(setup_mod, "create_portfolio_manager",
                        lambda llm: judges.setdefault("pm", llm) and real_pm(llm))
    config = {
        **DEFAULT_CONFIG,
        "llm_provider": "anthropic",
        "deep_think_llm": "claude-opus-5-5",
        "quick_think_llm": "claude-haiku-5-5",
        "anthropic_deep_effort": "medium",
        "anthropic_quick_effort": "low",
        "results_dir": str(tmp_path / "logs"),
        "data_cache_dir": str(tmp_path / "cache"),
        "memory_log_path": str(tmp_path / "memory.md"),
        "checkpoint_enabled": False,
        **overrides,
    }
    graph = tg.TradingAgentsGraph(selected_analysts=["market"], config=config)
    return graph, judges


@pytest.mark.unit
def test_research_manager_gets_its_own_model_with_deep_knobs(monkeypatch, tmp_path):
    graph, judges = _build_graph(monkeypatch, tmp_path, research_manager_llm="claude-sonnet-5-5")
    assert judges["rm"] == ("llm", "claude-sonnet-5-5", (("effort", "medium"),))
    assert judges["pm"] == ("llm", "claude-opus-5-5", (("effort", "medium"),))
    assert graph.deep_thinking_llm == judges["pm"]


@pytest.mark.unit
@pytest.mark.parametrize("rm_model", [None, "", "claude-opus-5-5"])
def test_research_manager_defaults_to_the_deep_model(monkeypatch, tmp_path, rm_model):
    graph, judges = _build_graph(monkeypatch, tmp_path, research_manager_llm=rm_model)
    assert judges["rm"] is judges["pm"] is graph.deep_thinking_llm


@pytest.mark.unit
def test_profiles_and_digest_carry_the_research_manager_model():
    import pipeline.analysis_runner as ar
    from pipeline.llm_routing import CLAUDE, DEEPSEEK
    from tradingagents.default_config import _apply_env_overrides

    assert CLAUDE.env["TRADINGAGENTS_RESEARCH_MANAGER_LLM"] == "claude-sonnet-5-5"
    assert CLAUDE.env["TRADINGAGENTS_DEEP_THINK_LLM"] == "claude-opus-5-5"
    # DeepSeek clears it, so a stray .env value can never send its RM to Claude.
    assert DEEPSEEK.env["TRADINGAGENTS_RESEARCH_MANAGER_LLM"] == ""
    assert ar.effective_analysis_config(CLAUDE.env)["graph"]["research_manager_llm"] == "claude-sonnet-5-5"
    assert ar.effective_analysis_config(DEEPSEEK.env)["graph"]["research_manager_llm"] is None
    config = _apply_env_overrides({"research_manager_llm": None},
                                  {"TRADINGAGENTS_RESEARCH_MANAGER_LLM": "claude-sonnet-5-5"})
    assert config["research_manager_llm"] == "claude-sonnet-5-5"


# ---------------------------------------------------------------------------
# R10 — per-brief web-search cap (env switch) and search counts in the ledger
# ---------------------------------------------------------------------------

_ORIGINAL_PROTOCOL = (
    "Work section by section. For each of the five sections below, run at least two\n"
    "distinct web searches with different angles before writing. Emphasize the\n"
)


def _render(max_searches=None):
    from pipeline.ticker_collector import render_ticker_prompt

    return render_ticker_prompt(date(2026, 11, 2), "AAPL", "us", "claude", "seed",
                                max_searches=max_searches)


@pytest.mark.unit
def test_uncapped_prompt_keeps_the_original_protocol():
    assert _ORIGINAL_PROTOCOL in _render()


@pytest.mark.unit
def test_capped_prompt_states_the_cap():
    text = _render(8)
    assert "Run at most 8 web searches in total for this brief" in text
    assert "at least two" not in text
    assert "{{" not in text


@pytest.mark.unit
@pytest.mark.parametrize(("raw", "cap"), [(None, None), ("", None), (" ", None), ("0", None),
                                          ("8", 8), (" 12 ", 12), ("5", 5)])
def test_collect_max_searches_parses(raw, cap):
    from pipeline.config import COLLECT_MAX_SEARCHES_ENV, collect_max_searches

    env = {} if raw is None else {COLLECT_MAX_SEARCHES_ENV: raw}
    assert collect_max_searches(env) == cap


@pytest.mark.unit
@pytest.mark.parametrize("raw", ["eight", "8.5", "4", "-1"])
def test_collect_max_searches_is_loud_on_junk(raw):
    from pipeline.config import COLLECT_MAX_SEARCHES_ENV, collect_max_searches

    with pytest.raises(ValueError, match=COLLECT_MAX_SEARCHES_ENV):
        collect_max_searches({COLLECT_MAX_SEARCHES_ENV: raw})


@pytest.mark.unit
def test_collector_passes_the_cap_to_every_job(monkeypatch, tmp_path):
    import pipeline.ticker_collector as tc

    seen = []
    monkeypatch.setenv("TRADINGAGENTS_COLLECT_MAX_SEARCHES", "8")
    monkeypatch.setattr(tc, "render_ticker_prompt",
                        lambda *a, max_searches=None, **k: seen.append(max_searches) or "prompt")
    monkeypatch.setattr(tc, "_generate_validated",
                        lambda *a, **k: (_ for _ in ()).throw(tc.CollectorError("stop")))
    jobs = [tc.TickerJob("AAPL"), tc.TickerJob("MSFT", "earnings", "dated print")]
    monkeypatch.setattr(tc, "resolve_jobs", lambda *a, **k: (jobs, None))
    monkeypatch.setattr(tc, "_emit_collect_funnel", lambda *a, **k: None)
    summary = tc.collect_all("us", "2026-11-02", backend="claude", runner=lambda *a, **k: "",
                             brief_dir=tmp_path / "briefs", pool_dir=tmp_path / "pool")
    assert seen == [8, 8]
    assert [o.outcome for o in summary.outcomes] == ["failed", "failed"]


@pytest.mark.unit
def test_collector_rejects_a_junk_cap_before_any_job(monkeypatch, tmp_path):
    import pipeline.ticker_collector as tc

    monkeypatch.setenv("TRADINGAGENTS_COLLECT_MAX_SEARCHES", "lots")
    monkeypatch.setattr(tc, "resolve_jobs", lambda *a, **k: pytest.fail("resolved jobs"))
    with pytest.raises(tc.CollectorError, match="TRADINGAGENTS_COLLECT_MAX_SEARCHES"):
        tc.collect_all("us", "2026-11-02", backend="claude", runner=lambda *a, **k: "",
                       brief_dir=tmp_path / "briefs", pool_dir=tmp_path / "pool")


def _stream(*events):
    # ensure_ascii=False: the CLI (JSON.stringify) leaves U+2028 etc. raw.
    return "\n".join(json.dumps(e, ensure_ascii=False) for e in events) + "\n"


def _tool_use(name, **args):
    return {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": name, "input": args}]}}


def _result(**extra):
    return {"type": "result", "subtype": "success", "is_error": False, "result": "brief",
            "usage": {"input_tokens": 10, "output_tokens": 5,
                      "server_tool_use": {"web_search_requests": 0, "web_fetch_requests": 0}},
            "modelUsage": {"claude-haiku-5-5": {"webSearchRequests": 2, "costUSD": 0.02}},
            "total_cost_usd": 0.02, **extra}


def _usage_line(monkeypatch, tmp_path, envelope, outcome="ok"):
    import pipeline.claude_cli as cc

    log = tmp_path / "claude_usage.jsonl"
    monkeypatch.setattr(cc, "usage_log_path", lambda: log)
    cc.record_usage("ticker_collector", "claude-haiku-5-5", "low", envelope, outcome=outcome)
    return json.loads(log.read_text().splitlines()[-1])


@pytest.mark.unit
def test_stream_envelope_carries_queries_and_billed_searches(monkeypatch, tmp_path):
    import pipeline.claude_cli as cc

    stdout = _stream(
        {"type": "system", "subtype": "init", "tools": ["WebSearch", "WebFetch"]},
        _tool_use("WebSearch", query="AAPL earnings date", mode="standard"),
        _tool_use("WebSearch", query="AAPL news this week"),
        _tool_use("WebFetch", url="https://example.com/a", prompt="summarize"),
        {"type": "user", "message": {"content": [{"type": "tool_result", "content": "x"}]}},
        _result(),
    )
    envelope = cc.parse_envelope(stdout)
    assert cc.result_text(envelope) == "brief"
    line = _usage_line(monkeypatch, tmp_path, envelope)
    assert line["web_search_requests"] == 2  # modelUsage, not usage.server_tool_use
    assert line["web_search_queries"] == ["AAPL earnings date", "AAPL news this week"]
    assert line["web_fetch_urls"] == ["https://example.com/a"]
    assert line["web_fetch_requests"] == 1


@pytest.mark.unit
def test_single_json_envelope_still_parses():
    import pipeline.claude_cli as cc

    envelope = cc.parse_envelope(json.dumps(_result()))
    assert cc.result_text(envelope) == "brief"
    assert envelope["_tool_calls"] == {"web_search_queries": [], "web_fetch_urls": [], "web_fetch_requests": 0}


@pytest.mark.unit
@pytest.mark.parametrize("char", ["\u2028", "\u2029", "\u0085"])  # controls are always escaped
def test_raw_line_separator_characters_do_not_split_events(monkeypatch, tmp_path, char):
    import pipeline.claude_cli as cc

    stdout = _stream(_tool_use("WebSearch", query=f"q{char}x"),
                     {"type": "user", "message": {"content": [{"type": "tool_result", "content": f"web{char}page"}]}},
                     _result(result=f"para1{char}para2"))
    assert char in stdout  # raw, as the CLI writes it
    envelope = cc.parse_envelope(stdout)
    assert cc.result_text(envelope) == f"para1{char}para2"
    line = _usage_line(monkeypatch, tmp_path, envelope)
    assert line["web_search_queries"] == [f"q{char}x"]


@pytest.mark.unit
@pytest.mark.parametrize("stdout", ["", "not json", _stream({"type": "system", "subtype": "init"})])
def test_stream_without_result_is_an_envelope_error(stdout):
    import pipeline.claude_cli as cc

    with pytest.raises(cc.EnvelopeError):
        cc.parse_envelope(stdout)


@pytest.mark.unit
def test_error_stream_is_logged_with_its_queries(monkeypatch, tmp_path):
    import pipeline.claude_cli as cc

    log = tmp_path / "claude_usage.jsonl"
    monkeypatch.setattr(cc, "usage_log_path", lambda: log)
    stdout = _stream(_tool_use("WebSearch", query="q1"),
                     _result(is_error=True, subtype="error_during_execution", result="boom"))
    with pytest.raises(cc.EnvelopeError, match="boom"):
        cc.parse_envelope(stdout)
    cc.record_failed_call("evaluator", "claude-opus-5-5", "medium", stdout)
    line = json.loads(log.read_text().splitlines()[-1])
    assert (line["outcome"], line["web_search_queries"]) == ("error", ["q1"])
    assert json.loads(cc.envelope_line(stdout))["result"] == "boom"
    assert cc.envelope_line("plain codex text") == "plain codex text"
    assert cc.envelope_line(_stream({"type": "system", "subtype": "init"})) == ""


@pytest.mark.unit
def test_tool_calls_are_bounded_and_junk_reads_empty(monkeypatch, tmp_path):
    import pipeline.claude_cli as cc

    stdout = _stream(*[_tool_use("WebSearch", query="x" * 1000) for _ in range(100)],
                     *[_tool_use("WebFetch", url="https://e.com") for _ in range(100)],
                     _tool_use("WebSearch"), {"type": "assistant", "message": "junk"},
                     _result(modelUsage={"m": {"webSearchRequests": "junk"}, "n": "junk"}))
    envelope = cc.parse_envelope(stdout)
    queries = envelope["_tool_calls"]["web_search_queries"]
    assert len(queries) == cc._MAX_TOOL_CALLS and all(len(q) == cc._MAX_TOOL_ARG_CHARS for q in queries)
    assert len(envelope["_tool_calls"]["web_fetch_urls"]) == cc._MAX_TOOL_CALLS
    assert _usage_line(monkeypatch, tmp_path, envelope)["web_fetch_requests"] == 100  # unbounded count
    line = _usage_line(monkeypatch, tmp_path, {**envelope, "_tool_calls": "junk"})
    assert (line["web_search_requests"], line["web_search_queries"], line["web_fetch_requests"]) == (0, [], 0)


def _runner_cases():
    import pipeline.evaluator as ev
    import pipeline.macro_collector as mc
    import pipeline.pool_builder as pb
    import pipeline.ticker_collector as tc

    return [
        (tc, lambda: tc.default_runner("claude", "PROMPT", 60), tc.CollectorError),
        (mc, lambda: mc.default_runner("claude", "PROMPT"), mc.CollectorError),
        (pb, lambda: pb.default_runner("claude", "PROMPT"), pb.NominationError),
        (ev, lambda: ev.claude_runner("PROMPT"), ev.BackendError),
    ]


_INIT = {"type": "system", "subtype": "init", "tools": ["WebSearch"]}


@pytest.mark.unit
@pytest.mark.parametrize("case", range(4), ids=["ticker", "macro", "pool", "evaluator"])
@pytest.mark.parametrize(("events", "expected"), [
    ([_INIT, _result(is_error=True, subtype="error_during_execution", result="boom")], "boom"),
    ([_INIT, _tool_use("WebSearch", query="q")], None),  # cut before its result
], ids=["result", "no-result"])
def test_failed_call_messages_quote_the_result_envelope(monkeypatch, tmp_path, case, events, expected):
    import subprocess

    import pipeline.claude_cli as cc

    monkeypatch.setenv(cc.USAGE_LOG_ENV, str(tmp_path / "usage.jsonl"))
    module, run, error = _runner_cases()[case]
    stdout = _stream(*events)
    monkeypatch.setattr(module.subprocess, "run",
                        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, stdout=stdout, stderr=""))
    with pytest.raises(error) as raised:
        run()
    message = str(raised.value)
    assert '"subtype": "init"' not in message and '"type": "tool_use"' not in message
    if expected is not None:
        assert expected in message


@pytest.mark.unit
def test_a_stream_cut_after_one_event_logs_nothing(monkeypatch, tmp_path):
    import pipeline.claude_cli as cc

    log = tmp_path / "usage.jsonl"
    monkeypatch.setenv(cc.USAGE_LOG_ENV, str(log))
    for event in (_INIT, _tool_use("WebSearch", query="q"), {"type": "user", "message": {}}):
        cc.record_failed_call("ticker_collector", "m", "low", _stream(event))
    assert not log.exists()
    cc.record_failed_call("ticker_collector", "m", "low", json.dumps({"usage": {}, "total_cost_usd": 0.01}))
    assert json.loads(log.read_text())["outcome"] == "error"  # a lone non-event object still logs
