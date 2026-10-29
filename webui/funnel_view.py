"""Decision-funnel page data (specs/decision-funnel.md R4).

Pure functions over the R3 snapshot files
(``<state_dir>/funnel/<session>/<date>.json``): date listing, one snapshot
read through the contract's lenient reader, cross-date aggregation and the
CSV export. Nothing here writes a file or calls the network.
"""

from __future__ import annotations

import csv
import io
import json
import re
from collections import Counter
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

from pipeline.common import SESSIONS
from pipeline.contracts.funnel import FunnelSnapshot
from pipeline.funnel_events import funnel_dir

_SNAPSHOT_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})\.json$")
STAGES = ("nominate", "pool", "collect", "evaluate", "trigger", "decide", "execute")
FINALS = ("order-live", "order-dry", "excluded", "pending", "unknown")
#: Cells starting with these are formulas in spreadsheet apps (CSV injection);
#: reasons are model-written text, so they are neutralised with a quote.
_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def snapshot_dates(state_dir: str | Path) -> dict[str, list[str]]:
    """Snapshot dates per session, newest first."""
    out: dict[str, list[str]] = {}
    for session in SESSIONS:
        directory = funnel_dir(state_dir, session)
        dates = (
            [m.group(1) for p in directory.iterdir() if p.is_file() and (m := _SNAPSHOT_RE.match(p.name))]
            if directory.is_dir()
            else []
        )
        out[session] = sorted(dates, reverse=True)
    return out


def read_snapshot(state_dir: str | Path, session: str, day: str) -> tuple[dict | None, str | None]:
    """``(snapshot, None)`` or ``(None, reason)`` — the contract's reader mode
    (unknown fields warned and dropped), never an exception."""
    path = funnel_dir(state_dir, session) / f"{day}.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None, "not yet run"
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None, f"snapshot unreadable: {path.name}"
    try:
        model = FunnelSnapshot.parse_lenient(data)
    except Exception as exc:
        return None, f"snapshot invalid: {' '.join(str(exc).split())[:200]}"
    return model.model_dump(mode="json", by_alias=True), None


def aggregate(snapshots: Iterable[dict]) -> dict[str, Any]:
    """Counts over many slots: where tickers stop (stage × code), finals per
    date, and per-ticker exclusion frequency."""
    stops: Counter[tuple[str, str | None]] = Counter()
    finals: Counter[str] = Counter()
    by_date: list[dict[str, Any]] = []
    tickers: dict[str, dict[str, Any]] = {}
    slots = 0
    for snap in sorted(snapshots, key=lambda s: (s["date"], s["session"])):
        slots += 1
        day_finals: Counter[str] = Counter()
        for entry in snap["tickers"]:
            final = entry["final"]
            finals[final] += 1
            day_finals[final] += 1
            stop = entry.get("stop")
            if stop:
                stops[(stop["stage"], stop["code"])] += 1
            row = tickers.setdefault(
                entry["ticker"],
                {"ticker": entry["ticker"], "slots": 0, "excluded": 0, "orders": 0,
                 "stops": Counter()},
            )
            row["slots"] += 1
            if final == "excluded":
                row["excluded"] += 1
            if final in ("order-live", "order-dry"):
                row["orders"] += 1
            if stop:
                row["stops"][f"{stop['stage']}:{stop['code'] or 'unknown'}"] += 1
        by_date.append({
            "date": snap["date"],
            "session": snap["session"],
            "counts": {stage: snap["counts"][stage] for stage in STAGES},
            "finals": {f: day_finals.get(f, 0) for f in FINALS},
        })
    ticker_rows = []
    for row in tickers.values():
        top = row["stops"].most_common()
        ticker_rows.append({
            "ticker": row["ticker"],
            "slots": row["slots"],
            "excluded": row["excluded"],
            "orders": row["orders"],
            # Deterministic: most frequent first, then by stage:code.
            "stops": [
                {"stop": key, "count": n}
                for key, n in sorted(top, key=lambda kv: (-kv[1], kv[0]))
            ],
        })
    ticker_rows.sort(key=lambda r: (-r["excluded"], -r["slots"], r["ticker"]))
    return {
        "slots": slots,
        "finals": {f: finals.get(f, 0) for f in FINALS},
        "stops": [
            {"stage": stage, "code": code, "count": n}
            for (stage, code), n in sorted(
                stops.items(),
                key=lambda kv: (-kv[1], STAGES.index(kv[0][0]), kv[0][1] or ""),
            )
        ],
        "by_date": by_date,
        "tickers": ticker_rows,
    }


CSV_COLUMNS = (
    "date", "session", "ticker", "layer", "furthest_stage", "final",
    "stop_stage", "stop_code", "stop_reason", "stop_provenance", "provenance",
    *STAGES,
)


def _cell(value: object) -> str:
    text = "" if value is None else str(value)
    return "'" + text if text.startswith(_FORMULA_PREFIXES) else text


def snapshot_csv(snapshot: dict, *, columns: Sequence[str] = CSV_COLUMNS) -> str:
    """One row per ticker; each stage column is ``outcome:code`` (code
    ``unknown`` when the stage could not be explained)."""
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(columns)
    for entry in snapshot["tickers"]:
        stop = entry.get("stop") or {}
        row = {
            "date": snapshot["date"],
            "session": snapshot["session"],
            "ticker": entry["ticker"],
            "layer": entry.get("layer"),
            "furthest_stage": entry.get("furthest_stage"),
            "final": entry["final"],
            "stop_stage": stop.get("stage"),
            "stop_code": stop.get("code"),
            "stop_reason": stop.get("reason"),
            "stop_provenance": stop.get("provenance"),
            "provenance": "+".join(entry.get("provenance") or ()),
        }
        for stage in STAGES:
            record = entry["stages"].get(stage)
            row[stage] = (
                f"{record['outcome']}:{record['code'] or 'unknown'}" if record else ""
            )
        writer.writerow([_cell(row[column]) for column in columns])
    return buffer.getvalue()
