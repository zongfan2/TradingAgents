"""Analysis runner — slot step 6: triggers, A/B pairing, TradePlans, ledger.

Spec: ``specs/analysis-runner.md`` (R1–R5, AC1–3); ledger shape:
``specs/trade-plan-ledger-contract.md`` (writer); pool input:
``specs/pool-data-contract.md`` (reader); eval gating over the revision-bound
verdict seam (``tradingagents.dataflows.brief_evals``).

One invocation decides which tickers get a full multi-agent analysis this
slot (core = every pool member; opportunity = day's ticker brief with
``catalyst_score ≥ analysis_trigger_threshold`` and a non-``fail`` eval;
manual = ``--ticker``), applies the eval-gating state table, pairs runs into
arm bundles per the A/B protocol (stateless core rotation + every
catalyst-triggered ticker), runs each analysis in a ``pipeline.run_one``
subprocess (env overrides apply at ``tradingagents`` import time — arm
switching is impossible in-process), validates the trader's TradePlan with the
ledger contract's deterministic validator, and appends one
``decisions.jsonl`` record per run immediately after it completes.

stdout carries one JSON line per completed run (R4) and ends with one JSON
slot-summary line the orchestrator maps to component status; everything else
goes to stderr. Exit codes: ``0`` — slot ran (per-run failures become
``decision: "ERROR"`` rows, R3); ``1`` hard failure; ``2`` usage; ``130``
interrupted.

Config surface: ``pipeline/config.py`` is frozen this change set, so the
spec-named keys are resolved privately from env with the spec defaults —
``analysis_trigger_threshold`` / ``ab_pairing`` / ``ab_core_pairs_per_slot`` /
``max_runs_per_slot`` / ``execute_on_missing_eval`` (plus the runner-private
per-run timeout and preset name). All are flagged as hoist candidates for the
shared config.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import logging
import math
import os
import re
import signal
import subprocess
import sys
import threading
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from pydantic import ValidationError

from pipeline.common import (
    LOG_FIELD_SEPARATOR,
    SESSIONS,
    atomic_write,
    locked_append,
    session_date,
    session_for_ticker,
    sha256_file,
    sha256_text,
    to_utc_iso,
)
from pipeline.config import PipelineConfig, load_config
from pipeline.contracts.base import ContractError
from pipeline.contracts.briefs import split_frontmatter
from pipeline.contracts.ledger import (
    _ID_TICKER,  # frozen contract module: charset not public yet (hoist candidate)
    Arm,
    DecisionRecord,
    GateVerdict,
    PositionsSnapshot,
    TradePlan,
    Trigger,
    validate_trade_plan,
)
from pipeline.contracts.pool import PoolReadResult, read_pool
from pipeline.funnel_events import FunnelEvents, best_effort
from pipeline.llm_routing import (
    DEEPSEEK,
    DEFAULT_ANTHROPIC_MONTHLY_BUDGET_USD,
    ROUTING_CHOICES,
    Profile,
    RoutingDecision,
    choose_profile,
)
from pipeline.llm_usage import (
    append_usage,
    is_credit_exhausted_error,
    mark_exhausted,
    month_spend,
)
from pipeline.run_one import REPAIR_PROMPT, RESULT_SENTINEL, USAGE_SENTINEL

logger = logging.getLogger("pipeline.analysis_runner")

#: Full-match form of the ledger contract's id-ticker charset: run_id/pair_id
#: minting embeds the ticker verbatim, so an id-illegal symbol must be caught
#: at planning time (before any LLM spend), never at ledger-write time.
TICKER_RE = re.compile(rf"^{_ID_TICKER}$")

DECISIONS_NAME = "decisions.jsonl"
POSITIONS_NAME = "positions.json"
CONFIG_SNAPSHOT_DIR = "config_snapshots"
RUNNER_LOG_NAME = "analysis_runner.log"

# ---------------------------------------------------------------------------
# Settings (env-resolved; config.py hoist candidates — frozen this change set)
# ---------------------------------------------------------------------------

TRIGGER_THRESHOLD_ENV = "TRADINGAGENTS_TRIGGER_THRESHOLD"
ANALYSIS_JOB_CONCURRENCY_ENV = "TRADINGAGENTS_ANALYSIS_JOB_CONCURRENCY"
AB_PAIRING_ENV = "TRADINGAGENTS_AB_PAIRING"
AB_CORE_PAIRS_ENV = "TRADINGAGENTS_AB_CORE_PAIRS_PER_SLOT"
MAX_RUNS_ENV = "TRADINGAGENTS_MAX_RUNS_PER_SLOT"
EXECUTE_ON_MISSING_EVAL_ENV = "TRADINGAGENTS_EXECUTE_ON_MISSING_EVAL"
RUN_TIMEOUT_ENV = "TRADINGAGENTS_ANALYSIS_RUN_TIMEOUT"
PRESET_ENV = "TRADINGAGENTS_ANALYSIS_PRESET"
#: specs/llm-cost.md R2 — unset keeps the legacy single-provider behavior
#: (the analysis LLM comes from the inherited env, the ledger preset from
#: TRADINGAGENTS_ANALYSIS_PRESET).
LLM_ROUTING_ENV = "TRADINGAGENTS_LLM_ROUTING"
ANTHROPIC_BUDGET_ENV = "TRADINGAGENTS_ANTHROPIC_MONTHLY_BUDGET_USD"

_AB_PAIRING_CHOICES = ("off", "paired")


@dataclass(frozen=True)
class RunnerSettings:
    """Spec-defaulted knobs (design-doc config appendix)."""

    analysis_trigger_threshold: float = 7.0
    ab_pairing: str = "paired"
    ab_core_pairs_per_slot: int = 3
    max_runs_per_slot: int = 30
    execute_on_missing_eval: bool = False
    run_timeout_seconds: float = 1800.0
    preset: str = "default"
    analysis_job_concurrency: int = 2
    llm_routing: str | None = None
    anthropic_monthly_budget_usd: float = DEFAULT_ANTHROPIC_MONTHLY_BUDGET_USD

    def __post_init__(self) -> None:
        if self.analysis_job_concurrency <= 0:
            raise ValueError("analysis_job_concurrency must be greater than zero")
        if self.llm_routing is not None and self.llm_routing not in ROUTING_CHOICES:
            raise ValueError(
                f"llm_routing {self.llm_routing!r} — expected one of {ROUTING_CHOICES}"
            )
        if not self.anthropic_monthly_budget_usd >= 0:
            raise ValueError("anthropic_monthly_budget_usd must be >= 0")
        if self.ab_pairing not in _AB_PAIRING_CHOICES:
            raise ValueError(
                f"ab_pairing {self.ab_pairing!r} — expected one of {_AB_PAIRING_CHOICES}"
            )


def load_settings(
    env: Mapping[str, str] | None = None, preset_override: str | None = None
) -> RunnerSettings:
    """Resolve settings from env with spec defaults (loud on junk values)."""
    if env is None:
        env = os.environ
    defaults = RunnerSettings()

    def raw(key: str) -> str:
        return (env.get(key) or "").strip()

    return RunnerSettings(
        analysis_job_concurrency=int(
            raw(ANALYSIS_JOB_CONCURRENCY_ENV) or defaults.analysis_job_concurrency
        ),
        analysis_trigger_threshold=float(
            raw(TRIGGER_THRESHOLD_ENV) or defaults.analysis_trigger_threshold
        ),
        ab_pairing=(raw(AB_PAIRING_ENV) or defaults.ab_pairing).lower(),
        ab_core_pairs_per_slot=int(raw(AB_CORE_PAIRS_ENV) or defaults.ab_core_pairs_per_slot),
        max_runs_per_slot=int(raw(MAX_RUNS_ENV) or defaults.max_runs_per_slot),
        execute_on_missing_eval=raw(EXECUTE_ON_MISSING_EVAL_ENV).lower()
        in {"1", "true", "yes", "on"},
        run_timeout_seconds=float(raw(RUN_TIMEOUT_ENV) or defaults.run_timeout_seconds),
        preset=preset_override or raw(PRESET_ENV) or defaults.preset,
        llm_routing=raw(LLM_ROUTING_ENV).lower() or None,
        anthropic_monthly_budget_usd=float(
            raw(ANTHROPIC_BUDGET_ENV) or defaults.anthropic_monthly_budget_usd
        ),
    )


# ---------------------------------------------------------------------------
# Brief inspection (eval gating + inputs block share ONE resolution)
# ---------------------------------------------------------------------------


class BriefUnavailable(RuntimeError):
    """The reader's selection serves no brief (absent / beyond staleness)."""


@dataclass(frozen=True)
class Resolvers:
    """The revision-binding seam (specs/pipeline-consumption-v2.md §5).

    ``macro_resolver(date_iso, session)`` / ``ticker_resolver(ticker,
    date_iso)`` return the path of the exact brief the readers would serve
    (raising :class:`BriefUnavailable` otherwise); ``verdict_reader(path)``
    returns the revision-bound eval verdict for that file. Using the same
    resolution for gating AND the ledger ``inputs`` block means the recorded
    hash always describes the revision the gate examined.
    """

    macro_resolver: Callable[[str, str], str]
    ticker_resolver: Callable[[str, str], str]
    verdict_reader: Callable[[str], str]


def default_resolvers(config: PipelineConfig) -> Resolvers:
    """Production seam over ``tradingagents.dataflows`` (lazy heavy import).

    The resolved brief dirs are exported to env *before* the import because
    ``DEFAULT_CONFIG`` applies ``TRADINGAGENTS_*`` overrides at import time
    (the orchestrator's ``component_env`` split-brain guard, applied to our
    own in-process reads).
    """
    os.environ["TRADINGAGENTS_MACRO_BRIEF_DIR"] = str(config.macro_brief_dir)
    os.environ["TRADINGAGENTS_TICKER_BRIEF_DIR"] = str(config.ticker_brief_dir)
    from tradingagents.dataflows.brief_evals import get_eval_verdict
    from tradingagents.dataflows.errors import VendorNotConfiguredError
    from tradingagents.dataflows.macro_brief import resolve_macro_brief_path
    from tradingagents.dataflows.ticker_brief import resolve_ticker_brief_path

    def macro(date_iso: str, session: str) -> str:
        try:
            return resolve_macro_brief_path(date_iso, session)
        except VendorNotConfiguredError as exc:
            raise BriefUnavailable(str(exc)) from exc

    def ticker(symbol: str, date_iso: str) -> str:
        try:
            return resolve_ticker_brief_path(symbol, date_iso)
        except VendorNotConfiguredError as exc:
            raise BriefUnavailable(str(exc)) from exc

    return Resolvers(macro, ticker, get_eval_verdict)


@dataclass(frozen=True)
class BriefInfo:
    """One brief as gated and served: verdict + the exact-revision InputRef.

    ``abs_path`` keeps the resolved on-disk location so the runner can re-hash
    the file after a child returns and flag mid-run revision drift.
    """

    verdict: GateVerdict
    ref: dict | None = None  # {"path", "generated_at", "sha256"} or None
    catalyst_score: float | None = None
    abs_path: str | None = None


def _frontmatter(path: str | Path) -> dict:
    try:
        data, _body, _errors = split_frontmatter(Path(path).read_text(encoding="utf-8"))
    except OSError:
        return {}
    return data if isinstance(data, dict) else {}


def _generated_at_of(path: str | Path, frontmatter: dict) -> str:
    value = frontmatter.get("generated_at")
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return to_utc_iso(value)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return to_utc_iso(datetime.fromtimestamp(Path(path).stat().st_mtime, tz=timezone.utc))


def _input_ref(path: str | Path, rel_path: str, frontmatter: dict) -> dict:
    return {
        "path": rel_path,
        "generated_at": _generated_at_of(path, frontmatter),
        "sha256": sha256_file(path),
    }


def inspect_macro_brief(resolvers: Resolvers, date_iso: str, session: str) -> BriefInfo:
    try:
        path = resolvers.macro_resolver(date_iso, session)
    except BriefUnavailable as exc:
        logger.warning("macro brief unavailable (%s %s): %s", session, date_iso, exc)
        return BriefInfo("missing")
    verdict = resolvers.verdict_reader(path)
    return BriefInfo(
        verdict, _input_ref(path, Path(path).name, _frontmatter(path)), abs_path=str(path)
    )


def inspect_ticker_brief(resolvers: Resolvers, ticker: str, date_iso: str) -> BriefInfo:
    try:
        path = resolvers.ticker_resolver(ticker, date_iso)
    except BriefUnavailable as exc:
        logger.info("ticker brief unavailable (%s %s): %s", ticker, date_iso, exc)
        return BriefInfo("missing")
    verdict = resolvers.verdict_reader(path)
    frontmatter = _frontmatter(path)
    try:
        score = float(frontmatter["catalyst_score"])
    except (KeyError, TypeError, ValueError):
        score = None
    rel = f"{Path(path).parent.name}/{Path(path).name}"
    return BriefInfo(verdict, _input_ref(path, rel, frontmatter), score, str(path))


# ---------------------------------------------------------------------------
# Trigger selection + A/B pairing (spec: Trigger rules / A/B pairing)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RunJob:
    """One ticker's runs this slot: a solo arm or a back-to-back pair."""

    ticker: str
    trigger: Trigger
    arms: tuple[Arm, ...]
    paired: bool
    catalyst_score: float | None
    macro: BriefInfo
    ticker_brief: BriefInfo
    withhold_ticker_brief: bool = False
    flags: tuple[str, ...] = ()


def normalize_tickers(tickers: Sequence[str], source: str) -> list[str]:
    """Uppercase, dedupe, and drop id-illegal symbols — loudly, at planning.

    Pool nominations come from an LLM backend and neither the pool contract
    nor ``core.<session>.yaml`` constrains charset/case, while ``run_id`` /
    ``pair_id`` embed the ticker verbatim under the contract's
    ``[A-Z0-9]``-segment rule. An id-illegal ticker must be rejected here —
    before any child LLM budget is spent — because at ledger-write time it
    would poison the record mint instead (R3: the slot never aborts on one
    ticker).
    """
    kept: list[str] = []
    seen: set[str] = set()
    for raw in tickers:
        symbol = str(raw).strip().upper()
        if not TICKER_RE.match(symbol):
            logger.warning(
                "%s ticker %r is not a valid ledger symbol — dropped before any run",
                source,
                raw,
            )
            continue
        if symbol not in seen:
            seen.add(symbol)
            kept.append(symbol)
    return kept


def core_pair_rotation(core: Sequence[str], k: int, slot_date: date) -> list[str]:
    """Stateless rotating core subset (spec formula, verbatim).

    Sort the session's core tickers, take ``k`` wrapping from offset
    ``(date.toordinal() mod ceil(len/k)) × k`` — every core ticker is paired
    within ``ceil(len/k)`` slots for ANY k/len combination, with no cross-day
    state file.
    """
    tickers = sorted(core)
    length = len(tickers)
    if length == 0 or k <= 0:
        return []
    cycle = math.ceil(length / k)
    offset = (slot_date.toordinal() % cycle) * k
    return [tickers[(offset + i) % length] for i in range(min(k, length))]


def plan_jobs(
    *,
    session: str,
    slot_date: date,
    core_tickers: Sequence[str],
    opportunity_tickers: Sequence[str],
    settings: RunnerSettings,
    macro: BriefInfo,
    inspect_ticker: Callable[[str], BriefInfo],
    untriggered: list[tuple[str, str, str, dict]] | None = None,
) -> list[RunJob]:
    """Trigger rules + eval-gating state table + pairing → the slot's jobs.

    ``untriggered`` (optional) collects ``(ticker, code, reason, details)``
    for every opportunity ticker that does not trigger (decision funnel).

    - core: every member, every slot (a ticker eval ``fail`` withholds the
      contaminated brief, never the coverage).
    - opportunity: brief present, eval non-``fail``, and the *brief's*
      ``catalyst_score ≥ analysis_trigger_threshold``.
    - macro eval ``fail``: every brief-arm run is replaced by a single
      feeds-arm run flagged ``macro-fail`` — when it would have been paired,
      that run *is* the feeds arm, so no second shadow runs and the run is
      recorded unpaired (a pair_id must always own exactly two rows).
    - a ``missing`` macro/ticker eval still runs but is flagged
      (``macro-eval-missing`` / ``ticker-eval-missing``, state-table "runs,
      flagged") so the R4 lines carry the degradation, not only the verdicts.
    """
    macro_failed = macro.verdict == "fail"
    paired_core = (
        set(core_pair_rotation(core_tickers, settings.ab_core_pairs_per_slot, slot_date))
        if settings.ab_pairing == "paired" and not macro_failed
        else set()
    )

    def arms_for(would_pair: bool) -> tuple[tuple[Arm, ...], bool, tuple[str, ...]]:
        if macro_failed:
            return ("feeds",), False, ("macro-fail",)
        if would_pair:
            return ("brief", "feeds"), True, ()
        return ("brief",), False, ()

    def eval_flags(info: BriefInfo) -> tuple[str, ...]:
        if macro_failed:  # the macro-fail flag subsumes the table row
            return ()
        flags: tuple[str, ...] = ()
        if macro.verdict == "missing":
            flags = ("macro-eval-missing",)
        if info.verdict == "missing":
            flags = (*flags, "ticker-eval-missing")
        return flags

    jobs: list[RunJob] = []
    for ticker in core_tickers:
        info = inspect_ticker(ticker)
        arms, paired, flags = arms_for(ticker in paired_core)
        flags = (*flags, *eval_flags(info))
        withhold = info.verdict == "fail" and not macro_failed
        if withhold:
            flags = (*flags, "ticker-eval-fail-withheld")
        jobs.append(
            RunJob(
                ticker=ticker,
                trigger="core",
                arms=arms,
                paired=paired,
                catalyst_score=info.catalyst_score,
                macro=macro,
                ticker_brief=info,
                withhold_ticker_brief=withhold,
                flags=flags,
            )
        )

    catalyst: list[RunJob] = []
    for ticker in opportunity_tickers:
        info = inspect_ticker(ticker)
        threshold = settings.analysis_trigger_threshold
        if info.catalyst_score is None:
            logger.info("%s: no ticker brief catalyst_score — no auto-trigger", ticker)
            if untriggered is not None:
                untriggered.append(
                    (ticker, "no-score", "no ticker brief catalyst_score", {})
                )
            continue
        if info.verdict == "fail":
            logger.warning(
                "%s: catalyst trigger suppressed (skip reason ticker-eval-fail, score %.1f)",
                ticker,
                info.catalyst_score,
            )
            if untriggered is not None:
                untriggered.append(
                    (
                        ticker,
                        "eval-fail",
                        f"ticker eval fail (score {info.catalyst_score:g})",
                        {"score": info.catalyst_score},
                    )
                )
            continue
        if info.catalyst_score < threshold:
            logger.info(
                "%s: brief catalyst_score %.1f < trigger %.1f — no auto-trigger",
                ticker,
                info.catalyst_score,
                threshold,
            )
            if untriggered is not None:
                untriggered.append(
                    (
                        ticker,
                        "below-threshold",
                        f"brief catalyst_score {info.catalyst_score:g} < trigger {threshold:g}",
                        {"score": info.catalyst_score, "threshold": threshold},
                    )
                )
            continue
        would_pair = settings.ab_pairing == "paired" and not macro_failed
        arms, paired, flags = arms_for(would_pair)
        flags = (*flags, *eval_flags(info))
        catalyst.append(
            RunJob(
                ticker=ticker,
                trigger="catalyst",
                arms=arms,
                paired=paired,
                catalyst_score=info.catalyst_score,
                macro=macro,
                ticker_brief=info,
                flags=flags,
            )
        )
    # Highest-conviction catalysts first — also the budget guard's keep order.
    catalyst.sort(key=lambda job: (-(job.catalyst_score or 0.0), job.ticker))
    return jobs + catalyst


def build_manual_job(
    ticker: str, macro: BriefInfo, info: BriefInfo
) -> RunJob:
    """Manual trigger: single run, never paired, never auto-executed (S5)."""
    macro_failed = macro.verdict == "fail"
    withhold = info.verdict == "fail" and not macro_failed
    flags: tuple[str, ...] = ("macro-fail",) if macro_failed else ()
    if not macro_failed and macro.verdict == "missing":
        flags = (*flags, "macro-eval-missing")
    if not macro_failed and info.verdict == "missing":
        flags = (*flags, "ticker-eval-missing")
    if withhold:
        flags = (*flags, "ticker-eval-fail-withheld")
    return RunJob(
        ticker=ticker,
        trigger="manual",
        arms=("feeds",) if macro_failed else ("brief",),
        paired=False,
        catalyst_score=info.catalyst_score,
        macro=macro,
        ticker_brief=info,
        withhold_ticker_brief=withhold,
        flags=flags,
    )


# ---------------------------------------------------------------------------
# Budget guard (R2)
# ---------------------------------------------------------------------------


def enforce_budget(
    jobs: Sequence[RunJob], max_runs: int
) -> tuple[list[RunJob], list[dict]]:
    """R2 drop order, every drop logged (no silent truncation).

    1. unpaired triggered runs, lowest ``catalyst_score`` first;
    2. whole pairs, both arms together — triggered pairs (lowest score first)
       vanish entirely; a core pair is dropped *as a pair* by demoting it to
       the unpaired solo brief run core coverage mandates anyway (dropping the
       baseline would violate "core: every member, every slot", and keeping a
       lone arm labeled as a pair would orphan it);
    3. core solo runs are never dropped — an over-budget core layer runs
       anyway, loudly.
    """
    kept = list(jobs)
    dropped: list[dict] = []

    def total() -> int:
        return sum(len(job.arms) for job in kept)

    def log_drop(job: RunJob, reason: str, runs: int) -> None:
        entry = {
            "ticker": job.ticker,
            "trigger": job.trigger,
            "catalyst_score": job.catalyst_score,
            "runs": runs,
            "reason": reason,
        }
        dropped.append(entry)
        logger.warning(
            "budget drop: %s (%s, score %s) — %s (-%d run(s))",
            job.ticker,
            job.trigger,
            job.catalyst_score,
            reason,
            runs,
        )

    def victims(predicate: Callable[[RunJob], bool]) -> list[RunJob]:
        found = [job for job in kept if predicate(job)]
        found.sort(key=lambda job: (job.catalyst_score or 0.0, job.ticker))
        return found

    while total() > max_runs:
        stage1 = victims(lambda j: j.trigger == "catalyst" and not j.paired)
        if not stage1:
            break
        job = stage1[0]
        kept.remove(job)
        log_drop(job, "unpaired triggered run over budget", len(job.arms))
    while total() > max_runs:
        stage2 = victims(lambda j: j.trigger == "catalyst" and j.paired)
        if not stage2:
            break
        job = stage2[0]
        kept.remove(job)
        log_drop(job, "triggered pair over budget (both arms dropped together)", len(job.arms))
    while total() > max_runs:
        stage3 = [job for job in kept if job.trigger == "core" and job.paired]
        if not stage3:
            break
        job = stage3[-1]  # last of the rotation subset — deterministic
        index = kept.index(job)
        kept[index] = replace(job, arms=("brief",), paired=False)
        log_drop(job, "core pair dropped (demoted to the mandatory solo brief run)", 1)
    if total() > max_runs:
        logger.warning(
            "budget %d still exceeded by %d core solo run(s) — core coverage is never dropped",
            max_runs,
            total() - max_runs,
        )
    return kept, dropped


# ---------------------------------------------------------------------------
# Ledger IO (contract: run_id minted under flock; append-only single lines)
# ---------------------------------------------------------------------------


def _parse_rows(text: str) -> list[dict]:
    rows: list[dict] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            logger.warning("decisions.jsonl: skipping unparseable line")
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def read_decision_rows(ledger_dir: str | Path) -> list[dict]:
    path = Path(ledger_dir) / DECISIONS_NAME
    try:
        return _parse_rows(path.read_text(encoding="utf-8"))
    except OSError:
        return []


def _max_pair_attempt(rows: Sequence[Mapping], prefix: str) -> int:
    best = 0
    for row in rows:
        pair_id = row.get("pair_id")
        if isinstance(pair_id, str) and pair_id.startswith(prefix) and pair_id[len(prefix):].isdigit():
            best = max(best, int(pair_id[len(prefix):]))
    return best


def mint_and_append_decision(
    ledger_dir: str | Path,
    fields: Mapping[str, object],
    *,
    mint_pair_attempt: bool = False,
) -> DecisionRecord:
    """Count-and-append under one exclusive ``flock`` (ledger contract).

    ``fields`` is the full :class:`DecisionRecord` payload minus ``run_id``;
    ``<n>`` = 1 + the count of existing rows with the same (date, session,
    ticker, arm), counted and appended under the same lock so concurrent slot
    and manual runs cannot mint duplicate ids.

    ``mint_pair_attempt`` mints ``pair_id`` = ``a<k>`` (1 + max existing
    attempt for the row's date/session/ticker) under the SAME lock as the
    append. A pair's first row reserves the attempt atomically — its second
    arm reuses the minted id — so two concurrent paired invocations of the
    same ticker can never share an ``a<k>`` (contract: a pair_id always owns
    exactly two rows, one per arm).
    """
    path = Path(ledger_dir) / DECISIONS_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = dict(fields)
    key = (fields["date"], fields["session"], fields["ticker"], fields["arm"])
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            size = os.fstat(fd).st_size
            text = os.pread(fd, size, 0).decode("utf-8", errors="replace") if size else ""
            rows = _parse_rows(text)
            if mint_pair_attempt:
                prefix = f"{fields['date']}-{fields['session']}-{fields['ticker']}-a"
                fields["pair_id"] = f"{prefix}{_max_pair_attempt(rows, prefix) + 1}"
            count = sum(
                1
                for row in rows
                if (row.get("date"), row.get("session"), row.get("ticker"), row.get("arm")) == key
            )
            run_id = "-".join([*map(str, key), str(count + 1)])
            record = DecisionRecord.model_validate({**fields, "run_id": run_id})
            line = json.dumps(record.model_dump(mode="json"), ensure_ascii=False) + "\n"
            os.write(fd, line.encode("utf-8"))
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)
    return record


def completed_coverage(
    rows: Sequence[Mapping], date_iso: str, session: str
) -> tuple[set[str], set[str]]:
    """``(solo_done, paired_done)`` — tickers whose slot coverage completed.

    The no-``--force`` skip must count only *scheduled, completed* work:

    - ``trigger: "manual"`` rows never substitute for scheduled coverage — a
      morning manual run is never auto-executed (adapter S5), so skipping the
      core run on its account would leave a holding with zero
      execution-eligible analysis ("core: every member, every slot");
    - ``decision: "ERROR"`` rows are failures the documented recovery rerun
      (orchestrator R3, no ``--force``) must retry, not proof of coverage;
    - a paired ticker counts only when some attempt has BOTH arms completed —
      a crash between a pair's runs leaves a half attempt that the rerun
      replaces wholesale with ``a<k+1>`` (single arms are never re-run in
      isolation).
    """
    scheduled = [
        row
        for row in rows
        if row.get("date") == date_iso
        and row.get("session") == session
        and row.get("trigger") != "manual"
        and row.get("decision") != "ERROR"
    ]
    solo_done = {
        str(row.get("ticker")) for row in scheduled if not row.get("pair_id")
    }
    pair_arms: dict[tuple[str, str], set[str]] = {}
    for row in scheduled:
        pair_id = row.get("pair_id")
        if isinstance(pair_id, str):
            pair_arms.setdefault((str(row.get("ticker")), pair_id), set()).add(str(row.get("arm")))
    paired_done = {
        ticker for (ticker, _pair), arms in pair_arms.items() if {"brief", "feeds"} <= arms
    }
    return solo_done, paired_done


#: Graph-config keys that differentiate one analysis run from another,
#: resolved through ``DEFAULT_CONFIG``'s env overlay — exactly what a
#: ``pipeline.run_one`` child re-resolves from the env it inherits. The arm
#: axis is deliberately absent: it is recorded per-row in ``arm``.
_GRAPH_CONFIG_KEYS = (
    "llm_provider",
    "deep_think_llm",
    "quick_think_llm",
    "backend_url",
    "temperature",
    "llm_max_retries",
    "max_debate_rounds",
    "max_risk_discuss_rounds",
    "output_language",
    "google_thinking_level",
    "openai_reasoning_effort",
    "anthropic_effort",
    "anthropic_deep_effort",
    "anthropic_quick_effort",
    "deepseek_quick_thinking",
)


def effective_analysis_config(env_overrides: Mapping[str, str] | None = None) -> dict:
    """The analysis-side effective configuration (config-digest input).

    Covers what actually differentiates analysis runs: the graph's resolved
    LLM provider/models/knobs (env-switched — two runs under different
    analysis LLMs must mint different digests) and the trader/repair prompt
    texts the plans come from. The ``tradingagents`` import is lazy on
    purpose: ``DEFAULT_CONFIG`` applies ``TRADINGAGENTS_*`` overrides at
    import time, mirroring what the child subprocess will resolve from the
    same environment.
    """
    from tradingagents.agents.trader.trader import (
        POSITION_MAINTAIN_INSTRUCTIONS,
        TRADE_PLAN_INSTRUCTIONS,
    )
    from tradingagents.default_config import DEFAULT_CONFIG, _apply_env_overrides

    graph_config = DEFAULT_CONFIG
    if env_overrides:
        # The config a child with this profile overlay resolves (llm-cost R2).
        # An empty override CLEARS an inherited knob (the child then sees the
        # shipped default, None for every key a profile clears).
        from tradingagents.default_config import _ENV_OVERRIDES

        graph_config = _apply_env_overrides(dict(DEFAULT_CONFIG), {**os.environ, **env_overrides})
        for env_var, value in env_overrides.items():
            if value == "" and env_var in _ENV_OVERRIDES:
                graph_config[_ENV_OVERRIDES[env_var]] = None
    return {
        "graph": {key: graph_config.get(key) for key in _GRAPH_CONFIG_KEYS},
        "prompt_templates": {
            "trader.trade_plan_instructions": sha256_text(TRADE_PLAN_INSTRUCTIONS),
            "trader.position_maintain_instructions": sha256_text(POSITION_MAINTAIN_INSTRUCTIONS),
            "run_one.repair_prompt": sha256_text(REPAIR_PROMPT),
        },
    }


def write_config_digest(
    ledger_dir: str | Path,
    settings: RunnerSettings,
    analysis_config: Mapping[str, object] | None = None,
) -> str:
    """Content-addressed effective-config snapshot (ledger contract).

    The digest covers the resolved runner settings PLUS the analysis-side
    configuration that produced the plans — the graph's resolved LLM
    backend/models and the trader-prompt text hashes
    (:func:`effective_analysis_config`) — so runs under a different analysis
    LLM (env-switched) or an edited trader prompt can never silently share a
    digest. The first time a digest appears the snapshot is written to
    ``ledger/config_snapshots/<digest>.json`` so every recorded digest
    resolves to the full configuration that produced the run.
    """
    snapshot = {
        "preset": settings.preset,
        "analysis_job_concurrency": settings.analysis_job_concurrency,
        "analysis_trigger_threshold": settings.analysis_trigger_threshold,
        "ab_pairing": settings.ab_pairing,
        "ab_core_pairs_per_slot": settings.ab_core_pairs_per_slot,
        "max_runs_per_slot": settings.max_runs_per_slot,
        "execute_on_missing_eval": settings.execute_on_missing_eval,
        "run_timeout_seconds": settings.run_timeout_seconds,
        **(dict(analysis_config) if analysis_config is not None else effective_analysis_config()),
    }
    digest = sha256_text(json.dumps(snapshot, sort_keys=True, ensure_ascii=False))
    target = Path(ledger_dir) / CONFIG_SNAPSHOT_DIR / f"{digest}.json"
    if not target.exists():
        atomic_write(target, json.dumps(snapshot, indent=2, sort_keys=True, ensure_ascii=False) + "\n")
    return digest


# ---------------------------------------------------------------------------
# positions.json → trader position context
# ---------------------------------------------------------------------------


def load_positions(ledger_dir: str | Path) -> PositionsSnapshot | None:
    path = Path(ledger_dir) / POSITIONS_NAME
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return PositionsSnapshot.parse_lenient(payload)
    except FileNotFoundError:
        return None
    except (OSError, ValueError, ValidationError) as exc:
        logger.warning("positions.json unreadable (%s) — no position context this slot", exc)
        return None


def _weekdays_between(start: date, end: date) -> int:
    """Weekdays strictly between two dates (both exclusive)."""
    return sum(
        1
        for offset in range(1, max(0, (end - start).days))
        if (start + timedelta(days=offset)).weekday() < 5
    )


def position_snapshot_stale(
    as_of: datetime, slot_date: date, next_session: date | None = None
) -> bool:
    """Contract: ``as_of`` older than 1 trading day ⇒ stale.

    A Friday refresh is still the most recent trading day's state on Monday
    (only weekend days in between); a whole trading session between ``as_of``
    and the slot means that session's refresh was missed.

    ``next_session`` is the snapshot's broker-calendar session after
    ``as_of`` (ledger contract): stale iff that session is earlier than the
    slot. Without it, sessions are approximated as weekdays — a holiday then
    reads as a missed refresh, which degrades to "position data unavailable",
    never to stale numbers.
    """
    as_of_day = as_of.astimezone(timezone.utc).date()
    if as_of_day >= slot_date:
        return False
    if next_session is not None:
        return next_session < slot_date
    return _weekdays_between(as_of_day, slot_date) >= 1


def position_context_for(
    snapshot: PositionsSnapshot | None, ticker: str, slot_date: date
) -> str | None:
    """Rendered trader position block for a held ticker; ``None`` when flat."""
    if snapshot is None:
        return None
    entry = next(
        (p for p in snapshot.positions if p.ticker.upper() == ticker.upper() and p.qty),
        None,
    )
    if entry is None:
        return None
    if position_snapshot_stale(snapshot.as_of, slot_date, snapshot.next_session):
        return (
            "position data unavailable (positions snapshot as_of "
            f"{to_utc_iso(snapshot.as_of)} is older than 1 trading day)"
        )
    stops = [leg for leg in entry.open_orders if leg.leg.lower() == "stop"]
    stop_text = ", ".join(f"{leg.price:g} ({leg.client_order_id})" for leg in stops) or "none"
    return "\n".join(
        [
            f"- qty: {entry.qty:g}",
            f"- avg_entry: {entry.avg_entry:g}",
            f"- unrealized_pl: {entry.unrealized_pl:g}",
            f"- tranches: {entry.tranches}",
            f"- protective stops: {stop_text}",
        ]
    )


# ---------------------------------------------------------------------------
# Child subprocess seam (tests fake this; the child itself is pipeline.run_one)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ChildSpec:
    ticker: str
    date: str
    session: str
    arm: Arm
    withhold_ticker_brief: bool = False
    position_context: str | None = None
    #: The routed profile's env overlay (llm-cost R2); empty = inherited env.
    env: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class ChildResult:
    returncode: int
    payload: dict | None = None
    error: str | None = None
    #: Per-model token usage (completed runs: from the payload; failed runs:
    #: the child's usage line), or None when nothing was reported.
    usage: dict | None = None


ChildRunner = Callable[[ChildSpec, float], ChildResult]

#: Data-dir env the child must resolve exactly as this runner did
#: (orchestrator ``component_env`` symmetry).
_CHILD_ENV_KEYS = {
    "TRADINGAGENTS_STATE_DIR": "state_dir",
    "TRADINGAGENTS_MACRO_BRIEF_DIR": "macro_brief_dir",
    "TRADINGAGENTS_TICKER_BRIEF_DIR": "ticker_brief_dir",
    "TRADINGAGENTS_POOL_DIR": "pool_dir",
    "TRADINGAGENTS_LEDGER_DIR": "ledger_dir",
}


def _last_nonempty_line(text: str) -> str:
    for line in reversed(text.splitlines()):
        if line.strip():
            return line.strip()
    return ""


def _extract_result_payload(stdout: str) -> dict | None:
    """Parse the child's sentinel-tagged result line (see ``pipeline.run_one``).

    Scans line-wise from the end for :data:`RESULT_SENTINEL` and tolerates
    junk *before* the tag on the same line (an fd-level stdout write lacking a
    trailing newline concatenates with the result print — ``redirect_stdout``
    cannot intercept C-level writes) as well as stray bytes after the JSON
    object (``raw_decode``). A completed, fully billed run must never be
    discarded as ERROR over stray output.
    """
    for line in reversed(stdout.splitlines()):
        index = line.rfind(RESULT_SENTINEL)
        if index < 0:
            continue
        candidate = line[index + len(RESULT_SENTINEL):].lstrip()
        try:
            payload, _end = json.JSONDecoder().raw_decode(candidate)
        except json.JSONDecodeError:
            return None
        return payload if isinstance(payload, dict) else None
    return None


def _extract_usage(stdout: str) -> dict | None:
    """A failed child's usage line (``pipeline.run_one.USAGE_SENTINEL``)."""
    for line in reversed(stdout.splitlines()):
        index = line.rfind(USAGE_SENTINEL)
        if index < 0:
            continue
        try:
            usage, _end = json.JSONDecoder().raw_decode(line[index + len(USAGE_SENTINEL):].lstrip())
        except json.JSONDecodeError:
            return None
        return usage if isinstance(usage, dict) else None
    return None


class _LlmRouter:
    """Per-job profile choice + per-profile config digests (llm-cost R2).

    Thread-safe: jobs run concurrently; each job asks for a profile right
    before its first child starts (both arms of a pair share it), so a run
    never mixes providers and the budget is re-read for every job."""

    def __init__(
        self,
        config: PipelineConfig,
        settings: RunnerSettings,
        clock: Callable[[], datetime],
    ) -> None:
        self.config = config
        self.settings = settings
        self.clock = clock
        self._lock = threading.Lock()
        self._digests: dict[str, str] = {}
        self.last_decision: RoutingDecision | None = None

    def choose(self) -> RoutingDecision:
        decision = choose_profile(
            self.config.state_dir,
            self.settings.llm_routing,
            self.settings.anthropic_monthly_budget_usd,
            self.clock(),
        )
        with self._lock:
            self.last_decision = decision
        return decision

    def digest(self, profile: Profile) -> str:
        with self._lock:
            cached = self._digests.get(profile.name)
            if cached is None:
                cached = write_config_digest(
                    self.config.ledger_dir,
                    replace(self.settings, preset=profile.name),
                    effective_analysis_config(profile.env),
                )
                self._digests[profile.name] = cached
            return cached

    def month_spend(self) -> float:
        return month_spend(self.config.state_dir, self.clock().strftime("%Y-%m"), "anthropic")


def make_child_runner(config: PipelineConfig) -> ChildRunner:
    """Default child boundary: one ``pipeline.run_one`` subprocess per run.

    The child starts in its own session so a timeout kills the whole process
    group (the orchestrator's ``make_subprocess_runner`` pattern) — killing
    only the direct child would orphan any backend CLI grandchild a future
    graph tool spawns, leaving it burning quota.
    """
    base_env = {key: str(getattr(config, attr)) for key, attr in _CHILD_ENV_KEYS.items()}

    def run(spec: ChildSpec, timeout: float) -> ChildResult:
        argv = [
            str(config.python_executable),
            "-m",
            "pipeline.run_one",
            "--ticker",
            spec.ticker,
            "--date",
            spec.date,
            "--session",
            spec.session,
            "--arm",
            spec.arm,
        ]
        if spec.withhold_ticker_brief:
            argv.append("--withhold-ticker-brief")
        if spec.position_context:
            argv.extend(["--position-context", spec.position_context])
        env = {**os.environ, **base_env, **dict(spec.env)}
        try:
            proc = subprocess.Popen(
                argv,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=env,
                start_new_session=True,
            )
        except OSError as exc:
            return ChildResult(-1, None, f"child spawn failed: {exc}")
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(proc.pid, signal.SIGKILL)
            stdout, _stderr = proc.communicate()  # reap + drain after the group kill
            # The child streams its running usage after every LLM call, so a
            # killed run still reports what it spent (llm-cost R1).
            return ChildResult(
                -1, None, f"run timed out after {int(timeout)}s", _extract_usage(stdout)
            )
        if proc.returncode != 0:
            reason = _last_nonempty_line(stderr) or f"exit {proc.returncode}"
            return ChildResult(proc.returncode, None, reason[:300], _extract_usage(stdout))
        payload = _extract_result_payload(stdout)
        if payload is None:
            return ChildResult(
                0, None, "child exited 0 without a parseable result line", _extract_usage(stdout)
            )
        usage = payload.get("usage")
        return ChildResult(0, payload, usage=usage if isinstance(usage, dict) else None)

    return run


# ---------------------------------------------------------------------------
# OHLCV reference data for the deterministic validator (injectable)
# ---------------------------------------------------------------------------

#: ``(ticker, analysis_date) -> (last_close, atr14)`` — either may be ``None``
#: when unavailable; the validator then reports BUY reference data unusable.
OhlcvFetcher = Callable[[str, date], "tuple[float | None, float | None]"]


def default_ohlcv_fetcher(ticker: str, on_date: date) -> tuple[float | None, float | None]:
    """Daily last close ≤ date and ATR14 from the same yfinance series the
    market analyst was served: symbol normalized identically and yfinance's
    default dividend adjustment kept (``get_YFin_data_online`` uses
    ``ticker.history`` defaults and ``stockstats_utils`` passes
    ``auto_adjust=True`` — an unadjusted series here would shift the ATR14 /
    last-close anchors around ex-dividend dates)."""
    try:
        import pandas as pd
        import yfinance as yf

        from tradingagents.dataflows.symbol_utils import normalize_symbol

        history = yf.Ticker(normalize_symbol(ticker)).history(
            start=(on_date - timedelta(days=140)).isoformat(),
            end=(on_date + timedelta(days=1)).isoformat(),
            interval="1d",
        )
        if history is None or history.empty:
            return None, None
        history = history[[d.date() <= on_date for d in history.index]]
        if history.empty:
            return None, None
        closes = history["Close"]
        last_close = float(closes.iloc[-1])
        prev_close = closes.shift(1)
        true_range = pd.concat(
            [
                history["High"] - history["Low"],
                (history["High"] - prev_close).abs(),
                (history["Low"] - prev_close).abs(),
            ],
            axis=1,
        ).max(axis=1)
        window = true_range.dropna().iloc[-14:]
        atr14 = float(window.mean()) if len(window) else None
        return last_close, atr14
    except Exception as exc:
        logger.warning("OHLCV reference fetch failed for %s: %s", ticker, exc)
        return None, None


# ---------------------------------------------------------------------------
# Pool input
# ---------------------------------------------------------------------------


def load_pool_inputs(
    config: PipelineConfig, session: str, slot_date: date
) -> tuple[dict | None, list[str], list[str]]:
    """The day's trigger source: ``(pool InputRef | None, core, opportunity)``.

    Pool contract reading rule: an absent/over-stale pool empties the
    opportunity layer and core coverage falls back to ``core.<session>.yaml``
    (never consumed ⇒ the ledger ``pool`` input is null).
    """
    try:
        result = read_pool(config.pool_dir, session, slot_date, config.pool_max_staleness_days)
    except ContractError as exc:
        logger.warning("pool unreadable — treating as absent: %s", exc)
        result = PoolReadResult(None, None, None, None, "absent")
    if result.staleness == "absent" or result.pool is None or result.path is None:
        from pipeline.ticker_collector import CollectorError, read_core_yaml

        try:
            core = read_core_yaml(config.pool_dir, session)
        except CollectorError as exc:
            logger.warning("core.%s.yaml unreadable: %s", session, exc)
            core = []
        return None, core, []
    if result.staleness == "warn":
        logger.warning(
            "STALE POOL: serving %s for %s (%d day(s) old)",
            result.path.name,
            slot_date.isoformat(),
            result.gap_days or 0,
        )
    pool_ref = {
        "path": f"{session}/{result.path.name}",
        "generated_at": to_utc_iso(result.pool.generated_at),
        "sha256": sha256_file(result.path),
    }
    core = [entry.ticker for entry in result.pool.core]
    opportunity = [entry.ticker for entry in result.pool.opportunity]
    return pool_ref, core, opportunity


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------


def execution_eligible(
    arm: Arm, trigger: Trigger, macro_verdict: str, ticker_verdict: str, settings: RunnerSettings
) -> bool:
    """Auto-execution column of the gating state table (feeds never executes;
    manual runs are never auto-executed — adapter S5)."""
    if arm != "brief" or trigger == "manual":
        return False
    verdicts = (macro_verdict, ticker_verdict)
    if "fail" in verdicts:
        return False
    if "missing" in verdicts:
        return settings.execute_on_missing_eval
    return True


@dataclass
class SlotOutcome:
    summary: dict
    records: list[DecisionRecord] = field(default_factory=list)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _revision_drift_flags(macro: BriefInfo, ticker_brief: BriefInfo | None) -> list[str]:
    """Post-run re-hash of the served brief revisions (consumption audit).

    A mismatch means the file changed on disk *while the child ran* — the
    recorded spawn-time sha256 then describes the revision resolved at spawn,
    not necessarily the bytes the run's tool call read (archive-on-overwrite
    preserves both revisions, so either resolves). Flagged and logged, never
    fatal.
    """
    flags: list[str] = []
    for label, info in (("macro", macro), ("ticker", ticker_brief)):
        if info is None or info.ref is None or info.abs_path is None:
            continue
        try:
            current = sha256_file(info.abs_path)
        except OSError:
            current = None
        if current != info.ref["sha256"]:
            flags.append(f"{label}-brief-revision-drift")
            logger.warning(
                "%s brief %s changed on disk while the run executed — the recorded "
                "sha256 is the revision resolved at spawn (both revisions are "
                "preserved by archive-on-overwrite)",
                label,
                info.abs_path,
            )
    return flags


@dataclass
class JobOutcome:
    """The ledger records and R4 lines produced by one complete ticker job."""

    records: list[DecisionRecord] = field(default_factory=list)
    run_lines: list[dict] = field(default_factory=list)


def _error_run_line(
    job: RunJob,
    arm: Arm,
    error: str,
    *,
    record: DecisionRecord | None = None,
    pair_id: str | None = None,
) -> dict:
    """Build truthful R4 progress for an arm that could not finish cleanly."""
    return {
        "run_id": record.run_id if record else None,
        "pair_id": record.pair_id if record else pair_id,
        "ticker": job.ticker,
        "arm": arm,
        "trigger": job.trigger,
        "decision": record.decision if record else "ERROR",
        "plan_valid": record.plan_valid if record else False,
        "violations": None,
        "repair_used": False,
        "flags": list(job.flags) or None,
        "execute_eligible": False,
        "error": error,
    }


def _complete_failed_outcome(job: RunJob, outcome: JobOutcome, error: str) -> JobOutcome:
    """Preserve committed progress and mark every unreported arm as failed."""
    reported = {line["arm"] for line in outcome.run_lines}
    records = {record.arm: record for record in outcome.records}
    pair_id = next((record.pair_id for record in outcome.records if record.pair_id), None)
    for arm in job.arms:
        if arm not in reported:
            outcome.run_lines.append(
                _error_run_line(
                    job,
                    arm,
                    error,
                    record=records.get(arm),
                    pair_id=pair_id,
                )
            )
    return outcome


def _run_job(
    job: RunJob,
    *,
    session: str,
    slot_date: date,
    date_iso: str,
    config: PipelineConfig,
    settings: RunnerSettings,
    resolvers: Resolvers,
    child_runner: ChildRunner,
    ohlcv_fetcher: OhlcvFetcher,
    clock: Callable[[], datetime],
    pool_ref: dict | None,
    digest: str,
    positions: PositionsSnapshot | None,
    log_path: Path,
    router: _LlmRouter | None = None,
) -> JobOutcome:
    """Run one complete ticker job without letting one worker abort the slot."""
    outcome = JobOutcome()
    try:
        return _run_job_impl(
            job,
            session=session,
            slot_date=slot_date,
            date_iso=date_iso,
            config=config,
            settings=settings,
            resolvers=resolvers,
            child_runner=child_runner,
            ohlcv_fetcher=ohlcv_fetcher,
            clock=clock,
            pool_ref=pool_ref,
            digest=digest,
            positions=positions,
            log_path=log_path,
            outcome=outcome,
            router=router,
        )
    except Exception as exc:
        error = f"job worker raised: {_one_line(exc)}"
        logger.exception("%s: %s", job.ticker, error)
        return _complete_failed_outcome(job, outcome, error)


def _run_job_impl(
    job: RunJob,
    *,
    session: str,
    slot_date: date,
    date_iso: str,
    config: PipelineConfig,
    settings: RunnerSettings,
    resolvers: Resolvers,
    child_runner: ChildRunner,
    ohlcv_fetcher: OhlcvFetcher,
    clock: Callable[[], datetime],
    pool_ref: dict | None,
    digest: str,
    positions: PositionsSnapshot | None,
    log_path: Path,
    outcome: JobOutcome,
    router: _LlmRouter | None = None,
) -> JobOutcome:
    """Run one ticker's arms sequentially and append each row immediately."""
    records = outcome.records
    run_lines = outcome.run_lines
    # Consumption-time resolution (ledger contract: "the hashes describe
    # what the run actually saw"): triggers and arms were decided from the
    # planning-time inspection, but the briefs are re-resolved HERE,
    # immediately before the job's children spawn — a mid-slot re-collection
    # is served and recorded as the new revision instead of keeping a hash
    # minted hours earlier. The pair's two runs execute back-to-back and share
    # this one resolution by design.
    try:
        fresh_macro = inspect_macro_brief(resolvers, date_iso, session)
        fresh_ticker = inspect_ticker_brief(resolvers, job.ticker, date_iso)
    except Exception as exc:
        raise RuntimeError(f"brief revision resolution failed: {_one_line(exc)}") from exc
    # A brief whose eval went to ``fail`` mid-slot is withheld too — the
    # contaminated-input rule follows the fresh verdict, never a stale one.
    withhold = job.withhold_ticker_brief or (
        fresh_ticker.verdict == "fail" and fresh_macro.verdict != "fail"
    )
    job_flags = list(job.flags)
    if withhold and "ticker-eval-fail-withheld" not in job_flags:
        job_flags.append("ticker-eval-fail-withheld")
    context = position_context_for(positions, job.ticker, slot_date)
    pair_id: str | None = None  # minted under flock with the pair's first row
    # llm-cost R2: one profile per JOB, so both arms of a pair run on the same
    # provider (the A/B contrast is the information source, not the model);
    # it only changes when Anthropic reports the credits gone.
    profile: Profile | None = router.choose().profile if router is not None else None
    switched_profile = False  # a credit-exhausted rerun moved this job to DeepSeek
    for arm_index, arm in enumerate(job.arms):
        # Resolved before the child spends anything: a snapshot write failure
        # must fail the run before tokens are billed, not after.
        run_digest = router.digest(profile) if router is not None and profile is not None else digest
        spec = ChildSpec(
            ticker=job.ticker,
            date=date_iso,
            session=session,
            arm=arm,
            withhold_ticker_brief=withhold and arm == "brief",
            position_context=context,
            env=tuple(sorted(profile.env.items())) if profile is not None else (),
        )
        logger.info(
            "run %s %s arm=%s trigger=%s%s",
            job.ticker,
            date_iso,
            arm,
            job.trigger,
            f" flags={','.join(job.flags)}" if job.flags else "",
        )
        try:
            result = child_runner(spec, settings.run_timeout_seconds)
        except Exception as exc:  # R3: the slot never aborts on one ticker
            result = ChildResult(-1, None, f"child runner raised: {exc}")
        if (
            profile is not None
            and profile.provider == "anthropic"
            and result.returncode != 0
            and is_credit_exhausted_error(result.error)
        ):
            # llm-cost R2: the credits are gone — mark the month and rerun
            # this run once on DeepSeek; only the final result is recorded.
            month = clock().strftime("%Y-%m")
            mark_exhausted(config.state_dir, month, result.error or "credit balance too low")
            logger.warning("%s: anthropic credits exhausted — retrying on deepseek", job.ticker)
            append_usage(
                config.state_dir, run_id=None, date=date_iso, session=session,
                ticker=job.ticker, arm=arm, profile=profile.name,
                models=result.usage or {}, error=result.error, clock=clock,
            )
            profile = DEEPSEEK  # also for the job's remaining arms
            switched_profile = True
            run_digest = router.digest(profile)
            spec = replace(spec, env=tuple(sorted(profile.env.items())))
            try:
                result = child_runner(spec, settings.run_timeout_seconds)
            except Exception as exc:
                result = ChildResult(-1, None, f"child runner raised: {exc}")

        try:
            decided_at = to_utc_iso(clock())
        except Exception as exc:
            raise RuntimeError(f"completion clock failed: {_one_line(exc)}") from exc
        plan_dict: dict | None = None
        plan_valid = False
        violations: list[str] = []
        repair_used = False
        error = None
        pm_rating: str | None = None
        if result.returncode == 0 and isinstance(result.payload, dict):
            payload = result.payload
            raw_rating = payload.get("pm_rating")
            pm_rating = raw_rating if isinstance(raw_rating, str) else None
            raw_decision = payload.get("decision")
            decision = raw_decision if raw_decision in ("BUY", "SELL", "HOLD") else "HOLD"
            repair_used = bool(payload.get("repair_used"))
            raw_plan = payload.get("plan")
            if raw_plan is not None:
                plan_obj: TradePlan | None = None
                try:
                    plan_obj = TradePlan.model_validate(raw_plan)
                except ValidationError as exc:
                    violations = [f"plan shape invalid: {exc}"]
                if plan_obj is not None:
                    last_close = atr14 = None
                    if plan_obj.action == "BUY":
                        try:
                            last_close, atr14 = ohlcv_fetcher(job.ticker, slot_date)
                        except Exception as exc:
                            raise RuntimeError(
                                f"OHLCV reference fetch failed: {_one_line(exc)}"
                            ) from exc
                    violations = validate_trade_plan(plan_obj, last_close, atr14)
                    plan_dict = plan_obj.model_dump(mode="json")
                    plan_valid = not violations
            elif decision == "HOLD":
                plan_valid = True  # HOLD-without-plan is trivially valid
            else:
                violations = [payload.get("plan_error") or "no TradePlan block extracted"]
        else:
            decision = "ERROR"
            error = result.error or f"child exit {result.returncode}"
            logger.warning("run failed for %s (%s arm): %s", job.ticker, arm, error)

        run_flags = list(job_flags)
        if switched_profile:
            # Arms before the switch ran on Claude: a pair with this flag mixes
            # providers and is not an A/B contrast of information sources.
            run_flags.append("llm-profile-switched")
        if arm == "brief":
            run_flags += _revision_drift_flags(fresh_macro, None if withhold else fresh_ticker)
        fields = {
            "pair_id": pair_id,
            "date": date_iso,
            "session": session,
            "ticker": job.ticker,
            "arm": arm,
            "preset": profile.name if profile is not None else settings.preset,
            "trigger": job.trigger,
            "catalyst_score": job.catalyst_score,
            "macro_eval_verdict": fresh_macro.verdict,
            "ticker_eval_verdict": fresh_ticker.verdict,
            "inputs": {
                "macro_brief": fresh_macro.ref if arm == "brief" else None,
                "ticker_brief": (
                    fresh_ticker.ref if arm == "brief" and not withhold else None
                ),
                "pool": pool_ref,
                "config_digest": run_digest,
            },
            "decided_at": decided_at,
            "decision": decision,
            "plan": plan_dict,
            "plan_valid": plan_valid,
        }
        record: DecisionRecord | None = None
        try:
            record = mint_and_append_decision(
                config.ledger_dir,
                fields,
                mint_pair_attempt=job.paired and pair_id is None,
            )
        except Exception as exc:  # R3: an unwritable row must not abort the slot
            error = f"ledger append failed: {_one_line(exc)}"
            logger.error("%s (%s arm): %s", job.ticker, arm, error)
        if record is not None:
            records.append(record)
            if job.paired:
                pair_id = record.pair_id
        usage_line = append_usage(
            config.state_dir,
            run_id=record.run_id if record is not None else None,
            date=date_iso, session=session, ticker=job.ticker, arm=arm,
            profile=profile.name if profile is not None else settings.preset,
            models=result.usage or {}, error=error, clock=clock,
        )
        if violations:
            logger.warning(
                "plan invalid for %s (%s): %s",
                record.run_id if record else job.ticker,
                arm,
                "; ".join(violations),
            )
        line_decision = decision if record is not None else "ERROR"
        line_plan_valid = plan_valid if record is not None else False
        try:
            locked_append(
                log_path,
                LOG_FIELD_SEPARATOR.join(
                    str(part)
                    for part in (
                        date_iso,
                        session,
                        job.ticker,
                        arm,
                        job.trigger,
                        record.run_id if record else "append-failed",
                        line_decision,
                        line_plan_valid,
                    )
                ),
            )
        except Exception as exc:
            raise RuntimeError(f"runner log append failed: {_one_line(exc)}") from exc
        line = {
            "run_id": record.run_id if record else None,
            "pair_id": record.pair_id if record else pair_id,
            "ticker": job.ticker,
            "arm": arm,
            "trigger": job.trigger,
            "decision": line_decision,
            "plan_valid": line_plan_valid,
            "violations": violations or None,
            "pm_rating": pm_rating,
            "repair_used": repair_used,
            "profile": profile.name if profile is not None else None,
            "cost_usd": usage_line["cost_usd"] if usage_line else None,
            "flags": run_flags or None,
            "execute_eligible": (
                record is not None  # no ledger row ⇒ nothing to join/execute
                and decision in ("BUY", "SELL")
                and plan_valid
                and execution_eligible(
                    arm, job.trigger, fresh_macro.verdict, fresh_ticker.verdict, settings
                )
            ),
            "error": error,
        }
        run_lines.append(line)
        if job.paired and pair_id is None:
            pair_error = f"pair aborted after {arm} ledger append failed"
            for remaining_arm in job.arms[arm_index + 1:]:
                run_lines.append(_error_run_line(job, remaining_arm, pair_error))
            break
    return outcome


def run_slot(
    session: str,
    slot_date: date,
    *,
    config: PipelineConfig,
    settings: RunnerSettings,
    resolvers: Resolvers,
    child_runner: ChildRunner,
    ohlcv_fetcher: OhlcvFetcher = default_ohlcv_fetcher,
    force: bool = False,
    manual_ticker: str | None = None,
    clock: Callable[[], datetime] = _utc_now,
    emit: Callable[[dict], None] | None = None,
    funnel: FunnelEvents | None = None,
) -> SlotOutcome:
    """Run the slot (or one manual ticker) and append the decision records.

    ``emit`` receives one JSON-serializable dict per completed run (R4 stdout
    lines). Per-run failures never abort the slot (R3).
    """
    date_iso = slot_date.isoformat()
    macro = inspect_macro_brief(resolvers, date_iso, session)

    def inspect(ticker: str) -> BriefInfo:
        return inspect_ticker_brief(resolvers, ticker, date_iso)

    skipped: list[str] = []
    dropped: list[dict] = []
    # Manual runs never auto-execute: they stay out of the slot's funnel.
    events = (
        None
        if manual_ticker is not None
        else funnel or FunnelEvents(config.state_dir, session, slot_date, "analysis_runner")
    )
    untriggered: list[tuple[str, str, str, dict]] = []
    if manual_ticker is not None:
        manual = manual_ticker.strip().upper()
        if not TICKER_RE.match(manual):
            raise ValueError(f"manual ticker {manual_ticker!r} is not a valid ledger symbol")
        pool_ref = None
        jobs = [build_manual_job(manual, macro, inspect(manual))]
    else:
        pool_ref, core_tickers, opportunity_tickers = load_pool_inputs(config, session, slot_date)
        jobs = plan_jobs(
            session=session,
            slot_date=slot_date,
            core_tickers=normalize_tickers(core_tickers, "core"),
            opportunity_tickers=normalize_tickers(opportunity_tickers, "opportunity"),
            settings=settings,
            macro=macro,
            inspect_ticker=inspect,
            untriggered=untriggered,
        )
        if not force:
            solo_done, paired_done = completed_coverage(
                read_decision_rows(config.ledger_dir), date_iso, session
            )
            kept: list[RunJob] = []
            for job in jobs:
                # Pair coverage subsumes solo coverage; a paired job needs a
                # COMPLETE attempt (half pairs and ERROR rows are re-run).
                done = job.ticker in paired_done or (
                    not job.paired and job.ticker in solo_done
                )
                if done:
                    skipped.append(job.ticker)
                    logger.info(
                        "%s already has completed decision row(s) for %s/%s — "
                        "skipping (--force reruns)",
                        job.ticker,
                        date_iso,
                        session,
                    )
                else:
                    kept.append(job)
            jobs = kept
        jobs, dropped = enforce_budget(jobs, settings.max_runs_per_slot)
        if events is not None:
            _emit_trigger_funnel(events, jobs, untriggered, skipped, dropped)

    router = _LlmRouter(config, settings, clock) if settings.llm_routing else None
    digest = write_config_digest(config.ledger_dir, settings) if router is None else ""
    positions = load_positions(config.ledger_dir)
    log_path = config.state_dir / RUNNER_LOG_NAME

    outcomes_by_index: dict[int, JobOutcome] = {}
    if jobs:
        max_workers = min(settings.analysis_job_concurrency, len(jobs))
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_jobs = {
                executor.submit(
                    _run_job,
                    job,
                    session=session,
                    slot_date=slot_date,
                    date_iso=date_iso,
                    config=config,
                    settings=settings,
                    resolvers=resolvers,
                    child_runner=child_runner,
                    ohlcv_fetcher=ohlcv_fetcher,
                    clock=clock,
                    pool_ref=pool_ref,
                    digest=digest,
                    positions=positions,
                    log_path=log_path,
                    router=router,
                ): (index, job)
                for index, job in enumerate(jobs)
            }
            for future in as_completed(future_jobs):
                index, job = future_jobs[future]
                try:
                    outcome = future.result()
                except Exception as exc:
                    error = f"worker raised: {_one_line(exc)}"
                    logger.exception("%s: %s", job.ticker, error)
                    outcome = _complete_failed_outcome(job, JobOutcome(), error)
                outcomes_by_index[index] = outcome
                for line in outcome.run_lines:
                    if events is not None:
                        _emit_decide_funnel(events, line)
                    if emit is not None:
                        emit(line)

    ordered_outcomes = [outcomes_by_index[index] for index in range(len(jobs))]
    records = [record for outcome in ordered_outcomes for record in outcome.records]
    run_lines = [line for outcome in ordered_outcomes for line in outcome.run_lines]

    errors = sum(1 for line in run_lines if line["error"] is not None)
    completed = len(run_lines) - errors
    invalid_plans = sum(
        1 for line in run_lines if line["error"] is None and not line["plan_valid"]
    )
    summary = {
        "date": date_iso,
        "session": session,
        "planned": sum(len(job.arms) for job in jobs),
        "completed": completed,
        "errors": errors,
        "invalid_plans": invalid_plans,
        "pairs": sum(1 for job in jobs if job.paired),
        "skipped": skipped,
        "dropped": dropped,
        "run_ids": [line["run_id"] for line in run_lines if line["run_id"]],
    }
    if router is not None:
        profiles: dict[str, int] = {}
        for line in run_lines:
            if line.get("profile"):
                profiles[line["profile"]] = profiles.get(line["profile"], 0) + 1
        summary["llm"] = {
            "routing": settings.llm_routing,
            "profiles": profiles,
            "cost_usd": round(sum(line.get("cost_usd") or 0.0 for line in run_lines), 4),
            "anthropic_month_usd": round(router.month_spend(), 2),
            "anthropic_budget_usd": settings.anthropic_monthly_budget_usd,
        }
    return SlotOutcome(summary=summary, records=records)


@best_effort
def _emit_trigger_funnel(
    events: FunnelEvents,
    jobs: Sequence[RunJob],
    untriggered: Sequence[tuple[str, str, str, dict]],
    skipped: Sequence[str],
    dropped: Sequence[dict],
) -> None:
    """Trigger-stage funnel events (spec decision-funnel R1)."""
    for ticker, code, reason, details in untriggered:
        events.emit("trigger", ticker, "excluded", code, reason, **details)
    for ticker in skipped:
        events.emit(
            "trigger", ticker, "info", "already-done",
            "completed decision row(s) exist for this slot — not re-run",
        )
    demoted = set()
    for entry in dropped:
        if entry.get("trigger") == "core":
            demoted.add(entry["ticker"])  # still runs as the solo brief run
            continue
        events.emit(
            "trigger", entry["ticker"], "excluded", "budget-dropped", str(entry["reason"]),
            score=entry.get("catalyst_score"),
        )
    for job in jobs:
        if "macro-fail" in job.flags:
            code = "macro-fail-feeds-only"
            reason = "macro eval fail — feeds-only run"
        elif job.trigger == "catalyst":
            code, reason = "catalyst", f"catalyst_score {job.catalyst_score:g}"
        elif job.paired:
            code, reason = "core-pair", "core A/B pair (brief + feeds)"
        else:
            code, reason = "core", "core watchlist"
            if job.ticker in demoted:
                reason += " (pair demoted by the run budget)"
        events.emit(
            "trigger", job.ticker, "passed", code, reason,
            arms=list(job.arms), flags=list(job.flags), score=job.catalyst_score,
        )


@best_effort
def _emit_decide_funnel(events: FunnelEvents, line: dict) -> None:
    """One decide-stage event per run line (both arms of a pair)."""
    decision = line.get("decision") or "ERROR"
    if decision not in ("BUY", "SELL", "HOLD"):
        decision = "ERROR"
    if decision == "ERROR":
        reason = str(line.get("error") or "run failed")
    elif not line.get("plan_valid"):
        reason = "plan invalid: " + "; ".join(line.get("violations") or ["?"])
    else:
        reason = f"{decision}" + (f" (PM: {line['pm_rating']})" if line.get("pm_rating") else "")
    actionable = decision in ("BUY", "SELL") and bool(line.get("plan_valid"))
    events.emit(
        "decide", line.get("ticker"), "passed" if actionable else "excluded", decision, reason,
        arm=line.get("arm"),
        run_id=line.get("run_id"),
        pair_id=line.get("pair_id"),
        pm_rating=line.get("pm_rating"),
        plan_valid=line.get("plan_valid"),
        violations=line.get("violations"),
        error=line.get("error"),
        flags=line.get("flags"),
        repair_used=line.get("repair_used"),
        execute_eligible=line.get("execute_eligible"),
    )


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
        prog="python -m pipeline.analysis_runner",
        description="Run the slot's multi-agent analyses with A/B pairing and "
        "ledger writes (specs/analysis-runner.md).",
    )
    parser.add_argument("--session", required=True, choices=SESSIONS, help="session slot: cn | us")
    parser.add_argument(
        "--date",
        type=_parse_date,
        default=None,
        metavar="YYYY-MM-DD",
        help="slot date in the session's local calendar "
        "(default: today in the session timezone)",
    )
    parser.add_argument(
        "--ticker",
        default=None,
        help="manual trigger: run this one ticker (trigger 'manual', never auto-executed)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="rerun tickers that already have decision rows for the slot "
        "(whole pairs re-run; pair attempt and run_id <n> increment)",
    )
    parser.add_argument(
        "--preset",
        default=None,
        help=f"preset name recorded in the ledger (default: ${PRESET_ENV} or 'default')",
    )
    return parser


def _one_line(exc: BaseException) -> str:
    return " ".join(str(exc).split()) or exc.__class__.__name__


def main(
    argv: list[str] | None = None,
    *,
    child_runner: ChildRunner | None = None,
    resolvers: Resolvers | None = None,
    ohlcv_fetcher: OhlcvFetcher = default_ohlcv_fetcher,
) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.INFO, format="%(levelname)s %(name)s: %(message)s", stream=sys.stderr
    )
    try:
        config = load_config()
        settings = load_settings(preset_override=args.preset)
        slot_date = args.date or session_date(args.session)
        manual = args.ticker.strip().upper() if args.ticker else None
        if manual and not TICKER_RE.match(manual):
            print(
                f"analysis-runner: ticker {args.ticker!r} is not a valid ledger symbol "
                "(alphanumeric segments joined by '.'/'-')",
                file=sys.stderr,
            )
            return 2
        if manual and session_for_ticker(manual) != args.session:
            print(
                f"analysis-runner: ticker {manual} belongs to session "
                f"'{session_for_ticker(manual)}', not '{args.session}'",
                file=sys.stderr,
            )
            return 2

        def emit(line: dict) -> None:
            print(json.dumps(line, ensure_ascii=False), flush=True)

        outcome = run_slot(
            args.session,
            slot_date,
            config=config,
            settings=settings,
            resolvers=resolvers or default_resolvers(config),
            child_runner=child_runner or make_child_runner(config),
            ohlcv_fetcher=ohlcv_fetcher,
            force=args.force,
            manual_ticker=manual,
            emit=emit,
        )
    except KeyboardInterrupt:
        print("analysis-runner: interrupted", file=sys.stderr)
        return 130
    except Exception as exc:  # hard failure — orchestrator maps to 'failed'
        logger.debug("runner failure detail", exc_info=True)
        print(f"analysis-runner: {_one_line(exc)}", file=sys.stderr)
        return 1
    # The final stdout line is the slot summary the orchestrator parses.
    print(json.dumps(outcome.summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
