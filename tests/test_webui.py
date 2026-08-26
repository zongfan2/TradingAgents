"""webui server: .env round-trip, report-tree resolution, and API contracts.

Covers the review findings: cleared settings must delete their .env override
(otherwise a stale value silently drives every run), `export KEY=` and duplicate
assignments must be handled, report history sorts by recency, and the run
snapshot must not be taken while the worker thread mutates the dict.
"""
import importlib
import threading

import pytest


@pytest.fixture
def srv(tmp_path, monkeypatch):
    """Import webui.server with .env and settings.json redirected to tmp_path."""
    import webui.server as server

    importlib.reload(server)
    monkeypatch.setattr(server, "ENV_PATH", tmp_path / ".env")
    monkeypatch.setattr(server, "SETTINGS_PATH", tmp_path / "settings.json")
    return server


@pytest.mark.unit
def test_env_parse_handles_export_and_quotes(srv):
    srv.ENV_PATH.write_text(
        'export OPENAI_API_KEY=sk-exported\n'
        'DEEPSEEK_API_KEY="sk-quoted"\n'
        "# comment=ignored\n"
        "MALFORMED\n",
        encoding="utf-8",
    )
    env = srv._read_env_file()
    assert env["OPENAI_API_KEY"] == "sk-exported"
    assert env["DEEPSEEK_API_KEY"] == "sk-quoted"
    assert "# comment" not in env and "MALFORMED" not in env


@pytest.mark.unit
def test_cleared_setting_deletes_env_line(srv):
    srv.ENV_PATH.write_text(
        "TRADINGAGENTS_LLM_BACKEND_URL=http://localhost:3456/v1\n"
        "DEEPSEEK_API_KEY=sk-keep\n",
        encoding="utf-8",
    )
    srv._write_env_updates({"TRADINGAGENTS_LLM_BACKEND_URL": None})
    text = srv.ENV_PATH.read_text(encoding="utf-8")
    assert "TRADINGAGENTS_LLM_BACKEND_URL" not in text  # stale override gone
    assert "DEEPSEEK_API_KEY=sk-keep" in text           # unrelated line kept


@pytest.mark.unit
def test_duplicate_key_lines_collapse_to_written_value(srv):
    srv.ENV_PATH.write_text(
        "TRADINGAGENTS_LLM_PROVIDER=openai\n"
        "OTHER=1\n"
        "TRADINGAGENTS_LLM_PROVIDER=anthropic\n",
        encoding="utf-8",
    )
    srv._write_env_updates({"TRADINGAGENTS_LLM_PROVIDER": "deepseek"})
    lines = [line for line in srv.ENV_PATH.read_text(encoding="utf-8").splitlines()
             if line.startswith("TRADINGAGENTS_LLM_PROVIDER")]
    assert lines == ["TRADINGAGENTS_LLM_PROVIDER=deepseek"]
    assert srv._read_env_file()["TRADINGAGENTS_LLM_PROVIDER"] == "deepseek"


@pytest.mark.unit
def test_post_config_clearing_backend_url_removes_override(srv):
    srv.ENV_PATH.write_text("TRADINGAGENTS_LLM_BACKEND_URL=http://old:1234/v1\n", encoding="utf-8")
    srv.post_config(srv.ConfigPayload(settings={"backend_url": ""}))
    assert "TRADINGAGENTS_LLM_BACKEND_URL" not in srv.ENV_PATH.read_text(encoding="utf-8")


@pytest.mark.unit
def test_config_never_returns_key_values(srv):
    srv.ENV_PATH.write_text("DEEPSEEK_API_KEY=sk-secret-value\n", encoding="utf-8")
    cfg = srv.get_config()
    assert cfg["keys"]["DEEPSEEK_API_KEY"] is True
    assert "sk-secret-value" not in repr(cfg)


@pytest.mark.unit
def test_read_report_tree_from_directory(srv, tmp_path):
    d = tmp_path / "NVDA_20260728"
    (d / "1_analysts").mkdir(parents=True)
    (d / "1_analysts" / "news.md").write_text("news body", encoding="utf-8")
    (d / "complete_report.md").write_text("full body", encoding="utf-8")
    sections = srv._read_report_tree(d)
    assert sections["complete_report.md"] == "full body"
    assert sections["1_analysts/news.md"] == "news body"


@pytest.mark.unit
def test_read_report_tree_on_file_path_is_empty(srv, tmp_path):
    # save_reports() returns the complete_report.md FILE; passing it straight in
    # yields nothing, which is why the caller must use its parent directory.
    f = tmp_path / "complete_report.md"
    f.write_text("x", encoding="utf-8")
    assert srv._read_report_tree(f) == {}


@pytest.mark.unit
def test_reports_sorted_by_recency_not_name(srv, tmp_path, monkeypatch):
    base = tmp_path / "reports"
    base.mkdir()
    older = base / "ZZZZ_20260101_000000"
    newer = base / "AAAA_20260728_000000"
    older.mkdir(), newer.mkdir()
    import os
    os.utime(older, (1_000_000, 1_000_000))
    os.utime(newer, (2_000_000, 2_000_000))
    monkeypatch.setattr(srv, "_results_reports_dir", lambda: base)
    assert [e["name"] for e in srv.list_reports()][0] == newer.name


@pytest.mark.unit
def test_get_run_snapshot_is_thread_safe(srv):
    run = {"id": "r1", "status": "running", "log": ["a"], "ticker": "NVDA"}
    srv._runs["r1"] = run
    stop = threading.Event()

    def churn():
        i = 0
        while not stop.is_set():
            run[f"k{i}"] = i          # mutate while the endpoint reads
            run["log"].append(str(i))
            run.pop(f"k{i}", None)
            i += 1

    t = threading.Thread(target=churn, daemon=True)
    t.start()
    try:
        for _ in range(300):
            view = srv.get_run("r1")   # must never raise "changed size"
            assert "log" not in view and "log_tail" in view
    finally:
        stop.set()
        t.join(timeout=2)
    srv._runs.pop("r1", None)


@pytest.mark.unit
def test_analyze_rejects_future_date_and_bad_format(srv):
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        srv.post_analyze(srv.AnalyzePayload(ticker="NVDA", date="2099-01-01"))
    assert exc.value.status_code == 400
    with pytest.raises(HTTPException):
        srv.post_analyze(srv.AnalyzePayload(ticker="NVDA", date="28/07/2026"))


@pytest.mark.unit
def test_report_path_traversal_blocked(srv, tmp_path, monkeypatch):
    from fastapi import HTTPException

    base = tmp_path / "reports"
    base.mkdir()
    monkeypatch.setattr(srv, "_results_reports_dir", lambda: base)
    with pytest.raises(HTTPException):
        srv.get_report("../../../etc")
