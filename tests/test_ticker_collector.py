"""Ticker collector behavior (specs/ticker-brief-collector.md R1–R7, AC1–AC4).

Fully offline: the backend subprocess boundary (``claude -p`` / ``codex exec``)
is exercised only through injected fakes or a monkeypatched ``subprocess.run``;
pool files and ``core.<session>.yaml`` are tmp-path fixtures.
"""

import json
import re
import subprocess
import sys
import threading
import time
from datetime import date
from pathlib import Path

import pytest

from pipeline import ticker_collector
from pipeline.common import TemplateError
from pipeline.contracts import briefs
from pipeline.ticker_collector import (
    NO_SEED_BLOCK,
    SEED_HEADER,
    CollectorError,
    NoTickersError,
    collect_all,
    main,
    render_ticker_prompt,
)

DATE = "2026-08-08"
AS_OF = date(2026, 8, 8)

# ---------------------------------------------------------------------------
# Fixtures & fakes
# ---------------------------------------------------------------------------


def make_ticker_brief(
    *,
    ticker="NVDA",
    date_str=DATE,
    session="us",
    generator="claude-deep-search",
    generated_at="2026-08-08T12:40:00Z",
    n_urls=6,
    sources_count=None,
    catalyst_score=7.5,
    catalyst_type="earnings",
    catalyst_window="2026-08-27",
    words_per_section=90,
    titles=briefs.TICKER_SECTIONS,
):
    """A contract-valid ticker brief (or an invalid one via the knobs)."""
    if sources_count is None:
        sources_count = n_urls
    citations = " ".join(f"[Src](https://example.com/{ticker}/src{i})" for i in range(n_urls))
    blocks = []
    for i, title in enumerate(titles):
        content = ("filler " * words_per_section).strip()
        if i == 0:
            content += " " + citations
        blocks.append(f"## {title}\n\n{content}\n")
    return (
        "---\n"
        f"as_of_date: {date_str}\n"
        f"ticker: {ticker}\n"
        f"session: {session}\n"
        f"generated_at: {generated_at}\n"
        f"generator: {generator}\n"
        f"sources_count: {sources_count}\n"
        f"catalyst_score: {catalyst_score}\n"
        f"catalyst_type: {catalyst_type}\n"
        f"catalyst_window: {catalyst_window}\n"
        "---\n\n"
        + "\n".join(blocks)
        + "\n**Impact**: bullish — strong dated catalyst inside two weeks.\n"
    )


def write_pool(
    pool_dir,
    *,
    date_str=DATE,
    session="us",
    core=("NVDA",),
    opportunity=(("AVGO", "earnings", "AI capex ramp into the Sep 4 print"),),
):
    """A contract-valid pool file for the session/date."""
    payload = {
        "as_of_date": date_str,
        "session": session,
        "generated_at": f"{date_str}T12:31:00Z",
        "generator": "claude-deep-search",
        "core": [{"ticker": t, "note": "holding"} for t in core],
        "opportunity": [
            {
                "ticker": t,
                "score": 7.5,
                "catalyst_type": ct,
                "rationale": rationale,
                "citations": ["https://example.com/nomination"],
                "entered_on": date_str,
                "low_score_streak": 0,
                "gate_fail_streak": 0,
                "technical": {"gate": "pass"},
            }
            for (t, ct, rationale) in opportunity
        ],
        "watch": [],
        "removed": [],
    }
    path = Path(pool_dir) / session / f"{date_str}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


class FakeBackend:
    """Injected in place of the backend subprocess: replays queued per-ticker
    outputs (an Exception instance raises) and tracks max in-flight calls so
    the concurrency cap is observable (AC1)."""

    def __init__(self, outputs, delay=0.0):
        self.outputs = {ticker: list(seq) for ticker, seq in outputs.items()}
        self.delay = delay
        self.calls = []  # (backend, ticker, prompt)
        self.in_flight = 0
        self.max_in_flight = 0
        self._lock = threading.Lock()

    def prompts_for(self, ticker):
        return [prompt for _backend, t, prompt in self.calls if t == ticker]

    def __call__(self, backend, prompt):
        ticker = re.search(r"^ticker: (\S+)$", prompt, re.MULTILINE).group(1)
        with self._lock:
            self.calls.append((backend, ticker, prompt))
            self.in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            if self.delay:
                time.sleep(self.delay)
            result = self.outputs[ticker].pop(0)
            if isinstance(result, BaseException):
                raise result
            return result
        finally:
            with self._lock:
                self.in_flight -= 1


@pytest.fixture()
def dirs(tmp_path, monkeypatch):
    brief_dir = tmp_path / "ticker_briefs"
    pool_dir = tmp_path / "pools"
    monkeypatch.setenv("TRADINGAGENTS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("TRADINGAGENTS_TICKER_BRIEF_DIR", str(brief_dir))
    monkeypatch.setenv("TRADINGAGENTS_POOL_DIR", str(pool_dir))
    monkeypatch.delenv(ticker_collector.CONCURRENCY_ENV, raising=False)
    monkeypatch.delenv("TRADINGAGENTS_POOL_MAX_STALENESS_DAYS", raising=False)
    # These tests' fixtures are claude-flavored; the shipped default backend
    # is codex per D19, so pin the config env (the resolution itself is
    # covered by the dedicated default-backend test below).
    monkeypatch.setenv("TRADINGAGENTS_COLLECT_BACKEND", "claude")
    return brief_dir, pool_dir


def collector_log(brief_dir):
    return (brief_dir / "collector.log").read_text(encoding="utf-8")


def all_files(root):
    return sorted(str(p.relative_to(root)) for p in Path(root).rglob("*") if p.is_file())


# ---------------------------------------------------------------------------
# Fan-out over the pool (R1/R2/R3/R7, AC1)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_fanout_writes_briefs_for_core_and_opportunity(dirs):
    brief_dir, pool_dir = dirs
    write_pool(pool_dir)
    fake = FakeBackend(
        {"NVDA": [make_ticker_brief(ticker="NVDA")], "AVGO": [make_ticker_brief(ticker="AVGO")]}
    )
    summary = collect_all("us", as_of=AS_OF, runner=fake)
    assert summary.as_dict() == {
        "date": DATE,
        "session": "us",
        "requested": 2,
        "written": 2,
        "skipped": 0,
        "failed": [],
    }
    for ticker in ("NVDA", "AVGO"):
        path = brief_dir / ticker / f"{DATE}.md"
        meta, _body = briefs.parse_ticker_brief(path.read_text(encoding="utf-8"))
        briefs.validate_ticker_brief_path(path, meta)  # contract-valid on disk
        assert f"{DATE} | us | {ticker} | 6 | 7.5 | written" in collector_log(brief_dir)


@pytest.mark.unit
def test_prompt_carries_seed_for_opportunity_and_no_seed_for_core(dirs):
    _brief_dir, pool_dir = dirs
    write_pool(pool_dir)
    fake = FakeBackend(
        {"NVDA": [make_ticker_brief(ticker="NVDA")], "AVGO": [make_ticker_brief(ticker="AVGO")]}
    )
    collect_all("us", as_of=AS_OF, runner=fake)
    core_prompt = fake.prompts_for("NVDA")[0]
    opp_prompt = fake.prompts_for("AVGO")[0]
    assert NO_SEED_BLOCK in core_prompt and SEED_HEADER not in core_prompt
    assert SEED_HEADER in opp_prompt
    assert "- catalyst_type: earnings" in opp_prompt
    assert "- rationale: AI capex ramp into the Sep 4 print" in opp_prompt
    for prompt in (core_prompt, opp_prompt):
        assert f"as_of_date: {DATE}" in prompt  # {{DATE}} rendered (R2)
        assert "generator: claude-deep-search" in prompt


@pytest.mark.unit
def test_concurrency_cap_is_respected(dirs):
    _brief_dir, pool_dir = dirs
    tickers = ("ALFA", "BRVO", "CHRL", "DLTA", "ECHO", "FXTR")
    write_pool(pool_dir, core=tickers, opportunity=())
    fake = FakeBackend(
        {t: [make_ticker_brief(ticker=t)] for t in tickers}, delay=0.05
    )
    summary = collect_all("us", as_of=AS_OF, runner=fake, concurrency=2)
    assert summary.written == len(tickers)
    assert fake.max_in_flight == 2  # capped AND actually parallel


@pytest.mark.unit
def test_concurrency_env_override(dirs, monkeypatch):
    _brief_dir, pool_dir = dirs
    tickers = ("ALFA", "BRVO", "CHRL")
    write_pool(pool_dir, core=tickers, opportunity=())
    monkeypatch.setenv(ticker_collector.CONCURRENCY_ENV, "1")
    fake = FakeBackend({t: [make_ticker_brief(ticker=t)] for t in tickers}, delay=0.02)
    collect_all("us", as_of=AS_OF, runner=fake)
    assert fake.max_in_flight == 1


@pytest.mark.unit
def test_failing_ticker_is_isolated_from_the_rest(dirs):
    brief_dir, pool_dir = dirs
    write_pool(pool_dir)
    fake = FakeBackend(
        {"NVDA": [RuntimeError("backend crashed")], "AVGO": [make_ticker_brief(ticker="AVGO")]}
    )
    summary = collect_all("us", as_of=AS_OF, runner=fake)
    assert summary.written == 1
    assert summary.as_dict()["failed"] == [{"ticker": "NVDA", "reason": "backend crashed"}]
    assert (brief_dir / "AVGO" / f"{DATE}.md").exists()
    assert not (brief_dir / "NVDA").exists()
    assert f"{DATE} | us | NVDA | - | - | failed" in collector_log(brief_dir)


@pytest.mark.unit
def test_default_date_resolves_in_session_timezone(dirs, monkeypatch):
    _brief_dir, pool_dir = dirs
    write_pool(pool_dir, opportunity=())
    seen = {}

    def fake_session_date(session):
        seen["session"] = session
        return AS_OF

    monkeypatch.setattr(ticker_collector, "session_date", fake_session_date)
    summary = collect_all("us", runner=FakeBackend({"NVDA": [make_ticker_brief()]}))
    assert seen["session"] == "us"
    assert summary.date == AS_OF


# ---------------------------------------------------------------------------
# Failure never leaves a partial file (R5, AC2)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_runner_crash_mid_run_leaves_no_partial_files(dirs):
    brief_dir, pool_dir = dirs
    write_pool(pool_dir, core=("NVDA",), opportunity=())
    summary = collect_all(
        "us", as_of=AS_OF, runner=FakeBackend({"NVDA": [RuntimeError("killed mid-search")]})
    )
    assert summary.written == 0 and len(summary.failed) == 1
    # No temp files, no partial brief, no stray ticker directory.
    assert all_files(brief_dir) == ["collector.log"]


@pytest.mark.unit
def test_invalid_brief_never_written(dirs):
    brief_dir, pool_dir = dirs
    write_pool(pool_dir, core=("NVDA",), opportunity=())
    missing_section = make_ticker_brief(titles=tuple(briefs.TICKER_SECTIONS[:-1]))
    summary = collect_all(
        "us", as_of=AS_OF, runner=FakeBackend({"NVDA": [missing_section, missing_section]})
    )
    assert [o.ticker for o in summary.failed] == ["NVDA"]
    assert "validation failed after retry" in summary.failed[0].reason
    assert all_files(brief_dir) == ["collector.log"]


# ---------------------------------------------------------------------------
# Validation + single retry with errors appended (R4, AC3)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_ticker_directory_mismatch_rejected_with_exactly_one_retry(dirs):
    brief_dir, pool_dir = dirs
    write_pool(pool_dir)
    wrong = make_ticker_brief(ticker="AMD")  # frontmatter ticker ≠ NVDA directory
    fake = FakeBackend(
        {"NVDA": [wrong, wrong], "AVGO": [make_ticker_brief(ticker="AVGO")]}
    )
    summary = collect_all("us", as_of=AS_OF, runner=fake)
    prompts = fake.prompts_for("NVDA")
    assert len(prompts) == 2  # exactly one retry (AC3)
    assert prompts[1].startswith(prompts[0])
    assert "does not match frontmatter ticker 'AMD'" in prompts[1]  # errors fed back
    assert [o.ticker for o in summary.failed] == ["NVDA"]
    assert summary.written == 1  # the healthy ticker still lands (AC1)
    assert not (brief_dir / "NVDA" / f"{DATE}.md").exists()


@pytest.mark.unit
def test_retry_with_valid_brief_recovers(dirs):
    brief_dir, pool_dir = dirs
    write_pool(pool_dir, core=("NVDA",), opportunity=())
    bad = make_ticker_brief(sources_count=99)  # sources_count ≠ body count
    fake = FakeBackend({"NVDA": [bad, make_ticker_brief()]})
    summary = collect_all("us", as_of=AS_OF, runner=fake)
    assert summary.written == 1
    assert summary.outcomes[0].attempts == 2
    assert (brief_dir / "NVDA" / f"{DATE}.md").read_text(encoding="utf-8") == make_ticker_brief()


# ---------------------------------------------------------------------------
# Idempotence and --force (R5, AC4)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_existing_brief_skips_without_invoking_backend(dirs):
    brief_dir, pool_dir = dirs
    write_pool(pool_dir, core=("NVDA",), opportunity=())
    collect_all("us", as_of=AS_OF, runner=FakeBackend({"NVDA": [make_ticker_brief()]}))

    second = FakeBackend({"NVDA": []})
    summary = collect_all("us", as_of=AS_OF, runner=second)
    assert second.calls == []  # backend never invoked (R5)
    assert summary.as_dict()["skipped"] == 1 and summary.written == 0
    assert summary.success  # all-skipped is success (R6)
    assert f"{DATE} | us | NVDA | - | - | skipped" in collector_log(brief_dir)


@pytest.mark.unit
def test_force_overwrites_and_archives_previous_revision(dirs):
    brief_dir, pool_dir = dirs
    write_pool(pool_dir, core=("NVDA",), opportunity=())
    first = make_ticker_brief(generated_at="2026-08-08T11:00:00Z")
    second = make_ticker_brief(generated_at="2026-08-08T12:40:00Z")
    collect_all("us", as_of=AS_OF, runner=FakeBackend({"NVDA": [first]}))

    summary = collect_all("us", as_of=AS_OF, force=True, runner=FakeBackend({"NVDA": [second]}))
    assert summary.written == 1
    target = brief_dir / "NVDA" / f"{DATE}.md"
    assert target.read_text(encoding="utf-8") == second
    archived = brief_dir / "NVDA" / "archive" / f"{DATE}.2026-08-08T11:00:00Z.md"
    assert archived.read_text(encoding="utf-8") == first  # revision preserved


# ---------------------------------------------------------------------------
# Pool reading rule + core-yaml fallback (R1)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_stale_pool_within_cap_warns_and_still_collects(dirs, caplog):
    _brief_dir, pool_dir = dirs
    write_pool(pool_dir, date_str="2026-08-06")  # gap 2 ≤ default cap 3
    fake = FakeBackend(
        {"NVDA": [make_ticker_brief(ticker="NVDA")], "AVGO": [make_ticker_brief(ticker="AVGO")]}
    )
    with caplog.at_level("WARNING", logger="pipeline.ticker_collector"):
        summary = collect_all("us", as_of=AS_OF, runner=fake)
    assert summary.written == 2  # core AND opportunity still collected
    assert "STALE POOL" in caplog.text


@pytest.mark.unit
def test_pool_beyond_staleness_cap_falls_back_to_core_yaml(dirs):
    _brief_dir, pool_dir = dirs
    write_pool(pool_dir, date_str="2026-08-01")  # gap 7 > cap 3 ⇒ absent
    pool_dir.mkdir(parents=True, exist_ok=True)
    (pool_dir / "core.us.yaml").write_text(
        "- ticker: NVDA\n  note: holding\n", encoding="utf-8"
    )
    fake = FakeBackend({"NVDA": [make_ticker_brief(ticker="NVDA")]})
    summary = collect_all("us", as_of=AS_OF, runner=fake)
    # Opportunity layer dropped; core coverage survives via the yaml (R1).
    assert {t for _b, t, _p in fake.calls} == {"NVDA"}
    assert summary.as_dict()["requested"] == 1 and summary.written == 1


@pytest.mark.unit
def test_missing_pool_falls_back_to_core_yaml(dirs):
    brief_dir, pool_dir = dirs
    pool_dir.mkdir(parents=True, exist_ok=True)
    (pool_dir / "core.us.yaml").write_text("- ticker: NVDA\n", encoding="utf-8")
    summary = collect_all("us", as_of=AS_OF, runner=FakeBackend({"NVDA": [make_ticker_brief()]}))
    assert summary.written == 1
    assert (brief_dir / "NVDA" / f"{DATE}.md").exists()


@pytest.mark.unit
def test_core_yaml_skips_other_session_symbols(dirs, caplog):
    _brief_dir, pool_dir = dirs
    pool_dir.mkdir(parents=True, exist_ok=True)
    (pool_dir / "core.us.yaml").write_text(
        "- ticker: NVDA\n- ticker: 0700.HK\n", encoding="utf-8"
    )
    fake = FakeBackend({"NVDA": [make_ticker_brief()]})
    with caplog.at_level("WARNING", logger="pipeline.ticker_collector"):
        summary = collect_all("us", as_of=AS_OF, runner=fake)
    assert summary.as_dict()["requested"] == 1  # only the us symbol
    assert "0700.HK" in caplog.text


@pytest.mark.unit
def test_cn_session_collects_cn_symbols(dirs):
    brief_dir, pool_dir = dirs
    pool_dir.mkdir(parents=True, exist_ok=True)
    (pool_dir / "core.cn.yaml").write_text("- ticker: 0700.HK\n", encoding="utf-8")
    fake = FakeBackend({"0700.HK": [make_ticker_brief(ticker="0700.HK", session="cn")]})
    summary = collect_all("cn", as_of=AS_OF, runner=fake)
    assert summary.written == 1
    path = brief_dir / "0700.HK" / f"{DATE}.md"
    meta, _ = briefs.parse_ticker_brief(path.read_text(encoding="utf-8"))
    assert meta.session == "cn"


@pytest.mark.unit
def test_no_pool_and_no_core_raises_distinct_no_tickers_error(dirs):
    with pytest.raises(NoTickersError, match="no tickers to collect"):
        collect_all("us", as_of=AS_OF, runner=FakeBackend({}))


@pytest.mark.unit
def test_unreadable_pool_is_a_collector_error(dirs):
    _brief_dir, pool_dir = dirs
    path = pool_dir / "us" / f"{DATE}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(CollectorError, match="pool file unreadable"):
        collect_all("us", as_of=AS_OF, runner=FakeBackend({}))


# ---------------------------------------------------------------------------
# --tickers subset (R1, AC4)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_tickers_subset_collects_only_that_subset(dirs):
    brief_dir, pool_dir = dirs
    write_pool(pool_dir)
    fake = FakeBackend({"AVGO": [make_ticker_brief(ticker="AVGO")]})
    summary = collect_all("us", as_of=AS_OF, tickers=["AVGO"], runner=fake)
    assert {t for _b, t, _p in fake.calls} == {"AVGO"}
    assert summary.as_dict()["requested"] == 1 and summary.written == 1
    assert not (brief_dir / "NVDA").exists()


@pytest.mark.unit
def test_tickers_subset_unknown_ticker_fails_that_ticker(dirs):
    _brief_dir, pool_dir = dirs
    write_pool(pool_dir)
    fake = FakeBackend({"NVDA": [make_ticker_brief()]})
    summary = collect_all("us", as_of=AS_OF, tickers=["NVDA", "TSLA"], runner=fake)
    assert summary.written == 1
    assert summary.as_dict()["failed"] == [
        {"ticker": "TSLA", "reason": "not in pool (core ∪ opportunity) for this session"}
    ]


# ---------------------------------------------------------------------------
# Prompt template (R2)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_prompt_contains_contract_scale_verbatim_and_anti_inflation():
    prompt = render_ticker_prompt(AS_OF, "NVDA", "us", "claude-deep-search", NO_SEED_BLOCK)
    assert "0–3 nothing actionable" in prompt
    assert "4–6 notable but not imminent" in prompt
    assert "7–8 strong dated catalyst inside ~2 weeks" in prompt
    assert "9–10 imminent, high-impact (≤ 48h)" in prompt
    assert "Do NOT inflate the score" in prompt
    assert "`earnings|product|regulatory|M&A|guidance|flow|macro-exposure|other`" in prompt
    assert "catalyst_window" in prompt
    for section in briefs.TICKER_SECTIONS:
        assert f"## {section}" in prompt
    assert "ticker: NVDA" in prompt and f"as_of_date: {DATE}" in prompt


@pytest.mark.unit
def test_prompt_render_raises_on_unresolved_placeholder(tmp_path):
    template = tmp_path / "broken.md"
    template.write_text("hello {{TICKER}} {{BOGUS}}", encoding="utf-8")
    with pytest.raises(TemplateError, match="BOGUS"):
        render_ticker_prompt(
            AS_OF, "NVDA", "us", "claude-deep-search", "seed", template_path=template
        )


# ---------------------------------------------------------------------------
# Default backend runner (subprocess boundary, faked)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_default_runner_claude_command(monkeypatch):
    seen = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        seen["input"] = kwargs.get("input")
        seen["timeout"] = kwargs.get("timeout")
        return subprocess.CompletedProcess(cmd, 0, stdout="BRIEF TEXT", stderr="")

    monkeypatch.setattr(ticker_collector.subprocess, "run", fake_run)
    assert ticker_collector.default_runner("claude", "PROMPT") == "BRIEF TEXT"
    assert seen["cmd"] == ["claude", "-p", "--allowedTools", "WebSearch,WebFetch"]
    assert seen["input"] == "PROMPT"  # prompt goes over stdin
    assert seen["timeout"] == ticker_collector.BACKEND_TIMEOUT_SECONDS  # ceiling default


@pytest.mark.unit
def test_default_runner_codex_command_is_a_working_invocation(monkeypatch):
    seen = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        seen["input"] = kwargs.get("input")
        return subprocess.CompletedProcess(cmd, 0, stdout="BRIEF TEXT", stderr="")

    monkeypatch.setattr(ticker_collector.subprocess, "run", fake_run)
    assert ticker_collector.default_runner("codex", "PROMPT") == "BRIEF TEXT"
    # D19 (codex is the collection default): web search on, git-repo trust
    # check skipped (components inherit an arbitrary cwd), prompt over stdin.
    assert seen["cmd"] == ["codex", "exec", "-c", "tools.web_search=true", "--skip-git-repo-check", "-"]
    assert seen["input"] == "PROMPT"


@pytest.mark.unit
def test_default_backend_is_codex_when_env_unset(dirs, monkeypatch):
    # D19: with no TRADINGAGENTS_COLLECT_BACKEND set, the fan-out collects on
    # codex and stamps codex-deep-search briefs.
    _brief_dir, pool_dir = dirs
    monkeypatch.delenv("TRADINGAGENTS_COLLECT_BACKEND", raising=False)
    write_pool(pool_dir, core=("NVDA",), opportunity=())
    fake = FakeBackend({"NVDA": [make_ticker_brief(generator="codex-deep-search")]})
    summary = collect_all("us", as_of=AS_OF, runner=fake)
    assert summary.written == 1
    backend, _ticker, prompt = fake.calls[0]
    assert backend == "codex"
    assert "generator: codex-deep-search" in prompt


@pytest.mark.unit
def test_explicit_backend_beats_the_env_default(dirs):
    # dirs pins TRADINGAGENTS_COLLECT_BACKEND=claude; the explicit argument
    # still wins (CLI --backend routes through the same parameter).
    _brief_dir, pool_dir = dirs
    write_pool(pool_dir, core=("NVDA",), opportunity=())
    fake = FakeBackend({"NVDA": [make_ticker_brief(generator="codex-deep-search")]})
    summary = collect_all("us", as_of=AS_OF, backend="codex", runner=fake)
    assert summary.written == 1
    assert fake.calls[0][0] == "codex"


@pytest.mark.unit
def test_fanout_backend_timeout_fits_the_component_budget():
    f = ticker_collector.fanout_backend_timeout
    assert f(2, 3, 2700.0) == 1800.0  # ceiling
    assert f(15, 3, 2700.0) == 516.0  # 2580 / 5 waves
    assert f(4, 1, 2700.0) == 645.0   # 2580 / 4 waves


@pytest.mark.unit
def test_fanout_passes_job_window_and_deadline_to_default_runner(dirs, monkeypatch):
    _brief_dir, pool_dir = dirs
    write_pool(pool_dir, core=("NVDA",), opportunity=())
    monkeypatch.delenv("TRADINGAGENTS_TIMEOUT_TICKER_COLLECTORS", raising=False)
    seen = {}

    def fake_default_runner(backend, prompt, timeout=None, deadline=None):
        seen["timeout"] = timeout
        seen["deadline"] = deadline
        return make_ticker_brief()

    monkeypatch.setattr(ticker_collector, "default_runner", fake_default_runner)
    clock = iter((100.0, 100.0))  # aggregate start, then the worker begins
    monkeypatch.setattr(ticker_collector.time, "monotonic", lambda: next(clock))
    summary = collect_all("us", as_of=AS_OF)  # no injected runner: default path
    assert summary.written == 1
    assert seen == {"timeout": 1800.0, "deadline": 1900.0}


@pytest.mark.unit
def test_ticker_retry_uses_the_remaining_job_deadline(dirs, monkeypatch):
    _brief_dir, pool_dir = dirs
    write_pool(pool_dir, core=("NVDA",), opportunity=())
    timeouts = []
    clock = iter((100.0, 100.0, 100.0, 400.0))

    def fake_run(cmd, **kwargs):
        timeouts.append(kwargs["timeout"])
        text = (
            make_ticker_brief(titles=tuple(briefs.TICKER_SECTIONS[:-1]))
            if len(timeouts) == 1
            else make_ticker_brief()
        )
        return subprocess.CompletedProcess(cmd, 0, stdout=text, stderr="")

    monkeypatch.setattr(ticker_collector.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(ticker_collector.subprocess, "run", fake_run)
    summary = collect_all("us", as_of=AS_OF)

    assert summary.written == 1
    assert timeouts == [1800.0, 1500.0]


@pytest.mark.unit
def test_queued_ticker_cannot_start_after_the_aggregate_deadline(dirs, monkeypatch):
    _brief_dir, pool_dir = dirs
    write_pool(pool_dir, core=("NVDA", "AVGO"), opportunity=())
    launched = []
    # Aggregate deadline: 100 + (121 - 120) = 101. NVDA starts at 100;
    # constrained concurrency queues AVGO until monotonic time 102.
    clock = iter((100.0, 100.0, 100.0, 102.0, 102.0))

    def fake_run(cmd, **kwargs):
        prompt = kwargs["input"]
        ticker = re.search(r"^ticker: (\S+)$", prompt, re.MULTILINE).group(1)
        launched.append((ticker, kwargs["timeout"]))
        return subprocess.CompletedProcess(
            cmd, 0, stdout=make_ticker_brief(ticker=ticker), stderr=""
        )

    monkeypatch.setenv("TRADINGAGENTS_TIMEOUT_TICKER_COLLECTORS", "121")
    monkeypatch.setattr(ticker_collector.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(ticker_collector.subprocess, "run", fake_run)

    summary = collect_all("us", as_of=AS_OF, concurrency=1)

    assert summary.written == 1
    assert [(outcome.ticker, outcome.outcome) for outcome in summary.outcomes] == [
        ("NVDA", "written"),
        ("AVGO", "failed"),
    ]
    assert launched == [("NVDA", 1.0)]


@pytest.mark.unit
def test_default_runner_nonzero_exit_raises_one_line_reason(monkeypatch):
    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 2, stdout="", stderr="not logged in\nmore detail")

    monkeypatch.setattr(ticker_collector.subprocess, "run", fake_run)
    with pytest.raises(CollectorError, match="exited 2: not logged in"):
        ticker_collector.default_runner("codex", "PROMPT")


# ---------------------------------------------------------------------------
# CLI (JSON summary + exit codes, R6)
# ---------------------------------------------------------------------------


def last_stdout_json(capsys):
    captured = capsys.readouterr()
    return json.loads(captured.out.strip().splitlines()[-1]), captured.err


@pytest.mark.unit
def test_cli_partial_success_exits_zero_with_json_summary(dirs, capsys):
    _brief_dir, pool_dir = dirs
    write_pool(pool_dir)
    fake = FakeBackend(
        {"NVDA": [RuntimeError("boom")], "AVGO": [make_ticker_brief(ticker="AVGO")]}
    )
    rc = main(["--session", "us", "--date", DATE], runner=fake)
    summary, _err = last_stdout_json(capsys)
    assert rc == 0  # partial success is success (R6)
    assert summary == {
        "date": DATE,
        "session": "us",
        "requested": 2,
        "written": 1,
        "skipped": 0,
        "failed": [{"ticker": "NVDA", "reason": "boom"}],
    }


@pytest.mark.unit
def test_cli_all_skipped_exits_zero(dirs, capsys):
    _brief_dir, pool_dir = dirs
    write_pool(pool_dir, core=("NVDA",), opportunity=())
    assert main(["--session", "us", "--date", DATE],
                runner=FakeBackend({"NVDA": [make_ticker_brief()]})) == 0
    capsys.readouterr()
    rc = main(["--session", "us", "--date", DATE], runner=FakeBackend({"NVDA": []}))
    summary, _err = last_stdout_json(capsys)
    assert rc == 0
    assert summary["skipped"] == 1 and summary["written"] == 0 and summary["failed"] == []


@pytest.mark.unit
def test_cli_every_ticker_failed_exits_nonzero(dirs, capsys):
    _brief_dir, pool_dir = dirs
    write_pool(pool_dir, core=("NVDA",), opportunity=())
    rc = main(
        ["--session", "us", "--date", DATE],
        runner=FakeBackend({"NVDA": [RuntimeError("boom")]}),
    )
    summary, err = last_stdout_json(capsys)
    assert rc == 1
    assert summary["failed"] == [{"ticker": "NVDA", "reason": "boom"}]
    assert "all 1 requested ticker(s) failed" in err


@pytest.mark.unit
def test_cli_rerun_with_skipped_and_failed_mix_exits_zero(dirs, capsys):
    # R6: non-zero only when EVERY ticker failed. On a rerun the healthy
    # ticker skips (its brief already exists — R5) and the broken one fails
    # again: the day's on-disk coverage is identical to the exit-0 first run,
    # so the rerun must be exit 0 too, not a whole-component failure.
    _brief_dir, pool_dir = dirs
    write_pool(pool_dir)
    assert (
        main(
            ["--session", "us", "--date", DATE],
            runner=FakeBackend(
                {"NVDA": [make_ticker_brief()], "AVGO": [RuntimeError("boom")]}
            ),
        )
        == 0
    )
    capsys.readouterr()

    rc = main(
        ["--session", "us", "--date", DATE],
        runner=FakeBackend({"AVGO": [RuntimeError("boom")]}),
    )
    summary, err = last_stdout_json(capsys)
    assert rc == 0
    assert summary["written"] == 0 and summary["skipped"] == 1
    assert summary["failed"] == [{"ticker": "AVGO", "reason": "boom"}]
    assert "requested ticker(s) failed" not in err  # no all-failed banner


@pytest.mark.unit
def test_cli_no_tickers_is_a_distinct_exit(dirs, capsys):
    rc = main(["--session", "us", "--date", DATE], runner=FakeBackend({}))
    assert rc == 3  # distinct from ordinary failure (R1)
    err = capsys.readouterr().err
    assert "no tickers to collect" in err


@pytest.mark.unit
def test_cli_unreadable_pool_exits_one(dirs, capsys):
    _brief_dir, pool_dir = dirs
    path = pool_dir / "us" / f"{DATE}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    rc = main(["--session", "us", "--date", DATE], runner=FakeBackend({}))
    assert rc == 1
    assert "pool file unreadable" in capsys.readouterr().err


@pytest.mark.unit
def test_cli_tickers_and_force_flags(dirs, capsys):
    brief_dir, pool_dir = dirs
    write_pool(pool_dir)
    first = make_ticker_brief(ticker="AVGO", generated_at="2026-08-08T11:00:00Z")
    assert main(
        ["--session", "us", "--date", DATE, "--tickers", "AVGO"],
        runner=FakeBackend({"AVGO": [first]}),
    ) == 0
    capsys.readouterr()

    second = make_ticker_brief(ticker="AVGO", generated_at="2026-08-08T12:40:00Z")
    forced = FakeBackend({"AVGO": [second]})
    rc = main(
        ["--session", "us", "--date", DATE, "--tickers", "AVGO", "--force"], runner=forced
    )
    summary, _err = last_stdout_json(capsys)
    assert rc == 0 and summary["written"] == 1
    assert len(forced.calls) == 1
    assert (brief_dir / "AVGO" / f"{DATE}.md").read_text(encoding="utf-8") == second
    archive = brief_dir / "AVGO" / "archive" / f"{DATE}.2026-08-08T11:00:00Z.md"
    assert archive.read_text(encoding="utf-8") == first
    assert not (brief_dir / "NVDA").exists()  # subset respected across both runs


@pytest.mark.unit
def test_cli_backend_flag_selects_generator(dirs):
    _brief_dir, pool_dir = dirs
    write_pool(pool_dir, core=("NVDA",), opportunity=())
    fake = FakeBackend({"NVDA": [make_ticker_brief(generator="codex-deep-search")]})
    assert main(["--session", "us", "--date", DATE, "--backend", "codex"], runner=fake) == 0
    backend, _ticker, prompt = fake.calls[0]
    assert backend == "codex"
    assert "generator: codex-deep-search" in prompt


@pytest.mark.unit
@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["--session", "eu"],
        ["--session", "us", "--date", "08/08/2026"],
        ["--session", "us", "--backend", "gemini"],
    ],
)
def test_cli_rejects_invalid_arguments(argv, capsys):
    with pytest.raises(SystemExit) as excinfo:
        main(argv)
    assert excinfo.value.code == 2


@pytest.mark.unit
def test_module_invocable_via_python_dash_m():
    repo_root = Path(__file__).resolve().parents[1]
    proc = subprocess.run(
        [sys.executable, "-m", "pipeline.ticker_collector", "--help"],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=repo_root,
    )
    assert proc.returncode == 0
    assert "--session" in proc.stdout
    assert "--tickers" in proc.stdout
    assert "--backend" in proc.stdout
