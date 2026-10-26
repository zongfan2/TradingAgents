"""Decision-funnel snapshot builder — specs/decision-funnel.md R3.

One fixture us slot covers every reconstruction rule; recorded events,
pending execution, cn slots, determinism, backfill and the CLI build on it.
"""

import hashlib
import json
from datetime import date, datetime, timezone

import pytest

from pipeline import funnel
from pipeline.config import PipelineConfig
from pipeline.funnel_events import FunnelEvents
from tests.test_execution_adapter import BUY_PLAN, write_decision

DAY = date(2026, 10, 23)
D = DAY.isoformat()
BANDS = {"close": 100.0, "mid": 98.0, "upper": 110.0, "lower": 90.0}


def technical(gate="pass"):
    return {"gate": gate, "boll_daily": BANDS, "boll_weekly": BANDS}


def member(ticker, score, entered_on=D):
    return {
        "ticker": ticker, "score": score, "catalyst_type": "earnings",
        "rationale": "r", "citations": ["https://example.com/a"],
        "technical": technical(), "entered_on": entered_on,
        "low_score_streak": 0, "gate_fail_streak": 0,
    }


def on_slot_day():
    return datetime(2026, 10, 23, 13, 45, tzinfo=timezone.utc)  # 09:45 New York


def make_config(tmp_path) -> PipelineConfig:
    return PipelineConfig(state_dir=tmp_path / "state")


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def write_brief(config, ticker, score, verdict=None, session="us", tamper=False):
    path = config.ticker_brief_dir / ticker / f"{D}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    text = (
        f"---\nas_of_date: {D}\nticker: {ticker}\nsession: {session}\n"
        f"generated_at: {D}T10:00:00Z\ngenerator: test\nsources_count: 1\n"
        f"catalyst_score: {score}\ncatalyst_type: earnings\ncatalyst_window: none\n---\n\nbody\n"
    )
    path.write_text(text, encoding="utf-8")
    if verdict is not None:
        sha = hashlib.sha256(text.encode()).hexdigest()
        write_json(path.with_name(f"{D}.eval.json"), {
            "brief_sha256": "0" * 64 if tamper else sha, "verdict": verdict,
        })
    return path


def write_order(config, ticker, kind, *, dry_run=False, reason=None, status="accepted", arm="brief",
                written_at=f"{D}T13:45:00Z", attempt=1):
    row = {
        "run_id": f"{D}-us-{ticker}-{arm}-{attempt}", "written_at": written_at,
        "session": "us", "ticker": ticker, "kind": kind, "dry_run": dry_run,
        "reason": reason, "client_order_id": f"{D}-us-{ticker}-entry",
        "order_kind": None if kind == "skip" else "bracket", "tif": "gtc", "qty": 10,
        "limit_price": 100.0, "stop_price": 90.0, "target_price": 110.0,
        "broker_status": None if kind == "skip" else status,
    }
    path = config.ledger_dir / "orders.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(row) + "\n")


def seed_slot(config, *, orders=True):
    """us slot: core AAA (paired, BUY live) + BBB (no brief, no decision);
    opportunity CCC (entered today, BUY, stale-quote), DDD (member, score 5,
    below the trigger), HHH (score 8, eval fail); watch EEE; removed FFF;
    nominees GGG (not in the pool), 0700.HK (wrong session), CCC twice."""
    write_json(config.pool_dir / "us" / f"{D}.json", {
        "as_of_date": D, "session": "us", "generated_at": f"{D}T09:30:00Z",
        "generator": "test", "carried_forward": False,
        "core": [{"ticker": "AAA"}, {"ticker": "BBB"}],
        "opportunity": [
            member("CCC", 8.0), member("DDD", 5.0, "2026-10-20"), member("HHH", 8.0, "2026-10-21"),
        ],
        "watch": [{k: v for k, v in member("EEE", 6.5).items()
                   if k not in ("entered_on", "low_score_streak", "gate_fail_streak")}],
        "removed": [{"ticker": "FFF", "reason": "score<4 for 3 pools", "last_score": 3.0}],
    })
    write_json(config.pool_dir / "us" / f".nomination-cache-{D}.json", {"backend": "claude", "nominations": [
        {"ticker": "CCC", "score": 8.0, "catalyst_type": "earnings"},
        {"ticker": "EEE", "score": 6.5, "catalyst_type": "earnings"},
        {"ticker": "GGG", "score": 3.0, "catalyst_type": "guidance"},
        {"ticker": "0700.HK", "score": 9.0, "catalyst_type": "earnings"},
        {"ticker": "CCC", "score": 8.0, "catalyst_type": "earnings"},
    ]})
    log = config.ticker_brief_dir / "collector.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(
        f"{D} | us | AAA | 9 | 6.0 | written\n{D} | us | CCC | 9 | 8.0 | written\n"
        f"{D} | cn | CCC | 9 | 8.0 | failed\n2026-10-22 | us | DDD | 1 | 5.0 | failed\n",
        encoding="utf-8",
    )
    write_brief(config, "AAA", 6.0, "pass")
    write_brief(config, "CCC", 8.0, "warn")
    write_brief(config, "DDD", 5.0)
    write_brief(config, "HHH", 8.0, "fail")
    write_decision(config, ticker="AAA", date_iso=D, plan=BUY_PLAN)
    write_decision(config, ticker="AAA", arm="feeds", date_iso=D, plan=None, decision="HOLD")
    # write_decision writes unpaired rows: make AAA's two arms a pair.
    path = config.ledger_dir / "decisions.jsonl"
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    for row in rows:
        if row["ticker"] == "AAA":
            row["pair_id"] = f"{D}-us-AAA-a1"
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))
    write_decision(config, ticker="CCC", trigger="catalyst", date_iso=D, plan=BUY_PLAN)
    if orders:
        write_order(config, "AAA", "intent")
        write_order(config, "AAA", "submitted")
        write_order(config, "CCC", "skip", reason="stale-quote")


def ticker_map(snapshot):
    return {entry["ticker"]: entry for entry in snapshot["tickers"]}


def stage(snapshot, ticker, name):
    entry = ticker_map(snapshot)[ticker]["stages"].get(name)
    return None if entry is None else (entry["outcome"], entry["code"], entry["provenance"])


@pytest.mark.unit
def test_backfilled_slot_reconstructs_every_ticker_once(tmp_path):
    config = make_config(tmp_path)
    seed_slot(config)
    snap = funnel.build_snapshot(config, "us", DAY)
    tickers = [entry["ticker"] for entry in snap["tickers"]]
    assert tickers == sorted(set(tickers))
    assert set(tickers) == {"AAA", "BBB", "CCC", "DDD", "EEE", "FFF", "GGG", "HHH", "0700.HK"}

    # nominate: wrong session, duplicate keeps the passed nomination.
    assert stage(snap, "0700.HK", "nominate") == ("excluded", "wrong-session", "reconstructed")
    assert stage(snap, "CCC", "nominate") == ("passed", "nominated", "reconstructed")
    # pool: layers, removed reason prefix, unrecorded drop cause is unknown.
    assert stage(snap, "AAA", "pool") == ("passed", "core", "reconstructed")
    assert stage(snap, "CCC", "pool") == ("passed", "opportunity-enter", "reconstructed")
    assert stage(snap, "DDD", "pool") == ("passed", "opportunity-stay", "reconstructed")
    assert stage(snap, "EEE", "pool") == ("excluded", "watch", "reconstructed")
    assert stage(snap, "FFF", "pool") == ("excluded", "removed-low-score-streak", "reconstructed")
    assert stage(snap, "GGG", "pool") == ("excluded", None, "unknown")
    # collect: log rows for this slot only; watch is never collected.
    assert stage(snap, "AAA", "collect") == ("passed", "written", "reconstructed")
    assert stage(snap, "DDD", "collect") == ("passed", None, "unknown")  # brief, no log row
    assert stage(snap, "EEE", "collect") == ("excluded", "not-collected-watch", "reconstructed")
    assert stage(snap, "BBB", "collect") is None
    # evaluate: absent brief (core info), below threshold, fail (opportunity excluded).
    assert stage(snap, "BBB", "evaluate") == ("info", "skipped-absent", "reconstructed")
    assert stage(snap, "DDD", "evaluate") == ("excluded", "skipped-below-threshold", "reconstructed")
    assert stage(snap, "HHH", "evaluate") == ("excluded", "fail", "reconstructed")
    assert stage(snap, "CCC", "evaluate") == ("passed", "warn", "reconstructed")
    # trigger
    assert stage(snap, "AAA", "trigger") == ("passed", "core-pair", "reconstructed")
    assert stage(snap, "CCC", "trigger") == ("passed", "catalyst", "reconstructed")
    assert stage(snap, "DDD", "trigger") == ("excluded", "below-threshold", "reconstructed")
    assert stage(snap, "HHH", "trigger") == ("excluded", "eval-fail", "reconstructed")
    assert stage(snap, "BBB", "trigger") == ("info", None, "unknown")
    # decide: both arms kept; the strongest decides.
    aaa = ticker_map(snap)["AAA"]
    assert stage(snap, "AAA", "decide") == ("passed", "BUY", "reconstructed")
    assert [e["details"]["arm"] for e in aaa["stages"]["decide"]["events"]] == ["brief", "feeds"]
    # execute: the brief arm's live order wins over the feeds non-candidate.
    assert stage(snap, "AAA", "execute") == ("passed", "submitted", "reconstructed")
    assert aaa["final"] == "order-live"
    assert {e["code"] for e in aaa["stages"]["execute"]["events"]} == {"submitted", "feeds-arm"}
    ccc = ticker_map(snap)["CCC"]
    assert ccc["stop"] == {
        "stage": "execute", "code": "stale-quote", "reason": "stale-quote",
        "provenance": "reconstructed",
    }
    assert ccc["final"] == "excluded"
    assert ticker_map(snap)["EEE"]["stop"]["stage"] == "pool"
    assert ticker_map(snap)["BBB"]["final"] == "unknown"

    assert snap["counts"]["final"] == {
        "order-live": 1, "order-dry": 0, "excluded": 7, "pending": 0, "unknown": 1,
    }
    context = snap["context"]
    assert context["execute_ran"] is True
    assert context["thresholds"]["trigger"] == 7.0
    assert context["thresholds"]["source"]["trigger"] == "default"
    assert context["inputs"][f"pools/us/{D}.json"]
    assert "ticker_briefs/collector.log#slot" in context["inputs"]


@pytest.mark.unit
def test_backfill_never_invents_a_code(tmp_path):
    """Acceptance 3: a stage the files cannot explain is unknown with a null
    code — never a guessed one."""
    config = make_config(tmp_path)
    seed_slot(config)
    snap = funnel.build_snapshot(config, "us", DAY)
    for entry in snap["tickers"]:
        for record in entry["stages"].values():
            if record["provenance"] == "unknown":
                assert record["code"] is None
            else:
                assert record["code"] is not None


@pytest.mark.unit
def test_recorded_events_take_precedence_per_stage(tmp_path):
    config = make_config(tmp_path)
    seed_slot(config)
    events = FunnelEvents(config.state_dir, "us", DAY, "pool_builder")
    events.emit("pool", "GGG", "excluded", "dropped-low-score", "score 3 < exit 4",
                score=3.0, thresholds={"entry": 6.5, "exit": 4.5})
    events.emit("nominate", None, "info", "nomination-failed", "x")  # ticker-less: context only
    snap = funnel.build_snapshot(config, "us", DAY)
    assert stage(snap, "GGG", "pool") == ("excluded", "dropped-low-score", "recorded")
    assert stage(snap, "GGG", "nominate") == ("passed", "nominated", "reconstructed")
    assert ticker_map(snap)["GGG"]["provenance"] == ["reconstructed", "recorded"]
    assert snap["context"]["thresholds"]["entry"] == 6.5
    assert snap["context"]["thresholds"]["source"]["entry"] == "events"
    assert snap["context"]["nomination_failed"] == "x"
    assert snap["context"]["events"] == 2
    assert None not in ticker_map(snap)


@pytest.mark.unit
def test_us_slot_before_execute_is_pending(tmp_path):
    config = make_config(tmp_path)
    seed_slot(config, orders=False)
    snap = funnel.build_snapshot(config, "us", DAY)
    assert ticker_map(snap)["AAA"]["final"] == "pending"
    assert ticker_map(snap)["AAA"]["furthest_stage"] == "decide"
    assert snap["context"]["execute_ran"] is False


@pytest.mark.unit
def test_rejected_and_dry_orders(tmp_path):
    config = make_config(tmp_path)
    seed_slot(config, orders=False)
    write_order(config, "AAA", "submitted", dry_run=True)
    write_order(config, "CCC", "submitted", status="rejected")
    snap = funnel.build_snapshot(config, "us", DAY)
    assert ticker_map(snap)["AAA"]["final"] == "order-dry"
    assert stage(snap, "CCC", "execute") == ("excluded", "rejected", "reconstructed")


@pytest.mark.unit
def test_live_order_sticks_over_a_later_dedupe(tmp_path):
    config = make_config(tmp_path)
    seed_slot(config)
    write_order(config, "AAA", "skip", reason="dedupe: already placed")
    snap = funnel.build_snapshot(config, "us", DAY)
    assert ticker_map(snap)["AAA"]["final"] == "order-live"


@pytest.mark.unit
def test_cn_decisions_are_never_executed(tmp_path):
    config = make_config(tmp_path)
    write_decision(config, ticker="0700.HK", session="cn", date_iso=D, plan=BUY_PLAN)
    snap = funnel.build_snapshot(config, "cn", DAY)
    assert stage(snap, "0700.HK", "execute") == ("excluded", "non-us-session", "reconstructed")
    assert snap["context"]["execute_ran"] is False


@pytest.mark.unit
def test_eval_verdict_matches_the_runner_rule(tmp_path):
    """The local revision-bound reader agrees with get_eval_verdict."""
    from tradingagents.dataflows.brief_evals import get_eval_verdict

    config = make_config(tmp_path)
    cases = {"AAA": ("pass", False), "BBB": ("fail", True), "CCC": ("bogus", False), "DDD": (None, False)}
    for ticker, (verdict, tamper) in cases.items():
        path = write_brief(config, ticker, 7.0, verdict, tamper=tamper)
        assert funnel._verdict(funnel._Inputs(config), path) == get_eval_verdict(str(path))


@pytest.mark.unit
def test_snapshot_is_byte_identical_and_ignores_later_appends(tmp_path):
    """Acceptance 2: identical inputs give a byte-identical snapshot; another
    day's ledger/log appends do not change this slot's file."""
    config = make_config(tmp_path)
    seed_slot(config)
    path = funnel.write_snapshot(config, "us", DAY)
    first = path.read_bytes()
    assert funnel.write_snapshot(config, "us", DAY).read_bytes() == first
    write_decision(config, ticker="ZZZ", date_iso="2026-10-24", plan=BUY_PLAN)
    # A later day's refresh re-submission under this slot's run_id.
    write_order(config, "CCC", "submitted", written_at="2026-10-24T13:45:00Z")
    with open(config.ticker_brief_dir / "collector.log", "a", encoding="utf-8") as handle:
        handle.write("2026-10-24 | us | ZZZ | 1 | 7.0 | written\n")
    assert funnel.write_snapshot(config, "us", DAY).read_bytes() == first
    assert path == config.state_dir / "funnel" / "us" / f"{D}.json"


@pytest.mark.unit
def test_backfill_builds_slots_with_inputs_and_isolates_failures(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    seed_slot(config)
    write_decision(config, ticker="0700.HK", session="cn", date_iso="2026-10-22", plan=BUY_PLAN)
    written, failures = funnel.backfill(config, date(2026, 10, 21), date(2026, 10, 24))
    assert sorted(p.relative_to(config.state_dir).as_posix() for p in written) == [
        "funnel/cn/2026-10-22.json", f"funnel/us/{D}.json",
    ]
    assert failures == []

    real = funnel.write_snapshot

    def flaky(cfg, session, day):
        if session == "cn":
            raise RuntimeError("boom")
        return real(cfg, session, day)

    monkeypatch.setattr(funnel, "write_snapshot", flaky)
    written, failures = funnel.backfill(config, date(2026, 10, 21), date(2026, 10, 24))
    assert [p.name for p in written] == [f"{D}.json"]
    assert failures == ["cn 2026-10-22: boom"]


@pytest.mark.unit
def test_cli_build_and_backfill(tmp_path, monkeypatch, capsys):
    config = make_config(tmp_path)
    seed_slot(config)
    monkeypatch.setattr(funnel, "load_config", lambda: config)
    assert funnel.main(["build", "--session", "us", "--date", D]) == 0
    assert "written:" in capsys.readouterr().out
    assert funnel.main(["backfill", "--since", D, "--until", D, "--session", "us"]) == 0
    assert (config.state_dir / "funnel" / "us" / f"{D}.json").is_file()


@pytest.mark.unit
def test_core_collect_failure_does_not_stop_a_ticker_that_ran(tmp_path):
    """Review 45f1591 finding 1: the stop is the first exclusion after the
    last passed stage — a core ticker analysed despite a failed collection
    is pending, then stops where execution skipped it."""
    config = make_config(tmp_path)
    seed_slot(config, orders=False)
    write_decision(config, ticker="BBB", date_iso=D, plan=BUY_PLAN)
    with open(config.ticker_brief_dir / "collector.log", "a", encoding="utf-8") as handle:
        handle.write(f"{D} | us | BBB | 0 | - | failed\n")
    snap = funnel.build_snapshot(config, "us", DAY)
    assert stage(snap, "BBB", "collect") == ("info", "failed", "reconstructed")
    bbb = ticker_map(snap)["BBB"]
    assert (bbb["final"], bbb["stop"]) == ("pending", None)

    write_order(config, "BBB", "skip", reason="stale-quote")
    bbb = ticker_map(funnel.build_snapshot(config, "us", DAY))["BBB"]
    assert bbb["final"] == "excluded"
    assert (bbb["stop"]["stage"], bbb["stop"]["code"]) == ("execute", "stale-quote")


@pytest.mark.unit
def test_recorded_core_collect_exclusion_does_not_stop_either(tmp_path):
    """Events P1 wrote before the core rule still yield the right stop."""
    config = make_config(tmp_path)
    seed_slot(config)
    events = FunnelEvents(config.state_dir, "us", DAY, "ticker_collectors")
    events.emit("collect", "AAA", "excluded", "failed", "old core failure")
    aaa = ticker_map(funnel.build_snapshot(config, "us", DAY))["AAA"]
    assert (aaa["final"], aaa["stop"]) == ("order-live", None)


@pytest.mark.unit
def test_manual_runs_do_not_mark_execute_as_run(tmp_path):
    """Review 45f1591 finding 2: a manual run's decision, order rows and
    execute events on the slot day never turn scheduled tickers non-pending."""
    config = make_config(tmp_path)
    seed_slot(config, orders=False)
    write_decision(config, ticker="MMM", trigger="manual", date_iso=D, plan=BUY_PLAN)
    write_order(config, "MMM", "submitted", dry_run=True)
    events = FunnelEvents(config.state_dir, "us", DAY, "execution_adapter", clock=on_slot_day)
    events.emit("execute", "MMM", "passed", "submitted", "submitted (dry-run)",
                run_id=f"{D}-us-MMM-brief-1", dry_run=True)
    snap = funnel.build_snapshot(config, "us", DAY)
    assert snap["context"]["execute_ran"] is False
    assert ticker_map(snap)["AAA"]["final"] == "pending"
    assert "MMM" not in ticker_map(snap)


@pytest.mark.unit
def test_manual_attempt_shadowing_the_slot_decision_is_explained(tmp_path):
    """Review f59d3fa finding 1: a same-day manual attempt 2 is the adapter's
    current decision; the snapshot says so instead of 'superseded'/unknown."""
    config = make_config(tmp_path)
    seed_slot(config, orders=False)
    write_decision(config, ticker="AAA", n=2, trigger="manual", date_iso=D, plan=BUY_PLAN)
    write_order(config, "AAA", "skip", reason="manual-trigger", attempt=2)
    events = FunnelEvents(config.state_dir, "us", DAY, "execution_adapter", clock=on_slot_day)
    events.emit("execute", "AAA", "info", "superseded", "later attempt",
                run_id=f"{D}-us-AAA-brief-1", arm="brief")
    events.emit("execute", "AAA", "excluded", "manual-trigger", "manual-trigger",
                run_id=f"{D}-us-AAA-brief-2", arm="brief")
    snap = funnel.build_snapshot(config, "us", DAY)
    aaa = ticker_map(snap)["AAA"]
    assert stage(snap, "AAA", "decide") == ("passed", "BUY", "reconstructed")
    assert stage(snap, "AAA", "execute") == ("excluded", "manual-shadowed", "reconstructed")
    assert aaa["final"] == "excluded"
    assert aaa["stages"]["execute"]["details"]["manual_run_id"] == f"{D}-us-AAA-brief-2"


@pytest.mark.unit
def test_later_day_execute_events_do_not_change_an_old_snapshot(tmp_path):
    """Review f59d3fa finding 2: execute events follow the order-row rule."""
    config = make_config(tmp_path)
    seed_slot(config, orders=False)
    first = funnel.write_snapshot(config, "us", DAY).read_bytes()
    later = FunnelEvents(config.state_dir, "us", DAY, "execution_adapter",
                         clock=lambda: datetime(2026, 10, 24, 13, 45, tzinfo=timezone.utc))
    later.emit("execute", "AAA", "passed", "submitted", "submitted (live)",
               run_id=f"{D}-us-AAA-brief-1", arm="brief", dry_run=False)
    snap = funnel.build_snapshot(config, "us", DAY)
    assert ticker_map(snap)["AAA"]["final"] == "pending"
    assert snap["context"]["events"] == 0
    # Byte-identical: the events file is hashed by its kept lines only.
    assert funnel.write_snapshot(config, "us", DAY).read_bytes() == first


@pytest.mark.unit
def test_manual_rerun_after_execution_never_hides_the_live_order(tmp_path):
    """Review a22eb2f finding 1: a manual attempt written after the execute
    run shadowed nothing — the slot row's live order stands."""
    config = make_config(tmp_path)
    seed_slot(config)  # AAA brief-1 submitted live at 13:45Z
    events = FunnelEvents(config.state_dir, "us", DAY, "execution_adapter", clock=on_slot_day)
    events.emit("execute", "AAA", "passed", "submitted", "submitted (live)",
                run_id=f"{D}-us-AAA-brief-1", arm="brief", dry_run=False)
    row = write_decision(config, ticker="AAA", n=2, trigger="manual", date_iso=D, plan=BUY_PLAN)
    path = config.ledger_dir / "decisions.jsonl"
    lines = path.read_text().splitlines()
    lines[-1] = json.dumps({**row, "decided_at": f"{D}T15:00:00Z"})  # after execute
    path.write_text("\n".join(lines) + "\n")
    snap = funnel.build_snapshot(config, "us", DAY)
    assert stage(snap, "AAA", "execute") == ("passed", "submitted", "recorded")
    assert ticker_map(snap)["AAA"]["final"] == "order-live"
    # Even a manual attempt decided BEFORE execute never hides a live order.
    lines[-1] = json.dumps({**row, "decided_at": f"{D}T12:00:00Z"})
    path.write_text("\n".join(lines) + "\n")
    assert ticker_map(funnel.build_snapshot(config, "us", DAY))["AAA"]["final"] == "order-live"


def append_manual(config, ticker, n, decided_at):
    row = write_decision(config, ticker=ticker, n=n, trigger="manual", date_iso=D, plan=BUY_PLAN)
    path = config.ledger_dir / "decisions.jsonl"
    lines = path.read_text().splitlines()
    lines[-1] = json.dumps({**row, "decided_at": decided_at})
    path.write_text("\n".join(lines) + "\n")


@pytest.mark.unit
@pytest.mark.parametrize(
    "decided_at, expected",
    [(f"{D}T15:00:00Z", "stale-quote"), (f"{D}T12:00:00Z", "manual-shadowed")],
)
def test_manual_shadowing_follows_the_execute_timing(tmp_path, decided_at, expected):
    """Review 887c5a8 finding 1: with no live order involved, only a manual
    attempt decided before the execute run (13:45Z) shadows CCC's row."""
    config = make_config(tmp_path)
    seed_slot(config)  # CCC brief-1: skip stale-quote at 13:45Z
    append_manual(config, "CCC", 2, decided_at)
    snap = funnel.build_snapshot(config, "us", DAY)
    assert stage(snap, "CCC", "execute") == ("excluded", expected, "reconstructed")


@pytest.mark.unit
def test_no_usable_pool_falls_back_to_core_yaml(tmp_path):
    """Review 887c5a8 finding 2: as the components do, a slot without a
    usable pool reads core.<session>.yaml; a too-old pool's opportunity
    layer is reconstructed as dropped."""
    config = make_config(tmp_path)
    seed_slot(config, orders=False)
    pool = config.pool_dir / "us" / f"{D}.json"
    stale = json.loads(pool.read_text())
    stale["as_of_date"] = "2026-10-01"  # gap > the 3-day cap ⇒ absent
    pool.unlink()
    write_json(config.pool_dir / "us" / "2026-10-01.json", stale)
    (config.pool_dir / "core.us.yaml").write_text(
        "- ticker: AAA\n- ticker: BBB\n", encoding="utf-8"
    )
    # Without a usable pool only core is collected.
    (config.ticker_brief_dir / "collector.log").write_text(
        f"{D} | us | AAA | 9 | 6.0 | written\n", encoding="utf-8"
    )
    snap = funnel.build_snapshot(config, "us", DAY)
    tickers = ticker_map(snap)
    assert (tickers["AAA"]["layer"], tickers["BBB"]["layer"]) == ("core", "core")
    assert stage(snap, "BBB", "evaluate") == ("info", "skipped-absent", "reconstructed")
    assert stage(snap, "BBB", "trigger") == ("info", None, "unknown")
    assert stage(snap, "CCC", "collect") == (
        "excluded", "opportunity-dropped-stale-pool", "reconstructed",
    )
    assert snap["context"]["pool"]["core_fallback"] is True
    assert "pools/core.us.yaml" in snap["context"]["inputs"]


@pytest.mark.unit
def test_warn_stale_pool_serves_layers_and_watch(tmp_path):
    """Review dafcbf1 finding 1: a pool within the staleness cap but not of
    the slot day still gives layers and the uncollected watch members."""
    config = make_config(tmp_path)
    seed_slot(config, orders=False)
    pool = config.pool_dir / "us" / f"{D}.json"
    data = json.loads(pool.read_text())
    data["as_of_date"] = "2026-10-21"  # gap 2 ≤ cap 3 ⇒ warn
    pool.unlink()
    write_json(config.pool_dir / "us" / "2026-10-21.json", data)
    snap = funnel.build_snapshot(config, "us", DAY)
    tickers = ticker_map(snap)
    assert (tickers["AAA"]["layer"], tickers["CCC"]["layer"]) == ("core", "opportunity")
    assert stage(snap, "EEE", "collect") == ("excluded", "not-collected-watch", "reconstructed")
    assert stage(snap, "AAA", "pool") is None  # no pool was built on the slot day
    assert snap["context"]["pool"]["as_of_date"] == "2026-10-21"
    assert snap["context"]["pool"]["carried_forward"] is False


@pytest.mark.unit
def test_manual_rows_are_a_declared_input(tmp_path):
    """Review dafcbf1 finding 2: manual rows can change the execute stage, so
    they are hashed (under #manual)."""
    config = make_config(tmp_path)
    seed_slot(config)
    before = funnel.build_snapshot(config, "us", DAY)
    append_manual(config, "CCC", 2, f"{D}T12:00:00Z")
    after = funnel.build_snapshot(config, "us", DAY)
    assert stage(after, "CCC", "execute") != stage(before, "CCC", "execute")
    assert "ledger/decisions.jsonl#manual" not in before["context"]["inputs"]
    assert "ledger/decisions.jsonl#manual" in after["context"]["inputs"]


@pytest.mark.unit
def test_feeds_only_buy_is_not_pending(tmp_path):
    """Review dafcbf1 finding 3: brief HOLD + feeds BUY passes decide but can
    never execute — excluded by the shape filter even before execute runs."""
    config = make_config(tmp_path)
    write_decision(config, ticker="AAA", date_iso=D, plan=None, decision="HOLD")
    write_decision(config, ticker="AAA", arm="feeds", date_iso=D, plan=BUY_PLAN)
    snap = funnel.build_snapshot(config, "us", DAY)
    aaa = ticker_map(snap)["AAA"]
    assert snap["context"]["execute_ran"] is False
    assert stage(snap, "AAA", "decide")[0] == "passed"
    assert stage(snap, "AAA", "execute") == ("excluded", "hold", "reconstructed")
    assert aaa["final"] == "excluded"


@pytest.mark.unit
def test_snapshot_contract_is_strict_for_writers_lenient_for_readers(tmp_path):
    """Review 3a34391 finding 1 (specs/README.md rule 5)."""
    from pydantic import ValidationError

    from pipeline.contracts.funnel import FunnelSnapshot

    config = make_config(tmp_path)
    seed_slot(config)
    snap = json.loads(funnel.write_snapshot(config, "us", DAY).read_text(encoding="utf-8"))
    FunnelSnapshot.model_validate(snap)
    with pytest.raises(ValidationError):
        FunnelSnapshot.model_validate({**snap, "surprise": 1})
    assert FunnelSnapshot.parse_lenient({**snap, "surprise": 1}).session == "us"
    # An unknown stage must carry no code, and a code needs a known stage.
    bad = json.loads(json.dumps(snap))
    bad["tickers"][0]["stages"]["nominate"]["provenance"] = "unknown"
    with pytest.raises(ValidationError):
        FunnelSnapshot.model_validate(bad)


@pytest.mark.unit
def test_invalid_snapshot_is_never_written(tmp_path, monkeypatch):
    from pydantic import ValidationError

    config = make_config(tmp_path)
    seed_slot(config)
    real = funnel.build_snapshot
    monkeypatch.setattr(funnel, "build_snapshot", lambda *a: {**real(*a), "surprise": 1})
    with pytest.raises(ValidationError):
        funnel.write_snapshot(config, "us", DAY)
    assert not (config.state_dir / "funnel" / "us" / f"{D}.json").exists()


@pytest.mark.unit
def test_collector_rerun_skip_reads_exists(tmp_path):
    """Review 3a34391 finding 2: a rerun's 'skipped' row (brief on disk)."""
    config = make_config(tmp_path)
    seed_slot(config)
    with open(config.ticker_brief_dir / "collector.log", "a", encoding="utf-8") as handle:
        handle.write(f"{D} | us | AAA | - | - | skipped\n")
    snap = funnel.build_snapshot(config, "us", DAY)
    assert stage(snap, "AAA", "collect") == ("passed", "exists", "reconstructed")


@pytest.mark.unit
def test_refresh_rows_do_not_count_as_an_execute_run(tmp_path):
    """Review 3a34391 finding 3: refresh never applies the latest-attempt
    rule, so a refresh after a manual attempt shadows nothing."""
    config = make_config(tmp_path)
    seed_slot(config)  # CCC skip stale-quote at 13:45Z
    append_manual(config, "CCC", 2, f"{D}T15:00:00Z")
    write_order(config, "CCC", "refresh", written_at=f"{D}T16:00:00Z")
    snap = funnel.build_snapshot(config, "us", DAY)
    assert stage(snap, "CCC", "execute") == ("excluded", "stale-quote", "reconstructed")


@pytest.mark.unit
@pytest.mark.parametrize("source", ["row", "event"])
def test_manual_trigger_skip_alone_makes_execute_uncertain(tmp_path, source):
    """Reviews 31b5132 / 967d973: a manual run's manual-trigger skip is
    written by the scheduled submit AND by an operator's `submit --run-id`
    without --execute — indistinguishable, so alone it proves nothing: the
    shadowed ticker is unknown (never pending, never manual-shadowed), and
    the evidence is a declared input."""
    config = make_config(tmp_path)
    write_decision(config, ticker="CCC", trigger="catalyst", date_iso=D, plan=BUY_PLAN)
    append_manual(config, "CCC", 2, f"{D}T12:00:00Z")
    without = funnel.build_snapshot(config, "us", DAY)
    assert ticker_map(without)["CCC"]["final"] == "pending"
    if source == "row":
        write_order(config, "CCC", "skip", reason="manual-trigger", attempt=2)
    else:
        events = FunnelEvents(config.state_dir, "us", DAY, "execution_adapter", clock=on_slot_day)
        events.emit("execute", "CCC", "excluded", "manual-trigger", "manual-trigger",
                    run_id=f"{D}-us-CCC-brief-2", arm="brief")
    snap = funnel.build_snapshot(config, "us", DAY)
    context = snap["context"]
    assert (context["execute_ran"], context["execute_uncertain"]) == (False, True)
    assert stage(snap, "CCC", "execute") is None
    assert ticker_map(snap)["CCC"]["final"] == "unknown"
    assert context["inputs"] != without["context"]["inputs"]
