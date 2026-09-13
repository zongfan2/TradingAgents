"""Bounded diagnostics for failed subscription CLI invocations.

Startup warnings can precede the fatal error, and subprocess wrappers can
provide diagnostics in either captured stream. Inspect both streams for
terminal errors, not just stderr's first line. This is diagnostic text only:
it never turns a nonzero exit into success or makes a retry decision, and
never logs the whole captured transcript.
"""

from __future__ import annotations

import json
import re

_SCAN_CHARS = 32_768
_MESSAGE_CHARS = 500
_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_ERROR_PREFIX = re.compile(r"^(?:error|fatal)(?::|\s*\[)", re.IGNORECASE)
_URL = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
_API_TOKEN = re.compile(r"\bsk-[A-Za-z0-9_-]+")
_BEARER = re.compile(r"\bBearer\s+[^\s,;\"']+", re.IGNORECASE)
_AUTH_HEADER = re.compile(
    r"\b((?:proxy[-_])?authorization|(?:set[-_])?cookie)[\"']?\s*[:=].*",
    re.IGNORECASE,
)
_SECRET_ASSIGNMENT = re.compile(
    r"\b([A-Za-z_][A-Za-z0-9_-]*)[\"']?\s*[:=]\s*"
    r"(?:\"[^\"]*\"|'[^']*'|[^\s,;\"']+)"
)


def _one_line(text: str) -> str:
    text = _ANSI.sub("", text)
    text = "".join(char if char.isprintable() else " " for char in text)
    text = " ".join(text.split())
    # HTTP auth/cookie values have ambiguous delimiters (Basic, Digest, quoted
    # credentials, multiple cookies). Omit the remaining diagnostic text rather
    # than leave a secret behind by guessing where that header ends.
    text = _AUTH_HEADER.sub(r"\1=[redacted]", text)
    text = _URL.sub("[url redacted]", text)
    text = _API_TOKEN.sub("[redacted]", text)
    text = _BEARER.sub("Bearer [redacted]", text)
    text = _SECRET_ASSIGNMENT.sub(
        lambda match: (
            match[1] + "=[redacted]"
            if any(word in match[1].lower()
                   for word in ("key", "token", "secret", "password", "credential", "authorization"))
            else match[0]
        ),
        text,
    )
    return text[:_MESSAGE_CHARS]


def _error_message(line: str) -> str | None:
    try:
        obj = json.loads(line)
    except (ValueError, RecursionError):
        return None
    if not isinstance(obj, dict) or obj.get("type") not in ("error", "turn.failed"):
        return None
    error = obj.get("error")
    if isinstance(error, dict) and isinstance(error.get("message"), str):
        return error["message"]
    if obj.get("type") == "error" and isinstance(obj.get("message"), str):
        return obj["message"]
    return None


def sanitize_cli_error_detail(text: str) -> str:
    """Bound and sanitize an already-selected structured backend error detail.

    Unlike transcript selection, a result envelope's error text may span lines;
    preserve that context in a single printable line without logging the body.
    """
    return _one_line(text[:_SCAN_CHARS]) or "no output"


def cli_failure_reason(stderr: str | None, stdout: str | None) -> str:
    """One bounded reason; prefer terminal JSON/ERROR lines in either stream.

    Search only each stream's bounded tail. Unrecognized stderr retains its
    first nonblank line for CLIs with plain error text. Unrecognized stdout
    is used only when stderr is empty, preserving the existing fallback.
    """
    streams = [
        ["".join(c for c in line if c.isprintable()).strip()
         for line in _ANSI.sub("", text[-_SCAN_CHARS:]).splitlines()
         if line.strip()]
        for text in (stderr or "", stdout or "")
    ]
    for lines in streams:
        for line in reversed(lines):
            message = _error_message(line)
            if message and message.strip():
                return _one_line(message)
    for lines in streams:
        for line in reversed(lines):
            if _ERROR_PREFIX.match(line):
                return _one_line(line)
    for lines in streams:
        if lines:
            return _one_line(lines[0]) or "no output"
    return "no output"
