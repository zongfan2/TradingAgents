"""Brief evaluator (macro + ticker) — specs/macro-brief-evaluator.md.

Scores a collected brief for accuracy with a web-search-enabled CLI backend —
``claude -p`` (identity ``claude-eval``, the default; D20 pins its model and
effort, Opus 5.5 by default) or GPT-5.6 Terra via ``codex exec`` — and writes
the ``*.eval.json`` next to the brief per its data contract. The backend is
selectable (``--backend claude|codex``, default from config ``eval_backend``,
env ``TRADINGAGENTS_EVAL_BACKEND``); an evaluator on the same model as the
collector (same backend, and for claude the same pinned model) degrades
collector/evaluator independence (R2) and warns loudly on stderr without
failing. One evaluator
handles both brief kinds, detecting the kind from the frontmatter (``ticker``
present ⇒ ticker brief).

Exit codes
----------
- 0 — evaluation completed (**regardless of verdict**, R5) or skipped because
  the existing eval's ``brief_sha256`` matches the brief (R3).
- 1 — unreadable input / unexpected error.
- 2 — reserved for argparse CLI usage errors (bad flag/missing argument).
- 3 — backend failure (the backend CLI failed, or the model's JSON failed
  schema validation twice).
- 4 — structural refusal (R1): the brief fails its contract's hard structural
  requirements — that is a collector bug, not an evaluation result. (Not 2:
  argparse exits 2 on usage errors, and the refusal code must stay distinct.)

The backend subprocess boundary is injectable (``runner`` callable) so tests
run fully offline. Each real invocation spawns a fresh backend process
(``claude -p`` / ``codex exec``): the evaluator never reuses the collector's
backend session or context (R2).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from collections.abc import Callable
from datetime import date, datetime, timezone
from pathlib import Path

from pydantic import Field, ValidationError

from pipeline.claude_cli import (
    EnvelopeError,
    claude_command,
    parse_envelope,
    record_failed_call,
    record_usage,
)
from pipeline.common import append_log_line, archive_existing, atomic_write, sha256_text, to_utc_iso
from pipeline.config import core_analysis_sessions, load_config
from pipeline.contracts.base import ContractError, ContractModel, validation_error_messages
from pipeline.contracts.briefs import (
    LegacyMacroBriefMeta,
    MacroBriefMeta,
    Session,
    TickerBriefMeta,
    parse_macro_brief,
    parse_ticker_brief,
    split_frontmatter,
    validate_macro_brief_path,
    validate_ticker_brief_path,
)
from pipeline.contracts.evals import (
    SCORE_DIMENSIONS,
    EvalScores,
    FlaggedClaim,
    MacroEvalReport,
    TickerEvalReport,
    compute_verdict,
)
from pipeline.funnel_events import FunnelEvents

EVALUATOR_MODEL = "gpt-5.6-terra"
#: Evaluator identity written into the eval json for the claude backend (the
#: default). Never model-reported — the harness stamps it (R3).
CLAUDE_EVALUATOR = "claude-eval"

#: Per-backend evaluator identity for the eval json's ``evaluator`` field.
EVALUATOR_IDENTITIES = {"claude": CLAUDE_EVALUATOR, "codex": EVALUATOR_MODEL}

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_BACKEND_FAILURE = 3
#: R1 structural refusal — distinct from every other failure, including
#: argparse's usage-error exit 2 (a CLI typo must never read as a collector bug).
EXIT_REFUSED = 4

#: Worst-case backend calls per evaluation: the initial run plus one
#: schema-errors-appended retry (the strict-JSON retry path).
BACKEND_ATTEMPTS = 2
#: Share of the component budget reserved for parsing, validation, and the
#: atomic eval-json write around the backend calls.
HEADROOM_SECONDS = 120.0
#: Never squeeze an evaluation attempt below this, however small the budget.
MIN_BACKEND_TIMEOUT_SECONDS = 300.0


def backend_timeout_seconds() -> float:
    """Per-attempt backend timeout for the real runners.

    Sized so the worst case (``BACKEND_ATTEMPTS`` backend calls) plus the
    non-backend tail fit inside the orchestrator's ``macro_evaluator``
    component budget (config ``component_timeouts``, default 1200s, env
    ``TRADINGAGENTS_TIMEOUT_MACRO_EVALUATOR``) — the tighter of the two
    budgets the evaluator can run under, so standalone ticker evaluations are
    covered too. A flat inner timeout equal to or above the outer budget
    would let the orchestrator SIGKILL the process group while the first hung
    backend was still inside its own timeout, losing the exit-3 classification
    (and the retry) to a bare component ``timeout`` — the same derivation the
    collectors use (``pool_builder.backend_timeout_seconds``).
    """
    budget = float(load_config().component_timeouts.get("macro_evaluator", 1200))
    share = (budget - HEADROOM_SECONDS) / BACKEND_ATTEMPTS
    return max(MIN_BACKEND_TIMEOUT_SECONDS, share)

#: Collector R7's second half: when this is set, the macro eval json is
#: mirrored next to its brief in S3 after the local write.
S3_URI_ENV = "MACRO_BRIEF_S3_URI"
S3_TIMEOUT_SECONDS = 300.0

#: Injectable subprocess boundary: takes the evaluation prompt, returns the
#: model's raw text output. Tests fake it; production uses :func:`codex_runner`.
Runner = Callable[[str], str]

#: Injectable S3 boundary: (local file, MACRO_BRIEF_S3_URI).
S3Copy = Callable[[Path, str], None]

_LEGACY_MACRO_NAME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}\.md$")


class BackendError(RuntimeError):
    """The evaluation backend failed (subprocess error or unusable output)."""


# ---------------------------------------------------------------------------
# Backend runner (the injectable subprocess boundary)
# ---------------------------------------------------------------------------


def codex_runner(prompt: str) -> str:
    """Evaluator R2 backend: fresh ``codex exec`` with web search enabled.

    A new subprocess per evaluation guarantees independence from the
    collector's backend session/context. The prompt goes over stdin so brief
    content never hits the process argument list.
    """
    command = [
        "codex",
        "exec",
        "--model",
        EVALUATOR_MODEL,
        "-c",
        "tools.web_search=true",
        "--skip-git-repo-check",
        "-",
    ]
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
        raise BackendError("codex CLI not found on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise BackendError(f"codex exec timed out after {int(timeout)}s") from exc
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()[:500]
        raise BackendError(f"codex exec failed (exit {proc.returncode}): {detail}")
    return proc.stdout


def claude_runner(prompt: str) -> str:
    """Evaluator R2 claude backend: fresh ``claude -p`` with WebSearch/WebFetch.

    Same contract as :func:`codex_runner`: a new subprocess per evaluation
    (independence from the collector's backend session), prompt over stdin,
    same timeout class. Claude's result envelope is unwrapped here; the shared
    judgment path remains backend-neutral and parses only the judgment object.
    """
    schema = json.dumps(_ModelJudgment.model_json_schema(), separators=(",", ":"))
    config = load_config()
    model, effort = config.claude_eval_model, config.claude_eval_effort
    command = claude_command(model, effort, "--json-schema", schema)
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
        raise BackendError("claude CLI not found on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise BackendError(f"claude -p timed out after {int(timeout)}s") from exc
    if proc.returncode != 0:
        record_failed_call("evaluator", model, effort, proc.stdout)
        detail = (proc.stderr or proc.stdout or "").strip()[:500]
        raise BackendError(f"claude -p failed (exit {proc.returncode}): {detail}")
    try:
        envelope = parse_envelope(proc.stdout)
    except EnvelopeError as exc:
        record_failed_call("evaluator", model, effort, proc.stdout)
        raise BackendError(str(exc)) from exc
    structured_output = envelope.get("structured_output")
    if not isinstance(structured_output, dict):
        record_usage("evaluator", model, effort, envelope, outcome="error")
        raise BackendError("claude -p result missing structured_output object")
    record_usage("evaluator", model, effort, envelope)
    return json.dumps(structured_output)


#: Production runner per backend; tests inject fakes through ``runner=``.
BACKEND_RUNNERS: dict[str, Runner] = {"claude": claude_runner, "codex": codex_runner}


# ---------------------------------------------------------------------------
# Model-output schema (scores + flags + notes ONLY — identity is never
# model-reported; parse_lenient drops any extra keys the model invents,
# including a self-declared verdict)
# ---------------------------------------------------------------------------


class _ModelJudgment(ContractModel):
    scores: EvalScores
    flagged_claims: list[FlaggedClaim] = Field(default_factory=list)
    notes: str = ""


class _LegacyMacroEvalReport(MacroEvalReport):
    """Eval report for a legacy v1 (session-less) macro brief.

    Private to this module: the shared contracts package has no legacy eval
    model (flagged for a future contracts change). Serialized without the
    ``session`` key, mirroring how legacy briefs omit it.
    """

    session: Session | None = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Prompt construction (the spec's five dimensions)
# ---------------------------------------------------------------------------

_JSON_INSTRUCTIONS = """\
Respond with STRICT JSON only — no markdown fences, no prose before or after — with exactly
this shape and nothing else:
{"scores": {"factual_accuracy": 0.0, "citation_support": 0.0, "coverage": 0.0,
            "timeliness": 0.0, "consistency": 0.0},
 "flagged_claims": [{"section": "...", "claim": "...", "issue": "...",
                     "severity": "minor|major|fabrication"}],
 "notes": "free text: coverage misses, methodology caveats"}
All scores are 0-10 floats. Do NOT include a verdict, hashes, dates, or any other keys:
identity fields and the verdict are computed by the harness and anything extra you report
is discarded."""


def build_eval_prompt(
    kind: str, meta: MacroBriefMeta | TickerBriefMeta, brief_text: str
) -> str:
    """Evaluation prompt: five scored dimensions embedding the brief verbatim."""
    date = meta.as_of_date.isoformat()
    if kind == "ticker":
        assert isinstance(meta, TickerBriefMeta)
        subject = f"single-ticker research brief for {meta.ticker} (as of {date})"
        coverage_target = f"what moved {meta.ticker} in the 48h before {date}"
        consistency_extra = (
            f" For this ticker brief, consistency includes catalyst_score inflation: the "
            f"frontmatter self-reports catalyst_score={meta.catalyst_score}; a "
            f"catalyst_score >= 7 with no dated catalyst inside ~2 weeks of {date} in the "
            f"body is inflation — flag it with severity 'major'."
        )
    else:
        session = f", session {meta.session}" if meta.session else ""
        subject = f"daily global macro brief (as of {date}{session})"
        coverage_target = f"what moved global markets in the 48h before {date}"
        consistency_extra = ""
    return f"""\
You are an independent accuracy evaluator for a {subject}. You have web search
enabled: verify claims against independent sources — never by trusting the brief's own
citations alone. Score the brief on five dimensions, each a 0-10 float:

1. factual_accuracy — spot-check concrete numbers and events against independent
   searches, not just the brief's own citations.
2. citation_support — fetch a sample (at least 5, always including the most
   market-moving claims) of cited URLs and verify each supports the claim it anchors.
   An unreachable URL is a 'minor' flag; a URL that contradicts or does not contain the
   claim is a 'major' flag; a fabricated-looking source is a 'fabrication' flag.
3. coverage — run an independent "{coverage_target}" search; score down for obvious
   misses and list them in notes.
4. timeliness — is the content actually about the trailing 48h/week as of {date}?
5. consistency — internal contradictions between sections or Impact lines.{consistency_extra}

{_JSON_INSTRUCTIONS}

--- BRIEF UNDER EVALUATION (verbatim, including frontmatter) ---
{brief_text}"""


# ---------------------------------------------------------------------------
# Model-output parsing (strict JSON, one retry with the validation errors)
# ---------------------------------------------------------------------------


def _extract_json(raw: str) -> dict:
    candidates = [raw.strip()]
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.DOTALL)
    if fence:
        candidates.append(fence.group(1))
    start, end = raw.find("{"), raw.rfind("}")
    if start != -1 and end > start:
        candidates.append(raw[start : end + 1])
    for candidate in candidates:
        try:
            data = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            return data
    raise ValueError("no JSON object found in model output")


def _parse_judgment(raw: str) -> _ModelJudgment:
    """Raises :class:`ContractError` (with the error list) on schema failure."""
    try:
        data = _extract_json(raw)
    except ValueError as exc:
        raise ContractError([str(exc)]) from exc
    try:
        # Lenient: unknown keys (model-claimed verdict, identity fields) are
        # dropped with a warning — they are NEVER trusted (R3 identity rule).
        return _ModelJudgment.parse_lenient(data)
    except ValidationError as exc:
        raise ContractError(validation_error_messages(exc)) from exc


def _run_judgment(runner: Runner, prompt: str) -> _ModelJudgment:
    raw = runner(prompt)
    try:
        return _parse_judgment(raw)
    except ContractError as first:
        retry_prompt = (
            f"{prompt}\n\nYour previous response was rejected by schema validation:\n"
            + "\n".join(f"- {error}" for error in first.errors)
            + "\nReturn ONLY the corrected strict-JSON object described above."
        )
        raw = runner(retry_prompt)
        try:
            return _parse_judgment(raw)
        except ContractError as second:
            raise BackendError(
                "model output failed schema validation twice: " + "; ".join(second.errors)
            ) from second


# ---------------------------------------------------------------------------
# Brief kind detection + structural gate (R1)
# ---------------------------------------------------------------------------


def detect_kind(text: str) -> str:
    """``ticker`` when the frontmatter carries a ``ticker`` field, else ``macro``."""
    data, _, _ = split_frontmatter(text)
    return "ticker" if isinstance(data, dict) and "ticker" in data else "macro"


def _structural_check(
    kind: str, path: Path, text: str
) -> MacroBriefMeta | TickerBriefMeta:
    """Contract hard requirements only (``structural_only=True``) + the
    filename cross-check. Raises :class:`ContractError` — the R1 refusal."""
    if kind == "ticker":
        meta, _ = parse_ticker_brief(text, structural_only=True)
        validate_ticker_brief_path(path, meta)
        return meta
    legacy = bool(_LEGACY_MACRO_NAME_RE.match(path.name))
    meta, _ = parse_macro_brief(text, structural_only=True, legacy=legacy)
    validate_macro_brief_path(path, meta)
    return meta


# ---------------------------------------------------------------------------
# Report assembly + write
# ---------------------------------------------------------------------------


def _build_report(
    kind: str,
    meta: MacroBriefMeta | TickerBriefMeta,
    judgment: _ModelJudgment,
    brief_sha256: str,
    evaluated_at: datetime,
    evaluator_id: str,
) -> MacroEvalReport | TickerEvalReport:
    """Identity fields come from the brief meta / file content — never from the
    model; the verdict is recomputed via ``compute_verdict`` (R3/R4).
    ``evaluator_id`` is the invoked backend's harness-stamped identity."""
    common = {
        "as_of_date": meta.as_of_date,
        "brief_sha256": brief_sha256,
        "brief_generated_at": meta.generated_at,
        "evaluator": evaluator_id,
        "evaluated_at": evaluated_at,
        "scores": judgment.scores,
        "flagged_claims": judgment.flagged_claims,
        "verdict": compute_verdict(judgment.scores, judgment.flagged_claims),
        "notes": judgment.notes,
    }
    if kind == "ticker":
        assert isinstance(meta, TickerBriefMeta)
        return TickerEvalReport(ticker=meta.ticker, session=meta.session, **common)
    if isinstance(meta, LegacyMacroBriefMeta) and meta.session is None:
        return _LegacyMacroEvalReport(**common)
    return MacroEvalReport(session=meta.session, **common)


def eval_path_for(brief_path: Path) -> Path:
    """``YYYY-MM-DD.<session>.md`` → ``YYYY-MM-DD.<session>.eval.json`` (same
    rule covers ticker and legacy names)."""
    return brief_path.parent / f"{brief_path.stem}.eval.json"


def _existing_eval_sha(eval_path: Path) -> tuple[str | None, str | None]:
    """(brief_sha256, evaluated_at) of the eval on disk; (None, None) when
    absent or unreadable — unreadable means stale, i.e. re-evaluate."""
    try:
        data = json.loads(eval_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None, None
    if not isinstance(data, dict):
        return None, None
    sha = data.get("brief_sha256")
    evaluated_at = data.get("evaluated_at")
    return (
        sha if isinstance(sha, str) else None,
        evaluated_at if isinstance(evaluated_at, str) else None,
    )


def _archive_stale_eval(eval_path: Path, evaluated_at: str | None, now: datetime) -> None:
    """Contract archive-before-replace rule; the displaced revision keeps its
    own ``evaluated_at`` in the archived name (falls back to now)."""
    archive_existing(eval_path, evaluated_at or to_utc_iso(now))


def default_s3_copy(path: Path, uri: str) -> None:
    """Copy a file to ``$MACRO_BRIEF_S3_URI`` via the aws CLI.

    Private mirror of the collector's helper — flagged as a shared-helper
    candidate for ``pipeline.common`` (off-limits during the concurrent build).
    """
    dest = f"{uri.rstrip('/')}/{path.name}"
    proc = subprocess.run(
        ["aws", "s3", "cp", str(path), dest],
        capture_output=True,
        text=True,
        timeout=S3_TIMEOUT_SECONDS,
    )
    if proc.returncode != 0:
        lines = [line for line in (proc.stderr or proc.stdout or "").splitlines() if line.strip()]
        detail = lines[0].strip() if lines else f"exit {proc.returncode}"
        raise RuntimeError(f"aws s3 cp failed: {detail}")


def _maybe_s3_sync(kind: str, target: Path, s3_copy: S3Copy | None) -> None:
    """Mirror a macro eval json to S3 (collector R7: '… and later its eval
    json'). Ticker evals are out of scope: no ticker S3 contract exists, and a
    flat mirror would collide on the date-only ticker eval filenames. Failure
    is a warning — local remains the source of truth."""
    if kind != "macro":
        return
    uri = os.environ.get(S3_URI_ENV, "").strip()
    if not uri:
        return
    try:
        (s3_copy or default_s3_copy)(target, uri)
    except Exception as exc:
        print(f"warning: S3 sync of {target.name} to {uri} failed: {exc}", file=sys.stderr)


def _macro_brief_dir() -> Path:
    """R6 log location (env-resolution mirrors the macro data contract)."""
    override = os.environ.get("TRADINGAGENTS_MACRO_BRIEF_DIR")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".tradingagents" / "macro_briefs"


def _min_score(scores: EvalScores) -> float:
    return min(getattr(scores, dimension) for dimension in SCORE_DIMENSIONS)


def _print_flagged_claims(report: MacroEvalReport | TickerEvalReport, brief_name: str) -> None:
    print(f"verdict fail for {brief_name} — flagged claims:", file=sys.stderr)
    for claim in report.flagged_claims:
        print(
            f"  - [{claim.severity}] {claim.section}: {claim.claim} — {claim.issue}",
            file=sys.stderr,
        )


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def evaluate_brief(
    brief_path: Path,
    *,
    force: bool = False,
    backend: str | None = None,
    runner: Runner | None = None,
    s3_copy: S3Copy | None = None,
) -> int:
    """Evaluate one brief file; returns the process exit code.

    ``backend`` ``None`` resolves from the shared config's ``eval_backend``
    (env ``TRADINGAGENTS_EVAL_BACKEND``, default ``claude`` per D19/D20); an
    explicit value always wins. The backend picks the production runner and
    the harness-stamped ``evaluator`` identity; an injected ``runner`` (tests)
    replaces only the subprocess boundary.
    """
    config = load_config()
    if backend is None:
        backend = config.eval_backend
    if backend not in EVALUATOR_IDENTITIES:
        print(
            f"unknown eval backend '{backend}' — expected one of "
            f"{sorted(EVALUATOR_IDENTITIES)}",
            file=sys.stderr,
        )
        return EXIT_ERROR
    if backend == config.collect_backend and (
        backend != "claude" or config.claude_eval_model == config.claude_collect_model
    ):
        # Evaluator R2 independence: warn loudly, never fail — the run is
        # still an evaluation, just a same-model one. D20: claude on both
        # roles stays independent while the pinned models differ.
        print(
            f"warning: eval backend '{backend}' matches collect_backend on the "
            "same model — collector/evaluator independence (R2) is degraded; keep "
            "the eval and collect backends (or claude models) apart",
            file=sys.stderr,
        )
    evaluator_id = EVALUATOR_IDENTITIES[backend]
    runner = runner or BACKEND_RUNNERS[backend]
    try:
        text = brief_path.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"cannot read brief: {exc}", file=sys.stderr)
        return EXIT_ERROR

    kind = detect_kind(text)
    try:
        meta = _structural_check(kind, brief_path, text)
    except ContractError as exc:
        print(f"refusing structurally invalid {kind} brief {brief_path.name}:", file=sys.stderr)
        for error in exc.errors:
            print(f"  - {error}", file=sys.stderr)
        return EXIT_REFUSED

    brief_sha256 = sha256_text(text)
    eval_path = eval_path_for(brief_path)
    existing_sha, existing_evaluated_at = _existing_eval_sha(eval_path)
    if existing_sha == brief_sha256 and not force:
        # R3 hash-scoped idempotency: this exact revision is already evaluated.
        print(
            f"eval up to date for {brief_path.name} (brief_sha256 match) — skipping",
            file=sys.stderr,
        )
        return EXIT_OK

    prompt = build_eval_prompt(kind, meta, text)
    try:
        judgment = _run_judgment(runner, prompt)
    except BackendError as exc:
        print(f"backend failure: {exc}", file=sys.stderr)
        return EXIT_BACKEND_FAILURE

    now = datetime.now(timezone.utc)
    report = _build_report(kind, meta, judgment, brief_sha256, now, evaluator_id)
    payload = report.model_dump(mode="json")
    if payload.get("session") is None:
        payload.pop("session", None)  # legacy v1 briefs carry no session anywhere

    if eval_path.exists():
        _archive_stale_eval(eval_path, existing_evaluated_at, now)
    atomic_write(eval_path, json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
    _maybe_s3_sync(kind, eval_path, s3_copy)

    subject = meta.ticker if isinstance(meta, TickerBriefMeta) else (meta.session or "-")
    append_log_line(
        _macro_brief_dir() / "evaluator.log",
        meta.as_of_date.isoformat(),
        subject,
        report.verdict,
        _min_score(report.scores),
        len(report.flagged_claims),
    )

    if report.verdict == "fail":
        # R5: the component worked; the content failed — exit 0 either way,
        # the orchestrator reads the verdict from the eval json.
        _print_flagged_claims(report, brief_path.name)
    print(f"wrote {eval_path} (verdict {report.verdict})")
    return EXIT_OK


#: Concurrent backend evaluations in a batch — same cap class as ticker
#: collection (orchestrator R1: the evaluator fan-out shares the collection
#: concurrency cap).
TICKER_EVAL_CONCURRENCY = 3
#: Mirrors ``pipeline.analysis_runner.TRIGGER_THRESHOLD_ENV`` (hoist candidate
#: for pipeline/config.py) — the batch gates opportunity briefs on the same
#: threshold the runner triggers on (evaluator spec "Scope" quota policy).
TRIGGER_THRESHOLD_ENV = "TRADINGAGENTS_TRIGGER_THRESHOLD"
DEFAULT_TRIGGER_THRESHOLD = 7.0


def _batch_catalyst_score(text: str) -> float | None:
    """Frontmatter catalyst_score, or None when the brief is structurally
    broken (the batch still evaluates it so the refusal surfaces the collector
    bug — refusal happens before any backend spend)."""
    try:
        meta, _ = parse_ticker_brief(text, structural_only=True)
    except ContractError:
        return None
    return float(meta.catalyst_score)


def batch_tickers(
    session: str,
    *,
    date_str: str | None = None,
    force: bool = False,
    backend: str | None = None,
    runner: Runner | None = None,
    funnel: FunnelEvents | None = None,
) -> int:
    """Orchestrator step 5: evaluate the day's ticker briefs per the quota
    policy — every collected core brief, plus every opportunity brief whose
    ``catalyst_score`` ≥ the analysis trigger threshold (those are about to
    influence — and possibly execute — a run). Per-brief isolation: one
    failure never aborts the rest; the final stdout line is a JSON summary
    for the orchestrator's outcome mapper.
    """
    from concurrent.futures import ThreadPoolExecutor

    from pipeline.common import session_date
    from pipeline.contracts.pool import read_pool
    from pipeline.ticker_collector import CollectorError, read_core_yaml

    config = load_config()
    day = date.fromisoformat(date_str) if date_str else session_date(session)
    threshold = float(os.environ.get(TRIGGER_THRESHOLD_ENV) or DEFAULT_TRIGGER_THRESHOLD)

    core: list[str] = []
    opportunity: list[str] = []
    result = read_pool(config.pool_dir, session, day)
    if result.staleness == "absent" or result.pool is None:
        try:
            core = read_core_yaml(Path(config.pool_dir), session)
        except CollectorError as exc:
            print(f"evaluator: {exc}", file=sys.stderr)
            return EXIT_ERROR
        print(
            "evaluator: warning: no usable pool file — core coverage from "
            f"core.{session}.yaml only",
            file=sys.stderr,
        )
    else:
        core = [entry.ticker for entry in result.pool.core]
        opportunity = [
            entry.ticker for entry in result.pool.opportunity if entry.ticker not in set(core)
        ]
    if session not in core_analysis_sessions():
        # specs/llm-cost.md R8: core briefs of a catalyst-only session are
        # neither collected nor evaluated.
        core = []

    events = funnel or FunnelEvents(config.state_dir, session, day, "ticker_evaluators")
    jobs: list[Path] = []
    #: brief path -> (ticker, layer) for the funnel events
    job_tickers: dict[Path, tuple[str, str]] = {}
    absent = 0
    below_threshold = 0
    brief_root = Path(config.ticker_brief_dir)
    for ticker in core:
        path = brief_root / ticker / f"{day.isoformat()}.md"
        if path.is_file():
            jobs.append(path)
            job_tickers[path] = (ticker, "core")
        else:
            absent += 1
            events.emit("evaluate", ticker, "info", "skipped-absent", "no brief to evaluate")
    for ticker in opportunity:
        path = brief_root / ticker / f"{day.isoformat()}.md"
        if not path.is_file():
            absent += 1
            events.emit("evaluate", ticker, "excluded", "skipped-absent", "no brief to evaluate")
            continue
        score = _batch_catalyst_score(path.read_text(encoding="utf-8"))
        if score is not None and score < threshold:
            below_threshold += 1
            events.emit(
                "evaluate", ticker, "excluded", "skipped-below-threshold",
                f"brief catalyst_score {score:g} < trigger {threshold:g}",
                score=score, threshold=threshold,
            )
            continue
        jobs.append(path)
        job_tickers[path] = (ticker, "opportunity")

    counts = {"ok": 0, "refused": 0, "backend_failed": 0, "errors": 0}
    verdicts: dict[str, int] = {}

    def _evaluate(path: Path) -> None:
        rc = evaluate_brief(path, force=force, backend=backend, runner=runner)
        ticker, layer = job_tickers[path]
        if rc == EXIT_OK:
            counts["ok"] += 1
            flagged = None
            try:
                payload = json.loads(eval_path_for(path).read_text(encoding="utf-8"))
                verdict = str(payload.get("verdict", "unknown"))
                flagged = len(payload.get("flagged_claims") or [])
            except (OSError, ValueError, AttributeError):
                verdict = "unknown"
            verdicts[verdict] = verdicts.get(verdict, 0) + 1
            if verdict in ("pass", "warn"):
                events.emit(
                    "evaluate", ticker, "passed", verdict, f"eval {verdict}",
                    flagged_claims=flagged,
                )
            elif verdict == "fail":
                # Core still runs (brief withheld); an opportunity stops here.
                events.emit(
                    "evaluate", ticker, "info" if layer == "core" else "excluded", "fail",
                    "eval fail" + (" — brief withheld" if layer == "core" else ""),
                    flagged_claims=flagged,
                )
            else:
                events.emit("evaluate", ticker, "info", "eval-error", "eval verdict unreadable")
            return
        if rc == EXIT_REFUSED:
            counts["refused"] += 1
            why = "refused"
        elif rc == EXIT_BACKEND_FAILURE:
            counts["backend_failed"] += 1
            why = "backend failed"
        else:
            counts["errors"] += 1
            why = f"exit {rc}"
        events.emit("evaluate", ticker, "info", "eval-error", f"evaluation {why}")

    if jobs:
        with ThreadPoolExecutor(max_workers=min(TICKER_EVAL_CONCURRENCY, len(jobs))) as pool:
            for future in [pool.submit(_evaluate, path) for path in jobs]:
                try:
                    future.result()
                except Exception as exc:  # noqa: BLE001 — per-brief isolation
                    counts["errors"] += 1
                    print(f"evaluator: batch job crashed: {exc}", file=sys.stderr)

    print(
        json.dumps(
            {
                "entrypoint": "batch_tickers",
                "date": day.isoformat(),
                "session": session,
                "requested": len(jobs),
                "absent": absent,
                "below_threshold": below_threshold,
                **counts,
                "verdicts": verdicts,
            }
        )
    )
    attempted = len(jobs)
    wipeout = attempted > 0 and counts["ok"] == 0 and (
        counts["backend_failed"] + counts["errors"] == attempted
    )
    return EXIT_ERROR if wipeout else EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m pipeline.evaluator",
        description="Score a macro or ticker brief for accuracy (claude -p, or GPT-5.6 "
        "Terra via codex exec) and write its *.eval.json per the brief's data contract.",
    )
    parser.add_argument(
        "brief",
        nargs="?",
        help="path to the brief file (kind detected from frontmatter); omit with --batch-tickers",
    )
    parser.add_argument(
        "--batch-tickers",
        action="store_true",
        help="orchestrator step 5: evaluate the day's ticker briefs (core ∪ "
        "would-trigger opportunity) for --session/--date",
    )
    parser.add_argument("--session", choices=("cn", "us"), help="session for --batch-tickers")
    parser.add_argument("--date", help="YYYY-MM-DD for --batch-tickers (default: session today)")
    parser.add_argument(
        "--backend",
        choices=tuple(EVALUATOR_IDENTITIES),
        default=None,
        help="evaluation backend (default: config eval_backend — claude per D20, "
        "env TRADINGAGENTS_EVAL_BACKEND)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="re-evaluate even when the existing eval matches the brief hash",
    )
    return parser


def main(
    argv: list[str] | None = None,
    runner: Runner | None = None,
    s3_copy: S3Copy | None = None,
) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.batch_tickers:
        if args.brief is not None:
            parser.error("--batch-tickers takes no brief path")
        if not args.session:
            parser.error("--batch-tickers requires --session")
        return batch_tickers(
            args.session,
            date_str=args.date,
            force=args.force,
            backend=args.backend,
            runner=runner,
        )
    if args.brief is None:
        parser.error("a brief path is required (or use --batch-tickers)")
    return evaluate_brief(
        Path(args.brief),
        force=args.force,
        backend=args.backend,
        runner=runner,
        s3_copy=s3_copy,
    )


if __name__ == "__main__":
    raise SystemExit(main())
