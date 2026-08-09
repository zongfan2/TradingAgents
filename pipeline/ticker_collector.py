"""Ticker brief collector — pool-driven per-ticker deep-search briefs.

Spec: ``specs/ticker-brief-collector.md`` (R1–R7); data shape:
``specs/ticker-brief-data-contract.md`` (v1); pool input:
``specs/pool-data-contract.md`` (reader). One invocation reads the session's
pool per the pool contract's reading rule (R1 — newest file ≤ date, staleness
warning, ``pool_max_staleness_days`` cap with ``core.<session>.yaml``
fallback), renders the per-ticker deep-search prompt with the pool entry's
catalyst seed (R2), fans out over ``core ∪ opportunity`` through a
subscription CLI backend under a concurrency cap (R3), validates each brief in
full collector mode with a single errors-appended retry (R4), and atomically
writes ``<ticker_brief_dir>/<TICKER>/YYYY-MM-DD.md`` — archiving any displaced
revision first (R5). One ticker's failure never aborts the rest; stdout ends
with one JSON summary line and partial success is exit 0 (R6). Every ticker
appends one line to ``ticker_briefs/collector.log`` (R7).

CLI::

    python -m pipeline.ticker_collector --session cn|us [--date YYYY-MM-DD]
                                        [--tickers NVDA,AVGO]
                                        [--backend claude|codex] [--force]

Exit codes: ``0`` when ≥ 1 brief was written, ≥ 1 ticker was skipped (its
brief already exists on disk — R5 idempotence means the day's coverage is
there), or nothing failed; ``1`` only when **every** requested ticker failed
or the pool file was unreadable (R6's letter: skips are satisfied coverage,
so a skip+fail rerun mix is still partial success); ``2`` usage; ``3`` — the
distinct R1 exit — when even the core-yaml fallback yields no tickers;
``130`` interrupted.

The backend subprocess boundary is injectable (``runner`` callable) so tests
run fully offline.
"""

from __future__ import annotations

import argparse
import functools
import json
import logging
import math
import os
import subprocess
import sys
from collections.abc import Callable, Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path

import yaml

from pipeline.common import (
    LOG_FIELD_SEPARATOR,
    SESSIONS,
    archive_existing,
    atomic_write,
    locked_append,
    render_template,
    session_date,
    session_for_ticker,
    to_utc_iso,
)
from pipeline.config import load_config
from pipeline.contracts.base import ContractError
from pipeline.contracts.briefs import (
    TickerBriefMeta,
    parse_ticker_brief,
    split_frontmatter,
    validate_ticker_brief_path,
)
from pipeline.contracts.pool import PoolReadResult, read_pool
from pipeline.prompts import PROMPTS_DIR

logger = logging.getLogger("pipeline.ticker_collector")

# ---------------------------------------------------------------------------
# Configuration surface
# ---------------------------------------------------------------------------

TICKER_PROMPT_TEMPLATE = PROMPTS_DIR / "ticker_deep_search.md"
COLLECTOR_LOG_NAME = "collector.log"

#: Design-doc config key ``ticker_collect_concurrency`` (default 3). The key
#: is not yet in :class:`pipeline.config.PipelineConfig` (off-limits in this
#: change set), so it is resolved privately: explicit argument, else this env
#: var, else the default. Flagged for hoisting into the shared config.
DEFAULT_TICKER_COLLECT_CONCURRENCY = 3
CONCURRENCY_ENV = "TRADINGAGENTS_TICKER_COLLECT_CONCURRENCY"

#: R2/R3: generator identity written into the brief per invoked backend.
#: Private copies of the macro collector's backend seam (same commands, same
#: stdin-prompt convention) — candidates for a shared ``pipeline.backends``
#: helper once an integrator can touch shared modules.
GENERATORS = {
    "claude": "claude-deep-search",
    "codex": "codex-deep-search",
}

#: Default backend commands; the rendered prompt is piped on stdin.
BACKEND_COMMANDS = {
    "claude": ("claude", "-p", "--allowedTools", "WebSearch,WebFetch"),
    "codex": ("codex", "exec", "--search", "-"),
}

#: Worst-case backend calls per ticker: initial attempt + one errors-appended
#: validation retry (R4).
BACKEND_ATTEMPTS = 2
#: Per-attempt ceiling (the macro collector's precedent); the fan-out budget
#: maths below only ever shrink it.
BACKEND_TIMEOUT_SECONDS = 1800.0
#: Never squeeze a deep-search attempt below this — past this point the
#: aggregate-budget guarantee degrades gracefully instead of making every
#: search impossible on very large pools.
MIN_BACKEND_TIMEOUT_SECONDS = 300.0
#: Share of the component budget reserved for pool reading, validation, and
#: the atomic writes around the backend calls.
FANOUT_HEADROOM_SECONDS = 120.0


def fanout_backend_timeout(n_jobs: int, concurrency: int, component_budget: float) -> float:
    """Per-attempt deep-search timeout for the fan-out's default runner.

    Sized so the worst case — every ticker exhausting ``BACKEND_ATTEMPTS``
    attempts across ``ceil(n_jobs / concurrency)`` waves — fits inside the
    orchestrator's ``ticker_collectors`` aggregate budget (default 2700s, env
    ``TRADINGAGENTS_TIMEOUT_TICKER_COLLECTORS``). With the flat 1800s inner
    timeout, even a single wave of three hung backends (2 × 1800s) would
    overrun the 2700s budget, so the orchestrator would SIGKILL the fan-out
    mid-flight and the R6 JSON summary line — the informative per-ticker
    outcome report — would be lost to a bare component ``timeout``.
    """
    waves = max(1, math.ceil(n_jobs / max(1, concurrency)))
    share = (component_budget - FANOUT_HEADROOM_SECONDS) / (BACKEND_ATTEMPTS * waves)
    return min(BACKEND_TIMEOUT_SECONDS, max(MIN_BACKEND_TIMEOUT_SECONDS, share))


#: Injectable subprocess boundary (tests fake this).
Runner = Callable[[str, str], str]  # (backend, prompt) -> brief text


class CollectorError(RuntimeError):
    """A collector failure whose message is the one-line stderr reason (R6)."""


class NoTickersError(CollectorError):
    """R1's distinct failure: even the core-yaml fallback yields no tickers."""


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TickerJob:
    """One ticker to collect, with the pool entry's investigation seed
    (``None``/``None`` for core tickers, which carry no nomination)."""

    ticker: str
    seed_catalyst_type: str | None = None
    seed_rationale: str | None = None


@dataclass
class TickerOutcome:
    ticker: str
    outcome: str  # "written" | "skipped" | "failed"
    reason: str | None = None
    sources_count: int | None = None
    catalyst_score: float | None = None
    attempts: int = 0
    path: Path | None = None


@dataclass
class CollectSummary:
    """Fan-out result; ``as_dict()`` is the R6 stdout JSON summary line."""

    date: date
    session: str
    outcomes: list[TickerOutcome] = field(default_factory=list)

    @property
    def requested(self) -> int:
        return len(self.outcomes)

    @property
    def written(self) -> int:
        return sum(1 for o in self.outcomes if o.outcome == "written")

    @property
    def skipped(self) -> int:
        return sum(1 for o in self.outcomes if o.outcome == "skipped")

    @property
    def failed(self) -> list[TickerOutcome]:
        return [o for o in self.outcomes if o.outcome == "failed"]

    def as_dict(self) -> dict:
        return {
            "date": self.date.isoformat(),
            "session": self.session,
            "requested": self.requested,
            "written": self.written,
            "skipped": self.skipped,
            "failed": [{"ticker": o.ticker, "reason": o.reason} for o in self.failed],
        }

    @property
    def success(self) -> bool:
        """R6: non-zero **only when every requested ticker failed**. A skip
        means the ticker's brief already exists on disk (R5), so a rerun mix
        of skipped + failed is partial success exactly like written + failed —
        the day's coverage is identical to the exit-0 run that wrote it."""
        return self.written >= 1 or self.skipped >= 1 or not self.failed


# ---------------------------------------------------------------------------
# Backend invocation (R3) — the default runner shells out; tests inject fakes
# ---------------------------------------------------------------------------


def _first_line(text: str) -> str:
    for line in text.splitlines():
        if line.strip():
            return line.strip()
    return ""


def default_runner(
    backend: str, prompt: str, timeout: float = BACKEND_TIMEOUT_SECONDS
) -> str:
    """Run the deep-search CLI for ``backend`` with the prompt on stdin.

    ``timeout`` is per attempt; the fan-out passes a budget-derived value
    (:func:`fanout_backend_timeout`) so the whole run fits its component
    budget."""
    command = list(BACKEND_COMMANDS[backend])
    try:
        proc = subprocess.run(
            command,
            input=prompt,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise CollectorError(f"backend CLI '{command[0]}' not found on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise CollectorError(
            f"backend '{backend}' timed out after {int(timeout)}s"
        ) from exc
    if proc.returncode != 0:
        reason = _first_line(proc.stderr or proc.stdout) or "no output"
        raise CollectorError(f"backend '{backend}' exited {proc.returncode}: {reason}")
    return proc.stdout


# ---------------------------------------------------------------------------
# Prompt rendering (R2)
# ---------------------------------------------------------------------------

SEED_HEADER = (
    "Investigation seed from the pool builder's nomination — verify it "
    "independently with fresh searches; do not assume it still holds:"
)
NO_SEED_BLOCK = (
    "No investigation seed — this is a core holding; survey the name fresh "
    "with no prior hypothesis."
)


def render_seed_block(job: TickerJob) -> str:
    """The ``{{SEED}}`` block: the pool entry's catalyst_type/rationale as the
    investigation seed, or the explicit no-seed line for core tickers."""
    if job.seed_catalyst_type is None and job.seed_rationale is None:
        return NO_SEED_BLOCK
    lines = [SEED_HEADER]
    if job.seed_catalyst_type is not None:
        lines.append(f"- catalyst_type: {job.seed_catalyst_type}")
    if job.seed_rationale is not None:
        lines.append(f"- rationale: {job.seed_rationale}")
    return "\n".join(lines)


def render_ticker_prompt(
    as_of: date,
    ticker: str,
    session: str,
    generator: str,
    seed_block: str,
    template_path: str | Path | None = None,
) -> str:
    """Render the per-ticker deep-search prompt; any placeholder left
    unresolved raises (never send a partial render to the backend)."""
    path = Path(template_path) if template_path is not None else TICKER_PROMPT_TEMPLATE
    text = path.read_text(encoding="utf-8")
    return render_template(
        text,
        {
            "DATE": as_of.isoformat(),
            "TICKER": ticker,
            "SESSION": session,
            "GENERATOR": generator,
            "SEED": seed_block,
        },
    )


# ---------------------------------------------------------------------------
# Pool reading + core-yaml fallback (R1)
# ---------------------------------------------------------------------------


def read_core_yaml(pool_dir: Path, session: str) -> list[str]:
    """Tickers from the user-maintained ``core.<session>.yaml``.

    Private reader (the contracts module exposes none): the contract's shape
    is a list of ``{ticker, note?}`` entries; bare-string entries are accepted
    too. A missing file is an empty core (pool-builder stage 1 behavior);
    a malformed file is a hard :class:`CollectorError`. Tickers whose symbol
    suffix derives the other session are skipped with a warning.
    """
    path = Path(pool_dir) / f"core.{session}.yaml"
    if not path.exists():
        logger.warning("%s missing — empty core layer", path)
        return []
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise CollectorError(f"core file {path} unreadable: {exc}") from exc
    if data is None:
        return []
    if not isinstance(data, list):
        raise CollectorError(f"core file {path} is not a YAML list of entries")
    tickers: list[str] = []
    for i, entry in enumerate(data):
        if isinstance(entry, str):
            ticker = entry.strip()
        elif isinstance(entry, dict) and isinstance(entry.get("ticker"), str):
            ticker = entry["ticker"].strip()
        else:
            raise CollectorError(
                f"core file {path} entry {i} has no 'ticker' — expected {{ticker, note?}}"
            )
        if not ticker:
            raise CollectorError(f"core file {path} entry {i} has an empty ticker")
        if session_for_ticker(ticker) != session:
            logger.warning(
                "core file %s: skipping '%s' — symbol suffix derives session '%s', not '%s'",
                path,
                ticker,
                session_for_ticker(ticker),
                session,
            )
            continue
        tickers.append(ticker)
    return tickers


def resolve_jobs(
    pool_dir: Path,
    session: str,
    as_of: date,
    pool_max_staleness_days: int,
) -> tuple[list[TickerJob], PoolReadResult]:
    """R1: the collection set is ``core ∪ opportunity`` per the pool
    contract's reading rule; an absent pool (no file, or gap beyond
    ``pool_max_staleness_days``) drops the opportunity layer and falls back to
    ``core.<session>.yaml`` so holdings never lose coverage."""
    try:
        result = read_pool(pool_dir, session, as_of, pool_max_staleness_days)
    except ContractError as exc:
        raise CollectorError(f"pool file unreadable: {exc}") from exc

    jobs: list[TickerJob] = []
    seen: set[str] = set()

    def add(job: TickerJob) -> None:
        if job.ticker not in seen:
            seen.add(job.ticker)
            jobs.append(job)

    if result.staleness == "absent":
        if result.path is not None:
            logger.warning(
                "pool %s is %d days old (max %d) — treating the pool as absent; "
                "opportunity layer dropped, core coverage falls back to core.%s.yaml",
                result.path.name,
                result.gap_days,
                pool_max_staleness_days,
                session,
            )
        else:
            logger.warning(
                "no pool file for session '%s' dated on or before %s — "
                "falling back to core.%s.yaml",
                session,
                as_of.isoformat(),
                session,
            )
        for ticker in read_core_yaml(pool_dir, session):
            add(TickerJob(ticker))
        return jobs, result

    if result.staleness == "warn":
        logger.warning(
            "STALE POOL: serving %s for %s (%d day(s) old)",
            result.path.name if result.path else "?",
            as_of.isoformat(),
            result.gap_days or 0,
        )
    assert result.pool is not None  # fresh/warn always carry a parsed file
    for entry in result.pool.core:
        add(TickerJob(entry.ticker))
    for opp in result.pool.opportunity:
        add(TickerJob(opp.ticker, seed_catalyst_type=opp.catalyst_type,
                      seed_rationale=opp.rationale))
    return jobs, result


# ---------------------------------------------------------------------------
# Validation + single retry (R4)
# ---------------------------------------------------------------------------

RETRY_INSTRUCTIONS = (
    "# Previous attempt rejected by the structural validator\n"
    "\n"
    "Your previous brief failed validation with the errors listed below.\n"
    "Produce the complete corrected brief in the exact required format,\n"
    "fixing every error:\n"
)


def build_retry_prompt(prompt: str, errors: list[str]) -> str:
    """R4 retry: the original prompt with the validator's full error list appended."""
    bullets = "\n".join(f"- {error}" for error in errors)
    return f"{prompt}\n\n{RETRY_INSTRUCTIONS}\n{bullets}\n"


def _validate(text: str, target: Path, generator: str) -> TickerBriefMeta:
    """Full collector-mode validation (R4): the ticker contract's hard
    requirements, the word-bound quality gate, the generator match, and the
    filename/directory cross-check — all errors merged into one
    :class:`ContractError` so the retry prompt sees the complete list."""
    errors: list[str] = []
    meta: TickerBriefMeta | None = None
    try:
        meta, _body = parse_ticker_brief(text, expected_generator=generator)
    except ContractError as exc:
        errors.extend(exc.errors)
    if meta is not None:
        try:
            validate_ticker_brief_path(target, meta)
        except ContractError as exc:
            errors.extend(exc.errors)
    if errors or meta is None:
        raise ContractError(errors)
    return meta


def _generate_validated(
    runner: Runner, backend: str, prompt: str, target: Path, generator: str
) -> tuple[str, TickerBriefMeta, int]:
    text = runner(backend, prompt)
    try:
        return text, _validate(text, target, generator), 1
    except ContractError as first:
        logger.warning(
            "attempt 1 for %s failed validation (%d errors); retrying once",
            target,
            len(first.errors),
        )
        text = runner(backend, build_retry_prompt(prompt, first.errors))
        try:
            return text, _validate(text, target, generator), 2
        except ContractError as second:
            raise CollectorError(
                "validation failed after retry: " + "; ".join(second.errors)
            ) from second


# ---------------------------------------------------------------------------
# Per-ticker collection (R3 isolation, R5 atomic + idempotent, R7 log)
# ---------------------------------------------------------------------------


def _displaced_generated_at(path: Path) -> str:
    """``generated_at`` of the revision about to be displaced, for the stable
    archive name; falls back to the file's mtime when unreadable."""
    try:
        data, _body, _errors = split_frontmatter(path.read_text(encoding="utf-8"))
    except OSError:
        data = None
    if isinstance(data, dict):
        value = data.get("generated_at")
        if isinstance(value, datetime):
            if value.tzinfo is None:
                value = value.replace(tzinfo=timezone.utc)
            return to_utc_iso(value)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return to_utc_iso(datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc))


def _log(log_path: Path, as_of: date, session: str, ticker: str, *fields: object) -> None:
    """R7: one ``date | session | ticker | sources_count | catalyst_score |
    outcome`` line. ``locked_append`` (not ``append_log_line``) because the
    fan-out writes from worker threads concurrently."""
    parts = (as_of.isoformat(), session, ticker, *fields)
    locked_append(log_path, LOG_FIELD_SEPARATOR.join(str(part) for part in parts))


def _one_line(exc: BaseException) -> str:
    return " ".join(str(exc).split()) or exc.__class__.__name__


def collect_one(
    job: TickerJob,
    *,
    as_of: date,
    session: str,
    backend: str,
    force: bool,
    runner: Runner,
    brief_dir: Path,
    log_path: Path,
) -> TickerOutcome:
    """Collect one ticker's brief; never raises (R3: per-ticker isolation —
    any failure becomes a ``failed`` outcome in the summary)."""
    generator = GENERATORS[backend]
    target = brief_dir / job.ticker / f"{as_of.isoformat()}.md"
    try:
        if target.exists() and not force:
            # R5: idempotent — this ticker's brief already exists for the date.
            logger.info("%s already exists — skipping (use --force to re-collect)", target)
            _log(log_path, as_of, session, job.ticker, "-", "-", "skipped")
            return TickerOutcome(job.ticker, "skipped", path=target)

        prompt = render_ticker_prompt(as_of, job.ticker, session, generator,
                                      render_seed_block(job))
        text, meta, attempts = _generate_validated(runner, backend, prompt, target, generator)

        if target.exists():  # only reachable with --force
            archived = archive_existing(target, _displaced_generated_at(target))
            if archived is not None:
                logger.info("archived previous revision to %s", archived)
        atomic_write(target, text)
        logger.info(
            "wrote %s (%d sources, catalyst_score %.1f, attempt %d)",
            target,
            meta.sources_count,
            meta.catalyst_score,
            attempts,
        )
        _log(
            log_path, as_of, session, job.ticker,
            meta.sources_count, meta.catalyst_score, "written",
        )
        return TickerOutcome(
            job.ticker,
            "written",
            sources_count=meta.sources_count,
            catalyst_score=meta.catalyst_score,
            attempts=attempts,
            path=target,
        )
    except Exception as exc:
        reason = _one_line(exc)
        logger.warning("collection failed for %s: %s", job.ticker, reason)
        _log(log_path, as_of, session, job.ticker, "-", "-", "failed")
        return TickerOutcome(job.ticker, "failed", reason=reason)


# ---------------------------------------------------------------------------
# Fan-out (R3)
# ---------------------------------------------------------------------------


def _resolve_concurrency(concurrency: int | None) -> int:
    if concurrency is None:
        raw = os.environ.get(CONCURRENCY_ENV, "").strip()
        concurrency = int(raw) if raw else DEFAULT_TICKER_COLLECT_CONCURRENCY
    if concurrency < 1:
        raise CollectorError(f"concurrency must be >= 1, got {concurrency}")
    return concurrency


def _subset_jobs(
    jobs: list[TickerJob], tickers: Iterable[str]
) -> tuple[list[TickerJob], list[TickerOutcome]]:
    """``--tickers`` filter: keep the requested subset in pool order; a
    requested ticker outside ``core ∪ opportunity`` is a per-ticker failure
    (the collector never invents pool membership)."""
    requested = [t.strip() for t in tickers if t.strip()]
    known = {job.ticker for job in jobs}
    selected = [job for job in jobs if job.ticker in requested]
    prefailed = [
        TickerOutcome(t, "failed", reason="not in pool (core ∪ opportunity) for this session")
        for t in dict.fromkeys(requested)  # de-duped, order preserved
        if t not in known
    ]
    return selected, prefailed


def collect_all(
    session: str,
    as_of: date | str | None = None,
    *,
    backend: str = "claude",
    force: bool = False,
    tickers: Sequence[str] | None = None,
    runner: Runner | None = None,
    brief_dir: str | Path | None = None,
    pool_dir: str | Path | None = None,
    concurrency: int | None = None,
    pool_max_staleness_days: int | None = None,
) -> CollectSummary:
    """Collect briefs for the session's ``core ∪ opportunity`` tickers.

    Raises :class:`NoTickersError` (R1's distinct failure) when even the
    core-yaml fallback yields no tickers, and :class:`CollectorError` on an
    unreadable pool/core file; individual ticker failures never raise — they
    are ``failed`` entries in the returned summary (R6).
    """
    if session not in SESSIONS:
        raise CollectorError(f"unknown session '{session}' — expected one of {sorted(SESSIONS)}")
    if backend not in GENERATORS:
        raise CollectorError(f"unknown backend '{backend}' — expected one of {sorted(GENERATORS)}")
    if as_of is None:
        as_of = session_date(session)  # R1: today in the *session* timezone
    elif isinstance(as_of, str):
        as_of = date.fromisoformat(as_of)

    config = load_config()
    brief_dir = Path(brief_dir).expanduser() if brief_dir else config.ticker_brief_dir
    pool_dir = Path(pool_dir).expanduser() if pool_dir else config.pool_dir
    if pool_max_staleness_days is None:
        pool_max_staleness_days = config.pool_max_staleness_days
    max_workers = _resolve_concurrency(concurrency)
    log_path = brief_dir / COLLECTOR_LOG_NAME

    jobs, _pool_result = resolve_jobs(pool_dir, session, as_of, pool_max_staleness_days)
    prefailed: list[TickerOutcome] = []
    if tickers is not None:
        jobs, prefailed = _subset_jobs(jobs, tickers)
        for outcome in prefailed:
            logger.warning("requested ticker %s: %s", outcome.ticker, outcome.reason)
            _log(log_path, as_of, session, outcome.ticker, "-", "-", "failed")
    if not jobs and not prefailed:
        raise NoTickersError(
            f"no tickers to collect for session '{session}' on {as_of.isoformat()} — "
            f"pool absent/empty and core.{session}.yaml yields none"
        )

    outcomes: list[TickerOutcome] = []
    if jobs:
        run = runner
        if run is None:
            budget = float(config.component_timeouts.get("ticker_collectors", 2700))
            run = functools.partial(
                default_runner,
                timeout=fanout_backend_timeout(len(jobs), max_workers, budget),
            )
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = [
                pool.submit(
                    collect_one,
                    job,
                    as_of=as_of,
                    session=session,
                    backend=backend,
                    force=force,
                    runner=run,
                    brief_dir=brief_dir,
                    log_path=log_path,
                )
                for job in jobs
            ]
            outcomes = [future.result() for future in futures]  # collect_one never raises
    return CollectSummary(date=as_of, session=session, outcomes=outcomes + prefailed)


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
        prog="python -m pipeline.ticker_collector",
        description="Collect per-ticker deep-search briefs for the session's pool "
        "(specs/ticker-brief-collector.md).",
    )
    parser.add_argument(
        "--session", required=True, choices=SESSIONS, help="session slot: cn | us"
    )
    parser.add_argument(
        "--date",
        type=_parse_date,
        default=None,
        metavar="YYYY-MM-DD",
        help="as-of date in the session's local calendar "
        "(default: today in the session timezone; use for backfill)",
    )
    parser.add_argument(
        "--tickers",
        default=None,
        metavar="CSV",
        help="comma-separated subset of the pool's core ∪ opportunity tickers "
        "(manual runs; default: all)",
    )
    parser.add_argument(
        "--backend",
        choices=tuple(GENERATORS),
        default="claude",
        help="deep-search backend (default: claude)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="re-collect tickers whose brief already exists for the date",
    )
    return parser


def main(argv: list[str] | None = None, runner: Runner | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    try:
        summary = collect_all(
            args.session,
            as_of=args.date,
            backend=args.backend,
            force=args.force,
            tickers=args.tickers.split(",") if args.tickers else None,
            runner=runner,
        )
    except KeyboardInterrupt:
        print("ticker-collector: interrupted", file=sys.stderr)
        return 130
    except NoTickersError as exc:
        # R1's distinct exit: not a crash, just nothing to do — but loudly so
        # the orchestrator can surface a broken pool/core configuration.
        print(f"ticker-collector: {_one_line(exc)}", file=sys.stderr)
        return 3
    except Exception as exc:  # unreadable pool/core, bad config — one-line reason
        logger.debug("collector failure detail", exc_info=True)
        print(f"ticker-collector: {_one_line(exc)}", file=sys.stderr)
        return 1
    # R6: stdout ends with exactly one JSON summary line.
    print(json.dumps(summary.as_dict(), ensure_ascii=False))
    if summary.success:
        return 0
    # Only reachable when written == skipped == 0 and failed == requested.
    print(
        f"ticker-collector: all {summary.requested} requested ticker(s) failed; "
        "none written",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
