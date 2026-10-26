"""Decision-funnel snapshot builder (specs/decision-funnel.md R3).

``python -m pipeline.funnel build --session S --date D`` folds one slot's
R1 event log and its on-disk files (pool, nomination cache, collector log,
briefs + evals, ledger decisions, order rows) into
``<state_dir>/funnel/<session>/<date>.json``: one entry per ticker the slot
ever considered, with the stage it stopped at, the code and reason, and the
full per-stage trail.

Every stage of every ticker carries a ``provenance``:

- ``recorded`` — from R1 events, reduced by
  :func:`pipeline.funnel_events.current_outcomes` (the supersession rule);
- ``reconstructed`` — derived from files by a rule the component itself
  applies deterministically (a watch member is never collected, a brief
  below the trigger threshold is never evaluated, a cn decision is never
  executed …);
- ``unknown`` — the files show the ticker reached the stage but not why it
  stopped (``code`` is then ``null``): a code is never invented.

``backfill --since D`` rebuilds past slots the same way; slots that predate
R1 come out ``reconstructed``/``unknown``. The snapshot is a pure function of
its inputs (no build timestamp, sorted keys), so identical inputs give a
byte-identical file.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections import Counter
from collections.abc import Sequence
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from pipeline.common import (
    SESSIONS,
    atomic_write,
    session_date,
    session_for_ticker,
    sha256_text,
)
from pipeline.config import PipelineConfig, load_config
from pipeline.contracts.briefs import split_frontmatter
from pipeline.contracts.funnel import SNAPSHOT_SCHEMA_VERSION, FunnelSnapshot
from pipeline.contracts.ledger import DecisionRecord
from pipeline.contracts.pool import OPPORTUNITY_CAP, WATCH_CAP, PoolFile, read_pool
from pipeline.execution_adapter import (
    _latest_attempts,
    load_decision_records,
    non_candidate_reason,
    read_order_rows,
)
from pipeline.funnel_events import (
    EVENTS_SUFFIX,
    FUNNEL_DIRNAME,
    current_outcomes,
    events_path,
    funnel_dir,
    reason_code,
)

logger = logging.getLogger(__name__)

SNAPSHOT_SCHEMA = SNAPSHOT_SCHEMA_VERSION
STAGES = ("nominate", "pool", "collect", "evaluate", "trigger", "decide", "execute")
RECORDED, RECONSTRUCTED, UNKNOWN = "recorded", "reconstructed", "unknown"
FINALS = ("order-live", "order-dry", "excluded", "pending", "unknown")

#: Spec defaults, used when neither a config snapshot nor an event records
#: the value in force (``thresholds.source`` says which applied).
DEFAULT_TRIGGER_THRESHOLD = 7.0
DEFAULT_ENTRY_THRESHOLD = 6.0
DEFAULT_EXIT_THRESHOLD = 4.0

_STRENGTH = {"info": 0, "excluded": 1, "passed": 2}
_VERDICTS = ("pass", "warn", "fail")
#: Pool ``removed[].reason`` prefixes → the pool-stage code that wrote them.
_REMOVED_REASON_CODES = (
    ("score<", "removed-low-score-streak"),
    ("technical gate fail for", "removed-gate-fail-streak"),
    ("opportunity cap", "cap-truncated"),
)

Entry = dict[str, Any]


def snapshot_path(state_dir: str | Path, session: str, slot_date: date | str) -> Path:
    day = slot_date.isoformat() if isinstance(slot_date, date) else str(slot_date)
    return funnel_dir(state_dir, session) / f"{day}.json"


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------


class _Inputs:
    """Reads one slot's files and records a sha256 per input it used."""

    def __init__(self, config: PipelineConfig) -> None:
        self.config = config
        self.hashes: dict[str, str] = {}

    def _label(self, path: Path) -> str:
        try:
            return path.relative_to(self.config.state_dir).as_posix()
        except ValueError:
            return path.as_posix()

    def text(self, path: Path, *, hashed: bool = True) -> str | None:
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return None
        if hashed:
            self.hashes[self._label(path)] = sha256_text(text)
        return text

    def json(self, path: Path) -> Any:
        text = self.text(path)
        if text is None:
            return None
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return None

    def rows(self, label: str, rows: Sequence[Any]) -> None:
        """Hash only the slot's rows of a shared append-only file, so a later
        day's appends never change an earlier slot's snapshot."""
        if rows:
            self.hashes[label] = sha256_text(_dumps(list(rows)))


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _verdict(inputs: _Inputs, brief: Path) -> str:
    """Revision-bound eval verdict — the same rule as
    ``tradingagents.dataflows.brief_evals.get_eval_verdict`` (whose package
    import loads .env and the vendor stack, too heavy for the orchestrator)."""
    text = inputs.text(brief)
    if text is None:
        return "missing"
    evaluation = inputs.json(brief.with_name(f"{brief.stem}.eval.json"))
    if not isinstance(evaluation, dict):
        return "missing"
    if evaluation.get("brief_sha256") != sha256_text(text):
        return "missing"
    verdict = evaluation.get("verdict")
    if isinstance(verdict, str) and verdict.strip().lower() in _VERDICTS:
        return verdict.strip().lower()
    return "missing"


def _catalyst_score(inputs: _Inputs, brief: Path) -> float | None:
    text = inputs.text(brief)
    if text is None:
        return None
    meta, _body, _errors = split_frontmatter(text)
    try:
        return float((meta or {})["catalyst_score"])
    except (KeyError, TypeError, ValueError):
        return None


def _safe_session(ticker: str) -> str | None:
    try:
        return session_for_ticker(ticker)
    except Exception:
        return None


def _entry(
    outcome: str,
    code: str | None,
    reason: str,
    provenance: str = RECONSTRUCTED,
    **details: Any,
) -> Entry:
    return {
        "outcome": outcome,
        "code": code,
        "reason": reason,
        "provenance": provenance,
        "details": details,
        "events": [],
    }


def _strongest(entries: Sequence[Entry]) -> Entry:
    """Strongest outcome; the first of equals wins (input order is meaningful)."""
    return max(entries, key=lambda e: _STRENGTH.get(e["outcome"], -1))


# ---------------------------------------------------------------------------
# Snapshot
# ---------------------------------------------------------------------------


class _Slot:
    def __init__(self, config: PipelineConfig, session: str, slot_date: date) -> None:
        if session not in SESSIONS:
            raise ValueError(f"unknown session {session!r}")
        self.config = config
        self.session = session
        self.slot_date = slot_date
        self.day = slot_date.isoformat()
        self.inputs = _Inputs(config)
        self.stages: dict[str, dict[str, Entry]] = {stage: {} for stage in STAGES}

    # -- loading ---------------------------------------------------------

    def load(self) -> None:
        inputs = self.inputs
        # Hashed below by the kept lines only (later-day appends drop out).
        text = inputs.text(events_path(self.config.state_dir, self.session, self.day), hashed=False)
        self.events: list[dict] = []
        for line in (text or "").splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if (
                isinstance(event, dict)
                and event.get("session") == self.session
                and event.get("date") == self.day
                and event.get("stage") in STAGES
                # Execute events follow the order-row rule: a later-day
                # re-execution under this slot's run_id is that day's.
                and (event.get("stage") != "execute" or self._written_on_slot_day(event))
            ):
                self.events.append(event)

        self.pool: PoolFile | None = None
        self.pool_error: str | None = None
        self.pool_used: PoolFile | None = None  # newest ≤ date (what collectors read)
        #: No usable pool: collectors/evaluator/runner fall back to the core
        #: yaml, and a too-old pool's opportunity layer is dropped.
        self.core_fallback: list[str] | None = None
        self.core_error: str | None = None
        self.pool_stale: PoolFile | None = None
        self.pool_gap_days: int | None = None
        try:
            result = read_pool(
                self.config.pool_dir, self.session, self.slot_date,
                self.config.pool_max_staleness_days,
            )
        except Exception as exc:
            self.pool_error = " ".join(str(exc).split())[:300]
        else:
            if result.path is not None:
                inputs.text(Path(result.path))
            if result.pool is not None and result.staleness != "absent":
                self.pool_used = result.pool
                if result.pool.as_of_date == self.slot_date:
                    self.pool = result.pool
            else:
                self.pool_stale = result.pool
                self.pool_gap_days = result.gap_days
                self.core_fallback = self._core_yaml()

        cache = inputs.json(
            Path(self.config.pool_dir) / self.session / f".nomination-cache-{self.day}.json"
        )
        nominations = cache.get("nominations") if isinstance(cache, dict) else None
        self.nominations: list[dict] | None = (
            [n for n in nominations if isinstance(n, dict)] if isinstance(nominations, list) else None
        )

        self.collector_rows: dict[str, str] = {}
        # Shared and append-only: hash only this slot's lines.
        log_path = Path(self.config.ticker_brief_dir) / "collector.log"
        log_text = inputs.text(log_path, hashed=False) or ""
        slot_lines = []
        for line in log_text.splitlines():
            fields = [field.strip() for field in line.split("|")]
            if len(fields) >= 3 and fields[0] == self.day and fields[1] == self.session:
                slot_lines.append(line)
                self.collector_rows[fields[2]] = fields[-1]
        inputs.rows("ticker_briefs/collector.log#slot", slot_lines)

        prefix = f"{self.day}-{self.session}-"
        slot_records = [
            r for r in load_decision_records(self.config.ledger_dir)
            if r.date == self.slot_date and r.session == self.session
        ]
        # Manual runs share the slot's run_id prefix but are not the slot's
        # decisions (they emit no trigger/decide events either): drop them,
        # their order rows and their execute events.
        manual = {r.run_id for r in slot_records if r.trigger == "manual"}
        all_records = [r for r in slot_records if r.trigger != "manual"]
        scheduled = {(r.ticker, r.arm) for r in all_records}
        manual_latest = {
            (r.ticker, r.arm): r for r in _latest_attempts(slot_records)
            if r.trigger == "manual" and (r.ticker, r.arm) in scheduled
        }
        def manual_execute(event: dict) -> bool:
            return (
                event.get("stage") == "execute"
                and (event.get("details") or {}).get("run_id") in manual
            )

        # The SCHEDULED submit run skips a manual latest attempt with
        # 'manual-trigger' (an operator's submit --run-id --execute never
        # does): proof the execute step ran, though not the manual run's
        # outcome. Kept as evidence for execute_ran and shadow timing only.
        self.execute_marks: list[dict] = [
            e for e in self.events if manual_execute(e) and e.get("code") == "manual-trigger"
        ]
        self.events = [e for e in self.events if not manual_execute(e)]
        inputs.rows(
            f"{FUNNEL_DIRNAME}/{self.session}/{self.day}{EVENTS_SUFFIX}#slot", self.events
        )
        inputs.rows(
            "ledger/decisions.jsonl#slot",
            [r.model_dump(mode="json", by_alias=True) for r in all_records],
        )
        # Manual rows can shadow the slot's decision (manual-shadowed), so
        # they are an input too — under their own label.
        inputs.rows(
            "ledger/decisions.jsonl#manual",
            [r.model_dump(mode="json", by_alias=True)
             for r in slot_records if r.trigger == "manual"],
        )
        self.records: list[DecisionRecord] = _latest_attempts(all_records)
        current = {id(r) for r in self.records}
        self.superseded = [r for r in all_records if id(r) not in current]
        # This slot's decisions, as executed on the slot day: a close the
        # execute refresh re-submits on a later day keeps the original run_id
        # but belongs to that day, not to this snapshot.
        day_rows = [
            row for row in read_order_rows(self.config.ledger_dir)
            if str(row.get("run_id") or "").startswith(prefix)
            and self._written_on_slot_day(row)
        ]
        self.orders = [row for row in day_rows if row.get("run_id") not in manual]
        mark_rows = [
            row for row in day_rows
            if row.get("run_id") in manual and row.get("kind") == "skip"
            and reason_code(str(row.get("reason") or "")) == "manual-trigger"
        ]
        self.execute_marks.extend(mark_rows)
        inputs.rows("ledger/orders.jsonl#slot", self.orders)
        inputs.rows("ledger/orders.jsonl#manual-trigger", mark_rows)

        execute_events = [e for e in self.events if e.get("stage") == "execute"]
        self.execute_ran = self.session == "us" and bool(
            self.orders or execute_events or self.execute_marks
        )
        self.shadowed = self._shadowed(manual_latest, execute_events)
        macro_dir = Path(self.config.macro_brief_dir)
        self.macro_verdict = _verdict(inputs, macro_dir / f"{self.day}.{self.session}.md")
        self.thresholds = self._thresholds()

    def _shadowed(
        self, manual_latest: dict[tuple[str, str], DecisionRecord], execute_events: list[dict]
    ) -> dict[tuple[str, str], DecisionRecord]:
        """(ticker, arm) whose slot decision a manual attempt shadowed.

        Manual runs share the slot's attempt counter and the adapter's
        latest-attempt rule runs over every row, so a manual attempt that
        EXISTED when an execute run ran made that run supersede the slot's
        row and skip the manual one. A manual rerun after execution shadows
        nothing, and a live order the slot's row placed always stands."""
        # A submit run writes intent/submitted/skip rows; refresh/cancel rows
        # come from refresh, which never applies the latest-attempt rule.
        submit_rows = [r for r in self.orders if r.get("kind") in ("intent", "submitted", "skip")]
        activity = [
            t for t in (
                _instant(x.get("written_at"))
                for x in (*submit_rows, *execute_events, *self.execute_marks)
            )
            if t is not None
        ]
        live_runs = {
            row.get("run_id") for row in self.orders
            if row.get("kind") == "submitted" and not row.get("dry_run")
            and row.get("broker_status") != "rejected"
        } | {
            (e.get("details") or {}).get("run_id") for e in execute_events
            if e.get("code") == "submitted" and not (e.get("details") or {}).get("dry_run")
        }
        scheduled = {(r.ticker, r.arm): r for r in self.records}
        return {
            key: manual for key, manual in manual_latest.items()
            if key in scheduled
            and scheduled[key].run_id not in live_runs
            and any(t > manual.decided_at for t in activity)
        }

    def _core_yaml(self) -> list[str]:
        """The core layer the components fall back to without a usable pool
        (``ticker_collector.read_core_yaml``, the collectors' own reader).
        Note: the file is read as it is NOW — a backfill of an old degraded
        slot uses today's core list (its hash is in ``inputs``)."""
        from pipeline.ticker_collector import read_core_yaml

        self.inputs.text(Path(self.config.pool_dir) / f"core.{self.session}.yaml")
        try:
            return read_core_yaml(Path(self.config.pool_dir), self.session)
        except Exception as exc:
            self.core_error = " ".join(str(exc).split())[:300]
            return []

    def _written_on_slot_day(self, row: dict) -> bool:
        written = _instant(row.get("written_at"))
        return written is not None and session_date(self.session, written) == self.slot_date

    def _thresholds(self) -> dict[str, Any]:
        thresholds: dict[str, Any] = {
            "trigger": DEFAULT_TRIGGER_THRESHOLD,
            "entry": DEFAULT_ENTRY_THRESHOLD,
            "exit": DEFAULT_EXIT_THRESHOLD,
            "opportunity_cap": OPPORTUNITY_CAP,
            "watch_cap": WATCH_CAP,
        }
        sources = {"trigger": "default", "entry": "default", "exit": "default"}
        digests = sorted({r.inputs.config_digest for r in self.records})
        for digest in digests:
            snapshot = self.inputs.json(Path(self.config.ledger_dir) / "config_snapshots" / f"{digest}.json")
            if isinstance(snapshot, dict) and snapshot.get("analysis_trigger_threshold") is not None:
                thresholds["trigger"] = float(snapshot["analysis_trigger_threshold"])
                sources["trigger"] = "config-snapshot"
                break
        for event in self.events:
            details = event.get("details") or {}
            if event.get("stage") == "pool" and isinstance(details.get("thresholds"), dict):
                for key in ("entry", "exit"):
                    if details["thresholds"].get(key) is not None:
                        thresholds[key] = details["thresholds"][key]
                        sources[key] = "events"
            if (
                sources["trigger"] == "default"
                and event.get("code") in ("below-threshold", "skipped-below-threshold")
                and details.get("threshold") is not None
            ):
                thresholds["trigger"] = details["threshold"]
                sources["trigger"] = "events"
        thresholds["source"] = sources
        return thresholds

    # -- reconstruction ----------------------------------------------------

    def _brief(self, ticker: str) -> Path:
        return Path(self.config.ticker_brief_dir) / ticker / f"{self.day}.md"

    def _layers(self) -> tuple[list[str], list[str]]:
        """Core and opportunity tickers the collectors/evaluator served."""
        pool = self.pool_used
        if pool is None:
            return list(self.core_fallback or ()), []
        core = [e.ticker for e in pool.core]
        opportunity = [e.ticker for e in pool.opportunity if e.ticker not in set(core)]
        return core, opportunity

    def reconstruct(self) -> None:
        self._nominate()
        self._pool()
        self._collect()
        self._evaluate()
        self._trigger()
        self._decide()
        self._execute()

    def _nominate(self) -> None:
        out = self.stages["nominate"]
        for nomination in self.nominations or ():
            ticker = nomination.get("ticker")
            if not isinstance(ticker, str) or not ticker or ticker in out:
                continue  # a duplicate keeps its first, passed nomination
            if _safe_session(ticker) != self.session:
                out[ticker] = _entry("excluded", "wrong-session", "wrong session")
                continue
            score = nomination.get("score")
            label = f"score {score:g}" if isinstance(score, (int, float)) else "score ?"
            out[ticker] = _entry(
                "passed", "nominated", f"{label} ({nomination.get('catalyst_type')})",
                score=score, catalyst_type=nomination.get("catalyst_type"),
            )

    def _pool(self) -> None:
        pool, out = self.pool, self.stages["pool"]
        if pool is None:
            return
        for entry in pool.core:
            out[entry.ticker] = _entry(
                "passed", "core", "core watchlist (always analysed)", score=entry.score
            )
        for entry in pool.opportunity:
            if entry.ticker in out:
                continue
            if pool.carried_forward:
                code, reason = "carried-forward", "opportunity carried forward (nomination failed)"
            elif entry.entered_on == self.slot_date:
                code, reason = "opportunity-enter", f"entered with score {entry.score:g}"
            else:
                code = "opportunity-stay"
                reason = f"member since {entry.entered_on.isoformat()}, score {entry.score:g}"
            out[entry.ticker] = _entry(
                "passed", code, reason, score=entry.score, gate=entry.technical.gate
            )
        for entry in pool.watch:
            if pool.carried_forward:
                code, reason = "carried-forward", "watch carried forward (nomination failed)"
            else:
                code, reason = "watch", f"watch layer, score {entry.score:g}"
            out[entry.ticker] = _entry(
                "excluded", code, reason, score=entry.score, gate=entry.technical.gate
            )
        for entry in pool.removed:
            code = next(
                (c for prefix, c in _REMOVED_REASON_CODES if entry.reason.startswith(prefix)),
                None,
            )
            out[entry.ticker] = _entry(
                "excluded", code, entry.reason, RECONSTRUCTED if code else UNKNOWN,
                score=entry.last_score,
            )
        for ticker, nominated in self.stages["nominate"].items():
            if nominated["outcome"] == "passed" and ticker not in out:
                score = nominated["details"].get("score")
                out[ticker] = _entry(
                    "excluded", None,
                    f"nominated (score {score}) but not in the pool — low score, gate "
                    "fail or watch cap; the cause was not recorded",
                    UNKNOWN, score=score,
                )

    def _collect(self) -> None:
        out = self.stages["collect"]
        core, opportunity = self._layers()
        # Only core is collected without a usable pool; a core ticker is
        # analysed anyway, so its failure is info (as a core eval fail).
        core_set = set(core) if self.pool_used is not None else None
        for ticker, outcome in self.collector_rows.items():
            if outcome == "written":
                out[ticker] = _entry("passed", "written", "brief written")
            elif outcome == "skipped":  # a rerun found the brief on disk
                out[ticker] = _entry("passed", "exists", "brief already on disk")
            elif outcome == "failed":
                is_core = core_set is None or ticker in core_set
                out[ticker] = _entry(
                    "info" if is_core else "excluded", "failed",
                    "collector failed (reason not logged)",
                )
        for ticker in (*core, *opportunity):
            if ticker not in out and self._brief(ticker).is_file():
                out[ticker] = _entry(
                    "passed", None, "brief on disk; collector outcome not logged", UNKNOWN
                )
        if self.pool_stale is not None:
            for entry in self.pool_stale.opportunity:
                if entry.ticker not in core:
                    out.setdefault(entry.ticker, _entry(
                        "excluded", "opportunity-dropped-stale-pool",
                        f"pool {self.pool_gap_days} day(s) old — opportunity layer dropped",
                        gap_days=self.pool_gap_days,
                    ))
        if self.pool_used is not None:
            # The collectors skip the watch layer of whatever pool they
            # served (a same-day or a warn-stale one) and say so.
            for entry in self.pool_used.watch:
                out.setdefault(
                    entry.ticker,
                    _entry("excluded", "not-collected-watch", "watch layer is not collected"),
                )

    def _evaluate(self) -> None:
        out = self.stages["evaluate"]
        core, opportunity = self._layers()
        threshold = self.thresholds["trigger"]
        for layer, tickers in (("core", core), ("opportunity", opportunity)):
            stop = "info" if layer == "core" else "excluded"
            for ticker in tickers:
                brief = self._brief(ticker)
                if not brief.is_file():
                    out[ticker] = _entry(stop, "skipped-absent", "no brief to evaluate")
                    continue
                verdict = _verdict(self.inputs, brief)
                score = _catalyst_score(self.inputs, brief)
                if verdict in ("pass", "warn"):
                    out[ticker] = _entry("passed", verdict, f"eval {verdict}", score=score)
                elif verdict == "fail":
                    reason = "eval fail — brief withheld" if layer == "core" else "eval fail"
                    out[ticker] = _entry(stop, "fail", reason, score=score)
                elif layer == "opportunity" and score is not None and score < threshold:
                    out[ticker] = _entry(
                        "excluded", "skipped-below-threshold",
                        f"brief catalyst_score {score:g} < trigger {threshold:g}",
                        score=score, threshold=threshold,
                    )
                else:
                    out[ticker] = _entry(
                        "info", None, "no revision-bound eval on disk", UNKNOWN, score=score
                    )

    def _by_ticker(self) -> dict[str, list[DecisionRecord]]:
        grouped: dict[str, list[DecisionRecord]] = {}
        for record in sorted(self.records, key=lambda r: (r.ticker, r.arm != "brief")):
            grouped.setdefault(record.ticker, []).append(record)
        return grouped

    def _trigger(self) -> None:
        out = self.stages["trigger"]
        decided = self._by_ticker()
        for ticker, records in decided.items():
            arms = [r.arm for r in records]
            if arms == ["feeds"] and records[0].macro_eval_verdict == "fail":
                code, reason = "macro-fail-feeds-only", "macro eval fail — feeds-only run"
            elif any(r.trigger == "catalyst" for r in records):
                score = records[0].catalyst_score
                code = "catalyst"
                reason = f"catalyst_score {score:g}" if score is not None else "catalyst"
            elif any(r.pair_id for r in records):
                code, reason = "core-pair", "core A/B pair (brief + feeds)"
            else:
                code, reason = "core", "core watchlist"
            out[ticker] = _entry("passed", code, reason, arms=arms)
        core, opportunity = self._layers()
        threshold = self.thresholds["trigger"]
        for ticker in core:
            out.setdefault(
                ticker,
                _entry("info", None, "core ticker without a decision row (run failure or "
                       "not yet run — not recorded)", UNKNOWN),
            )
        for ticker in opportunity:
            if ticker in out:
                continue
            brief = self._brief(ticker)
            if not brief.is_file():
                out[ticker] = _entry(
                    "excluded", None,
                    "no same-day brief on disk (an earlier one may have been served)", UNKNOWN,
                )
                continue
            score = _catalyst_score(self.inputs, brief)
            if score is None:
                out[ticker] = _entry("excluded", "no-score", "no ticker brief catalyst_score")
            elif _verdict(self.inputs, brief) == "fail":
                out[ticker] = _entry(
                    "excluded", "eval-fail", f"ticker eval fail (score {score:g})", score=score
                )
            elif score < threshold:
                out[ticker] = _entry(
                    "excluded", "below-threshold",
                    f"brief catalyst_score {score:g} < trigger {threshold:g}",
                    score=score, threshold=threshold,
                )
            else:
                out[ticker] = _entry(
                    "excluded", None,
                    f"met the trigger rules (score {score:g}) but has no decision row — "
                    "budget guard or run failure, not recorded",
                    UNKNOWN, score=score,
                )

    def _decide(self) -> None:
        out = self.stages["decide"]
        for ticker, records in self._by_ticker().items():
            arms = []
            for record in records:
                actionable = record.decision in ("BUY", "SELL") and record.plan_valid
                if record.decision == "ERROR":
                    reason = "run error (text not recorded)"
                elif not record.plan_valid:
                    reason = "plan invalid (violations not recorded)"
                else:
                    reason = record.decision
                arms.append(
                    _entry(
                        "passed" if actionable else "excluded", record.decision, reason,
                        arm=record.arm, run_id=record.run_id, pair_id=record.pair_id,
                        plan_valid=record.plan_valid,
                    )
                )
            best = dict(_strongest(arms))
            best["events"] = [_compact(arm) for arm in arms]
            out[ticker] = best

    def _execute(self) -> None:
        out = self.stages["execute"]
        if self.session != "us":
            for ticker, records in self._by_ticker().items():
                out[ticker] = _entry(
                    "excluded", "non-us-session", "not an execution candidate: non-us-session",
                    arm=records[0].arm,
                )
            return
        if not self.execute_ran:
            # Not run yet: a ticker with no candidate arm (HOLD brief + BUY
            # feeds, …) is already excluded by the adapter's pure shape
            # filter; only one with a candidate arm is pending.
            for ticker, records in self._by_ticker().items():
                reasons = [non_candidate_reason(record) for record in records]
                if all(reasons):
                    entries = [
                        _entry("excluded", reason, f"not an execution candidate: {reason}",
                               arm=record.arm, run_id=record.run_id, decision=record.decision)
                        for record, reason in zip(records, reasons, strict=True)
                    ]
                    best = dict(_strongest(entries))
                    best["events"] = [_compact(entry) for entry in entries]
                    out[ticker] = best
            return
        rows_by_run: dict[str, list[dict]] = {}
        for row in self.orders:
            rows_by_run.setdefault(str(row.get("run_id")), []).append(row)
        for ticker, records in self._by_ticker().items():
            arm_results: list[Entry] = []  # one per arm, brief first
            trail: list[Entry] = []
            for record in records:
                shadow = self.shadowed.get((record.ticker, record.arm))
                if shadow is not None:
                    entry = _entry(
                        "excluded", "manual-shadowed",
                        f"manual run {shadow.run_id} became this ticker/arm's latest attempt; "
                        "the adapter superseded this decision and skips manual runs",
                        arm=record.arm, run_id=record.run_id, manual_run_id=shadow.run_id,
                    )
                    arm_results.append(entry)
                    trail.append(entry)
                    continue
                reason = non_candidate_reason(record)
                if reason is not None:
                    entry = _entry("excluded", reason, f"not an execution candidate: {reason}",
                                   arm=record.arm, run_id=record.run_id, decision=record.decision)
                    arm_results.append(entry)
                    trail.append(entry)
                    continue
                rows = [_order_entry(row, record) for row in rows_by_run.get(record.run_id, [])]
                rows = [row for row in rows if row is not None]
                trail.extend(rows)
                arm_results.append(_arm_result(rows) or _entry(
                    "info", None, "candidate without an order row", UNKNOWN,
                    arm=record.arm, run_id=record.run_id,
                ))
            for record in self.superseded:
                if record.ticker == ticker:
                    trail.append(
                        _entry("info", "superseded",
                               "a later attempt of this ticker/arm is the slot's decision",
                               arm=record.arm, run_id=record.run_id)
                    )
            best = dict(_strongest(arm_results))
            best["events"] = [_compact(entry) for entry in trail]
            out[ticker] = best

    # -- assembly ----------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        recorded = current_outcomes(self.events)
        nomination_failed = None
        for (stage, ticker), result in recorded.items():
            if ticker is None:
                if stage == "nominate" and result["code"] == "nomination-failed":
                    nomination_failed = result["reason"] or "nomination failed"
                continue
            if stage == "execute" and (ticker, "brief") in self.shadowed and self.execute_ran:
                continue  # the recorded 'superseded' would hide the manual shadow
            self.stages[stage][ticker] = _recorded_entry(result)
        if nomination_failed is None and self.pool is not None and self.pool.carried_forward:
            nomination_failed = "nomination failed (reason not recorded)"

        tickers = sorted({t for stage in STAGES for t in self.stages[stage]})
        layers = self._layer_map()
        entries = [self._ticker(ticker, layers.get(ticker)) for ticker in tickers]
        counts = {stage: len(self.stages[stage]) for stage in STAGES}
        finals = Counter(entry["final"] for entry in entries)
        return {
            "schema": SNAPSHOT_SCHEMA,
            "session": self.session,
            "date": self.day,
            "context": {
                "macro_verdict": self.macro_verdict,
                "pool": {
                    "as_of_date": self.pool_used.as_of_date.isoformat() if self.pool_used else None,
                    "carried_forward": (
                        self.pool_used.carried_forward if self.pool_used else None
                    ),
                    "error": self.pool_error,
                    "core_fallback": self.core_fallback is not None,
                    "core_error": self.core_error,
                },
                "nomination_failed": nomination_failed,
                "thresholds": self.thresholds,
                "execute_ran": self.execute_ran,
                "events": len(self.events),
                "inputs": dict(sorted(self.inputs.hashes.items())),
            },
            "counts": {**counts, "final": {f: finals.get(f, 0) for f in FINALS}},
            "tickers": entries,
        }

    def _layer_map(self) -> dict[str, str]:
        layers: dict[str, str] = {}
        if self.pool_used is not None:  # the same-day pool, or a warn-stale one
            for name in ("core", "opportunity", "watch", "removed"):
                for entry in getattr(self.pool_used, name):
                    layers.setdefault(entry.ticker, name)
        for ticker in self.core_fallback or ():
            layers.setdefault(ticker, "core")
        if self.pool_stale is not None:
            for entry in self.pool_stale.opportunity:
                layers.setdefault(entry.ticker, "opportunity")
        for ticker in self.stages["nominate"]:
            layers.setdefault(ticker, "nominee")
        return layers

    def _ticker(self, ticker: str, layer: str | None) -> dict[str, Any]:
        trail = {stage: self.stages[stage][ticker] for stage in STAGES if ticker in self.stages[stage]}
        reached = [stage for stage in STAGES if stage in trail]
        # The stop is the first exclusion after the last stage the ticker
        # passed: an earlier exclusion it went on past (a core collect
        # failure, a feeds-only HOLD …) did not stop it.
        passed = [i for i, s in enumerate(reached) if trail[s]["outcome"] == "passed"]
        after = reached[passed[-1] + 1:] if passed else reached
        stop = next(
            ({"stage": s, "code": trail[s]["code"], "reason": trail[s]["reason"],
              "provenance": trail[s]["provenance"]}
             for s in after if trail[s]["outcome"] == "excluded"),
            None,
        )
        execute = trail.get("execute")
        if execute is not None and execute["outcome"] == "passed":
            final = "order-dry" if execute["details"].get("dry_run") else "order-live"
        elif stop is not None:
            final = "excluded"
        elif (
            self.session == "us"
            and trail.get("decide", {}).get("outcome") == "passed"
            and "execute" not in trail
            and not self.execute_ran
        ):
            final = "pending"
        else:
            final = "unknown"
        provenance = sorted({entry["provenance"] for entry in trail.values()})
        return {
            "ticker": ticker,
            "layer": layer,
            "furthest_stage": reached[-1] if reached else None,
            "final": final,
            "stop": stop,
            "provenance": provenance,
            "stages": trail,
        }


def _instant(value: object) -> datetime | None:
    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return moment if moment.tzinfo is not None else None


def _compact(entry: Entry) -> dict[str, Any]:
    return {k: entry[k] for k in ("outcome", "code", "reason", "details")}


def _arm_result(rows: Sequence[Entry]) -> Entry | None:
    """One arm's execute result from its order rows (append order): a live
    submission stays at the broker whatever came later; otherwise the latest
    deciding row (``info`` rows — cancels, dedupe — only when nothing else)."""
    live = [r for r in rows if r["code"] == "submitted" and not r["details"].get("dry_run")]
    if live:
        return live[-1]
    deciding = [r for r in rows if r["outcome"] != "info"]
    if deciding:
        return deciding[-1]
    return rows[-1] if rows else None


def _order_entry(row: dict, record: DecisionRecord) -> Entry | None:
    """An orders.jsonl row as an execute-stage entry (the adapter's own
    mapping, specs/decision-funnel.md R1 execute row)."""
    kind = row.get("kind")
    details = {
        "arm": record.arm,
        "run_id": record.run_id,
        "client_order_id": row.get("client_order_id"),
        "dry_run": row.get("dry_run"),
        "order_kind": row.get("order_kind"),
        "qty": row.get("qty"),
        "limit_price": row.get("limit_price"),
        "broker_status": row.get("broker_status"),
    }
    if kind == "skip":
        reason = str(row.get("reason") or "skip")
        code = reason_code(reason)
        return _entry("info" if code == "dedupe" else "excluded", code, reason, **details)
    if kind == "submitted" and row.get("broker_status") == "rejected":
        return _entry("excluded", "rejected", "broker rejected the order", **details)
    if kind == "submitted":
        mode = "dry-run" if row.get("dry_run") else "live"
        return _entry("passed", "submitted", f"submitted ({mode})", **details)
    if kind == "cancel":
        return _entry("info", "cancel", "open order canceled", **details)
    return None  # intent / refresh rows carry no funnel decision


def _recorded_entry(result: dict) -> Entry:
    best = next(
        (e for e in result["events"]
         if e.get("outcome") == result["outcome"] and e.get("code") == result["code"]),
        result["events"][0] if result["events"] else {},
    )
    return {
        "outcome": result["outcome"],
        "code": result["code"],
        "reason": result["reason"],
        "provenance": RECORDED,
        "details": best.get("details") or {},
        "events": [
            {
                "outcome": e.get("outcome"),
                "code": e.get("code"),
                "reason": e.get("reason"),
                "details": e.get("details") or {},
            }
            for e in result["events"]
        ],
        "notes": len(result["notes"]),
    }


def build_snapshot(config: PipelineConfig, session: str, slot_date: date) -> dict[str, Any]:
    slot = _Slot(config, session, slot_date)
    slot.load()
    slot.reconstruct()
    return slot.snapshot()


def render_snapshot(snapshot: dict[str, Any]) -> str:
    return json.dumps(snapshot, ensure_ascii=False, sort_keys=True, indent=1, default=str) + "\n"


def write_snapshot(config: PipelineConfig, session: str, slot_date: date) -> Path:
    """Build, validate (strict writer side of the contract) and atomically
    write one slot's snapshot; returns its path."""
    snapshot = build_snapshot(config, session, slot_date)
    FunnelSnapshot.model_validate(snapshot)
    return atomic_write(snapshot_path(config.state_dir, session, slot_date), render_snapshot(snapshot))


def slot_has_inputs(config: PipelineConfig, session: str, slot_date: date) -> bool:
    """A slot ran on ``slot_date``: its pool file, an event log or a
    decision row exists."""
    day = slot_date.isoformat()
    if (Path(config.pool_dir) / session / f"{day}.json").is_file():
        return True
    if events_path(config.state_dir, session, day).is_file():
        return True
    return any(
        r.date == slot_date and r.session == session
        for r in load_decision_records(config.ledger_dir)
    )


def backfill(
    config: PipelineConfig,
    since: date,
    until: date | None = None,
    sessions: Sequence[str] = SESSIONS,
) -> tuple[list[Path], list[str]]:
    """Rebuild every slot in ``[since, until]`` that has inputs; one slot's
    failure is reported and never stops the others."""
    written: list[Path] = []
    failures: list[str] = []
    for session in sorted(sessions):
        last = until or session_date(session)
        day = since
        while day <= last:
            if slot_has_inputs(config, session, day):
                try:
                    written.append(write_snapshot(config, session, day))
                except Exception as exc:
                    failures.append(f"{session} {day.isoformat()}: {' '.join(str(exc).split())}")
            day += timedelta(days=1)
    return written, failures


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"invalid date {value!r} — expected YYYY-MM-DD") from None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m pipeline.funnel",
        description="Decision-funnel snapshots (specs/decision-funnel.md R3).",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    build = sub.add_parser("build", help="build one slot's snapshot")
    build.add_argument("--session", required=True, choices=SESSIONS)
    build.add_argument("--date", type=_parse_date, default=None, metavar="YYYY-MM-DD",
                       help="slot date (default: today in the session timezone)")
    fill = sub.add_parser("backfill", help="rebuild every slot since a date from its files")
    fill.add_argument("--since", type=_parse_date, required=True, metavar="YYYY-MM-DD")
    fill.add_argument("--until", type=_parse_date, default=None, metavar="YYYY-MM-DD")
    fill.add_argument("--session", choices=SESSIONS, default=None)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    config = load_config()
    if args.command == "build":
        slot_date = args.date or session_date(args.session)
        path = write_snapshot(config, args.session, slot_date)
        print(f"written: {path}")
        return 0
    sessions = (args.session,) if args.session else SESSIONS
    written, failures = backfill(config, args.since, args.until, sessions)
    for path in written:
        print(f"written: {path}")
    for failure in failures:
        print(f"failed: {failure}", file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
