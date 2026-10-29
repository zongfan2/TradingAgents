"""webui decision-funnel page + remote read-only access (specs/decision-funnel.md R4/R5).

Offline: snapshots are built with the real builder from the funnel fixture
slot into a tmp state dir; requests go through TestClient so the Host
allowlist and the read-only middleware run exactly as in production.
"""

import csv
import importlib
import io
import json

import pytest
from fastapi.testclient import TestClient

from pipeline.config import PipelineConfig
from tests.test_funnel import DAY, D, seed_slot

TAILNET = "desktop-p5u8dva.tailc36a4b.ts.net"


@pytest.fixture
def fsrv(tmp_path, monkeypatch):
    """webui.server over a tmp state dir, with the tailnet name allowlisted
    and one us snapshot (the funnel fixture slot) on disk."""
    state = tmp_path / "state"
    monkeypatch.setenv("TRADINGAGENTS_STATE_DIR", str(state))
    monkeypatch.setenv("TRADINGAGENTS_WEBUI_ALLOWED_HOSTS", f"{TAILNET.upper()}, *.ts.net, a:80")
    import webui.server as server

    importlib.reload(server)
    monkeypatch.setattr(server, "ENV_PATH", tmp_path / ".env")
    monkeypatch.setattr(server, "SETTINGS_PATH", tmp_path / "settings.json")
    from pipeline import funnel

    config = PipelineConfig(state_dir=state)
    seed_slot(config)
    funnel.write_snapshot(config, "us", DAY)
    server._config_for_tests = config
    yield server
    monkeypatch.delenv("TRADINGAGENTS_WEBUI_ALLOWED_HOSTS")
    importlib.reload(server)  # leave the loopback-only module for later tests


def client(server, host="127.0.0.1:8321", scheme="http"):
    return TestClient(server.app, base_url=f"{scheme}://{host}")


# ---------------------------------------------------------------------------
# R5 — Host allowlist and read-only remote access
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_allowed_hosts_accepts_plain_names_only(fsrv):
    # Lower-cased; the wildcard and the host:port entries are skipped.
    assert fsrv.ALLOWED_HOSTS == ["127.0.0.1", "localhost", TAILNET]
    assert fsrv.parse_allowed_hosts("localhost, x.ts.net. ,x.ts.net, http://y, *") == ["x.ts.net"]
    assert fsrv.parse_allowed_hosts(None) == []


@pytest.mark.unit
def test_allowlist_falls_back_to_the_repo_env_file(tmp_path, monkeypatch):
    monkeypatch.delenv("TRADINGAGENTS_SKIP_DOTENV")
    env = tmp_path / ".env"
    env.write_text(f"TRADINGAGENTS_WEBUI_ALLOWED_HOSTS={TAILNET}\n", encoding="utf-8")
    import webui.server as server

    monkeypatch.setattr(server, "ENV_PATH", env)
    assert server._allowed_hosts_setting() == TAILNET
    monkeypatch.setenv("TRADINGAGENTS_SKIP_DOTENV", "1")
    assert server._allowed_hosts_setting() is None


@pytest.mark.unit
def test_tailnet_host_reads_but_every_write_is_refused(fsrv):
    remote = client(fsrv, TAILNET, "https")
    halt = fsrv._halt_path(fsrv._pipeline_config())
    assert remote.get("/api/pipeline/funnel/dates").status_code == 200
    assert remote.get("/api/pipeline/status").status_code == 200
    assert remote.get("/api/pipeline/access").json() == {"writable": False, "remote_user": None}
    for path, body in (
        ("/api/pipeline/halt", {"halted": True}),
        ("/api/pipeline/trigger", {"ticker": "NVDA"}),
        ("/api/config", {"settings": {}}),
        ("/api/analyze", {"ticker": "NVDA", "date": D}),
    ):
        response = remote.post(path, json=body)
        assert response.status_code == 403, path
        assert "read-only" in response.json()["detail"]
    assert not halt.exists()


@pytest.mark.unit
def test_forwarded_request_is_remote_even_with_a_loopback_host(fsrv):
    """tailscale serve preserves the client's Host, so a tailnet client can
    send `Host: localhost`; the proxy's forwarding headers still mark it."""
    halt = fsrv._halt_path(fsrv._pipeline_config())
    local = client(fsrv, "localhost:8321")
    for header in (
        {"X-Forwarded-For": "100.64.0.7"},
        {"X-Forwarded-Host": "localhost"},
        {"Tailscale-User-Login": "someone@example.com"},
        {"Tailscale-User-Profile-Pic": "https://x"},
        {"Tailscale-Headers-Info": "x"},
        {"Tailscale-App-Capabilities": "{}"},
        {"Forwarded": "for=100.64.0.7"},
    ):
        assert local.post("/api/pipeline/halt", json={"halted": True}, headers=header).status_code == 403
        assert local.get("/api/pipeline/access", headers=header).json()["writable"] is False
    assert not halt.exists()
    access = local.get(
        "/api/pipeline/access", headers={"Tailscale-User-Login": "me@example.com"}
    ).json()
    assert access == {"writable": False, "remote_user": "me@example.com"}
    # A clean local request keeps every write action (the ssh-tunnel path).
    assert local.get("/api/pipeline/access").json()["writable"] is True
    assert local.post("/api/pipeline/halt", json={"halted": True}).status_code == 200
    assert halt.exists()


@pytest.mark.unit
def test_funnel_traffic_is_refused_outright(fsrv):
    remote = client(fsrv, TAILNET, "https")
    response = remote.get("/api/pipeline/funnel/dates", headers={"Tailscale-Funnel-Request": "?1"})
    assert response.status_code == 403
    assert client(fsrv).get("/", headers={"Tailscale-Funnel-Request": "?1"}).status_code == 403


@pytest.mark.unit
def test_foreign_host_is_still_400_before_the_read_only_check(fsrv):
    evil = client(fsrv, "evil.example:8321")
    assert evil.get("/api/pipeline/funnel/dates").status_code == 400
    assert evil.post("/api/pipeline/halt", json={"halted": True}).status_code == 400


# ---------------------------------------------------------------------------
# R4 — funnel endpoints
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_dates_and_latest_snapshot(fsrv):
    local = client(fsrv)
    assert local.get("/api/pipeline/funnel/dates").json()["dates"] == {"cn": [], "us": [D]}
    latest = local.get("/api/pipeline/funnel/us").json()
    assert latest["present"] is True and latest["date"] == D
    tickers = {t["ticker"]: t for t in latest["snapshot"]["tickers"]}
    assert tickers["AAA"]["final"] == "order-live"
    assert local.get(f"/api/pipeline/funnel/us?date={D}").json()["snapshot"]["date"] == D


@pytest.mark.unit
def test_missing_invalid_and_unknown_inputs(fsrv):
    local = client(fsrv)
    assert local.get("/api/pipeline/funnel/cn").json() == {
        "present": False, "date": None, "message": "not yet run", "halted": False,
    }
    gone = local.get("/api/pipeline/funnel/us?date=2026-10-01").json()
    assert (gone["present"], gone["message"]) == (False, "not yet run")
    assert local.get("/api/pipeline/funnel/us?date=../../x").status_code == 422
    assert local.get("/api/pipeline/funnel/hk").status_code == 404
    assert local.get("/api/pipeline/funnel/aggregate?session=hk").status_code == 404
    # An unreadable / contract-invalid snapshot renders a message, never a 500.
    path = fsrv._config_for_tests.state_dir / "funnel" / "us" / "2026-10-22.json"
    path.write_text("{not json", encoding="utf-8")
    assert local.get("/api/pipeline/funnel/us?date=2026-10-22").json()["message"].startswith(
        "snapshot unreadable"
    )
    path.write_text(json.dumps({"schema": 1}), encoding="utf-8")
    assert local.get("/api/pipeline/funnel/us?date=2026-10-22").json()["message"].startswith(
        "snapshot invalid"
    )
    agg = local.get("/api/pipeline/funnel/aggregate?session=us").json()
    assert agg["slots"] == 1 and agg["skipped"][0]["date"] == "2026-10-22"


@pytest.mark.unit
def test_aggregate_counts_over_dates(fsrv):
    from webui import funnel_view

    snap = client(fsrv).get("/api/pipeline/funnel/us").json()["snapshot"]
    other = json.loads(json.dumps(snap))
    other["date"] = "2026-10-26"
    result = funnel_view.aggregate([other, snap])
    assert result["slots"] == 2
    assert [d["date"] for d in result["by_date"]] == [D, "2026-10-26"]
    assert result["finals"] == {
        "order-live": 2, "order-dry": 0, "excluded": 14, "pending": 0, "unknown": 2,
    }
    stops = {(r["stage"], r["code"]): r["count"] for r in result["stops"]}
    assert stops[("execute", "stale-quote")] == 2
    assert stops[("pool", None)] == 2  # GGG: unexplained drop, counted as unknown
    counts = [r["count"] for r in result["stops"]]
    assert counts == sorted(counts, reverse=True)
    ccc = next(r for r in result["tickers"] if r["ticker"] == "CCC")
    assert (ccc["slots"], ccc["excluded"], ccc["orders"]) == (2, 2, 0)
    assert ccc["stops"] == [{"stop": "execute:stale-quote", "count": 2}]
    window = client(fsrv).get(f"/api/pipeline/funnel/aggregate?session=us&since={D}&until={D}")
    assert window.json()["slots"] == 1
    assert client(fsrv).get("/api/pipeline/funnel/aggregate?session=us&since=2027-01-01").json()["slots"] == 0


@pytest.mark.unit
def test_csv_export_one_row_per_ticker_and_neutralised_formulas(fsrv):
    from webui import funnel_view

    response = client(fsrv, TAILNET, "https").get(f"/api/pipeline/funnel/us/csv?date={D}")
    assert response.status_code == 200  # a GET: allowed from the tailnet
    assert response.headers["content-type"].startswith("text/csv")
    assert f'filename="funnel-us-{D}.csv"' in response.headers["content-disposition"]
    rows = list(csv.DictReader(io.StringIO(response.text)))
    assert sorted(r["ticker"] for r in rows) == sorted(
        {"AAA", "BBB", "CCC", "DDD", "EEE", "FFF", "GGG", "HHH", "0700.HK"}
    )
    ccc = next(r for r in rows if r["ticker"] == "CCC")
    assert (ccc["stop_stage"], ccc["stop_code"], ccc["execute"]) == (
        "execute", "stale-quote", "excluded:stale-quote",
    )
    assert next(r for r in rows if r["ticker"] == "GGG")["pool"] == "excluded:unknown"

    snap = client(fsrv).get("/api/pipeline/funnel/us").json()["snapshot"]
    snap["tickers"][0]["stop"] = {"stage": "pool", "code": "x", "reason": "=HYPERLINK(1)",
                                  "provenance": "recorded"}
    text = funnel_view.snapshot_csv(snap)
    assert "'=HYPERLINK(1)" in text and ",=HYPERLINK" not in text
    assert client(fsrv).get("/api/pipeline/funnel/cn/csv").status_code == 404


@pytest.mark.unit
def test_page_six_is_served_with_the_tab(fsrv):
    html = client(fsrv, TAILNET, "https").get("/pipeline.html")
    assert html.status_code == 200
    assert 'data-page="funnel"' in html.text and 'id="page-funnel"' in html.text


@pytest.mark.unit
def test_page_hides_write_controls_when_read_only():
    """Review fe26529: the page asks /api/pipeline/access and hides the halt
    toggle and the manual trigger (and shows 只读) when writes are refused."""
    from pathlib import Path

    static = Path(__file__).resolve().parent.parent / "webui" / "static"
    js = (static / "pipeline.js").read_text(encoding="utf-8")
    html = (static / "pipeline.html").read_text(encoding="utf-8")
    assert 'id="triggerCard"' in html and 'id="readOnlyBadge"' in html
    access = js[js.index("async function refreshAccess"):]
    access = access[: access.index("\n}\n") + 3]
    assert '"/api/pipeline/access"' in access
    for line in ('$("#haltToggle").hidden = !writable;',
                 '$("#triggerCard").hidden = !writable;',
                 '$("#readOnlyBadge").hidden = writable;'):
        assert line in access
    assert "catch (e) { writable = false; }" in access  # unreachable ⇒ read-only
    assert "refreshAccess();" in js[js.index("function init()"):]


@pytest.mark.unit
def test_session_switch_resets_the_statistics_range_and_spec_points_at_the_runbook():
    """Review 79b5b57: the cross-date range is recomputed per session, and
    the spec's operator-steps reference names the RUNBOOK section that has
    them."""
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    js = (root / "webui" / "static" / "pipeline.js").read_text(encoding="utf-8")
    handler = js[js.index('$("#funnelSession").addEventListener'):]
    handler = handler[: handler.index("});")]
    reset = handler.index('$("#faSince").value = ""; $("#faUntil").value = "";')
    assert reset < handler.index("loadFunnel();")

    spec = (root / "specs" / "decision-funnel.md").read_text(encoding="utf-8")
    section = re.search(r"RUNBOOK §(\d+)", spec[spec.index("### R5"):]).group(1)
    runbook = (root / "RUNBOOK.md").read_text(encoding="utf-8")
    start = runbook.index(f"\n## {section}.")
    end = runbook.find("\n## ", start + 1)
    assert "tailscale serve --bg 8321" in runbook[start:end]
