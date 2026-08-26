"""pipeline.common — atomic writes, archiving, sessions, templating.

Covers the contracts' file-handling rules (temp+rename, archive-on-replace
naming) and design-doc D2: session dates resolve in the session timezone,
never the host's (the host may be America/Chicago).
"""

import hashlib
import threading
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from pipeline import common


@pytest.mark.unit
def test_atomic_write_creates_file_and_leaves_no_temp(tmp_path):
    target = tmp_path / "sub" / "2026-08-03.us.md"
    common.atomic_write(target, "hello brief")
    assert target.read_text(encoding="utf-8") == "hello brief"
    assert [p.name for p in target.parent.iterdir()] == [target.name]


@pytest.mark.unit
def test_atomic_write_replaces_existing_content(tmp_path):
    target = tmp_path / "pool.json"
    common.atomic_write(target, "old")
    common.atomic_write(target, "new")
    assert target.read_text(encoding="utf-8") == "new"


@pytest.mark.unit
def test_archive_existing_brief_naming(tmp_path):
    brief = tmp_path / "2026-08-03.us.md"
    brief.write_text("revision 1", encoding="utf-8")
    archived = common.archive_existing(brief, "2026-08-03T12:35:00Z")
    assert archived == tmp_path / "archive" / "2026-08-03.us.2026-08-03T12:35:00Z.md"
    assert archived.read_text(encoding="utf-8") == "revision 1"
    assert not brief.exists()


@pytest.mark.unit
def test_archive_existing_preserves_suffix_for_pool_and_eval_files(tmp_path):
    pool = tmp_path / "2026-08-03.json"
    pool.write_text("{}", encoding="utf-8")
    archived = common.archive_existing(pool, "2026-08-03T12:31:00Z")
    assert archived.name == "2026-08-03.2026-08-03T12:31:00Z.json"

    eval_file = tmp_path / "2026-08-03.us.eval.json"
    eval_file.write_text("{}", encoding="utf-8")
    archived = common.archive_existing(eval_file, "2026-08-03T13:00:00Z")
    assert archived.name == "2026-08-03.us.eval.2026-08-03T13:00:00Z.json"


@pytest.mark.unit
def test_archive_existing_accepts_datetime_and_returns_none_when_missing(tmp_path):
    assert common.archive_existing(tmp_path / "absent.md", "2026-08-03T12:00:00Z") is None
    brief = tmp_path / "2026-08-03.cn.md"
    brief.write_text("x", encoding="utf-8")
    moment = datetime(2026, 8, 3, 7, 30, tzinfo=ZoneInfo("America/Chicago"))
    archived = common.archive_existing(brief, moment)
    assert archived.name == "2026-08-03.cn.2026-08-03T12:30:00Z.md"


@pytest.mark.unit
def test_sha256_helpers_agree_with_hashlib(tmp_path):
    text = "brief content 中文"
    expected = hashlib.sha256(text.encode("utf-8")).hexdigest()
    assert common.sha256_text(text) == expected
    path = tmp_path / "f.md"
    path.write_text(text, encoding="utf-8")
    assert common.sha256_file(path) == expected


@pytest.mark.unit
def test_append_log_line_joins_fields(tmp_path):
    log = tmp_path / "logs" / "collector.log"
    common.append_log_line(log, "2026-08-03", "us", "claude", 12, "ok")
    common.append_log_line(log, "2026-08-04", "cn", "codex", 9, "fail")
    lines = log.read_text(encoding="utf-8").splitlines()
    assert lines == [
        "2026-08-03 | us | claude | 12 | ok",
        "2026-08-04 | cn | codex | 9 | fail",
    ]


@pytest.mark.unit
def test_locked_append_adds_newline_and_appends(tmp_path):
    ledger = tmp_path / "decisions.jsonl"
    common.locked_append(ledger, '{"run_id": 1}')
    common.locked_append(ledger, '{"run_id": 2}\n')
    assert ledger.read_text(encoding="utf-8") == '{"run_id": 1}\n{"run_id": 2}\n'


@pytest.mark.unit
def test_locked_append_concurrent_lines_stay_intact(tmp_path):
    ledger = tmp_path / "orders.jsonl"
    payload = "x" * 200

    def writer(tag):
        for i in range(25):
            common.locked_append(ledger, f"{tag}-{i}-{payload}")

    threads = [threading.Thread(target=writer, args=(t,)) for t in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    lines = ledger.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 8 * 25
    assert all(line.endswith(payload) for line in lines)


@pytest.mark.unit
def test_session_tz_mapping_uses_iana_names():
    assert common.SESSION_TZ == {"cn": "Asia/Shanghai", "us": "America/New_York"}


@pytest.mark.unit
def test_session_now_is_aware_in_session_zone():
    now_cn = common.session_now("cn")
    assert now_cn.tzinfo is not None
    assert now_cn.utcoffset() == datetime.now(ZoneInfo("Asia/Shanghai")).utcoffset()


@pytest.mark.unit
def test_session_date_chicago_host_evening_is_next_cn_day():
    # 19:30 on the host (America/Chicago) is already 08:30 the NEXT day in
    # Asia/Shanghai — the cn slot date must come from the session tz (D2).
    at = datetime(2026, 8, 3, 19, 30, tzinfo=ZoneInfo("America/Chicago"))
    assert common.session_date("cn", at) == date(2026, 8, 4)
    assert common.session_date("us", at) == date(2026, 8, 3)


@pytest.mark.unit
def test_session_date_utc_instant():
    at = datetime(2026, 8, 3, 17, 0, tzinfo=timezone.utc)  # 01:00 Aug 4 Shanghai
    assert common.session_date("cn", at) == date(2026, 8, 4)
    assert common.session_date("us", at) == date(2026, 8, 3)


@pytest.mark.unit
def test_session_date_rejects_naive_datetime_and_unknown_session():
    with pytest.raises(ValueError, match="aware"):
        common.session_date("cn", datetime(2026, 8, 3, 19, 30))
    with pytest.raises(ValueError, match="unknown session"):
        common.session_date("eu")


@pytest.mark.unit
@pytest.mark.parametrize(
    ("ticker", "session"),
    [
        ("600519.SS", "cn"),
        ("000001.SZ", "cn"),
        ("0700.HK", "cn"),
        ("0700.hk", "cn"),
        ("NVDA", "us"),
        ("BRK.B", "us"),
        ("SPY", "us"),
    ],
)
def test_session_for_ticker(ticker, session):
    assert common.session_for_ticker(ticker) == session


@pytest.mark.unit
def test_render_template_substitutes_all_keys():
    out = common.render_template(
        "date={{DATE}} session={{SESSION}} again={{SESSION}}",
        {"DATE": "2026-08-03", "SESSION": "cn"},
    )
    assert out == "date=2026-08-03 session=cn again=cn"


@pytest.mark.unit
def test_render_template_raises_on_unresolved_placeholder():
    with pytest.raises(common.TemplateError, match="SESSION_BLOCK"):
        common.render_template("x {{DATE}} y {{SESSION_BLOCK}}", {"DATE": "2026-08-03"})


@pytest.mark.unit
def test_render_template_raises_on_malformed_braces():
    with pytest.raises(common.TemplateError):
        common.render_template("broken {{ not closed", {})


@pytest.mark.unit
def test_archive_existing_never_overwrites_on_generated_at_collision(tmp_path):
    # generated_at is generator-self-reported, so two displaced revisions can
    # collide on the archive name; archived revisions must stay immutable.
    brief = tmp_path / "2026-08-03.us.md"
    brief.write_text("revision 1", encoding="utf-8")
    first = common.archive_existing(brief, "2026-08-03T12:35:00Z")
    brief.write_text("revision 2", encoding="utf-8")
    second = common.archive_existing(brief, "2026-08-03T12:35:00Z")
    assert second != first
    assert second.name == "2026-08-03.us.2026-08-03T12:35:00Z.2.md"
    assert first.read_text(encoding="utf-8") == "revision 1"
    assert second.read_text(encoding="utf-8") == "revision 2"
    assert not brief.exists()
    # A third collision keeps counting up.
    brief.write_text("revision 3", encoding="utf-8")
    third = common.archive_existing(brief, "2026-08-03T12:35:00Z")
    assert third.name == "2026-08-03.us.2026-08-03T12:35:00Z.3.md"
    assert third.read_text(encoding="utf-8") == "revision 3"
