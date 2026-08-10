"""Pool builder — the day's tiered stock pool file.

Spec: ``specs/pool-builder.md`` (stages 1–5, R1–R5); data shape:
``specs/pool-data-contract.md`` (v1). One invocation per session slot:

1. **Load** — mirror ``core.<session>.yaml`` (missing ⇒ empty core + stderr
   warn; unreadable ⇒ hard failure) and load hysteresis state from the most
   recent pool file dated **strictly before** the run date (never today's own
   output — R3; streaks carry across weekends/holidays/failed days).
2. **Nominate** — render ``prompts/pool_nomination.md`` and run a
   subscription-backed deep-search CLI (same injectable-runner seam as the
   macro collector); validate the strict-JSON candidate list with a single
   errors-appended retry; cache the day's validated output so a same-day rerun
   without ``--force`` never spends a second deep search (R3/R5).
3. **Technical gate** — deterministic (R2, no LLM): daily OHLCV via an
   injectable fetcher, calendar-week resample (last close), Bollinger
   (20, 2σ, population σ — the TA-lib convention) on both frames, verdict per
   the configured :class:`GateRules` (v1.1: per-session 20d dollar-volume
   liquidity floor as a hard veto checked before structure, plus a 5d/20d
   volume-confirmation ratio that demotes a structural pass to watch).
4. **Hysteresis + caps** — the pool contract's lifecycle rules: enter fast
   (gate pass + score ≥ entry), exit slow (3-pool low-score streak / 2-pool
   gate-fail streak ⇒ ``removed``), absent-from-nomination decay,
   gate-fail-high-score ⇒ watch, lowest-score cap truncation (logged).
5. **Write** — strict contract validation, archive-before-replace +
   atomic write of ``<pool_dir>/<session>/YYYY-MM-DD.json``, one line to
   ``pools/builder.log``.

R4 — degrade, don't die: any nomination failure (backend or validation after
retry) carries the opportunity/watch layers forward from the prior pool file
verbatim (streaks untouched, ``carried_forward: true``), warns on stderr, and
exits 0. Only an unreadable core file or a write/validation failure exits
non-zero.

CLI::

    python -m pipeline.pool_builder --session cn|us [--date YYYY-MM-DD]
                                    [--backend claude|codex] [--force]

The backend subprocess and the price fetcher are injectable (``runner`` /
``fetch_ohlcv`` callables) so tests run fully offline.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import re
import subprocess
import sys
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import yaml
from pydantic import Field, ValidationError, field_validator

from pipeline.common import (
    SESSIONS,
    append_log_line,
    archive_existing,
    atomic_write,
    render_template,
    session_date,
    session_for_ticker,
    to_utc_iso,
)
from pipeline.config import load_config
from pipeline.contracts.base import (
    ContractError,
    ContractModel,
    validation_error_messages,
)
from pipeline.contracts.briefs import CatalystType
from pipeline.contracts.pool import (
    OPPORTUNITY_CAP,
    WATCH_CAP,
    BollBands,
    CoreEntry,
    Gate,
    OpportunityEntry,
    PoolFile,
    RemovedEntry,
    TechnicalBlock,
    WatchEntry,
    read_pool,
)
from pipeline.prompts import PROMPTS_DIR

logger = logging.getLogger("pipeline.pool_builder")

# ---------------------------------------------------------------------------
# Configuration surface
# ---------------------------------------------------------------------------

#: The contract's env override — honored via :func:`pipeline.config.load_config`
#: (config key ``pool_dir``), the same resolution the ticker collector uses.
POOL_DIR_ENV = "TRADINGAGENTS_POOL_DIR"
BUILDER_LOG_NAME = "builder.log"

NOMINATION_PROMPT_TEMPLATE = PROMPTS_DIR / "pool_nomination.md"

#: Generator identity written into the pool file per invoked backend
#: (mirrors the macro collector's mapping).
GENERATORS = {
    "claude": "claude-deep-search",
    "codex": "codex-deep-search",
}

#: Default backend commands; the rendered prompt is piped on stdin.
#: ``--skip-git-repo-check``: codex exec refuses to run outside a trusted git
#: worktree, and orchestrated components inherit an arbitrary cwd — without
#: it the codex path dies before searching (D19 makes codex the default).
#: Deliberately no ``--model`` (unlike the evaluator's pinned gpt-5.6-terra):
#: collection deep searches ride the codex CLI's user-configured default
#: model, and the generator id stays the CLI-level ``codex-deep-search``.
BACKEND_COMMANDS = {
    "claude": ("claude", "-p", "--allowedTools", "WebSearch,WebFetch"),
    "codex": ("codex", "exec", "--search", "--skip-git-repo-check", "-"),
}

#: Nomination worst case: the initial deep search plus one errors-appended
#: validation retry (R3).
NOMINATION_ATTEMPTS = 2
#: Share of the component budget reserved for everything that is not a
#: backend call: the serial OHLCV gate fetches and the validate/archive/write
#: tail (including the R4 carried-forward write itself).
GATE_HEADROOM_SECONDS = 180.0
#: Never squeeze a deep-search attempt below this, however small the budget.
MIN_BACKEND_TIMEOUT_SECONDS = 60.0


def backend_timeout_seconds() -> float:
    """Per-attempt deep-search timeout for :func:`default_runner`.

    Sized so the R3 worst case (``NOMINATION_ATTEMPTS`` backend calls) plus
    the gate fetches and the write tail fit inside the orchestrator's
    ``pool_builder`` component budget (config ``component_timeouts``, default
    900s, env ``TRADINGAGENTS_TIMEOUT_POOL_BUILDER``). An inner timeout equal
    to the outer budget would let the orchestrator SIGKILL the process group
    at the very moment a hung backend times out — preempting the R4
    carried-forward degrade, so the day would end with NO pool file (status
    ``timeout``) instead of the spec'd carried-forward pool.
    """
    budget = float(load_config().component_timeouts.get("pool_builder", 900))
    share = (budget - GATE_HEADROOM_SECONDS) / NOMINATION_ATTEMPTS
    return max(MIN_BACKEND_TIMEOUT_SECONDS, share)

#: Injectable boundaries (tests fake these).
Runner = Callable[[str, str], str]  # (backend, prompt) -> raw model output
#: One daily bar: (date, open, high, low, close, volume).
OhlcvBar = tuple[date, float, float, float, float, float]
FetchOhlcv = Callable[[str], Sequence[OhlcvBar]]


class BuilderError(RuntimeError):
    """A hard failure (R4): unreadable core file, contract-invalid output, or
    a write failure — the only non-zero exits the builder has."""


class NominationError(RuntimeError):
    """Nomination could not produce a validated candidate list — the R4
    degrade path (carried-forward pool), never a process failure."""


#: Gate v1.1 per-session liquidity floor defaults (spec stage 3): 20-day
#: average daily dollar volume in the listing currency — USD for ``us``, one
#: shared ``cn`` threshold (HKD for .HK, CNY for .SS/.SZ).
DEFAULT_MIN_AVG_DOLLAR_VOLUME: dict[str, float] = {
    "us": 20_000_000.0,
    "cn": 100_000_000.0,
}
#: Env overrides, per-session (``TRADINGAGENTS_SLOT_TIME_{CN,US}`` precedent).
MIN_AVG_DOLLAR_VOLUME_ENVS = {
    session: f"TRADINGAGENTS_MIN_AVG_DOLLAR_VOLUME_{session.upper()}"
    for session in DEFAULT_MIN_AVG_DOLLAR_VOLUME
}
VOLUME_CONFIRM_RATIO_ENV = "TRADINGAGENTS_VOLUME_CONFIRM_RATIO"

#: The liquidity metric windows are part of the metric's *definition* — the
#: contract pins them in the field names (``avg_dollar_volume_20d``,
#: ``volume_ratio_5d_20d``) — so they are constants, not :class:`GateRules`
#: strategy knobs.
DOLLAR_VOLUME_WINDOW = 20
VOLUME_CONFIRM_FAST_WINDOW = 5


@dataclass(frozen=True)
class GateRules:
    """Stage-3 knobs — the spec's defaults for the ``pool_gate_rules`` config
    key (structured config is file-only per the design doc; the shared
    :mod:`pipeline.config` has no field for it yet, so the defaults live here,
    are env-resolved via :func:`load_gate_rules`, and are injectable via
    :func:`build_pool`)."""

    boll_window: int = 20
    boll_sigma: float = 2.0
    #: Daily close must stay ≤ daily upper band × this (no chasing breaks).
    daily_upper_mult: float = 1.02
    #: Price history shorter than this many daily bars ⇒ gate ``fail``.
    #: NOTE (deliberate, spec-mandated): 60 is the spec's constant, but the
    #: weekly Bollinger needs ~20 ISO weeks (~100 trading days) to compute, so
    #: a name with 60–99 bars can reach at best ``watch`` (its weekly leg can
    #: never hold) — the effective *entry* minimum is ~100 bars. Raising this
    #: to ~100 (or shortening the weekly window) is a spec decision, flagged
    #: rather than changed here.
    min_daily_bars: int = 60
    #: v1.1 hard veto: per-session 20d average-dollar-volume floor. A session
    #: absent from the mapping has no floor (and unit callers that pass no
    #: session skip the check entirely — the floor needs a market context).
    min_avg_dollar_volume: Mapping[str, float] = field(
        default_factory=lambda: dict(DEFAULT_MIN_AVG_DOLLAR_VOLUME)
    )
    #: v1.1 soft demotion: a structural pass whose 5d/20d volume ratio is
    #: below this is demoted to ``watch`` (never lower; never touches
    #: watch/fail verdicts).
    volume_confirm_ratio: float = 1.2


def load_gate_rules(env: Mapping[str, str] | None = None) -> GateRules:
    """Gate rules from spec defaults + ``TRADINGAGENTS_*`` env (loud on junk
    values — the analysis runner's env-resolved settings pattern; a
    :mod:`pipeline.config` hoist candidate, frozen this change set)."""
    if env is None:
        env = os.environ
    defaults = GateRules()

    def raw(key: str) -> str:
        return (env.get(key) or "").strip()

    floors = dict(defaults.min_avg_dollar_volume)
    for session, key in MIN_AVG_DOLLAR_VOLUME_ENVS.items():
        value = raw(key)
        if value:
            floors[session] = float(value)
    ratio = raw(VOLUME_CONFIRM_RATIO_ENV)
    return GateRules(
        min_avg_dollar_volume=floors,
        volume_confirm_ratio=float(ratio) if ratio else defaults.volume_confirm_ratio,
    )


@dataclass(frozen=True)
class LifecycleRules:
    """Stage-4 thresholds (design-doc keys ``pool_entry_threshold`` /
    ``pool_exit_threshold`` and the contract's exit streak lengths)."""

    entry_threshold: float = 6.0
    exit_threshold: float = 4.0
    low_score_exit_streak: int = 3
    gate_fail_exit_streak: int = 2


def resolve_pool_dir(override: str | Path | None = None) -> Path:
    """Base pool directory: explicit override, else the shared config.

    The config resolves the contract's env var (``TRADINGAGENTS_POOL_DIR``),
    else ``<state_dir>/pools`` (honoring ``TRADINGAGENTS_STATE_DIR``), else
    ``~/.tradingagents/pools`` — the exact resolution the ticker collector
    uses, so a ``TRADINGAGENTS_STATE_DIR``-only override can never split-brain
    the pool directory between the two components on manual runs.
    """
    if override is not None:
        return Path(override).expanduser()
    return load_config().pool_dir


# ---------------------------------------------------------------------------
# Stage 1 — load core layer + hysteresis base
# ---------------------------------------------------------------------------


def core_path(pool_dir: Path, session: str) -> Path:
    return pool_dir / f"core.{session}.yaml"


def load_core(pool_dir: Path, session: str) -> list[CoreEntry]:
    """Read the user-maintained core layer (R1: read-only truth).

    Missing file ⇒ empty core + stderr warning; anything unreadable or
    structurally wrong raises :class:`BuilderError` (hard failure — holdings
    coverage must never silently shrink because the yaml broke).
    """
    path = core_path(pool_dir, session)
    if not path.exists():
        print(
            f"pool-builder: warning: {path} not found — core layer is empty",
            file=sys.stderr,
        )
        return []
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise BuilderError(f"unreadable core file {path}: {exc}") from exc
    if data is None:
        return []
    if not isinstance(data, list):
        raise BuilderError(
            f"unreadable core file {path}: expected a YAML list of "
            "{ticker, note?} entries"
        )
    entries: list[CoreEntry] = []
    for index, item in enumerate(data):
        if isinstance(item, str):
            item = {"ticker": item}
        if not isinstance(item, dict):
            raise BuilderError(
                f"unreadable core file {path}: entry {index} is not a mapping"
            )
        ticker = str(item.get("ticker") or "").strip()
        if not ticker:
            raise BuilderError(f"unreadable core file {path}: entry {index} has no ticker")
        if session_for_ticker(ticker) != session:
            raise BuilderError(
                f"unreadable core file {path}: ticker '{ticker}' does not belong "
                f"to the '{session}' session's market"
            )
        note = item.get("note")
        entries.append(CoreEntry(ticker=ticker, note=None if note is None else str(note)))
    return entries


def load_prior_pool(pool_dir: Path, session: str, run_date: date) -> PoolFile | None:
    """Hysteresis base: the most recent pool file dated **strictly before**
    the run date (a same-day rerun never reads its own output — R3).

    Staleness is irrelevant here — streaks count generated pool files, so an
    arbitrarily old prior file still carries state. A corrupt prior file
    degrades to fresh state (R4: only core/write failures die) but warns
    **loudly on stderr** — prior files were strict-validated at write time, so
    corruption means disk/tampering trouble, and silently wiping every prior
    opportunity member's membership and streaks would hide it.
    """
    try:
        result = read_pool(pool_dir, session, run_date - timedelta(days=1))
    except ContractError as exc:
        reason = " ".join(str(exc).split())
        print(
            f"pool-builder: warning: prior pool unreadable ({reason}) — "
            "starting hysteresis fresh; prior opportunity/watch members are "
            "dropped unless re-nominated today",
            file=sys.stderr,
        )
        logger.warning("prior pool unreadable — starting hysteresis fresh: %s", exc)
        return None
    return result.pool


# ---------------------------------------------------------------------------
# Stage 2 — nomination (deep search, injectable runner, cache, retry)
# ---------------------------------------------------------------------------


class NominationEntry(ContractModel):
    """One row of the model's strict-JSON nomination output."""

    ticker: str = Field(min_length=1)
    score: float = Field(ge=0.0, le=10.0)
    catalyst_type: CatalystType
    rationale: str = Field(min_length=1)
    citations: list[str] = Field(min_length=1)

    @field_validator("citations")
    @classmethod
    def _citations_are_urls(cls, value: list[str]) -> list[str]:
        for url in value:
            if not url.startswith(("http://", "https://")):
                raise ValueError(f"citation {url!r} is not an http(s) URL")
        return value


def _render_layer(rows: list[dict]) -> str:
    return json.dumps(rows, ensure_ascii=False) if rows else "(none)"


def render_nomination_prompt(
    run_date: date,
    session: str,
    core: Iterable[CoreEntry],
    prior_opportunity: Iterable[OpportunityEntry],
    prior_watch: Iterable[WatchEntry],
    template_path: str | Path | None = None,
) -> str:
    """Render the nomination prompt; refuses partial renders (shared
    ``render_template`` rule)."""
    path = Path(template_path) if template_path is not None else NOMINATION_PROMPT_TEMPLATE
    core_rows = [
        {"ticker": entry.ticker, **({"note": entry.note} if entry.note else {})}
        for entry in core
    ]
    member_rows = [
        {"ticker": e.ticker, "score": e.score, "catalyst_type": e.catalyst_type}
        for e in prior_opportunity
    ]
    watch_rows = [
        {"ticker": e.ticker, "score": e.score, "catalyst_type": e.catalyst_type}
        for e in prior_watch
    ]
    return render_template(
        path.read_text(encoding="utf-8"),
        {
            "DATE": run_date.isoformat(),
            "SESSION": session,
            "CORE": _render_layer(core_rows),
            "CURRENT_OPPORTUNITY": _render_layer(member_rows),
            "CURRENT_WATCH": _render_layer(watch_rows),
            "CAPS": f"opportunity <= {OPPORTUNITY_CAP}, watch <= {WATCH_CAP}",
        },
    )


def _first_line(text: str) -> str:
    for line in text.splitlines():
        if line.strip():
            return line.strip()
    return ""


def default_runner(backend: str, prompt: str) -> str:
    """Run the deep-search CLI for ``backend`` with the prompt on stdin.

    Failures raise :class:`NominationError` — the builder degrades to a
    carried-forward pool instead of dying (R4).
    """
    command = list(BACKEND_COMMANDS[backend])
    timeout = backend_timeout_seconds()
    try:
        proc = subprocess.run(
            command,
            input=prompt,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise NominationError(f"backend CLI '{command[0]}' not found on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise NominationError(
            f"backend '{backend}' timed out after {int(timeout)}s"
        ) from exc
    if proc.returncode != 0:
        reason = _first_line(proc.stderr or proc.stdout) or "no output"
        raise NominationError(f"backend '{backend}' exited {proc.returncode}: {reason}")
    return proc.stdout


_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(\[.*?\])\s*```", re.DOTALL)


def _extract_json_array(raw: str) -> list:
    """Best-effort strict-JSON-array extraction (mirrors the evaluator's
    object extractor): the raw text, a fenced block, or the outermost
    ``[...]`` span."""
    candidates = [raw.strip()]
    fence = _JSON_FENCE_RE.search(raw)
    if fence:
        candidates.append(fence.group(1))
    start, end = raw.find("["), raw.rfind("]")
    if start != -1 and end > start:
        candidates.append(raw[start : end + 1])
    for candidate in candidates:
        try:
            data = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(data, list):
            return data
    raise ContractError(["no JSON array found in model output"])


def parse_nominations(raw: str) -> list[NominationEntry]:
    """Validate the model's nomination JSON (shape + enums + score range +
    citation URLs). Raises :class:`ContractError` with the complete error
    list so the retry prompt sees every problem at once. Unknown keys the
    model invents are dropped leniently (evaluator precedent)."""
    data = _extract_json_array(raw)
    errors: list[str] = []
    entries: list[NominationEntry] = []
    for index, item in enumerate(data):
        if not isinstance(item, dict):
            errors.append(f"nomination[{index}]: not a JSON object")
            continue
        try:
            entries.append(NominationEntry.parse_lenient(item))
        except ValidationError as exc:
            errors.extend(validation_error_messages(exc, prefix=f"nomination[{index}]."))
    if errors:
        raise ContractError(errors)
    return entries


RETRY_INSTRUCTIONS = (
    "# Previous attempt rejected by the validator\n"
    "\n"
    "Your previous nomination list failed validation with the errors listed\n"
    "below. Return the complete corrected strict JSON array in the exact\n"
    "required shape, fixing every error:\n"
)


def build_retry_prompt(prompt: str, errors: list[str]) -> str:
    """Single retry, macro-collector R3 style: the original prompt with the
    validator's full error list appended."""
    bullets = "\n".join(f"- {error}" for error in errors)
    return f"{prompt}\n\n{RETRY_INSTRUCTIONS}\n{bullets}\n"


def nomination_cache_path(pool_dir: Path, session: str, run_date: date) -> Path:
    """Day-scoped cache of the validated nomination output (R3: a same-day
    rerun without ``--force`` reuses it — at most one deep search per slot,
    R5). The payload records the *originating* backend alongside the entries
    so a cache hit under a different ``--backend`` still stamps the pool's
    ``generator`` with where the nominations actually came from."""
    return pool_dir / session / f".nomination-cache-{run_date.isoformat()}.json"


def _read_cached_nominations(path: Path) -> tuple[list[NominationEntry], str] | None:
    """``(entries, originating_backend)`` from the cache, or ``None`` when the
    cache is absent or corrupt (a bare-list legacy payload counts as corrupt —
    it carries no provenance)."""
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("cache is not a JSON object with backend + nominations")
        cached_backend = data.get("backend")
        if cached_backend not in GENERATORS:
            raise ValueError(f"cache has unknown backend {cached_backend!r}")
        rows = data.get("nominations")
        if not isinstance(rows, list):
            raise ValueError("cache 'nominations' is not a JSON array")
        return [NominationEntry.model_validate(item) for item in rows], cached_backend
    except (OSError, ValueError, ValidationError) as exc:
        logger.warning("ignoring corrupt nomination cache %s: %s", path.name, exc)
        return None


def nominate(
    runner: Runner,
    backend: str,
    prompt: str,
    cache: Path,
    *,
    force: bool = False,
) -> tuple[list[NominationEntry], str]:
    """Produce the day's validated nomination list, as ``(entries, backend)``
    where ``backend`` is the one that actually produced the entries (the
    cache's originating backend on a hit — provenance for ``generator``).

    Cache hit (and not ``--force``) ⇒ no backend call. Otherwise one deep
    search, structural validation, and a single errors-appended retry; any
    failure raises :class:`NominationError` (the caller degrades per R4).
    """
    if not force:
        cached = _read_cached_nominations(cache)
        if cached is not None:
            entries, cached_backend = cached
            logger.info(
                "reusing cached nomination %s (%d entries, backend %s) — "
                "use --force to re-search",
                cache.name,
                len(entries),
                cached_backend,
            )
            return entries, cached_backend
    try:
        raw = runner(backend, prompt)
    except NominationError:
        raise
    except Exception as exc:
        raise NominationError(f"nomination backend failed: {exc}") from exc
    try:
        entries = parse_nominations(raw)
    except ContractError as first:
        logger.warning(
            "nomination attempt 1 failed validation (%d errors); retrying once",
            len(first.errors),
        )
        try:
            raw = runner(backend, build_retry_prompt(prompt, first.errors))
        except NominationError:
            raise
        except Exception as exc:
            raise NominationError(f"nomination backend failed on retry: {exc}") from exc
        try:
            entries = parse_nominations(raw)
        except ContractError as second:
            raise NominationError(
                "nomination validation failed after retry: " + "; ".join(second.errors)
            ) from second
    atomic_write(
        cache,
        json.dumps(
            {
                "backend": backend,
                "nominations": [entry.model_dump(mode="json") for entry in entries],
            },
            indent=2,
            ensure_ascii=False,
        )
        + "\n",
    )
    return entries, backend


# ---------------------------------------------------------------------------
# Stage 3 — deterministic technical gate (R2)
# ---------------------------------------------------------------------------


def _bar_date(bar: Sequence) -> date:
    value = bar[0]
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def bollinger(closes: Sequence[float], window: int = 20, sigma: float = 2.0) -> BollBands | None:
    """Bollinger bands on the trailing ``window`` closes; ``None`` when the
    history is shorter than the window. Population σ (ddof=0), the TA-lib
    convention — documented so R2's determinism is bit-for-bit checkable."""
    if len(closes) < window:
        return None
    tail = [float(value) for value in closes[-window:]]
    mid = sum(tail) / window
    variance = sum((value - mid) ** 2 for value in tail) / window
    band = sigma * math.sqrt(variance)
    return BollBands(close=tail[-1], mid=mid, upper=mid + band, lower=mid - band)


def weekly_closes(bars: Sequence[Sequence]) -> list[float]:
    """Resample daily bars to calendar (ISO) weeks — last close per week; a
    week with no bars simply contributes nothing. Input order is normalized
    by date so the resample is deterministic (R2)."""
    ordered = sorted(bars, key=_bar_date)
    closes: list[float] = []
    current_week: tuple[int, int] | None = None
    for bar in ordered:
        week = _bar_date(bar).isocalendar()[:2]
        close = float(bar[4])
        if week == current_week:
            closes[-1] = close
        else:
            closes.append(close)
            current_week = week
    return closes


def _trailing_mean(values: Sequence[float], window: int) -> float | None:
    if len(values) < window:
        return None
    return sum(values[-window:]) / window


def liquidity_metrics(bars: Sequence[Sequence]) -> tuple[float | None, float | None]:
    """Gate v1.1 liquidity numbers on date-ordered daily bars, as
    ``(avg_dollar_volume_20d, volume_ratio_5d_20d)``:

    - 20-day mean of close × volume (listing currency);
    - 5-day / 20-day mean-volume ratio.

    ``None`` marks an uncomputable metric: history shorter than the window,
    a zero 20d average volume (ratio only), or a non-finite close/volume
    anywhere in a trailing window — yfinance does emit NaN volume bars on
    thin tapes, and NaN must never leak into the snapshot (the contract
    rejects it, and ``NaN < floor`` is False, which would silently bypass
    the liquidity veto). Never a fabricated number.
    """
    ordered = sorted(bars, key=_bar_date)
    closes = [float(bar[4]) for bar in ordered]
    volumes = [float(bar[5]) for bar in ordered]
    dollar = _trailing_mean(
        [close * volume for close, volume in zip(closes, volumes, strict=True)],
        DOLLAR_VOLUME_WINDOW,
    )
    if dollar is not None and not math.isfinite(dollar):
        dollar = None
    fast = _trailing_mean(volumes, VOLUME_CONFIRM_FAST_WINDOW)
    slow = _trailing_mean(volumes, DOLLAR_VOLUME_WINDOW)
    ratio = None
    if (
        fast is not None
        and slow is not None
        and math.isfinite(fast)
        and math.isfinite(slow)
        and fast >= 0.0
        and slow > 0.0
    ):
        ratio = fast / slow
    return dollar, ratio


def evaluate_gate(
    n_daily_bars: int,
    daily: BollBands | None,
    weekly: BollBands | None,
    rules: GateRules | None = None,
    *,
    session: str | None = None,
    avg_dollar_volume_20d: float | None = None,
    volume_ratio_5d_20d: float | None = None,
) -> Gate:
    """The spec's v1.1 rule set, as a pure decision on snapshot values, in
    the spec's fixed order:

    1. History < ``min_daily_bars`` daily bars ⇒ ``fail``.
    2. **Liquidity floor** (hard veto, terminal): 20d average dollar volume
       below the session's ``min_avg_dollar_volume`` ⇒ ``fail`` regardless of
       everything else. Applies only when ``session`` has a configured floor;
       an uncomputable average (``None``) counts as below the floor.
    3. Bollinger structure — ``pass``: weekly close ≥ weekly lower band
       (trend not broken) AND daily close ≤ daily upper band ×
       ``daily_upper_mult`` (not chasing a break); ``watch``: exactly one
       holds; ``fail``: neither holds. A frame whose bands are not computable
       (history shorter than the Bollinger window) cannot hold its condition.
    4. **Volume confirmation** (soft demotion): a structural ``pass`` whose
       ``volume_ratio_5d_20d`` is provided and < ``volume_confirm_ratio`` is
       demoted to ``watch`` — never below watch, and never applied to a
       ``watch`` or ``fail`` verdict.
    """
    rules = rules or GateRules()
    if n_daily_bars < rules.min_daily_bars:
        return "fail"
    floor = rules.min_avg_dollar_volume.get(session) if session is not None else None
    if floor is not None and (avg_dollar_volume_20d is None or avg_dollar_volume_20d < floor):
        return "fail"
    daily_ok = daily is not None and daily.close <= daily.upper * rules.daily_upper_mult
    weekly_ok = weekly is not None and weekly.close >= weekly.lower
    if daily_ok and weekly_ok:
        if volume_ratio_5d_20d is not None and volume_ratio_5d_20d < rules.volume_confirm_ratio:
            return "watch"
        return "pass"
    if daily_ok or weekly_ok:
        return "watch"
    return "fail"


def technical_gate(
    bars: Sequence[Sequence],
    rules: GateRules | None = None,
    session: str | None = None,
) -> TechnicalBlock:
    """Full stage-3 verdict for one ticker's daily OHLCV history (gate v1.1).

    The band + liquidity snapshot is recorded whenever computable, regardless
    of the verdict (fail paths included). The per-session liquidity floor
    needs a market context, so it applies only when ``session`` is given (the
    builder always passes it); the volume-confirmation demotion is
    session-independent and always applies.
    """
    rules = rules or GateRules()
    ordered = sorted(bars, key=_bar_date)
    daily = bollinger([float(bar[4]) for bar in ordered], rules.boll_window, rules.boll_sigma)
    weekly = bollinger(weekly_closes(ordered), rules.boll_window, rules.boll_sigma)
    dollar_volume, volume_ratio = liquidity_metrics(ordered)
    return TechnicalBlock(
        gate=evaluate_gate(
            len(ordered),
            daily,
            weekly,
            rules,
            session=session,
            avg_dollar_volume_20d=dollar_volume,
            volume_ratio_5d_20d=volume_ratio,
        ),
        boll_daily=daily,
        boll_weekly=weekly,
        # The contract records only positive dollar volume (gt=0): an all-zero
        # tape stays None in the snapshot while the veto above still fails it.
        avg_dollar_volume_20d=(
            dollar_volume if dollar_volume is not None and dollar_volume > 0 else None
        ),
        volume_ratio_5d_20d=volume_ratio,
    )


def default_fetch_daily_ohlcv(ticker: str) -> list[OhlcvBar]:
    """Daily OHLCV via the yfinance library directly.

    Deviation from the spec's "existing yfinance vendor" wording, flagged in
    review: the ``tradingagents`` vendor functions return LLM-formatted report
    strings — unusable for band math — so the builder talks to the same data
    source numerically. ~2 years comfortably covers the 20-week Bollinger
    window plus the 60-bar minimum.
    """
    import yfinance as yf  # deferred: pipeline components stay import-light

    frame = yf.Ticker(ticker).history(period="2y", interval="1d", auto_adjust=False)
    bars: list[OhlcvBar] = []
    for index, row in frame.iterrows():
        moment = index.to_pydatetime() if hasattr(index, "to_pydatetime") else index
        day = moment.date() if isinstance(moment, datetime) else moment
        bars.append(
            (
                day,
                float(row["Open"]),
                float(row["High"]),
                float(row["Low"]),
                float(row["Close"]),
                float(row["Volume"]),
            )
        )
    return bars


def _gate_snapshots(
    tickers: Iterable[str],
    fetch: FetchOhlcv,
    rules: GateRules,
    session: str | None = None,
) -> dict[str, TechnicalBlock | None]:
    """Gate every ticker in the day's evaluation universe. A fetch failure —
    or a gate computation error on one ticker's pathological bars — is
    ``None`` (R4 degrade, don't die: one bad tape never takes down the whole
    slot's pool): candidates route as gate ``fail`` with no band or liquidity
    snapshot (numbers are never fabricated), existing members record the same
    snapshot but **freeze** their ``gate_fail_streak`` (an infra error is not
    a structural verdict), and core annotation is simply skipped."""
    snapshots: dict[str, TechnicalBlock | None] = {}
    for ticker in sorted(set(tickers)):
        try:
            bars = list(fetch(ticker))
        except Exception as exc:
            logger.warning("OHLCV fetch failed for %s — gate 'fail', no bands: %s", ticker, exc)
            snapshots[ticker] = None
            continue
        try:
            snapshots[ticker] = technical_gate(bars, rules, session)
        except Exception as exc:
            logger.warning(
                "technical gate failed for %s — treated like a fetch failure "
                "(gate 'fail', no bands): %s",
                ticker,
                exc,
            )
            snapshots[ticker] = None
    return snapshots


# ---------------------------------------------------------------------------
# Stage 4 — hysteresis + caps (contract lifecycle rules)
# ---------------------------------------------------------------------------


@dataclass
class LifecycleResult:
    opportunity: list[OpportunityEntry] = field(default_factory=list)
    watch: list[WatchEntry] = field(default_factory=list)
    removed: list[RemovedEntry] = field(default_factory=list)
    entered: list[str] = field(default_factory=list)
    exited: list[str] = field(default_factory=list)
    truncated: list[str] = field(default_factory=list)


def apply_lifecycle(
    run_date: date,
    prior: PoolFile | None,
    nominated: dict[str, NominationEntry],
    gates: dict[str, TechnicalBlock | None],
    core_tickers: set[str],
    rules: LifecycleRules | None = None,
) -> LifecycleResult:
    """Apply the contract's enter-fast/exit-slow rules and the layer caps."""
    rules = rules or LifecycleRules()
    result = LifecycleResult()
    prior_members = {entry.ticker: entry for entry in (prior.opportunity if prior else [])}

    def gate_block(ticker: str) -> TechnicalBlock:
        return gates.get(ticker) or TechnicalBlock(gate="fail")

    # Existing members: retention or exit. Exit is slow — a member leaves only
    # via the streak rules, never because today's gate or score dipped once.
    for ticker, member in prior_members.items():
        if ticker in core_tickers:
            logger.info(
                "%s is now a core ticker — opportunity membership folds into core", ticker
            )
            continue
        nomination = nominated.get(ticker)
        snapshot = gates.get(ticker)
        technical = snapshot or TechnicalBlock(gate="fail")
        # Absent from nomination ⇒ keep the last score; silence counts as
        # below the exit threshold for this pool's low_score_streak (decay).
        score = member.score if nomination is None else nomination.score
        below_exit = nomination is None or nomination.score < rules.exit_threshold
        low_streak = member.low_score_streak + 1 if below_exit else 0
        if snapshot is None:
            # An OHLCV fetch error is an infra failure, not the contract's
            # price-structure ``fail`` verdict: freeze the streak (the same
            # way nomination failure freezes a carried-forward pool) so a
            # vendor outage spanning two builder runs can never evict members
            # with a misleading "technical gate fail" reason.
            fail_streak = member.gate_fail_streak
        elif technical.gate == "fail":
            fail_streak = member.gate_fail_streak + 1
        else:
            fail_streak = 0
        if low_streak >= rules.low_score_exit_streak:
            result.removed.append(
                RemovedEntry(
                    ticker=ticker,
                    reason=(
                        f"score<{rules.exit_threshold:g} for "
                        f"{rules.low_score_exit_streak} pools"
                    ),
                    last_score=score,
                )
            )
            result.exited.append(ticker)
            continue
        if fail_streak >= rules.gate_fail_exit_streak:
            result.removed.append(
                RemovedEntry(
                    ticker=ticker,
                    reason=(
                        f"technical gate fail for {rules.gate_fail_exit_streak} pools"
                    ),
                    last_score=score,
                )
            )
            result.exited.append(ticker)
            continue
        result.opportunity.append(
            OpportunityEntry(
                ticker=ticker,
                score=score,
                catalyst_type=member.catalyst_type if nomination is None else nomination.catalyst_type,
                rationale=member.rationale if nomination is None else nomination.rationale,
                citations=member.citations if nomination is None else nomination.citations,
                entered_on=member.entered_on,
                low_score_streak=low_streak,
                gate_fail_streak=fail_streak,
                technical=technical,
            )
        )

    # Candidates (nominated, non-core, non-member): enter fast or route to
    # watch per the contract's decision table.
    for ticker, nomination in nominated.items():
        if ticker in core_tickers or ticker in prior_members:
            continue
        technical = gate_block(ticker)
        gate = technical.gate
        if gate == "pass" and nomination.score >= rules.entry_threshold:
            result.opportunity.append(
                OpportunityEntry(
                    ticker=ticker,
                    score=nomination.score,
                    catalyst_type=nomination.catalyst_type,
                    rationale=nomination.rationale,
                    citations=nomination.citations,
                    entered_on=run_date,
                    low_score_streak=0,
                    gate_fail_streak=0,
                    technical=technical,
                )
            )
            result.entered.append(ticker)
        elif (
            gate == "watch"
            or (gate == "pass" and nomination.score >= rules.exit_threshold)
            or (gate == "fail" and nomination.score >= rules.entry_threshold)
        ):
            # gate watch; or score in the [exit, entry) band; or strong
            # narrative on broken structure — watch, never enter.
            result.watch.append(
                WatchEntry(
                    ticker=ticker,
                    score=nomination.score,
                    catalyst_type=nomination.catalyst_type,
                    rationale=nomination.rationale,
                    citations=nomination.citations,
                    technical=technical,
                )
            )
        else:
            logger.info(
                "dropping nominee %s (gate %s, score %.1f)", ticker, gate, nomination.score
            )

    # Caps: truncate lowest-score-first and log every drop (contract hard
    # requirement 4). Sort is deterministic (score desc, ticker asc).
    def rank(entry: OpportunityEntry | WatchEntry) -> tuple[float, str]:
        return (-entry.score, entry.ticker)

    result.opportunity.sort(key=rank)
    result.watch.sort(key=rank)
    for entry in result.opportunity[OPPORTUNITY_CAP:]:
        result.truncated.append(entry.ticker)
        logger.warning(
            "opportunity cap (%d): dropping %s (score %.2f, lowest first)",
            OPPORTUNITY_CAP,
            entry.ticker,
            entry.score,
        )
        if entry.ticker in prior_members:
            result.removed.append(
                RemovedEntry(
                    ticker=entry.ticker,
                    reason=f"opportunity cap {OPPORTUNITY_CAP}: lowest score truncated",
                    last_score=entry.score,
                )
            )
            result.exited.append(entry.ticker)
        elif entry.ticker in result.entered:
            result.entered.remove(entry.ticker)
    result.opportunity = result.opportunity[:OPPORTUNITY_CAP]
    for entry in result.watch[WATCH_CAP:]:
        result.truncated.append(entry.ticker)
        logger.warning(
            "watch cap (%d): dropping %s (score %.2f, lowest first)",
            WATCH_CAP,
            entry.ticker,
            entry.score,
        )
    result.watch = result.watch[:WATCH_CAP]
    return result


# ---------------------------------------------------------------------------
# Stage 5 — validate, archive-before-replace, atomic write, log
# ---------------------------------------------------------------------------


def _displaced_generated_at(path: Path) -> str:
    """``generated_at`` of the pool revision about to be displaced (for the
    stable archive name); falls back to the file's mtime when unreadable."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        data = None
    if isinstance(data, dict):
        value = data.get("generated_at")
        if isinstance(value, str) and value.strip():
            return value.strip()
    return to_utc_iso(datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc))


@dataclass
class BuildResult:
    path: Path
    outcome: str  # "written" | "carried_forward"
    pool: PoolFile
    entered: list[str]
    exited: list[str]


def _filter_session_nominees(
    entries: list[NominationEntry], session: str
) -> dict[str, NominationEntry]:
    """Defensive post-validation filter: wrong-market symbols are dropped with
    a warning (they would fail the pool contract's session rule); duplicate
    tickers keep the first occurrence."""
    nominated: dict[str, NominationEntry] = {}
    for entry in entries:
        if session_for_ticker(entry.ticker) != session:
            logger.warning(
                "dropping nominee %s — not a '%s'-session symbol", entry.ticker, session
            )
            continue
        if entry.ticker in nominated:
            logger.warning(
                "duplicate nomination for %s — keeping the first occurrence", entry.ticker
            )
            continue
        nominated[entry.ticker] = entry
    return nominated


def build_pool(
    session: str,
    as_of: date | str | None = None,
    *,
    backend: str | None = None,
    force: bool = False,
    runner: Runner | None = None,
    fetch_ohlcv: FetchOhlcv | None = None,
    pool_dir: str | Path | None = None,
    gate_rules: GateRules | None = None,
    lifecycle_rules: LifecycleRules | None = None,
) -> BuildResult:
    """Run stages 1–5 for one session slot; raises :class:`BuilderError` only
    on the R4 hard failures (unreadable core, contract-invalid output, write
    failure). ``backend`` ``None`` resolves from the shared config's
    ``collect_backend`` (env ``TRADINGAGENTS_COLLECT_BACKEND``, default
    ``codex`` per D19); an explicit value always wins."""
    if session not in SESSIONS:
        raise BuilderError(f"unknown session '{session}' — expected one of {sorted(SESSIONS)}")
    if backend is None:
        backend = load_config().collect_backend
    if backend not in GENERATORS:
        raise BuilderError(f"unknown backend '{backend}' — expected one of {sorted(GENERATORS)}")
    if as_of is None:
        as_of = session_date(session)  # today in the *session* timezone
    elif isinstance(as_of, str):
        as_of = date.fromisoformat(as_of)
    #: Explicit injection wins; otherwise spec defaults with env overrides.
    gate_rules = gate_rules or load_gate_rules()
    lifecycle_rules = lifecycle_rules or LifecycleRules()

    directory = resolve_pool_dir(pool_dir)
    target = directory / session / f"{as_of.isoformat()}.json"
    log_path = directory / BUILDER_LOG_NAME

    # Stage 1 — core (hard failure) + hysteresis base (degrades).
    try:
        core = load_core(directory, session)
    except BuilderError:
        append_log_line(log_path, as_of.isoformat(), session, backend, "-", "failed")
        raise
    core_tickers = {entry.ticker for entry in core}
    prior = load_prior_pool(directory, session, as_of)
    prior_opportunity = list(prior.opportunity) if prior else []
    prior_watch = list(prior.watch) if prior else []

    # Stage 2 — nomination (R4: failure degrades to a carried-forward pool).
    prompt = render_nomination_prompt(as_of, session, core, prior_opportunity, prior_watch)
    carried_forward = False
    nominations: list[NominationEntry] = []
    #: The backend whose search produced today's nominations — the cache's
    #: originating backend on a hit, so ``generator`` never claims a backend
    #: that did not do the searching.
    nomination_backend = backend
    try:
        nominations, nomination_backend = nominate(
            runner or default_runner,
            backend,
            prompt,
            nomination_cache_path(directory, session, as_of),
            force=force,
        )
    except NominationError as exc:
        carried_forward = True
        print(
            f"pool-builder: warning: {exc} — carrying opportunity/watch forward "
            "from the prior pool (streaks untouched)",
            file=sys.stderr,
        )

    if carried_forward:
        # R4: prior layers verbatim; no gating, no streak movement.
        core_entries = core
        lifecycle = LifecycleResult(
            opportunity=[e for e in prior_opportunity if e.ticker not in core_tickers],
            watch=[e for e in prior_watch if e.ticker not in core_tickers],
        )
    else:
        nominated = _filter_session_nominees(nominations, session)
        # Stages 3+4 — gate the whole evaluation universe, then apply the
        # lifecycle. Core is gated too, but only as a renderable annotation
        # (R1: the gate never drops a core ticker).
        universe = core_tickers | set(nominated) | {e.ticker for e in prior_opportunity}
        gates = _gate_snapshots(
            universe, fetch_ohlcv or default_fetch_daily_ohlcv, gate_rules, session
        )
        lifecycle = apply_lifecycle(
            as_of, prior, nominated, gates, core_tickers, lifecycle_rules
        )
        core_entries = [
            CoreEntry(
                ticker=entry.ticker,
                note=entry.note,
                score=nominated[entry.ticker].score if entry.ticker in nominated else None,
                technical=gates.get(entry.ticker),
            )
            for entry in core
        ]

    # Stage 5 — strict validation, archive-before-replace, atomic write, log.
    try:
        pool_file = PoolFile(
            as_of_date=as_of,
            session=session,  # type: ignore[arg-type]
            generated_at=datetime.now(timezone.utc),
            generator=GENERATORS[nomination_backend],
            carried_forward=carried_forward,
            core=core_entries,
            opportunity=lifecycle.opportunity,
            watch=lifecycle.watch,
            removed=lifecycle.removed,
        )
    except ValidationError as exc:
        append_log_line(log_path, as_of.isoformat(), session, backend, "-", "failed")
        raise BuilderError(
            "pool failed contract validation: "
            + "; ".join(validation_error_messages(exc))
        ) from exc

    payload = (
        json.dumps(pool_file.model_dump(mode="json", exclude_none=True), indent=2,
                   ensure_ascii=False)
        + "\n"
    )
    try:
        if target.exists():
            archived = archive_existing(target, _displaced_generated_at(target))
            if archived is not None:
                logger.info("archived previous revision to %s", archived)
        atomic_write(target, payload)
    except OSError as exc:
        append_log_line(log_path, as_of.isoformat(), session, backend, "-", "failed")
        raise BuilderError(f"pool write failed: {exc}") from exc

    outcome = "carried_forward" if carried_forward else "written"
    append_log_line(
        log_path,
        as_of.isoformat(),
        session,
        f"core={len(core_entries)} opp={len(lifecycle.opportunity)} "
        f"watch={len(lifecycle.watch)} removed={len(lifecycle.removed)}",
        f"enter={','.join(lifecycle.entered) or '-'}",
        f"exit={','.join(lifecycle.exited) or '-'}",
        f"truncated={','.join(lifecycle.truncated) or '-'}",
        outcome,
    )
    logger.info(
        "wrote %s (%s: core=%d opp=%d watch=%d removed=%d)",
        target,
        outcome,
        len(core_entries),
        len(lifecycle.opportunity),
        len(lifecycle.watch),
        len(lifecycle.removed),
    )
    return BuildResult(
        path=target,
        outcome=outcome,
        pool=pool_file,
        entered=lifecycle.entered,
        exited=lifecycle.exited,
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
        prog="python -m pipeline.pool_builder",
        description="Build the day's tiered stock pool file (specs/pool-builder.md): "
        "core mirror + deep-search nomination + Bollinger gate + hysteresis.",
    )
    parser.add_argument(
        "--session", required=True, choices=SESSIONS, help="session slot: cn | us"
    )
    parser.add_argument(
        "--date",
        type=_parse_date,
        default=None,
        metavar="YYYY-MM-DD",
        help="run date in the session's local calendar "
        "(default: today in the session timezone)",
    )
    parser.add_argument(
        "--backend",
        choices=tuple(GENERATORS),
        default=None,
        help="deep-search backend for nomination (default: config "
        "collect_backend — codex per D19, env TRADINGAGENTS_COLLECT_BACKEND)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="re-run the nomination deep search even when today's validated "
        "nomination cache exists",
    )
    return parser


def _one_line(exc: BaseException) -> str:
    return " ".join(str(exc).split()) or exc.__class__.__name__


def main(
    argv: list[str] | None = None,
    runner: Runner | None = None,
    fetch_ohlcv: FetchOhlcv | None = None,
) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    try:
        result = build_pool(
            args.session,
            as_of=args.date,
            backend=args.backend,
            force=args.force,
            runner=runner,
            fetch_ohlcv=fetch_ohlcv,
        )
    except KeyboardInterrupt:
        print("pool-builder: interrupted", file=sys.stderr)
        return 130
    except Exception as exc:  # R4 hard failures only — one-line stderr reason
        logger.debug("pool builder failure detail", exc_info=True)
        print(f"pool-builder: {_one_line(exc)}", file=sys.stderr)
        return 1
    print(f"{result.outcome}: {result.path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
