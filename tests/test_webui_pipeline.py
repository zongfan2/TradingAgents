"""webui pipeline pages (specs/webui-pages.md): endpoints, write actions, bind.

Fully offline: every endpoint renders from fixture files under a tmp state
dir (TRADINGAGENTS_STATE_DIR) and the manual-trigger subprocess is faked.
Covers R1 (missing files render "not yet run", never a 500), the staleness
rule, carried-forward and verdict badges' data, the halt toggle round-trip
and its visibility from every page, the manual trigger argv, and the R4
localhost-only bind.
"""

import hashlib
import importlib
import json
import time
from datetime import datetime, timezone

import pytest


@pytest.fixture
def psrv(tmp_path, monkeypatch):
    """webui.server with the whole pipeline state dir redirected to tmp_path."""
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setenv("TRADINGAGENTS_STATE_DIR", str(state))
    # The dirs derive from state_dir; slot times must be the 08:30 defaults.
    for var in (
        "TRADINGAGENTS_MACRO_BRIEF_DIR", "TRADINGAGENTS_TICKER_BRIEF_DIR",
        "TRADINGAGENTS_POOL_DIR", "TRADINGAGENTS_LEDGER_DIR",
        "TRADINGAGENTS_SLOT_TIME_CN", "TRADINGAGENTS_SLOT_TIME_US",
        "TRADINGAGENTS_PIPELINE_PYTHON",
    ):
        monkeypatch.delenv(var, raising=False)
    import webui.server as server

    importlib.reload(server)
    monkeypatch.setattr(server, "ENV_PATH", tmp_path / ".env")
    monkeypatch.setattr(server, "SETTINGS_PATH", tmp_path / "settings.json")
    server._state_dir_for_tests = state  # convenience handle
    return server


def _clock(psrv, monkeypatch, iso_utc):
    moment = datetime.fromisoformat(iso_utc).replace(tzinfo=timezone.utc)
    monkeypatch.setattr(psrv, "_now_utc", lambda: moment)


# ---------------------------------------------------------------------------
# R1 — every page tolerates the pre-first-run state (no files at all)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_all_pages_render_with_no_files(psrv, monkeypatch):
    _clock(psrv, monkeypatch, "2026-08-08T14:00:00")  # Saturday — no staleness
    status = psrv.get_pipeline_status()
    assert status["sessions"]["us"]["present"] is False
    assert status["sessions"]["cn"]["present"] is False
    assert status["halted"] is False

    pool = psrv.get_pipeline_pool("us")
    assert pool["present"] is False and pool["message"] == "not yet run"

    briefs = psrv.list_pipeline_briefs()
    assert briefs["macro"] == [] and briefs["ticker"] == []

    ab = psrv.get_pipeline_ab()
    assert ab["present"] is False and ab["pair_count"] == 0

    decisions = psrv.get_pipeline_decisions()
    assert decisions["present"] is False and decisions["rows"] == []


# ---------------------------------------------------------------------------
# Page 1 — status banner + staleness rule
# ---------------------------------------------------------------------------


def _write_status(psrv, session, data):
    path = psrv._state_dir_for_tests / f"pipeline_status.{session}.json"
    path.write_text(json.dumps(data), encoding="utf-8")


@pytest.mark.unit
def test_staleness_red_when_no_slot_by_deadline(psrv, monkeypatch):
    # 2026-08-05 02:00 UTC: cn-local Wed 10:00 (> 08:30+1h) with no status
    # file at all; us-local Tue 22:00 (Aug 4, also past its deadline).
    _clock(psrv, monkeypatch, "2026-08-05T02:00:00")
    status = psrv.get_pipeline_status()
    assert status["sessions"]["cn"]["stale"] is True
    assert "08:30+1h" in status["sessions"]["cn"]["stale_reason"]
    assert status["sessions"]["us"]["stale"] is True


@pytest.mark.unit
def test_staleness_not_flagged_on_weekend_or_before_deadline(psrv, monkeypatch):
    # Saturday in both session timezones ⇒ never stale.
    _clock(psrv, monkeypatch, "2026-08-08T14:00:00")
    status = psrv.get_pipeline_status()
    assert status["sessions"]["cn"]["stale"] is False
    assert status["sessions"]["us"]["stale"] is False
    # Wednesday 08:00 us-local: before 08:30+1h ⇒ not yet stale.
    _clock(psrv, monkeypatch, "2026-08-05T12:00:00")
    assert psrv.get_pipeline_status()["sessions"]["us"]["stale"] is False


@pytest.mark.unit
def test_started_slot_suppresses_staleness_and_running_passes_through(psrv, monkeypatch):
    _clock(psrv, monkeypatch, "2026-08-05T15:00:00")  # us-local Wed 11:00
    _write_status(psrv, "us", {
        "slot": {"date": "2026-08-05", "session": "us",
                 "started_at": "2026-08-05T12:31:00Z", "finished_at": None},
        "components": {"macro_collector": {
            "status": "running", "started_at": "2026-08-05T12:31:05Z",
            "finished_at": None, "duration_s": None, "error": None}},
        "history": [],
    })
    info = psrv.get_pipeline_status()["sessions"]["us"]
    assert info["present"] is True and info["stale"] is False
    assert info["status"]["components"]["macro_collector"]["status"] == "running"
    # No completed slot yet ⇒ no end-of-slot summary line.
    assert info["summary_line"] is None


@pytest.mark.unit
def test_staleness_catches_slot_time_within_grace_of_midnight(psrv, monkeypatch):
    # Mirrors run_watchdog's two-day scan: a 23:30 slot's +1h deadline falls
    # on the NEXT calendar day, so yesterday's missed slot must still flag —
    # evaluating only today's slot would silently under-report.
    monkeypatch.setenv("TRADINGAGENTS_SLOT_TIME_CN", "23:30")
    # 16:45 UTC Tue = Wed 00:45 Shanghai — past Tuesday's 00:30 deadline.
    _clock(psrv, monkeypatch, "2026-08-04T16:45:00")
    info = psrv.get_pipeline_status()["sessions"]["cn"]
    assert info["stale"] is True
    assert "2026-08-04" in info["stale_reason"]
    # A started Tuesday slot suppresses the flag.
    _write_status(psrv, "cn", {
        "slot": {"date": "2026-08-04", "session": "cn",
                 "started_at": "2026-08-04T15:31:00Z", "finished_at": None},
        "components": {}, "history": [],
    })
    assert psrv.get_pipeline_status()["sessions"]["cn"]["stale"] is False


@pytest.mark.unit
def test_slot_time_env_override_moves_the_deadline(psrv, monkeypatch):
    monkeypatch.setenv("TRADINGAGENTS_SLOT_TIME_US", "13:00")
    _clock(psrv, monkeypatch, "2026-08-05T15:00:00")  # us-local Wed 11:00 < 14:00
    assert psrv.get_pipeline_status()["sessions"]["us"]["stale"] is False


@pytest.mark.unit
def test_summary_line_mirrors_end_of_slot_format(psrv, monkeypatch):
    _clock(psrv, monkeypatch, "2026-08-08T14:00:00")
    _write_status(psrv, "us", {
        "slot": {"date": "2026-08-05", "session": "us",
                 "started_at": "2026-08-05T12:31:00Z",
                 "finished_at": "2026-08-05T13:40:00Z"},
        "components": {
            "macro_collector": {"status": "ok", "error": None},
            "macro_evaluator": {"status": "warn", "error": "verdict=fail: 2 flagged claims"},
            "pool_builder": {"status": "failed", "error": "boom"},
        },
        "history": [],
    })
    line = psrv.get_pipeline_status()["sessions"]["us"]["summary_line"]
    assert line == "slot us 2026-08-05: 1 ok, 1 warn, 1 failed; failures: pool_builder"


# ---------------------------------------------------------------------------
# Page 2 — pool view
# ---------------------------------------------------------------------------


def _write_pool(psrv, session, name, pool):
    pool_dir = psrv._state_dir_for_tests / "pools" / session
    pool_dir.mkdir(parents=True, exist_ok=True)
    (pool_dir / name).write_text(json.dumps(pool), encoding="utf-8")


@pytest.mark.unit
def test_pool_renders_latest_file_with_carried_forward_badge(psrv):
    _write_pool(psrv, "us", "2026-08-01.json", {"carried_forward": False, "core": []})
    _write_pool(psrv, "us", "2026-08-03.json", {
        "as_of_date": "2026-08-03", "session": "us",
        "generated_at": "2026-08-03T12:31:00Z", "generator": "claude-deep-search",
        "carried_forward": True,
        "core": [{"ticker": "NVDA", "note": "holding", "score": 6.8}],
        "opportunity": [{
            "ticker": "AVGO", "score": 7.5, "catalyst_type": "earnings",
            "rationale": "earnings beat setup", "citations": ["https://x"],
            "entered_on": "2026-08-01", "low_score_streak": 0, "gate_fail_streak": 0,
            "technical": {
                "gate": "pass",
                "boll_daily": {"close": 291.2, "mid": 285.1, "upper": 301.4, "lower": 268.8},
                "boll_weekly": {"close": 291.2, "mid": 262.0, "upper": 315.5, "lower": 208.4},
                "avg_dollar_volume_20d": 41200000.0,
                "volume_ratio_5d_20d": 1.35,
            },
        }],
        "watch": [{"ticker": "MRVL", "score": 6.2, "catalyst_type": "product",
                   "rationale": "watching", "citations": ["https://y"],
                   "technical": {"gate": "watch"}}],
        "removed": [{"ticker": "SMCI", "reason": "score<4.0 for 3 sessions",
                     "last_score": 3.1}],
    })
    result = psrv.get_pipeline_pool("us")
    assert result["present"] is True and result["file"] == "2026-08-03.json"
    assert result["carried_forward"] is True
    pool = result["pool"]
    assert [e["ticker"] for e in pool["core"]] == ["NVDA"]
    assert pool["core"][0]["score"] == 6.8  # informative core score surfaced
    tech = pool["opportunity"][0]["technical"]
    assert tech["boll_daily"]["mid"] == 285.1
    assert tech["avg_dollar_volume_20d"] == 41200000.0
    assert pool["removed"][0]["reason"].startswith("score<4.0")


@pytest.mark.unit
def test_pool_unknown_session_404(psrv):
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        psrv.get_pipeline_pool("uk")
    assert exc.value.status_code == 404


# ---------------------------------------------------------------------------
# Page 3 — briefs + eval verdicts (hash binding via brief_evals)
# ---------------------------------------------------------------------------


def _write_brief(psrv, rel, body, verdict=None, *, bind_hash=True, flagged=None):
    if rel.startswith("ticker/"):
        path = psrv._state_dir_for_tests / "ticker_briefs" / rel[len("ticker/"):]
    else:
        path = psrv._state_dir_for_tests / "macro_briefs" / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    if verdict is not None:
        digest = hashlib.sha256(body.encode()).hexdigest() if bind_hash else "0" * 64
        eval_path = path.with_name(path.stem + ".eval.json")
        eval_path.write_text(json.dumps({
            "brief_sha256": digest, "verdict": verdict,
            "flagged_claims": flagged or [],
        }), encoding="utf-8")
    return path


@pytest.mark.unit
def test_briefs_listing_with_verdicts_and_hash_binding(psrv):
    _write_brief(psrv, "2026-08-03.us.md", "# macro body", "warn",
                 flagged=[{"section": "China & Asia", "claim": "c", "issue": "i",
                           "severity": "major"}])
    # Eval bound to a DIFFERENT revision ⇒ verdict must read missing.
    _write_brief(psrv, "2026-08-04.us.md", "# newer body", "pass", bind_hash=False)
    _write_brief(psrv, "ticker/NVDA/2026-08-03.md", "# nvda body", "pass")
    _write_brief(psrv, "ticker/0700.HK/2026-08-03.md", "# tencent body")  # no eval

    briefs = psrv.list_pipeline_briefs()
    macro = {b["name"]: b for b in briefs["macro"]}
    assert macro["2026-08-03.us.md"]["verdict"] == "warn"
    assert macro["2026-08-03.us.md"]["session"] == "us"
    assert macro["2026-08-04.us.md"]["verdict"] == "missing"  # revision binding
    ticker = {b["name"]: b for b in briefs["ticker"]}
    assert ticker["NVDA/2026-08-03.md"]["verdict"] == "pass"
    assert ticker["NVDA/2026-08-03.md"]["session"] == "us"
    assert ticker["0700.HK/2026-08-03.md"]["verdict"] == "missing"
    assert ticker["0700.HK/2026-08-03.md"]["session"] == "cn"


@pytest.mark.unit
def test_brief_detail_renders_markdown_and_flagged_claims(psrv):
    _write_brief(psrv, "2026-08-03.us.md", "## Monetary Policy & Rates\nbody", "warn",
                 flagged=[{"section": "Growth & Earnings", "claim": "x", "issue": "y",
                           "severity": "major"}])
    detail = psrv.get_pipeline_brief(kind="macro", name="2026-08-03.us.md")
    assert "Monetary Policy" in detail["markdown"]
    assert detail["verdict"] == "warn"
    assert detail["flagged_claims"][0]["severity"] == "major"


@pytest.mark.unit
def test_brief_detail_blocks_path_traversal(psrv, tmp_path):
    from fastapi import HTTPException

    (tmp_path / "secret.md").write_text("secret", encoding="utf-8")
    for name in ("../secret.md", "../../secret.md"):
        with pytest.raises(HTTPException) as exc:
            psrv.get_pipeline_brief(kind="macro", name=name)
        assert exc.value.status_code == 404


# ---------------------------------------------------------------------------
# Pages 4 + 5 — ledger-backed endpoints
# ---------------------------------------------------------------------------


def _write_ledger(psrv, name, rows):
    ledger = psrv._state_dir_for_tests / "ledger"
    ledger.mkdir(exist_ok=True)
    (ledger / name).write_text(
        "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
    )


def _seed_ledger(psrv):
    from tests.test_webui_aggregates import DECISIONS, ORDERS, OUTCOMES

    _write_ledger(psrv, "decisions.jsonl", DECISIONS)
    _write_ledger(psrv, "orders.jsonl", ORDERS)
    _write_ledger(psrv, "outcomes.jsonl", OUTCOMES)


@pytest.mark.unit
def test_ab_endpoint_serves_aggregates_and_never_persists(psrv):
    _seed_ledger(psrv)
    ledger = psrv._state_dir_for_tests / "ledger"
    before = sorted(p.name for p in ledger.iterdir())
    result = psrv.get_pipeline_ab()
    assert result["present"] is True
    assert result["pair_count"] == 2
    assert result["direction_agreement"]["all"] == pytest.approx(0.5)
    assert result["caveats"]  # rendered with the numbers
    # Filters pass through to the pure function.
    paired = psrv.get_pipeline_ab(paired_only=True)
    assert paired["row_counts"]["used"] == 4
    preset = psrv.get_pipeline_ab(preset="p2")
    assert preset["arms"]["feeds"]["all"]["n_rows"] == 1
    # No aggregate is ever persisted: the ledger dir is untouched.
    assert sorted(p.name for p in ledger.iterdir()) == before


@pytest.mark.unit
def test_decisions_join_and_filters(psrv):
    _seed_ledger(psrv)
    all_rows = psrv.get_pipeline_decisions()
    assert all_rows["present"] is True and len(all_rows["rows"]) == 9
    assert "never auto-executed" in all_rows["manual_note"].lower()
    by_id = {row["run_id"]: row for row in all_rows["rows"]}
    # Join rule: r3 derived filled (submit + later fill refresh), r5 only
    # submitted, r4 dry-run, r6 skipped with reason, r8 no order rows.
    assert by_id["2026-08-03-us-NVDA-brief-2"]["execution"]["state"] == "filled"
    assert by_id["2026-08-03-us-AMD-brief-1"]["execution"]["state"] == "submitted"
    assert by_id["2026-08-03-us-NVDA-feeds-2"]["execution"]["state"] == "dry_run"
    assert by_id["2026-08-03-us-AMD-feeds-1"]["execution"]["skip_reasons"] == [
        "EXECUTION_HALT active"
    ]
    assert by_id["2026-08-03-us-MSFT-brief-1"]["execution"]["state"] == "none"
    # Outcomes join uses the latest settled row.
    assert by_id["2026-08-03-us-NVDA-brief-2"]["outcome"]["returns"]["d1"] == 0.02
    # Filters: date/session/ticker/arm (ticker case-insensitive).
    assert len(psrv.get_pipeline_decisions(date="2026-08-04")["rows"]) == 1
    assert len(psrv.get_pipeline_decisions(ticker="nvda")["rows"]) == 5
    assert len(psrv.get_pipeline_decisions(arm="feeds")["rows"]) == 4
    assert len(psrv.get_pipeline_decisions(session="cn")["rows"]) == 0


# ---------------------------------------------------------------------------
# Write action 1 — EXECUTION_HALT toggle (round-trip + visibility everywhere)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_halt_filename_matches_the_adapter_kill_switch(psrv):
    from pipeline.execution_adapter import HALT_FILENAME

    assert psrv.HALT_FILENAME == HALT_FILENAME


@pytest.mark.unit
def test_halt_toggle_round_trip_and_visibility_on_every_page(psrv, monkeypatch):
    _clock(psrv, monkeypatch, "2026-08-08T14:00:00")
    halt_file = psrv._state_dir_for_tests / "EXECUTION_HALT"
    assert psrv.get_pipeline_halt()["halted"] is False

    assert psrv.post_pipeline_halt(psrv.HaltPayload(halted=True))["halted"] is True
    assert halt_file.exists()
    # While halted, every page's payload carries the banner flag.
    assert psrv.get_pipeline_status()["halted"] is True
    assert psrv.get_pipeline_pool("us")["halted"] is True
    assert psrv.list_pipeline_briefs()["halted"] is True
    assert psrv.get_pipeline_ab()["halted"] is True
    assert psrv.get_pipeline_decisions()["halted"] is True

    assert psrv.post_pipeline_halt(psrv.HaltPayload(halted=False))["halted"] is False
    assert not halt_file.exists()
    assert psrv.get_pipeline_status()["halted"] is False


# ---------------------------------------------------------------------------
# Write action 2 — manual analysis trigger (subprocess faked)
# ---------------------------------------------------------------------------


def _wait_trigger(psrv, trigger_id, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        run = psrv.get_pipeline_trigger(trigger_id)
        if run["status"] != "running":
            return run
        time.sleep(0.01)
    raise AssertionError("trigger did not finish")


@pytest.mark.unit
def test_manual_trigger_invokes_runner_with_exact_argv(psrv, monkeypatch):
    import sys

    calls = []

    def fake_run(argv):
        calls.append(argv)
        return 0, '{"planned": 1, "completed": 1}', ""

    monkeypatch.setattr(psrv, "_run_trigger_subprocess", fake_run)
    response = psrv.post_pipeline_trigger(
        psrv.PipelineTriggerPayload(ticker="nvda", date="2026-08-03")
    )
    assert "never auto-executed" in response["note"].lower()
    run = _wait_trigger(psrv, response["trigger_id"])
    assert run["status"] == "done"
    assert calls == [[
        sys.executable, "-m", "pipeline.analysis_runner",
        "--session", "us", "--date", "2026-08-03", "--ticker", "NVDA",
    ]]


@pytest.mark.unit
def test_manual_trigger_derives_session_and_default_date(psrv, monkeypatch):
    calls = []
    monkeypatch.setattr(
        psrv, "_run_trigger_subprocess", lambda argv: (calls.append(argv) or (0, "", ""))
    )
    # 2026-08-05 02:00 UTC is already Wed 2026-08-05 in Shanghai — the default
    # date must resolve in the SESSION timezone, and .HK derives session cn.
    _clock(psrv, monkeypatch, "2026-08-05T02:00:00")
    response = psrv.post_pipeline_trigger(psrv.PipelineTriggerPayload(ticker="0700.HK"))
    _wait_trigger(psrv, response["trigger_id"])
    assert calls[0][1:] == [
        "-m", "pipeline.analysis_runner",
        "--session", "cn", "--date", "2026-08-05", "--ticker", "0700.HK",
    ]


@pytest.mark.unit
def test_manual_trigger_validation_and_single_flight(psrv, monkeypatch):
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        psrv.post_pipeline_trigger(psrv.PipelineTriggerPayload(ticker="  "))
    assert exc.value.status_code == 400
    with pytest.raises(HTTPException) as exc:
        psrv.post_pipeline_trigger(
            psrv.PipelineTriggerPayload(ticker="NVDA", date="03/08/2026")
        )
    assert exc.value.status_code == 400
    # Single-flight: a running trigger blocks a second one.
    psrv._trigger_runs["busy"] = {"status": "running"}
    try:
        with pytest.raises(HTTPException) as exc:
            psrv.post_pipeline_trigger(psrv.PipelineTriggerPayload(ticker="NVDA"))
        assert exc.value.status_code == 409
    finally:
        psrv._trigger_runs.pop("busy", None)


@pytest.mark.unit
def test_failed_trigger_surfaces_stderr(psrv, monkeypatch):
    monkeypatch.setattr(
        psrv, "_run_trigger_subprocess",
        lambda argv: (1, "", "analysis-runner: ticker bad"),
    )
    response = psrv.post_pipeline_trigger(
        psrv.PipelineTriggerPayload(ticker="NVDA", date="2026-08-03")
    )
    run = _wait_trigger(psrv, response["trigger_id"])
    assert run["status"] == "error" and run["returncode"] == 1
    assert "analysis-runner" in run["stderr_tail"]


# ---------------------------------------------------------------------------
# R4 — localhost-only bind
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_server_binds_loopback_only(psrv, monkeypatch):
    import uvicorn

    assert psrv.HOST == "127.0.0.1"
    seen = {}

    def fake_run(app, **kwargs):
        seen.update(kwargs)

    monkeypatch.setattr(uvicorn, "run", fake_run)
    psrv.main(["--port", "9999"])
    assert seen == {"host": "127.0.0.1", "port": 9999}
