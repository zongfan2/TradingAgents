import json
from datetime import datetime, timezone

import pytest

from devtools.verification import review_runtime


@pytest.mark.unit
def test_runtime_records_stages_and_unique_attempts(tmp_path):
    ticks = iter([1.0, 2.0, 5.0, 6.0])
    messages = []
    runtime = review_runtime.ReviewRuntime(
        tmp_path, "a" * 40, "b" * 40,
        clock=lambda: datetime(2026, 9, 5, tzinfo=timezone.utc),
        monotonic=lambda: next(ticks), progress=messages.append,
    )
    with runtime.stage("claude"):
        runtime.packet("safe packet")
    runtime.finish("complete")
    data = json.loads((runtime.directory / "run.json").read_text())
    assert data["status"] == "complete"
    assert data["duration_seconds"] == 5.0
    assert data["stages"][0]["duration_seconds"] == 3.0
    assert data["stages"][0]["status"] == "complete"
    assert data["packet_bytes"] == 11
    assert len(data["packet_sha256"]) == 64
    assert data["started_at"].startswith("2026-09-05T00:00:00")
    assert messages[0].startswith("review: claude started")
    other = review_runtime.ReviewRuntime(tmp_path, "a" * 40, "b" * 40)
    assert other.directory != runtime.directory


@pytest.mark.unit
def test_runtime_does_not_record_exception_bodies(tmp_path):
    runtime = review_runtime.ReviewRuntime(tmp_path, "a" * 40, "b" * 40)
    with pytest.raises(RuntimeError, match="secret"), runtime.stage("claude"):
        raise RuntimeError("secret webhook token")
    runtime.finish("error")
    text = (runtime.directory / "run.json").read_text()
    data = json.loads(text)
    assert data["stages"][0]["status"] == "error"
    assert data["stages"][0]["error_type"] == "RuntimeError"
    assert "secret" not in text and "token" not in text


@pytest.mark.unit
def test_runtime_refuses_symlink_archive(tmp_path):
    other = tmp_path / "elsewhere"
    other.mkdir()
    (tmp_path / "runs").symlink_to(other, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        review_runtime.ReviewRuntime(tmp_path, "a" * 40, "b" * 40)
    assert list(other.iterdir()) == []


@pytest.mark.unit
def test_runtime_write_failure_cannot_look_successful(tmp_path, monkeypatch):
    runtime = review_runtime.ReviewRuntime(tmp_path, "a" * 40, "b" * 40)
    monkeypatch.setattr(review_runtime.os, "replace", lambda *args: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError, match="disk full"):
        runtime.finish("complete")
    data = json.loads((runtime.directory / "run.json").read_text())
    assert data["status"] == "running"
    assert list(runtime.directory.iterdir()) == [runtime.directory / "run.json"]


@pytest.mark.unit
def test_failed_progress_callback_cannot_mask_original_failure(tmp_path):
    def progress(message):
        if "error" in message:
            raise BrokenPipeError("closed progress channel")

    runtime = review_runtime.ReviewRuntime(tmp_path, "a" * 40, "b" * 40, progress=progress)
    with pytest.raises(RuntimeError, match="original"), runtime.stage("claude"):
        raise RuntimeError("original failure")


@pytest.mark.unit
@pytest.mark.parametrize("failure_at", ["started", "complete"])
def test_failed_progress_callback_does_not_abort_success(tmp_path, failure_at):
    def progress(message):
        if failure_at in message:
            raise BrokenPipeError("closed progress channel")

    runtime = review_runtime.ReviewRuntime(tmp_path, "a" * 40, "b" * 40, progress=progress)
    with runtime.stage("claude"):
        pass
    runtime.finish("complete")
    data = json.loads((runtime.directory / "run.json").read_text())
    assert data["status"] == "complete"
    assert data["stages"][0]["status"] == "complete"
