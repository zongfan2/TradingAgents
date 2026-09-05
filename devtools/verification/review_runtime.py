"""Per-attempt review timing without subprocess output or model reasoning."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import tempfile
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from datetime import datetime, timezone
from pathlib import Path


def _now() -> datetime:
    return datetime.now(timezone.utc)


def reason_code(error: BaseException) -> str:
    """Map only trusted categories, never arbitrary exception messages."""
    code = getattr(error, "code", None)
    allowed = {
        "invalid-json", "invalid-schema", "checkout-failed", "layer1-failed",
        "layer1-timeout", "claude-timeout", "claude-exit", "cli-not-found",
        "claude-start-failed", "test-evidence-conflict", "invalid-context",
        "layer1-definition-mismatch",
    }
    if code in allowed:
        return code
    return "io-error" if isinstance(error, OSError) else "review-error"


class ReviewRuntime:
    """A unique diagnostic archive; it never determines a review verdict."""

    def __init__(
        self, root: Path, base_sha: str, head_sha: str, *,
        clock: Callable[[], datetime] = _now,
        monotonic: Callable[[], float] = time.monotonic,
        progress: Callable[[str], None] | None = None,
    ) -> None:
        if not all(re.fullmatch(r"[a-f0-9]{40,64}", value) for value in (base_sha, head_sha)):
            raise ValueError("invalid review identity")
        self.clock = clock
        self.monotonic = monotonic
        self.progress = progress or (lambda message: print(message, file=sys.stderr, flush=True))
        self.started = monotonic()
        run_id = uuid.uuid4().hex
        self.directory = root / "runs" / head_sha / run_id
        if not root.is_absolute() or self.directory.resolve() != self.directory:
            raise ValueError("review archive must be absolute and not symlinked")
        self.directory.mkdir(parents=True, mode=0o700, exist_ok=False)
        self.data: dict = {
            "schema_version": 1, "run_id": run_id, "base_sha": base_sha,
            "head_sha": head_sha, "started_at": self.timestamp(),
            "finished_at": None, "duration_seconds": None, "status": "running",
            "stages": [], "review_notes": [],
            "reviewer_model": None,
            "model_metadata_note": "CLI default; text transport does not attest the resolved model or usage.",
        }
        self.write()

    def timestamp(self) -> str:
        value = self.clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("review clock must be timezone-aware")
        return value.astimezone(timezone.utc).isoformat()

    def write(self) -> None:
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=self.directory, delete=False,
            ) as stream:
                temporary = Path(stream.name)
                json.dump(self.data, stream, ensure_ascii=False, indent=2, allow_nan=False)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.directory / "run.json")
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    @contextmanager
    def stage(self, name: str) -> Iterator[None]:
        if name not in {"checkout", "layer1", "packet", "claude", "validation", "persistence"}:
            raise ValueError("unknown review stage")
        started = self.monotonic()
        stage = {"name": name, "started_at": self.timestamp(), "status": "running"}
        self.data["stages"].append(stage)
        self.write()
        with suppress(OSError):
            self.progress(f"review: {name} started")
        error: BaseException | None = None
        try:
            yield
        except BaseException as exc:
            error = exc
            stage["status"] = "error"
            stage["error_type"] = type(exc).__name__
            stage["reason_code"] = reason_code(exc)
            self.data["reason_code"] = reason_code(exc)
            raise
        else:
            stage["status"] = "complete"
        finally:
            stage["finished_at"] = self.timestamp()
            stage["duration_seconds"] = round(max(0.0, self.monotonic() - started), 6)
            try:
                self.write()
            except OSError:
                if error is None:
                    raise
            with suppress(OSError):
                self.progress(f"review: {name} {stage['status']} ({stage['duration_seconds']:.2f}s)")

    def packet(self, text: str) -> None:
        encoded = text.encode("utf-8")
        self.data["packet_bytes"] = len(encoded)
        self.data["packet_sha256"] = hashlib.sha256(encoded).hexdigest()
        self.write()

    def finish(self, status: str) -> None:
        if status not in {"complete", "error"}:
            raise ValueError("invalid terminal review status")
        self.data["status"] = status
        self.data["finished_at"] = self.timestamp()
        self.data["duration_seconds"] = round(max(0.0, self.monotonic() - self.started), 6)
        self.write()
