"""Fake ``claude -p --output-format json`` result envelopes for offline tests."""

from __future__ import annotations

import json


def claude_envelope(result: str = "", **extra: object) -> str:
    """A successful envelope carrying ``result`` text and sample usage."""
    envelope = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": result,
        "usage": {
            "input_tokens": 100,
            "cache_creation_input_tokens": 50,
            "cache_read_input_tokens": 900,
            "output_tokens": 40,
        },
        "total_cost_usd": 0.12,
        "num_turns": 3,
        "duration_ms": 4000,
    }
    envelope.update(extra)
    return json.dumps(envelope)
