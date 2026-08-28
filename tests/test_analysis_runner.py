"""Analysis runner (``pipeline/analysis_runner.py``) — spec AC1–AC3, offline.

The child subprocess is faked at the runner seam (``ChildRunner``); brief
resolution is faked at the ``Resolvers`` seam while the eval verdicts go
through the REAL revision-bound reader (``brief_evals.get_eval_verdict``), so
the hash-mismatch ⇒ ``missing`` rule is exercised for real. Covers the trigger
matrix (incl. ticker normalization/id-charset drops), the eval-gating state
table (incl. the missing-eval flags), stateless core-pair rotation (coverage
property over k × len), pair-attempt minting on whole-pair reruns and under
concurrency (attempt reserved by the pair's first row, under the append
flock), the no-``--force`` coverage rules (manual/ERROR rows never suppress
scheduled runs; half pairs re-run whole), the budget guard drop order (pair
atomicity, core never dropped), run_id flock uniqueness under concurrent
appends, contract validation of every emitted row, consumption-time inputs
hashing (spawn-time re-resolution + mid-run drift flags), the config-digest
snapshot (analysis-side effective config), TradePlan validator wiring, ERROR
rows on child crash, the guarded ledger append, the sentinel result-line
protocol + child group-kill, and the positions.json trader context (fresh vs
stale).
"""

from __future__ import annotations

import hashlib
import json
import math
import signal
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from pipeline import analysis_runner as ar
from pipeline.common import sha256_file, sha256_text
from pipeline.config import PipelineConfig
from pipeline.contracts.ledger import DecisionRecord

SLOT = date(2026, 8, 7)  # a Friday
NOW = datetime(2026, 8, 7, 13, 0, tzinfo=timezone.utc)

VALID_PLAN = {
    "action": "BUY",
    "conviction": 0.7,
    "add_intent": False,
    "add_rationale": None,
    "entry_zone": [99.0, 101.0],
    "stop": 95.0,
    "targets": [110.0, 120.0],
    "horizon_days": 10,
    "invalidation": "daily close below the weekly mid-band",
    "sizing": {"risk_pct": 1.0},
    "source_levels": "entry = daily BOLL mid, stop = below daily lower band",
}


def make_config(tmp_path) -> PipelineConfig:
    return PipelineConfig(state_dir=tmp_path / "state")


def write_brief(path: Path, *, generated_at="2026-08-07T04:00:00Z", catalyst_score=None) -> Path:
    lines = ["---", f"generated_at: {generated_at}"]
    if catalyst_score is not None:
        lines.append(f"catalyst_score: {catalyst_score}")
    lines += ["---", "", "# Brief", "", "Body text."]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def write_eval(brief_path: Path, verdict="pass", sha=None) -> Path:
    """Sibling revision-bound eval; ``sha`` overrides for mismatch fixtures."""
    digest = sha if sha is not None else hashlib.sha256(brief_path.read_bytes()).hexdigest()
    eval_path = Path(str(brief_path)[: -len(".md")] + ".eval.json")
    eval_path.write_text(
        json.dumps({"verdict": verdict, "brief_sha256": digest}), encoding="utf-8"
    )
    return eval_path


def write_pool(config, session, slot_date, core=(), opportunity=()) -> Path:
    """Contract-valid pool file; ``opportunity`` is (ticker, score) pairs."""
    payload = {
        "as_of_date": slot_date.isoformat(),
        "session": session,
        "generated_at": "2026-08-07T03:00:00Z",
        "generator": "test",
        "core": [{"ticker": t} for t in core],
        "opportunity": [
            {
                "ticker": t,
                "score": s,
                "catalyst_type": "earnings",
                "rationale": "seed",
                "citations": ["https://example.com/x"],
                "technical": {"gate": "pass"},
                "entered_on": slot_date.isoformat(),
                "low_score_streak": 0,
                "gate_fail_streak": 0,
            }
            for t, s in opportunity
        ],
        "watch": [],
        "removed": [],
    }
    path = config.pool_dir / session / f"{slot_date.isoformat()}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def make_resolvers(macro_path=None, ticker_paths=None) -> ar.Resolvers:
    """Fake path resolution over tmp files + the REAL eval-verdict reader."""
    from tradingagents.dataflows.brief_evals import get_eval_verdict

    paths = dict(ticker_paths or {})

    def macro(date_iso, session):
        if macro_path is None:
            raise ar.BriefUnavailable("no macro brief")
        return str(macro_path)

    def ticker(symbol, date_iso):
        path = paths.get(symbol)
        if path is None:
            raise ar.BriefUnavailable(f"no brief for {symbol}")
        return str(path)

    return ar.Resolvers(macro, ticker, get_eval_verdict)


class FakeChild:
    """Runner-seam child fake: canned payloads keyed by ticker."""

    def __init__(self, plans=None, decisions=None, fail=(), plan_errors=None):
        self.plans = plans or {}
        self.decisions = decisions or {}
        self.fail = set(fail)
        self.plan_errors = plan_errors or {}
        self.specs: list[ar.ChildSpec] = []
        self.timeouts: list[float] = []

    def __call__(self, spec: ar.ChildSpec, timeout: float) -> ar.ChildResult:
        self.specs.append(spec)
        self.timeouts.append(timeout)
        if spec.ticker in self.fail:
            return ar.ChildResult(1, None, "child boom")
        plan = self.plans.get(spec.ticker, VALID_PLAN)
        default_decision = "HOLD" if plan is None else plan.get("action", "BUY")
        return ar.ChildResult(
            0,
            {
                "ticker": spec.ticker,
                "date": spec.date,
                "session": spec.session,
                "arm": spec.arm,
                "decision": self.decisions.get(spec.ticker, default_decision),
                "plan": plan,
                "plan_error": self.plan_errors.get(spec.ticker),
                "repair_used": False,
                "report_dir": None,
            },
        )


def run(config, resolvers, child, *, session="us", slot=SLOT, settings=None, **kwargs):
    emitted = []
    outcome = ar.run_slot(
        session,
        slot,
        config=config,
        settings=settings or ar.RunnerSettings(ab_pairing="off"),
        resolvers=resolvers,
        child_runner=child,
        ohlcv_fetcher=lambda ticker, on_date: (100.0, 2.0),
        clock=lambda: NOW,
        emit=emitted.append,
        **kwargs,
    )
    return outcome, emitted


def read_records(config) -> list[DecisionRecord]:
    """Every emitted row must validate against the ledger contract (AC3)."""
    path = config.ledger_dir / ar.DECISIONS_NAME
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
    return [DecisionRecord.model_validate(row) for row in rows]


def by_ticker_arm(records):
    return {(r.ticker, r.arm): r for r in records}


# ---------------------------------------------------------------------------
# Trigger matrix (AC1)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_core_all_every_member_runs(tmp_path):
    config = make_config(tmp_path)
    write_pool(config, "us", SLOT, core=("AAA", "BBB", "CCC"))
    macro = write_brief(config.macro_brief_dir / "2026-08-07.us.md")
    write_eval(macro, "pass")
    child = FakeChild()
    outcome, emitted = run(config, make_resolvers(macro), child)

    records = read_records(config)
    assert sorted(r.ticker for r in records) == ["AAA", "BBB", "CCC"]
    assert all(r.trigger == "core" and r.arm == "brief" for r in records)
    assert [r.run_id for r in records] == [f"2026-08-07-us-{t}-brief-1" for t in ("AAA", "BBB", "CCC")]
    assert outcome.summary["planned"] == 3
    assert outcome.summary["completed"] == 3
    assert outcome.summary["errors"] == 0
    assert len(emitted) == 3


@pytest.mark.unit
def test_job_concurrency_overlaps_tickers_preserves_pairs_and_summary_order(tmp_path):
    config = make_config(tmp_path)
    write_pool(config, "us", SLOT, core=("AAA", "BBB"))
    settings = ar.RunnerSettings(
        analysis_job_concurrency=2,
        ab_pairing="paired",
        ab_core_pairs_per_slot=2,
    )
    first_arms = threading.Barrier(2)
    bbb_emitted = threading.Event()
    activity_lock = threading.Lock()
    active = 0
    peak = 0
    arms_by_ticker: dict[str, list[str]] = {}

    class ConcurrentChild(FakeChild):
        def __call__(self, spec, timeout):
            nonlocal active, peak
            with activity_lock:
                active += 1
                peak = max(peak, active)
                arms_by_ticker.setdefault(spec.ticker, []).append(spec.arm)
            try:
                if spec.arm == "brief":
                    first_arms.wait(timeout=2)
                    if spec.ticker == "AAA":
                        assert bbb_emitted.wait(timeout=2)
                return super().__call__(spec, timeout)
            finally:
                with activity_lock:
                    active -= 1

    parent_thread = threading.get_ident()
    emitted: list[dict] = []
    emit_threads: list[int] = []

    def emit(line):
        emit_threads.append(threading.get_ident())
        emitted.append(line)
        if line["ticker"] == "BBB":
            bbb_emitted.set()

    outcome = ar.run_slot(
        "us",
        SLOT,
        config=config,
        settings=settings,
        resolvers=make_resolvers(None),
        child_runner=ConcurrentChild(),
        ohlcv_fetcher=lambda ticker, on_date: (100.0, 2.0),
        clock=lambda: NOW,
        emit=emit,
    )

    assert peak == 2
    assert arms_by_ticker == {
        "AAA": ["brief", "feeds"],
        "BBB": ["brief", "feeds"],
    }
    assert [line["ticker"] for line in emitted] == ["BBB", "BBB", "AAA", "AAA"]
    assert emit_threads == [parent_thread] * 4
    expected_ids = [
        "2026-08-07-us-AAA-brief-1",
        "2026-08-07-us-AAA-feeds-1",
        "2026-08-07-us-BBB-brief-1",
        "2026-08-07-us-BBB-feeds-1",
    ]
    assert outcome.summary["planned"] == 4
    assert outcome.summary["run_ids"] == expected_ids
    assert [record.run_id for record in outcome.records] == expected_ids


@pytest.mark.unit
def test_catalyst_threshold_gates_opportunity(tmp_path):
    config = make_config(tmp_path)
    write_pool(config, "us", SLOT, core=("AAA",), opportunity=(("HOT", 6.0), ("COLD", 9.0)))
    macro = write_brief(config.macro_brief_dir / "2026-08-07.us.md")
    write_eval(macro, "pass")
    # Trigger uses the BRIEF's catalyst_score, not the pool nomination score.
    hot = write_brief(config.ticker_brief_dir / "HOT" / "2026-08-07.md", catalyst_score=7.5)
    write_eval(hot, "pass")
    cold = write_brief(config.ticker_brief_dir / "COLD" / "2026-08-07.md", catalyst_score=6.9)
    write_eval(cold, "pass")
    resolvers = make_resolvers(macro, {"HOT": hot, "COLD": cold})
    outcome, _ = run(config, resolvers, FakeChild())

    records = by_ticker_arm(read_records(config))
    assert ("HOT", "brief") in records
    assert records[("HOT", "brief")].trigger == "catalyst"
    assert records[("HOT", "brief")].catalyst_score == 7.5
    assert not any(t == "COLD" for t, _ in records)


@pytest.mark.unit
def test_catalyst_suppressed_on_eval_fail_and_absent_brief(tmp_path):
    config = make_config(tmp_path)
    write_pool(config, "us", SLOT, core=(), opportunity=(("BAD", 9.0), ("NOBRIEF", 9.0)))
    macro = write_brief(config.macro_brief_dir / "2026-08-07.us.md")
    write_eval(macro, "pass")
    bad = write_brief(config.ticker_brief_dir / "BAD" / "2026-08-07.md", catalyst_score=9.5)
    write_eval(bad, "fail")  # eval fail ⇒ no auto-trigger
    resolvers = make_resolvers(macro, {"BAD": bad})  # NOBRIEF resolves nothing
    outcome, _ = run(config, resolvers, FakeChild())

    assert not (config.ledger_dir / ar.DECISIONS_NAME).exists()
    assert outcome.summary["planned"] == 0


@pytest.mark.unit
def test_manual_trigger_runs_any_ticker(tmp_path):
    config = make_config(tmp_path)  # no pool at all
    macro = write_brief(config.macro_brief_dir / "2026-08-07.us.md")
    write_eval(macro, "pass")
    child = FakeChild()
    outcome, emitted = run(config, make_resolvers(macro), child, manual_ticker="ZZZ")

    records = read_records(config)
    assert len(records) == 1
    record = records[0]
    assert (record.trigger, record.arm, record.pair_id) == ("manual", "brief", None)
    assert record.ticker == "ZZZ"
    assert record.ticker_eval_verdict == "missing"  # no brief for ZZZ
    assert record.inputs.pool is None
    # Manual runs are never auto-executed (adapter S5).
    assert emitted[0]["execute_eligible"] is False


@pytest.mark.unit
def test_absent_pool_falls_back_to_core_yaml(tmp_path):
    """Pool contract reading rule: absent pool ⇒ opportunity empty, core
    coverage from core.<session>.yaml, and the ledger pool input is null."""
    config = make_config(tmp_path)
    core_yaml = config.pool_dir / "core.us.yaml"
    core_yaml.parent.mkdir(parents=True, exist_ok=True)
    core_yaml.write_text("- ticker: AAA\n- BBB\n", encoding="utf-8")
    run(config, make_resolvers(None), FakeChild())
    records = read_records(config)
    assert sorted(r.ticker for r in records) == ["AAA", "BBB"]
    assert all(r.trigger == "core" and r.inputs.pool is None for r in records)


@pytest.mark.unit
def test_pool_tickers_normalized_and_id_illegal_symbols_dropped(tmp_path, caplog):
    """Pool nominations come from an LLM and the pool contract does not
    constrain case/charset, while run_id embeds the ticker under the
    contract's [A-Z0-9]-segment rule. Lowercase symbols are normalized;
    id-illegal ones are dropped loudly at planning — BEFORE any child LLM
    spend — so the ledger write can never blow up the slot (R3)."""
    config = make_config(tmp_path)
    write_pool(config, "us", SLOT, core=("brk.b", "bad ticker", "AAA", "aaa"))
    child = FakeChild()
    with caplog.at_level("WARNING", logger="pipeline.analysis_runner"):
        outcome, _ = run(config, make_resolvers(None), child)

    records = read_records(config)
    assert sorted(r.ticker for r in records) == ["AAA", "BRK.B"]  # dupes collapse too
    assert sorted(r.run_id for r in records) == [
        "2026-08-07-us-AAA-brief-1",
        "2026-08-07-us-BRK.B-brief-1",
    ]
    # The illegal symbol never reached a child (no LLM budget spent) and the
    # drop was loud.
    assert sorted({spec.ticker for spec in child.specs}) == ["AAA", "BRK.B"]
    assert any("bad ticker" in message for message in caplog.messages)
    assert outcome.summary["errors"] == 0


@pytest.mark.unit
def test_default_resolvers_use_readers_and_translate_vendor_errors(tmp_path, monkeypatch):
    """The production seam binds gating to the exact revision the readers
    serve (resolve_*_brief_path + the revision-bound get_eval_verdict)."""
    from tradingagents.dataflows.config import set_config

    config = make_config(tmp_path)
    # default_resolvers exports these for a fresh child process; in-process the
    # already-imported dataflows config is steered via set_config below.
    monkeypatch.setenv("TRADINGAGENTS_MACRO_BRIEF_DIR", "sentinel")
    monkeypatch.setenv("TRADINGAGENTS_TICKER_BRIEF_DIR", "sentinel")
    resolvers = ar.default_resolvers(config)
    set_config(
        {
            "macro_brief_dir": str(config.macro_brief_dir),
            "ticker_brief_dir": str(config.ticker_brief_dir),
        }
    )
    with pytest.raises(ar.BriefUnavailable):
        resolvers.macro_resolver("2026-08-07", "us")

    macro = write_brief(config.macro_brief_dir / "2026-08-07.us.md")
    write_eval(macro, "warn")
    path = resolvers.macro_resolver("2026-08-07", "us")
    assert Path(path) == macro
    assert resolvers.verdict_reader(path) == "warn"

    brief = write_brief(config.ticker_brief_dir / "AAA" / "2026-08-07.md")
    assert Path(resolvers.ticker_resolver("AAA", "2026-08-07")) == brief
    assert resolvers.verdict_reader(str(brief)) == "missing"  # no eval written
    with pytest.raises(ar.BriefUnavailable):
        resolvers.ticker_resolver("NOPE", "2026-08-07")


# ---------------------------------------------------------------------------
# Eval-gating state table (normative rows)
# ---------------------------------------------------------------------------


def _gating_fixture(tmp_path, *, macro_verdict="pass", ticker_verdict="pass",
                    eval_sha=None, no_ticker_eval=False):
    config = make_config(tmp_path)
    write_pool(config, "us", SLOT, core=("AAA",))
    macro = write_brief(config.macro_brief_dir / "2026-08-07.us.md")
    write_eval(macro, macro_verdict)
    brief = write_brief(config.ticker_brief_dir / "AAA" / "2026-08-07.md", catalyst_score=5.0)
    if not no_ticker_eval:
        write_eval(brief, ticker_verdict, sha=eval_sha)
    return config, make_resolvers(macro, {"AAA": brief}), brief


@pytest.mark.unit
def test_gating_pass_pass_serves_brief_and_is_eligible(tmp_path):
    config, resolvers, brief = _gating_fixture(tmp_path)
    child = FakeChild()
    _, emitted = run(config, resolvers, child)
    record = read_records(config)[0]
    assert (record.macro_eval_verdict, record.ticker_eval_verdict) == ("pass", "pass")
    assert child.specs[0].withhold_ticker_brief is False
    assert record.inputs.ticker_brief is not None
    assert emitted[0]["execute_eligible"] is True


@pytest.mark.unit
def test_gating_ticker_missing_runs_flagged_and_blocks_execution(tmp_path):
    config, resolvers, _brief = _gating_fixture(tmp_path, no_ticker_eval=True)
    _, emitted = run(config, resolvers, FakeChild())
    record = read_records(config)[0]
    assert record.ticker_eval_verdict == "missing"
    # The brief itself WAS served (only the verdict is missing) — consumed
    # inputs are recorded per the contract.
    assert record.inputs.ticker_brief is not None
    assert emitted[0]["execute_eligible"] is False
    # State table: missing eval ⇒ "runs, flagged" — the flag is on the line.
    assert "ticker-eval-missing" in emitted[0]["flags"]


@pytest.mark.unit
def test_gating_missing_eval_eligible_only_with_override(tmp_path):
    config, resolvers, _brief = _gating_fixture(tmp_path, no_ticker_eval=True)
    settings = ar.RunnerSettings(ab_pairing="off", execute_on_missing_eval=True)
    _, emitted = run(config, resolvers, FakeChild(), settings=settings)
    assert emitted[0]["execute_eligible"] is True


@pytest.mark.unit
def test_gating_hash_mismatch_is_missing(tmp_path):
    """Revision binding: a stale predecessor eval never blesses a re-collected
    brief — the REAL get_eval_verdict reports the mismatch as missing."""
    config, resolvers, _brief = _gating_fixture(tmp_path, eval_sha="0" * 64)
    _, emitted = run(config, resolvers, FakeChild())
    record = read_records(config)[0]
    assert record.ticker_eval_verdict == "missing"
    assert emitted[0]["execute_eligible"] is False


@pytest.mark.unit
def test_gating_ticker_fail_withholds_brief_but_core_runs(tmp_path):
    config, resolvers, _brief = _gating_fixture(tmp_path, ticker_verdict="fail")
    child = FakeChild()
    _, emitted = run(config, resolvers, child)
    record = read_records(config)[0]
    # Core coverage never stops; the contaminated input does.
    assert record.arm == "brief"
    assert record.ticker_eval_verdict == "fail"
    assert child.specs[0].withhold_ticker_brief is True
    assert record.inputs.ticker_brief is None
    assert record.inputs.macro_brief is not None
    assert emitted[0]["execute_eligible"] is False
    assert "ticker-eval-fail-withheld" in emitted[0]["flags"]


@pytest.mark.unit
def test_gating_macro_fail_replaces_with_flagged_feeds_run(tmp_path):
    config, resolvers, _brief = _gating_fixture(tmp_path, macro_verdict="fail")
    child = FakeChild()
    # Even under paired settings the replacement run IS the feeds arm — one
    # run, recorded unpaired (a pair_id must own exactly two rows).
    settings = ar.RunnerSettings(ab_pairing="paired", ab_core_pairs_per_slot=3)
    _, emitted = run(config, resolvers, child, settings=settings)
    records = read_records(config)
    assert len(records) == 1
    record = records[0]
    assert (record.arm, record.pair_id) == ("feeds", None)
    assert record.macro_eval_verdict == "fail"
    assert record.inputs.macro_brief is None
    assert record.inputs.ticker_brief is None
    assert "macro-fail" in emitted[0]["flags"]
    assert emitted[0]["execute_eligible"] is False  # feeds never executes


@pytest.mark.unit
def test_gating_macro_brief_absent_is_missing_and_still_runs(tmp_path):
    config = make_config(tmp_path)
    write_pool(config, "us", SLOT, core=("AAA",))
    _, emitted = run(config, make_resolvers(None), FakeChild())
    record = read_records(config)[0]
    assert record.arm == "brief"
    assert record.macro_eval_verdict == "missing"
    assert record.inputs.macro_brief is None
    assert emitted[0]["execute_eligible"] is False
    # State table: a missing macro eval still runs but is flagged (the ticker
    # brief is absent here too, so its missing flag rides along).
    assert "macro-eval-missing" in emitted[0]["flags"]
    assert "ticker-eval-missing" in emitted[0]["flags"]


@pytest.mark.unit
def test_execution_eligible_state_table():
    on = ar.RunnerSettings(execute_on_missing_eval=True)
    off = ar.RunnerSettings()
    assert ar.execution_eligible("brief", "core", "pass", "warn", off) is True
    assert ar.execution_eligible("brief", "catalyst", "warn", "pass", off) is True
    assert ar.execution_eligible("brief", "core", "pass", "missing", off) is False
    assert ar.execution_eligible("brief", "core", "pass", "missing", on) is True
    assert ar.execution_eligible("brief", "core", "missing", "pass", off) is False
    assert ar.execution_eligible("brief", "core", "pass", "fail", off) is False
    assert ar.execution_eligible("brief", "core", "fail", "pass", on) is False
    assert ar.execution_eligible("feeds", "core", "pass", "pass", off) is False
    assert ar.execution_eligible("brief", "manual", "pass", "pass", off) is False


# ---------------------------------------------------------------------------
# Core-pair rotation (AC1 fairness property)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_core_rotation_coverage_property(subtests):
    """Every core ticker is paired within ceil(len/k) consecutive slots, for
    every k × len combination and any starting date alignment."""
    for k in (1, 2, 3, 5):
        for length in (1, 3, 9, 10):
            with subtests.test(k=k, length=length):
                tickers = [f"T{i:02d}" for i in range(length)]
                cycle = math.ceil(length / k)
                base = date(2026, 8, 3)
                for align in range(cycle):
                    covered: set[str] = set()
                    for day in range(cycle):
                        selected = ar.core_pair_rotation(
                            tickers, k, base + timedelta(days=align + day)
                        )
                        assert len(selected) == min(k, length)
                        assert len(set(selected)) == len(selected)
                        covered.update(selected)
                    assert covered == set(tickers)


@pytest.mark.unit
def test_core_rotation_edge_cases():
    assert ar.core_pair_rotation([], 3, SLOT) == []
    assert ar.core_pair_rotation(["A"], 0, SLOT) == []
    # k >= len selects every ticker exactly once.
    assert sorted(ar.core_pair_rotation(["B", "A"], 5, SLOT)) == ["A", "B"]


# ---------------------------------------------------------------------------
# A/B pairing + pair-attempt minting
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_paired_core_rotation_and_catalyst_pairing(tmp_path):
    config = make_config(tmp_path)
    write_pool(config, "us", SLOT, core=("AAA", "BBB"), opportunity=(("HOT", 8.0),))
    macro = write_brief(config.macro_brief_dir / "2026-08-07.us.md")
    write_eval(macro, "pass")
    hot = write_brief(config.ticker_brief_dir / "HOT" / "2026-08-07.md", catalyst_score=8.5)
    write_eval(hot, "pass")
    settings = ar.RunnerSettings(ab_pairing="paired", ab_core_pairs_per_slot=1)
    child = FakeChild()
    run(config, make_resolvers(macro, {"HOT": hot}), child, settings=settings)

    rotated = ar.core_pair_rotation(["AAA", "BBB"], 1, SLOT)[0]
    solo = "BBB" if rotated == "AAA" else "AAA"
    records = by_ticker_arm(read_records(config))
    # The rotated core ticker and the catalyst ticker run both arms.
    for ticker in (rotated, "HOT"):
        brief_row, feeds_row = records[(ticker, "brief")], records[(ticker, "feeds")]
        assert brief_row.pair_id == feeds_row.pair_id == f"2026-08-07-us-{ticker}-a1"
    # The other core ticker is a solo brief run.
    assert records[(solo, "brief")].pair_id is None
    assert (solo, "feeds") not in records
    # Each pair keeps its own brief → feeds order even when other ticker jobs
    # run concurrently between those child calls.
    order = [(s.ticker, s.arm) for s in child.specs]
    for ticker in (rotated, "HOT"):
        assert [arm for symbol, arm in order if symbol == ticker] == ["brief", "feeds"]


@pytest.mark.unit
def test_force_rerun_mints_next_pair_attempt_for_whole_pair(tmp_path):
    config = make_config(tmp_path)
    write_pool(config, "us", SLOT, core=("AAA",))
    macro = write_brief(config.macro_brief_dir / "2026-08-07.us.md")
    write_eval(macro, "pass")
    settings = ar.RunnerSettings(ab_pairing="paired", ab_core_pairs_per_slot=1)
    resolvers = make_resolvers(macro)

    run(config, resolvers, FakeChild(), settings=settings)
    outcome2, _ = run(config, resolvers, FakeChild(), settings=settings, force=True)

    records = read_records(config)
    assert len(records) == 4  # two whole pairs, never a lone rerun arm
    attempts = sorted((r.pair_id, r.run_id) for r in records)
    assert [a for a, _ in attempts] == [
        "2026-08-07-us-AAA-a1", "2026-08-07-us-AAA-a1",
        "2026-08-07-us-AAA-a2", "2026-08-07-us-AAA-a2",
    ]
    # run_id <n> incremented per (date, session, ticker, arm) under flock.
    assert {r.run_id for r in records} == {
        "2026-08-07-us-AAA-brief-1", "2026-08-07-us-AAA-feeds-1",
        "2026-08-07-us-AAA-brief-2", "2026-08-07-us-AAA-feeds-2",
    }
    assert outcome2.summary["skipped"] == []


@pytest.mark.unit
def test_rerun_without_force_skips_existing(tmp_path):
    config = make_config(tmp_path)
    write_pool(config, "us", SLOT, core=("AAA", "BBB"))
    macro = write_brief(config.macro_brief_dir / "2026-08-07.us.md")
    write_eval(macro, "pass")
    resolvers = make_resolvers(macro)
    run(config, resolvers, FakeChild())
    outcome2, emitted2 = run(config, resolvers, FakeChild())
    assert sorted(outcome2.summary["skipped"]) == ["AAA", "BBB"]
    assert emitted2 == []
    assert len(read_records(config)) == 2  # no new rows


@pytest.mark.unit
def test_no_force_manual_row_never_suppresses_the_core_run(tmp_path):
    """A morning manual run is never auto-executed (adapter S5), so it cannot
    count as the slot's core coverage — 'core: every member, every slot'."""
    config = make_config(tmp_path)
    write_pool(config, "us", SLOT, core=("AAA",))
    resolvers = make_resolvers(None)
    run(config, resolvers, FakeChild(), manual_ticker="AAA")  # manual first
    outcome, _ = run(config, resolvers, FakeChild())  # scheduled slot later
    records = read_records(config)
    assert [r.trigger for r in records] == ["manual", "core"]
    assert [r.run_id for r in records] == [
        "2026-08-07-us-AAA-brief-1",
        "2026-08-07-us-AAA-brief-2",
    ]
    assert outcome.summary["skipped"] == []


@pytest.mark.unit
def test_no_force_error_rows_are_retried(tmp_path):
    """Recovery semantics (orchestrator R3 rerun, which never passes --force):
    a ticker whose only row is decision ERROR is re-run, completed tickers
    are still skipped."""
    config = make_config(tmp_path)
    write_pool(config, "us", SLOT, core=("AAA", "BBB"))
    resolvers = make_resolvers(None)
    run(config, resolvers, FakeChild(fail=("AAA",)))  # AAA → ERROR row
    outcome2, _ = run(config, resolvers, FakeChild())  # no --force
    aaa = [r for r in read_records(config) if r.ticker == "AAA"]
    assert [r.decision for r in aaa] == ["ERROR", "BUY"]  # retried and recovered
    assert outcome2.summary["skipped"] == ["BBB"]


def _seed_row(config, **overrides):
    """Append a contract-valid decision row directly (crash-state fixtures)."""
    fields = {**_base_fields(), "ticker": "AAA", "trigger": "core", **overrides}
    return ar.mint_and_append_decision(config.ledger_dir, fields)


@pytest.mark.unit
def test_no_force_half_pair_is_completed_by_a_whole_pair_rerun(tmp_path):
    """A crash between a pair's two runs leaves a half attempt; the rerun
    re-runs the WHOLE pair as a<k+1> (single arms are never run in isolation)
    instead of skipping the ticker forever."""
    config = make_config(tmp_path)
    write_pool(config, "us", SLOT, core=("AAA",))
    macro = write_brief(config.macro_brief_dir / "2026-08-07.us.md")
    write_eval(macro, "pass")
    _seed_row(config, pair_id="2026-08-07-us-AAA-a1")  # brief arm only — crash
    settings = ar.RunnerSettings(ab_pairing="paired", ab_core_pairs_per_slot=1)
    outcome, _ = run(config, make_resolvers(macro), FakeChild(), settings=settings)

    records = read_records(config)
    assert outcome.summary["skipped"] == []
    a2 = [r for r in records if r.pair_id == "2026-08-07-us-AAA-a2"]
    assert sorted(r.arm for r in a2) == ["brief", "feeds"]
    # A completed attempt IS skipped on the next no-force rerun.
    outcome2, _ = run(config, make_resolvers(macro), FakeChild(), settings=settings)
    assert outcome2.summary["skipped"] == ["AAA"]
    assert len(read_records(config)) == 3  # no rows added by the skip


@pytest.mark.unit
def test_completed_coverage_rules():
    rows = [
        {"date": "2026-08-07", "session": "us", "ticker": "SOLO", "arm": "brief",
         "trigger": "core", "decision": "BUY", "pair_id": None},
        {"date": "2026-08-07", "session": "us", "ticker": "MAN", "arm": "brief",
         "trigger": "manual", "decision": "BUY", "pair_id": None},
        {"date": "2026-08-07", "session": "us", "ticker": "ERR", "arm": "brief",
         "trigger": "core", "decision": "ERROR", "pair_id": None},
        {"date": "2026-08-07", "session": "us", "ticker": "HALF", "arm": "brief",
         "trigger": "core", "decision": "BUY", "pair_id": "2026-08-07-us-HALF-a1"},
        {"date": "2026-08-07", "session": "us", "ticker": "PAIR", "arm": "brief",
         "trigger": "core", "decision": "BUY", "pair_id": "2026-08-07-us-PAIR-a1"},
        {"date": "2026-08-07", "session": "us", "ticker": "PAIR", "arm": "feeds",
         "trigger": "core", "decision": "HOLD", "pair_id": "2026-08-07-us-PAIR-a1"},
        # An attempt whose second arm ERRORed is not complete either.
        {"date": "2026-08-07", "session": "us", "ticker": "PERR", "arm": "brief",
         "trigger": "core", "decision": "BUY", "pair_id": "2026-08-07-us-PERR-a1"},
        {"date": "2026-08-07", "session": "us", "ticker": "PERR", "arm": "feeds",
         "trigger": "core", "decision": "ERROR", "pair_id": "2026-08-07-us-PERR-a1"},
        # Other slots never count.
        {"date": "2026-08-06", "session": "us", "ticker": "OLD", "arm": "brief",
         "trigger": "core", "decision": "BUY", "pair_id": None},
    ]
    solo_done, paired_done = ar.completed_coverage(rows, "2026-08-07", "us")
    assert solo_done == {"SOLO"}
    assert paired_done == {"PAIR"}


# ---------------------------------------------------------------------------
# Budget guard (R2)
# ---------------------------------------------------------------------------


def _job(ticker, trigger, arms, paired, score=None):
    info = ar.BriefInfo("pass")
    return ar.RunJob(
        ticker=ticker, trigger=trigger, arms=arms, paired=paired,
        catalyst_score=score, macro=info, ticker_brief=info,
    )


@pytest.mark.unit
def test_budget_drops_unpaired_triggered_lowest_score_first():
    jobs = [
        _job("AAA", "core", ("brief",), False),
        _job("HI", "catalyst", ("brief",), False, score=8.0),
        _job("LO", "catalyst", ("brief",), False, score=7.2),
    ]
    kept, dropped = ar.enforce_budget(jobs, 2)
    assert [j.ticker for j in kept] == ["AAA", "HI"]
    assert [d["ticker"] for d in dropped] == ["LO"]
    assert dropped[0]["reason"].startswith("unpaired triggered run")


@pytest.mark.unit
def test_budget_drops_whole_pairs_then_demotes_core_pairs():
    jobs = [
        _job("AAA", "core", ("brief", "feeds"), True),
        _job("BBB", "core", ("brief", "feeds"), True),
        _job("HOT", "catalyst", ("brief", "feeds"), True, score=7.5),
    ]
    kept, dropped = ar.enforce_budget(jobs, 3)
    # The triggered pair vanished atomically (-2), then one core pair was
    # demoted to its mandatory solo brief run (-1): 6 → 3.
    assert sum(len(j.arms) for j in kept) == 3
    assert not any(j.ticker == "HOT" for j in kept)
    core = {j.ticker: j for j in kept}
    assert set(core) == {"AAA", "BBB"}  # core coverage never dropped
    paired_flags = sorted(j.paired for j in kept)
    assert paired_flags == [False, True]
    demoted = next(j for j in kept if not j.paired)
    assert demoted.arms == ("brief",)  # no orphaned feeds arm
    assert [d["ticker"] for d in dropped] == ["HOT", "BBB"]
    assert "both arms" in dropped[0]["reason"]
    assert "demoted" in dropped[1]["reason"]


@pytest.mark.unit
def test_budget_never_drops_core_solos():
    jobs = [_job(t, "core", ("brief",), False) for t in ("AAA", "BBB", "CCC")]
    kept, dropped = ar.enforce_budget(jobs, 2)
    assert len(kept) == 3  # over budget, loudly — but core runs anyway
    assert dropped == []


@pytest.mark.unit
def test_budget_end_to_end_logs_drops_in_summary(tmp_path):
    config = make_config(tmp_path)
    write_pool(config, "us", SLOT, core=("AAA", "BBB"), opportunity=(("HOT", 8.0),))
    macro = write_brief(config.macro_brief_dir / "2026-08-07.us.md")
    write_eval(macro, "pass")
    hot = write_brief(config.ticker_brief_dir / "HOT" / "2026-08-07.md", catalyst_score=8.5)
    write_eval(hot, "pass")
    settings = ar.RunnerSettings(
        ab_pairing="paired", ab_core_pairs_per_slot=2, max_runs_per_slot=3
    )
    outcome, _ = run(config, make_resolvers(macro, {"HOT": hot}), FakeChild(), settings=settings)
    assert outcome.summary["planned"] == 3
    assert [d["ticker"] for d in outcome.summary["dropped"]][0] == "HOT"
    records = read_records(config)
    assert sorted({r.ticker for r in records}) == ["AAA", "BBB"]


# ---------------------------------------------------------------------------
# Ledger writes: flock uniqueness, inputs hashes, config digest (AC3)
# ---------------------------------------------------------------------------


def _base_fields():
    return {
        "pair_id": None,
        "date": "2026-08-07",
        "session": "us",
        "ticker": "NVDA",
        "arm": "brief",
        "preset": "default",
        "trigger": "manual",
        "catalyst_score": None,
        "macro_eval_verdict": "missing",
        "ticker_eval_verdict": "missing",
        "inputs": {
            "macro_brief": None,
            "ticker_brief": None,
            "pool": None,
            "config_digest": "d" * 64,
        },
        "decided_at": "2026-08-07T13:00:00Z",
        "decision": "HOLD",
        "plan": None,
        "plan_valid": True,
    }


@pytest.mark.unit
def test_run_id_flock_uniqueness_under_concurrent_append(tmp_path):
    ledger = tmp_path / "ledger"
    with ThreadPoolExecutor(max_workers=8) as pool:
        records = list(
            pool.map(lambda _: ar.mint_and_append_decision(ledger, _base_fields()), range(16))
        )
    numbers = sorted(int(r.run_id.rsplit("-", 1)[1]) for r in records)
    assert numbers == list(range(1, 17))  # no duplicate <n> minted
    lines = (ledger / ar.DECISIONS_NAME).read_text(encoding="utf-8").splitlines()
    assert len(lines) == 16
    for line in lines:
        DecisionRecord.model_validate(json.loads(line))  # no interleaved writes


@pytest.mark.unit
def test_pair_attempt_minted_under_the_same_flock_is_unique(tmp_path):
    """Concurrent paired invocations of one ticker must never share an a<k>:
    the attempt is reserved by the pair's FIRST appended row, under the same
    flock as the append (contract: a pair_id owns exactly two rows)."""
    ledger = tmp_path / "ledger"

    def append_pair(_):
        fields = {**_base_fields(), "trigger": "core"}
        first = ar.mint_and_append_decision(ledger, fields, mint_pair_attempt=True)
        second = ar.mint_and_append_decision(
            ledger, {**fields, "arm": "feeds", "pair_id": first.pair_id}
        )
        return first, second

    with ThreadPoolExecutor(max_workers=8) as pool:
        pairs = list(pool.map(append_pair, range(8)))
    attempts = sorted(int(first.pair_id.rsplit("a", 1)[1]) for first, _ in pairs)
    assert attempts == list(range(1, 9))  # every invocation got its own attempt
    for first, second in pairs:
        assert first.pair_id == second.pair_id
        assert {first.arm, second.arm} == {"brief", "feeds"}


@pytest.mark.unit
def test_inputs_hashes_bind_to_exact_files_served(tmp_path):
    config = make_config(tmp_path)
    pool_path = write_pool(config, "us", SLOT, core=("AAA",))
    macro = write_brief(config.macro_brief_dir / "2026-08-07.us.md")
    write_eval(macro, "pass")
    brief = write_brief(config.ticker_brief_dir / "AAA" / "2026-08-07.md", catalyst_score=4.0)
    write_eval(brief, "warn")
    run(config, make_resolvers(macro, {"AAA": brief}), FakeChild())

    record = read_records(config)[0]
    assert record.inputs.macro_brief.path == "2026-08-07.us.md"
    assert record.inputs.macro_brief.sha256 == sha256_file(macro)
    assert record.inputs.ticker_brief.path == "AAA/2026-08-07.md"
    assert record.inputs.ticker_brief.sha256 == sha256_file(brief)
    assert record.inputs.pool.path == "us/2026-08-07.json"
    assert record.inputs.pool.sha256 == sha256_file(pool_path)
    assert record.ticker_eval_verdict == "warn"  # verdict as consumed
    assert record.decided_at == NOW
    assert record.preset == "default"


@pytest.mark.unit
def test_brief_re_resolved_at_spawn_time_not_planning_time(tmp_path):
    """Ledger contract: hashes are computed at CONSUMPTION time. A mid-slot
    re-collection landing while an earlier ticker runs must be served and
    recorded as the NEW revision (hash + verdict), not the planning-time one."""
    config = make_config(tmp_path)
    write_pool(config, "us", SLOT, core=("AAA", "BBB"))
    macro = write_brief(config.macro_brief_dir / "2026-08-07.us.md")
    write_eval(macro, "pass")
    brief_b = write_brief(config.ticker_brief_dir / "BBB" / "2026-08-07.md", catalyst_score=3.0)
    write_eval(brief_b, "pass")
    planning_sha = sha256_file(brief_b)

    class RecollectingChild(FakeChild):
        def __call__(self, spec, timeout):
            if spec.ticker == "AAA":  # re-collection lands during AAA's run
                write_brief(brief_b, generated_at="2026-08-07T09:00:00Z", catalyst_score=9.9)
                write_eval(brief_b, "warn")
            return super().__call__(spec, timeout)

    settings = ar.RunnerSettings(ab_pairing="off", analysis_job_concurrency=1)
    run(
        config,
        make_resolvers(macro, {"BBB": brief_b}),
        RecollectingChild(),
        settings=settings,
    )
    record = by_ticker_arm(read_records(config))[("BBB", "brief")]
    assert record.inputs.ticker_brief.sha256 == sha256_file(brief_b)  # NEW revision
    assert record.inputs.ticker_brief.sha256 != planning_sha
    assert record.ticker_eval_verdict == "warn"  # verdict re-read at spawn


@pytest.mark.unit
def test_mid_run_brief_overwrite_is_flagged_as_revision_drift(tmp_path, caplog):
    """A brief overwritten while the child runs cannot be re-recorded (the
    child may have read either revision) — the run is flagged instead."""
    config = make_config(tmp_path)
    write_pool(config, "us", SLOT, core=("AAA",))
    macro = write_brief(config.macro_brief_dir / "2026-08-07.us.md")
    write_eval(macro, "pass")

    class OverwritingChild(FakeChild):
        def __call__(self, spec, timeout):
            write_brief(macro, generated_at="2026-08-07T09:00:00Z")  # bytes change mid-run
            return super().__call__(spec, timeout)

    with caplog.at_level("WARNING", logger="pipeline.analysis_runner"):
        _, emitted = run(config, make_resolvers(macro), OverwritingChild())
    assert "macro-brief-revision-drift" in emitted[0]["flags"]
    assert any("changed on disk" in message for message in caplog.messages)


@pytest.mark.unit
def test_config_digest_snapshot_written_once_and_resolvable(tmp_path):
    config = make_config(tmp_path)
    write_pool(config, "us", SLOT, core=("AAA",))
    macro = write_brief(config.macro_brief_dir / "2026-08-07.us.md")
    write_eval(macro, "pass")
    resolvers = make_resolvers(macro)
    run(config, resolvers, FakeChild())
    run(config, resolvers, FakeChild(), force=True)

    record = read_records(config)[0]
    digest = record.inputs.config_digest
    snapshots = list((config.ledger_dir / ar.CONFIG_SNAPSHOT_DIR).iterdir())
    assert [p.name for p in snapshots] == [f"{digest}.json"]  # written exactly once
    snapshot = json.loads(snapshots[0].read_text(encoding="utf-8"))
    # Content-addressed: the digest re-derives from the snapshot content.
    assert sha256_text(json.dumps(snapshot, sort_keys=True, ensure_ascii=False)) == digest
    assert snapshot["ab_pairing"] == "off"
    assert snapshot["analysis_job_concurrency"] == 2
    # The snapshot covers the ANALYSIS-side effective configuration: the
    # graph's resolved LLM backend/models and the trader-prompt hashes — not
    # the offline collectors' templates.
    from tradingagents.agents.trader.trader import TRADE_PLAN_INSTRUCTIONS
    from tradingagents.default_config import DEFAULT_CONFIG

    assert snapshot["graph"]["llm_provider"] == DEFAULT_CONFIG["llm_provider"]
    assert snapshot["graph"]["deep_think_llm"] == DEFAULT_CONFIG["deep_think_llm"]
    assert snapshot["graph"]["quick_think_llm"] == DEFAULT_CONFIG["quick_think_llm"]
    assert set(snapshot["prompt_templates"]) == {
        "trader.trade_plan_instructions",
        "trader.position_maintain_instructions",
        "run_one.repair_prompt",
    }
    assert snapshot["prompt_templates"]["trader.trade_plan_instructions"] == sha256_text(
        TRADE_PLAN_INSTRUCTIONS
    )


@pytest.mark.unit
def test_config_digest_differs_across_analysis_llm_configs(tmp_path):
    """Two runs under different analysis LLMs (or an edited trader prompt)
    must never silently share a digest in the A/B ledger."""
    settings = ar.RunnerSettings()
    base = {"graph": {"deep_think_llm": "model-a"}, "prompt_templates": {"p": "h1"}}
    other_llm = {"graph": {"deep_think_llm": "model-b"}, "prompt_templates": {"p": "h1"}}
    other_prompt = {"graph": {"deep_think_llm": "model-a"}, "prompt_templates": {"p": "h2"}}
    digests = {
        ar.write_config_digest(tmp_path / name, settings, analysis_config=cfg)
        for name, cfg in (("l1", base), ("l2", other_llm), ("l3", other_prompt))
    }
    assert len(digests) == 3


# ---------------------------------------------------------------------------
# TradePlan validation wiring (AC2) + ERROR rows (R3)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_plan_validator_wiring_matrix(tmp_path):
    config = make_config(tmp_path)
    tickers = ("GOOD", "INVERT", "TIGHT", "WIDE", "FARR", "NANC", "ADDX", "HOLDP")
    write_pool(config, "us", SLOT, core=tickers)
    plans = {
        "GOOD": VALID_PLAN,
        "INVERT": {**VALID_PLAN, "entry_zone": [101.0, 99.0]},
        # entry mid 100, stop distance 0.6 < 0.5×ATR14 (1.0) but still below the zone
        "TIGHT": {**VALID_PLAN, "entry_zone": [99.5, 100.5], "stop": 99.4},
        "WIDE": {**VALID_PLAN, "stop": 80.0},  # distance 20 > 15% of entry mid
        # entry mid 116 — more than ±10% from the last close (100)
        "FARR": {**VALID_PLAN, "entry_zone": [115.0, 117.0], "stop": 110.0,
                 "targets": [130.0]},
        "NANC": {**VALID_PLAN, "conviction": float("nan")},
        "ADDX": {**VALID_PLAN, "add_intent": True, "add_rationale": "  "},
        # HOLD plan carries no execution fields — hygiene only, trivially valid.
        "HOLDP": {"action": "HOLD", "conviction": 0.5},
    }
    _, emitted = run(config, make_resolvers(None), FakeChild(plans=plans))

    valid = {r.ticker: r.plan_valid for r in read_records(config)}
    assert valid == {
        "GOOD": True, "INVERT": False, "TIGHT": False, "WIDE": False,
        "FARR": False, "NANC": False, "ADDX": False, "HOLDP": True,
    }
    violations = {line["ticker"]: line["violations"] for line in emitted}
    assert violations["GOOD"] is None
    assert violations["HOLDP"] is None
    assert any("not ordered" in v for v in violations["INVERT"])
    assert any("below 0.5×ATR14" in v for v in violations["TIGHT"])
    assert any("above 15%" in v for v in violations["WIDE"])
    assert any("not within ±10% of last close" in v for v in violations["FARR"])
    assert any("conviction" in v for v in violations["NANC"])
    assert any("add_rationale" in v for v in violations["ADDX"])


@pytest.mark.unit
def test_hold_without_plan_is_trivially_valid(tmp_path):
    config = make_config(tmp_path)
    write_pool(config, "us", SLOT, core=("AAA",))
    child = FakeChild(plans={"AAA": None}, decisions={"AAA": "HOLD"})
    outcome, _ = run(config, make_resolvers(None), child)
    record = read_records(config)[0]
    assert (record.decision, record.plan, record.plan_valid) == ("HOLD", None, True)
    assert outcome.summary["invalid_plans"] == 0


@pytest.mark.unit
def test_plan_extraction_failure_records_directional_opinion(tmp_path):
    config = make_config(tmp_path)
    write_pool(config, "us", SLOT, core=("AAA",))
    child = FakeChild(
        plans={"AAA": None}, decisions={"AAA": "BUY"},
        plan_errors={"AAA": "no fenced JSON block; after repair: still none"},
    )
    outcome, emitted = run(config, make_resolvers(None), child)
    record = read_records(config)[0]
    assert (record.decision, record.plan, record.plan_valid) == ("BUY", None, False)
    assert outcome.summary["invalid_plans"] == 1
    assert "no fenced JSON block" in emitted[0]["violations"][0]


@pytest.mark.unit
def test_child_crash_records_error_row_and_slot_continues(tmp_path):
    config = make_config(tmp_path)
    write_pool(config, "us", SLOT, core=("AAA", "BBB", "CCC"))
    outcome, emitted = run(config, make_resolvers(None), FakeChild(fail=("BBB",)))
    records = by_ticker_arm(read_records(config))
    assert len(records) == 3  # the slot never aborts on one ticker (R3)
    error_row = records[("BBB", "brief")]
    assert (error_row.decision, error_row.plan, error_row.plan_valid) == ("ERROR", None, False)
    assert records[("AAA", "brief")].decision == "BUY"
    assert records[("CCC", "brief")].decision == "BUY"
    assert outcome.summary["errors"] == 1
    assert outcome.summary["completed"] == 2
    line = next(entry for entry in emitted if entry["ticker"] == "BBB")
    assert line["decision"] == "ERROR"
    assert "child boom" in line["error"]


@pytest.mark.unit
def test_child_runner_exception_is_an_error_row_not_a_crash(tmp_path):
    config = make_config(tmp_path)
    write_pool(config, "us", SLOT, core=("AAA",))

    def exploding_child(spec, timeout):
        raise RuntimeError("seam blew up")

    outcome, _ = run(config, make_resolvers(None), exploding_child)
    assert read_records(config)[0].decision == "ERROR"
    assert outcome.summary["errors"] == 1


@pytest.mark.unit
def test_ledger_append_failure_never_aborts_the_slot(tmp_path, monkeypatch):
    """R3 belt-and-suspenders: a row that cannot be minted (validation bug,
    disk error) is surfaced on the run line and in the summary — the
    remaining tickers still run and their rows still land."""
    config = make_config(tmp_path)
    write_pool(config, "us", SLOT, core=("AAA", "BBB"))
    real_mint = ar.mint_and_append_decision

    def flaky_mint(ledger_dir, fields, **kwargs):
        if fields["ticker"] == "AAA":
            raise RuntimeError("disk full")
        return real_mint(ledger_dir, fields, **kwargs)

    monkeypatch.setattr(ar, "mint_and_append_decision", flaky_mint)
    outcome, emitted = run(config, make_resolvers(None), FakeChild())

    records = read_records(config)
    assert [r.ticker for r in records] == ["BBB"]  # the slot continued
    aaa = next(line for line in emitted if line["ticker"] == "AAA")
    assert aaa["run_id"] is None
    assert "ledger append failed" in aaa["error"]
    assert aaa["execute_eligible"] is False  # no row ⇒ nothing to join/execute
    assert outcome.summary["errors"] == 1
    assert outcome.summary["completed"] == 1
    assert outcome.summary["run_ids"] == [records[0].run_id]


# ---------------------------------------------------------------------------
# positions.json → trader context (contract staleness rule)
# ---------------------------------------------------------------------------


def _write_positions(config, as_of, ticker="AAA"):
    payload = {
        "as_of": as_of,
        "equity": 99985.65,
        "positions": [
            {
                "ticker": ticker,
                "qty": 12,
                "avg_entry": 283.6,
                "last_fill_at": "2026-08-06T14:32:00Z",
                "market_value": 3520.0,
                "unrealized_pl": 114.7,
                "tranches": 1,
                "open_orders": [
                    {"client_order_id": "2026-08-06-us-AAA-entry", "leg": "stop", "price": 268.0}
                ],
            }
        ],
    }
    config.ledger_dir.mkdir(parents=True, exist_ok=True)
    (config.ledger_dir / ar.POSITIONS_NAME).write_text(json.dumps(payload), encoding="utf-8")


@pytest.mark.unit
def test_fresh_position_snapshot_reaches_the_child(tmp_path):
    config = make_config(tmp_path)
    write_pool(config, "us", SLOT, core=("AAA", "BBB"))
    _write_positions(config, "2026-08-07T05:00:00Z")
    child = FakeChild()
    run(config, make_resolvers(None), child)
    contexts = {spec.ticker: spec.position_context for spec in child.specs}
    assert "- qty: 12" in contexts["AAA"]
    assert "- avg_entry: 283.6" in contexts["AAA"]
    assert "- unrealized_pl: 114.7" in contexts["AAA"]
    assert "268 (2026-08-06-us-AAA-entry)" in contexts["AAA"]
    assert contexts["BBB"] is None  # flat ticker: no position block


@pytest.mark.unit
def test_stale_position_snapshot_injects_unavailable(tmp_path):
    config = make_config(tmp_path)
    write_pool(config, "us", SLOT, core=("AAA",))
    _write_positions(config, "2026-08-05T12:00:00Z")  # Wednesday; Thursday missed
    child = FakeChild()
    run(config, make_resolvers(None), child)
    assert child.specs[0].position_context.startswith("position data unavailable")


@pytest.mark.unit
def test_position_snapshot_staleness_uses_trading_days():
    monday = date(2026, 8, 10)
    friday = datetime(2026, 8, 7, 20, 0, tzinfo=timezone.utc)
    thursday = datetime(2026, 8, 6, 20, 0, tzinfo=timezone.utc)
    # Friday's refresh is the most recent trading day's state on Monday.
    assert ar.position_snapshot_stale(friday, monday) is False
    assert ar.position_snapshot_stale(thursday, monday) is True
    assert ar.position_snapshot_stale(friday, date(2026, 8, 7)) is False  # same day


# ---------------------------------------------------------------------------
# Settings, child argv/env, CLI seams
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_load_settings_env_overrides_and_defaults():
    defaults = ar.load_settings(env={})
    assert defaults == ar.RunnerSettings()
    assert defaults.analysis_job_concurrency == 2
    assert defaults.analysis_trigger_threshold == 7.0
    assert defaults.ab_pairing == "paired"
    assert defaults.ab_core_pairs_per_slot == 3
    assert defaults.max_runs_per_slot == 30
    assert defaults.execute_on_missing_eval is False

    overridden = ar.load_settings(
        env={
            ar.ANALYSIS_JOB_CONCURRENCY_ENV: "3",
            ar.TRIGGER_THRESHOLD_ENV: "6.5",
            ar.AB_PAIRING_ENV: "off",
            ar.AB_CORE_PAIRS_ENV: "2",
            ar.MAX_RUNS_ENV: "10",
            ar.EXECUTE_ON_MISSING_EVAL_ENV: "true",
            ar.PRESET_ENV: "claude-sub",
        }
    )
    assert overridden.analysis_job_concurrency == 3
    assert overridden.analysis_trigger_threshold == 6.5
    assert overridden.ab_pairing == "off"
    assert overridden.ab_core_pairs_per_slot == 2
    assert overridden.max_runs_per_slot == 10
    assert overridden.execute_on_missing_eval is True
    assert overridden.preset == "claude-sub"
    # --preset beats the env value.
    assert ar.load_settings(env={ar.PRESET_ENV: "x"}, preset_override="y").preset == "y"
    with pytest.raises(ValueError, match="ab_pairing"):
        ar.load_settings(env={ar.AB_PAIRING_ENV: "sometimes"})
    with pytest.raises(ValueError, match="analysis_job_concurrency"):
        ar.load_settings(env={ar.ANALYSIS_JOB_CONCURRENCY_ENV: "0"})


@pytest.mark.unit
def test_make_child_runner_argv_and_env(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    captured = {}

    class FakeProc:
        pid = 4321
        returncode = 0

        def communicate(self, timeout=None):
            captured["timeout"] = timeout
            stdout = (
                "log noise\n" + ar.RESULT_SENTINEL + json.dumps({"decision": "HOLD"}) + "\n"
            )
            return (stdout, "")

    def fake_popen(argv, **kwargs):
        captured.update(argv=list(argv), kwargs=kwargs)
        return FakeProc()

    monkeypatch.setattr(ar.subprocess, "Popen", fake_popen)
    child = ar.make_child_runner(config)
    spec = ar.ChildSpec("NVDA", "2026-08-07", "us", "brief", True, "- qty: 1")
    result = child(spec, 123.0)
    assert result.payload == {"decision": "HOLD"}  # the sentinel line wins
    argv = captured["argv"]
    assert argv[1:3] == ["-m", "pipeline.run_one"]
    assert argv[argv.index("--ticker") + 1] == "NVDA"
    assert argv[argv.index("--arm") + 1] == "brief"
    assert "--withhold-ticker-brief" in argv
    assert argv[argv.index("--position-context") + 1] == "- qty: 1"
    assert captured["timeout"] == 123.0
    # Own session, so a timeout can kill the whole group (orchestrator pattern).
    assert captured["kwargs"]["start_new_session"] is True
    # The child resolves the SAME data dirs as this runner (env symmetry).
    env = captured["kwargs"]["env"]
    assert env["TRADINGAGENTS_STATE_DIR"] == str(config.state_dir)
    assert env["TRADINGAGENTS_TICKER_BRIEF_DIR"] == str(config.ticker_brief_dir)
    assert env["TRADINGAGENTS_LEDGER_DIR"] == str(config.ledger_dir)


@pytest.mark.unit
def test_child_runner_timeout_kills_the_process_group(tmp_path, monkeypatch):
    """Mirror of the orchestrator's group-kill: a timed-out run_one must not
    orphan a quota-burning grandchild."""
    config = make_config(tmp_path)
    events = []

    class HungProc:
        pid = 9999
        returncode = None

        def communicate(self, timeout=None):
            if timeout is not None:
                raise subprocess.TimeoutExpired(cmd="run_one", timeout=timeout)
            events.append("reaped")
            return ("", "")

    monkeypatch.setattr(ar.subprocess, "Popen", lambda argv, **kwargs: HungProc())
    monkeypatch.setattr(ar.os, "killpg", lambda pgid, sig: events.append((pgid, sig)))
    child = ar.make_child_runner(config)
    result = child(ar.ChildSpec("NVDA", "2026-08-07", "us", "brief"), 5.0)
    assert result.returncode == -1
    assert "timed out" in result.error
    assert (9999, signal.SIGKILL) in events
    assert "reaped" in events


@pytest.mark.unit
def test_child_result_sentinel_survives_fd_level_stdout_junk():
    """The result line is located by sentinel, not position: an fd-1 write
    without a trailing newline (invisible to redirect_stdout) concatenates
    with the result print and must not void a completed, billed run."""
    payload = {"decision": "HOLD"}
    tagged = ar.RESULT_SENTINEL + json.dumps(payload)
    assert ar._extract_result_payload("progress 42%" + tagged + "\n") == payload
    # Stray bytes AFTER the JSON object are tolerated too (raw_decode).
    assert ar._extract_result_payload(tagged + " trailing gunk\n") == payload
    # Later noise lines never shadow the sentinel line.
    assert ar._extract_result_payload(tagged + "\nshutdown noise\n") == payload
    # Untagged output can never be mistaken for a result line.
    assert ar._extract_result_payload(json.dumps(payload) + "\n") is None
    assert ar._extract_result_payload("") is None
    assert ar._extract_result_payload(ar.RESULT_SENTINEL + "{broken\n") is None


@pytest.mark.unit
def test_main_manual_session_mismatch_is_usage_error(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("TRADINGAGENTS_STATE_DIR", str(tmp_path / "state"))
    rc = ar.main(["--session", "us", "--ticker", "0700.HK"])
    assert rc == 2
    assert "belongs to session 'cn'" in capsys.readouterr().err


@pytest.mark.unit
def test_main_manual_id_illegal_ticker_is_usage_error(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("TRADINGAGENTS_STATE_DIR", str(tmp_path / "state"))
    rc = ar.main(["--session", "us", "--ticker", "nvda!!"])
    assert rc == 2
    assert "not a valid ledger symbol" in capsys.readouterr().err
    # A merely-lowercase manual ticker is normalized, not rejected.
    rc = ar.main(
        ["--session", "us", "--ticker", "aaa", "--date", SLOT.isoformat()],
        child_runner=FakeChild(),
        resolvers=make_resolvers(None),
        ohlcv_fetcher=lambda ticker, on_date: (100.0, 2.0),
    )
    assert rc == 0
    out_lines = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert out_lines[0]["ticker"] == "AAA"


@pytest.mark.unit
def test_main_emits_per_run_lines_and_final_summary(tmp_path, monkeypatch, capsys):
    config_dir = tmp_path / "state"
    monkeypatch.setenv("TRADINGAGENTS_STATE_DIR", str(config_dir))
    monkeypatch.setenv(ar.AB_PAIRING_ENV, "off")
    config = PipelineConfig(state_dir=config_dir)
    write_pool(config, "us", SLOT, core=("AAA",))
    rc = ar.main(
        ["--session", "us", "--date", SLOT.isoformat()],
        child_runner=FakeChild(),
        resolvers=make_resolvers(None),
        ohlcv_fetcher=lambda ticker, on_date: (100.0, 2.0),
    )
    assert rc == 0
    out_lines = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert len(out_lines) == 2  # one per run (R4) + the slot summary
    assert out_lines[0]["run_id"] == "2026-08-07-us-AAA-brief-1"
    summary = out_lines[-1]
    assert summary["planned"] == 1
    assert summary["completed"] == 1
    assert summary["errors"] == 0
    assert summary["invalid_plans"] == 0
