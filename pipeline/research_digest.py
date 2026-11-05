"""Research digest for the Lark trades group (specs/research-digest.md).

One message per slot, after its end-of-slot summary, in Chinese: positions
and planned orders, the macro read, then every ticker grouped by outcome —
analysed (the action in plain words, never the internal HOLD), blocked
(a failed brief), and not triggered (one line per reason). Analysed and
blocked names, and the macro brief, get a one-sentence Chinese summary from
one small model call (DIGEST_SUMMARY_MODEL); without it the digest still
goes out. Everything else comes from files the slot already wrote (the
funnel snapshot, the briefs and evals, the ledger, the trades book).
Delivered through the trades outbox (:func:`pipeline.trade_notify.enqueue`)
under the key ``research:<session>:<date>``, so a re-run or the us
execution reopen never sends it twice and it is never sent ahead of a
queued trade message.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
from datetime import date
from pathlib import Path
from typing import Any

from pipeline.common import session_for_ticker
from pipeline.config import PipelineConfig
from pipeline.contracts.briefs import MACRO_IMPACT_SECTIONS, split_frontmatter

logger = logging.getLogger("pipeline.research_digest")

SESSION_NAMES = {"cn": "A股/港股", "us": "美股"}
IMPACT_NAMES = {"bullish": "偏多", "bearish": "偏空", "neutral": "中性", "mixed": "分歧"}
MACRO_SECTION_NAMES = dict(zip(MACRO_IMPACT_SECTIONS, ("利率", "增长", "地缘", "流动性", "大宗", "中国/亚洲"), strict=True))
CATALYST_NAMES = {"earnings": "财报", "product": "产品", "regulatory": "监管", "M&A": "并购",
                  "guidance": "指引", "flow": "资金", "macro-exposure": "宏观"}
RATING_NAMES = {"Buy": "强烈看多", "Overweight": "看多", "Hold": "中性", "Underweight": "看淡", "Sell": "看空"}
#: Not-triggered reasons, in display order (funnel trigger-stage codes).
SKIP_REASONS = (
    ("core-unchanged", "近几日已分析且无新变化，不重复"),
    ("below-threshold", "机会池分数 <7，未评估"),
    ("core-session-off", "A股核心股本时段不分析"),
)
#: The summary call: one small, cheap model; a tight timeout so the digest
#: never waits long for it (DIGEST_DEADLINE_S in the orchestrator bounds all).
DIGEST_SUMMARY_MODEL = "claude-haiku-5-5"
DIGEST_SUMMARY_EFFORT = "low"
DIGEST_SUMMARY_TIMEOUT_S = 45.0
#: Brief text handed to the summary per ticker.
SUMMARY_INPUT_CHARS = 1500

_HEADING = re.compile(r"^##[ \t]+", re.MULTILINE)
_IMPACT = re.compile(r"^\*\*Impact\*\*:\s*([A-Za-z]+)\s*(?:[—–-]+\s*(.*))?$")
_WINDOW_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


def digest_key(session: str, slot_date: date) -> str:
    return f"research:{session}:{slot_date.isoformat()}"


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _last_impact(body: str) -> tuple[str, str] | None:
    """``(direction in Chinese, rationale)`` of a section's final Impact line."""
    lines = [line for line in body.splitlines() if line.strip()]
    match = _IMPACT.match(lines[-1].strip()) if lines else None
    if not match:
        return None
    word = match.group(1).lower()
    return IMPACT_NAMES.get(word, word), " ".join((match.group(2) or "").split())


def _sections(body: str) -> dict[str, str]:
    """``title -> text`` by the contract's heading rule (a line ``## Title``)."""
    out = {}
    for chunk in _HEADING.split(body)[1:]:
        title, _, section = chunk.partition("\n")
        out[title.strip()] = section
    return out


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------


def macro_read(config: PipelineConfig, session: str, slot_date: date) -> tuple[str, str | None]:
    """``(macro line, text for the summary)``."""
    from pipeline.funnel import eval_verdict

    brief = config.macro_brief_dir / f"{slot_date.isoformat()}.{session}.md"
    text = _read(brief)
    if text is None:
        return "宏观：无简报", None
    _meta, body, _errors = split_frontmatter(text)
    sections = _sections(body)
    reads = [(name, impact[0]) for title, name in MACRO_SECTION_NAMES.items()
             if (impact := _last_impact(sections.get(title, ""))) is not None]
    verdict = eval_verdict(config, brief)
    head = f"宏观（评估 {verdict}）" if verdict != "missing" else "宏观（未评估）"
    words = {word for _name, word in reads}
    if not reads:
        line = f"{head}：无影响判断"
    elif len(reads) == len(MACRO_SECTION_NAMES) and len(words) == 1:
        line = f"{head}：{len(reads)} 个板块均为{words.pop()}"
    else:
        line = f"{head}：" + " · ".join(f"{name} {word}" for name, word in reads)
    impacts = [f"{title}: {impact[1]}" for title in MACRO_SECTION_NAMES
               if (impact := _last_impact(sections.get(title, ""))) is not None and impact[1]]
    watch = " ".join(sections.get("Surprises & Watchlist", "").split())[:SUMMARY_INPUT_CHARS]
    return line, "\n".join(impacts + ([f"Surprises & Watchlist: {watch}"] if watch else []))


def _held(config: PipelineConfig) -> dict[str, dict[str, Any]] | None:
    """The trades book's open positions; None when there is no book."""
    from pipeline.trade_notify import Entries

    book = Entries(config.state_dir)
    return book.load() if book.path.exists() else None


def _plans(config: PipelineConfig) -> dict[str, dict[str, Any]]:
    """run_id -> plan of the ledger's decisions (for the summary input)."""
    out: dict[str, dict[str, Any]] = {}
    text = _read(config.ledger_dir / "decisions.jsonl") or ""
    for line in text.splitlines()[-500:]:
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict) and isinstance(row.get("plan"), dict):
            out[str(row.get("run_id"))] = row["plan"]
    return out


def _decide_event(stage: dict[str, Any] | None) -> dict[str, Any] | None:
    """The decision of the arm that trades: a paired ticker's decide stage
    keeps its strongest arm, so a feeds BUY would hide the brief arm's HOLD
    (specs/decision-funnel.md) — the brief arm's latest event is read when
    there is one."""
    if not stage:
        return None
    if not isinstance(stage, dict):
        raise TypeError("decide stage")

    def shaped(event: dict[str, Any]) -> dict[str, Any]:
        details = event.get("details")
        return {**event, "details": details if isinstance(details, dict) else {}}

    events = [shaped(event) for event in stage.get("events") or [] if isinstance(event, dict)]
    brief = [event for event in events if event["details"].get("arm") == "brief"]
    return brief[-1] if brief else shaped(stage)


def action_text(event: dict[str, Any], *, session: str, held: bool, execution_time: str | None) -> str:
    """The decision in plain words. HOLD only means "no order": with no
    position it is "不买", never "持有"; a BUY/SELL that cannot become an
    order says so."""
    code = event.get("code")
    details = event.get("details") or {}
    if code == "ERROR":
        return "分析出错"  # never the error text: it carries API error bodies
    rating = details.get("pm_rating")
    view = RATING_NAMES.get(str(rating), str(rating)) if rating else None
    pm = f"PM {view}" if view else None
    when = f"{execution_time} ET 下单" if execution_time else "下单"
    no_order = (session == "cn" or details.get("arm") == "feeds" or details.get("plan_valid") is False
                or details.get("execute_eligible") is False)
    why_not = ("A股不下单" if session == "cn" else "对照分析，不下单" if details.get("arm") == "feeds"
               else "计划无效，不下单" if details.get("plan_valid") is False else "不满足下单条件")

    def paren(*parts: str | None) -> str:
        kept = [part for part in parts if part]
        return f"（{'；'.join(kept)}）" if kept else ""

    if code == "BUY":
        if no_order:
            return "看多，不下单" + paren(pm, why_not)
        return ("加仓" if held else "买入") + paren(pm, when)
    if code == "SELL":
        if not held:
            return "不买" + paren(pm, "无持仓，不做空")
        if no_order:
            return "看空，不下单" + paren(pm, why_not)
        return "卖出" + paren(pm, when)
    if code == "HOLD":
        if held:
            return "继续持有" + paren(pm)
        bearish = rating in ("Underweight", "Sell")
        return "不买" + paren(pm, "无持仓，不做空" if bearish else None)
    return "无结论"


def _catalyst(meta: dict[str, Any], slot_date: date) -> str:
    """Type and window in Chinese; a window wholly before the slot date is
    stale (the event already happened) and dropped with its type."""
    window = str(meta.get("catalyst_window") or "")
    days = _WINDOW_DATE.findall(window)
    if days and max(days) < slot_date.isoformat():
        return ""
    kind = CATALYST_NAMES.get(str(meta.get("catalyst_type") or ""), "")
    span = "~".join(day[5:] for day in days)
    return " ".join(part for part in (kind, span) if part)


class _Ticker:
    def __init__(self, ticker: str, stages: dict[str, Any], brief: str | None, slot_date: date) -> None:
        self.ticker = ticker
        self.stages = stages
        meta, body, _errors = split_frontmatter(brief or "")
        meta = meta or {}
        try:
            self.score: float | None = float(meta.get("catalyst_score"))
        except (TypeError, ValueError):
            self.score = None
        self.catalyst = _catalyst(meta, slot_date)
        self.has_brief = brief is not None
        self.body = body if brief is not None else ""
        self.decide = _decide_event(stages.get("decide"))
        self.trigger = (stages.get("trigger") or {}).get("code")

    def head(self) -> str:
        score = f"{self.score:.1f}" if self.score is not None else "–"
        parts = [f"• {self.ticker} {score}", self.catalyst, "" if self.has_brief else "（无简报）"]
        return " ".join(part for part in parts if part)

    def summary_input(self, plan: dict[str, Any] | None) -> str:
        sections = _sections(self.body)
        impact = _last_impact(self.body)
        parts = [f"Impact: {impact[1]}" if impact else ""]
        for title in ("Company Developments (48h)", "Catalysts & Calendar"):
            text = " ".join(sections.get(title, "").split())
            if text:
                parts.append(f"{title}: {text[:SUMMARY_INPUT_CHARS // 2]}")
        if self.decide is not None:
            rating = str((self.decide.get("details") or {}).get("pm_rating") or "")
            parts.append(f"Portfolio manager's view: {RATING_NAMES.get(rating, rating)}")
            if plan:
                parts.append(f"conviction {plan.get('conviction')}; invalidation: {plan.get('invalidation')}")
        if self.trigger == "eval-fail":
            parts.append("The brief failed its accuracy evaluation, so it was not analysed.")
        return "\n".join(part for part in parts if part)


def _tickers(config: PipelineConfig, session: str, slot_date: date) -> list[_Ticker]:
    """Every ticker of the slot's own session (a wrong-session nominee is
    skipped: brief paths carry no session) with a brief for the slot's date
    or a decision; a malformed funnel row is skipped, never the digest."""
    from pipeline.funnel import snapshot_path

    raw = _read(snapshot_path(config.state_dir, session, slot_date))
    try:
        parsed = json.loads(raw) if raw is not None else None
    except json.JSONDecodeError:
        parsed = None
    entries = (parsed if isinstance(parsed, dict) else {}).get("tickers")
    out = []
    for entry in entries if isinstance(entries, list) else []:
        try:
            ticker = str(entry.get("ticker") or "")
            if not ticker or session_for_ticker(ticker) != session:
                continue
            stages = entry.get("stages") or {}
            if not isinstance(stages, dict):
                raise TypeError("stages")
            brief = _read(config.ticker_brief_dir / ticker / f"{slot_date.isoformat()}.md")
            item = _Ticker(ticker, stages, brief, slot_date)
            if brief is None and item.decide is None:
                continue
            out.append(item)
        except Exception as exc:  # one malformed row never costs the digest
            logger.warning("research digest: skipped a malformed funnel row (%s)", type(exc).__name__)
    return out


# ---------------------------------------------------------------------------
# Chinese summaries (one small model call)
# ---------------------------------------------------------------------------

_SUMMARY_SCHEMA = {
    "type": "object",
    "properties": {
        "macro": {"type": "string"},
        "tickers": {"type": "array", "items": {
            "type": "object",
            "properties": {"ticker": {"type": "string"}, "summary": {"type": "string"}},
            "required": ["ticker", "summary"], "additionalProperties": False}},
    },
    "required": ["macro", "tickers"],
    "additionalProperties": False,
}

SUMMARY_PROMPT = """You write one-sentence Chinese summaries for a trading research digest.
Use ONLY the text given below; add no facts, numbers or opinions of your own, and do
not search. Each summary: one sentence in simplified Chinese, at most 45 Chinese
characters, plain words, the main reason first (for an analysed name: why the
portfolio manager holds that view; for a blocked name: what the brief said and that
it failed its quality check). Never write 持有, 减持, 增持, 评级 or HOLD/BUY/SELL — the
action is shown separately; say the view as 看多/中性/看淡/看空 if needed. Keep
tickers and numbers as written. For "macro": one sentence on the single most
important market driver or risk in the macro text.

{items}"""


def _run_summary(prompt: str) -> str:
    """The summary call: hermetic ``claude -p`` (ladder-routed like every
    pipeline call) with a JSON schema; returns the structured output as JSON."""
    from pipeline.claude_cli import (
        EnvelopeError,
        call_env,
        claude_command,
        parse_envelope,
        record_failed_call,
        record_usage,
        timeout_stdout,
    )

    model, effort = DIGEST_SUMMARY_MODEL, DIGEST_SUMMARY_EFFORT
    command = claude_command(model, effort, "--json-schema", json.dumps(_SUMMARY_SCHEMA), role="collect")
    try:
        proc = subprocess.run(command, input=prompt, capture_output=True, text=True,
                              timeout=DIGEST_SUMMARY_TIMEOUT_S, env=call_env(command))
    except subprocess.TimeoutExpired as exc:
        record_failed_call("research_digest", model, effort, timeout_stdout(exc))
        raise
    if proc.returncode != 0:
        record_failed_call("research_digest", model, effort, proc.stdout)
        raise RuntimeError(f"summary call failed (exit {proc.returncode})")
    try:
        envelope = parse_envelope(proc.stdout)
    except EnvelopeError:
        record_failed_call("research_digest", model, effort, proc.stdout)
        raise
    output = envelope.get("structured_output")
    record_usage("research_digest", model, effort, envelope, outcome="ok" if isinstance(output, dict) else "error")
    if not isinstance(output, dict):
        raise RuntimeError("summary call returned no structured output")
    return json.dumps(output)


def summaries(macro_text: str | None, items: dict[str, str]) -> tuple[str | None, dict[str, str]]:
    """``(macro summary, ticker -> summary)``; empty on any failure (the
    digest goes out without them)."""
    if not items and not macro_text:
        return None, {}
    blocks = [f"[macro]\n{macro_text}" if macro_text else "[macro]\n(none)"]
    blocks += [f"[{ticker}]\n{text}" for ticker, text in items.items()]
    try:
        output = json.loads(_run_summary(SUMMARY_PROMPT.format(items="\n\n".join(blocks))))
        macro = " ".join(str(output.get("macro") or "").split()) or None
        found = {str(row.get("ticker")): " ".join(str(row.get("summary") or "").split())
                 for row in output.get("tickers") or [] if isinstance(row, dict)}
        return (macro if macro_text else None), {t: s for t, s in found.items() if t in items and s}
    except Exception as exc:
        logger.warning("research digest: Chinese summaries unavailable (%s)", type(exc).__name__)
        return None, {}


# ---------------------------------------------------------------------------
# The digest
# ---------------------------------------------------------------------------


def build_digest(config: PipelineConfig, session: str, slot_date: date) -> str:
    """The digest text for one slot; never raises on a missing or partial input."""
    try:
        macro, macro_text = macro_read(config, session, slot_date)
    except Exception as exc:  # e.g. a damaged frontmatter date: the tickers still go out
        logger.warning("research digest: macro brief unreadable (%s)", type(exc).__name__)
        macro, macro_text = "宏观：简报无法读取", None
    tickers = _tickers(config, session, slot_date)
    try:
        held = _held(config)
    except Exception:
        held = None
    try:
        plans = _plans(config)
    except Exception:
        plans = {}
    execution_time = config.sessions[session].execution_time if session in config.sessions else None

    analysed = sorted((t for t in tickers if t.decide is not None), key=lambda t: (-(t.score or -1), t.ticker))
    blocked = sorted((t for t in tickers if t.decide is None and t.trigger == "eval-fail"),
                     key=lambda t: (-(t.score or -1), t.ticker))
    rest = [t for t in tickers if t.decide is None and t.trigger != "eval-fail"]

    actions = {t.ticker: action_text(t.decide, session=session, held=held is not None and t.ticker in held,
                                     execution_time=execution_time) for t in analysed}
    planned = [f"{t.ticker} {actions[t.ticker].split('（')[0]}" for t in analysed
               if actions[t.ticker].startswith(("买入", "加仓", "卖出"))]
    macro_summary, notes = summaries(macro_text, {
        t.ticker: t.summary_input(plans.get(str((t.decide or {}).get("details", {}).get("run_id"))))
        for t in analysed + blocked})

    head = [f"📊 研究摘要 · {SESSION_NAMES.get(session, session)} · {slot_date.isoformat()[5:]}"]
    if held is not None:
        head.append("持仓：" + ("、".join(sorted(held)) if held else "无"))
    head.append("A股不下单" if session == "cn" else "计划下单：" + ("、".join(planned) if planned else "无"))
    lines = [" ｜ ".join(head), macro]
    if macro_summary:
        lines.append(f"  要点：{macro_summary}")
    if analysed:
        lines += ["", f"已分析（{len(analysed)}）"]
        for t in analysed:
            lines.append(f"{t.head()} → {actions[t.ticker]}")
            if notes.get(t.ticker):
                lines.append(f"  {notes[t.ticker]}")
    if blocked:
        lines += ["", f"被拦下（{len(blocked)}）"]
        for t in blocked:
            lines.append(f"{t.head()} → 简报评估 fail，未分析")
            if notes.get(t.ticker):
                lines.append(f"  {notes[t.ticker]}")
    if rest:
        lines += ["", f"未触发（{len(rest)}）"]
        known = dict(SKIP_REASONS)
        for code, label in [*SKIP_REASONS, (None, "其他")]:
            group = sorted((t for t in rest if (t.trigger if t.trigger in known else None) == code),
                           key=lambda t: (-(t.score or -1), t.ticker))
            if group:
                names = " · ".join(f"{t.ticker} {t.score:.1f}" if t.score is not None else t.ticker for t in group)
                lines.append(f"• {label}：{names}")
    if not tickers:
        lines.append("个股：无简报")
    return "\n".join(lines)


#: A pending digest older than this is dropped (logged) instead of retried.
PENDING_MAX_DAYS = 3


def _pending_dir(config: PipelineConfig) -> Path:
    return config.state_dir / "trade_notify" / "research_pending"


def mark_pending(config: PipelineConfig, session: str, slot_date: date) -> None:
    """Record, without the trades lock, that this slot's digest is due: it
    survives a send that never got the lock (the hourly watchdog retries)."""
    path = _pending_dir(config) / f"{session}_{slot_date.isoformat()}"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()


def send_digest(config: PipelineConfig, session: str, slot_date: date) -> int:
    """Queue the slot's digest on the trades channel (once per key) and flush;
    nothing when notifications are off or the trades webhook is unset. Once
    queued its pending mark is cleared — a Lark failure afterwards is the
    outbox's to retry. Returns messages sent; never raises."""
    if not config.notifications_enabled or not config.lark_trades_webhook_url:
        return 0
    try:
        from pipeline import trade_notify
        from pipeline.trade_monitor import make_trades_sender

        text = build_digest(config, session, slot_date)
        sent = trade_notify.enqueue(config.state_dir, digest_key(session, slot_date), text,
                                    make_trades_sender(config))
        (_pending_dir(config) / f"{session}_{slot_date.isoformat()}").unlink(missing_ok=True)
        return sent
    except Exception as exc:  # never the URL: LarkError text is scrubbed
        reason = str(exc) if exc.__class__.__name__ == "LarkError" else type(exc).__name__
        logger.warning("research digest for %s %s not sent: %s", session, slot_date, reason)
        return 0


def retry_pending(config: PipelineConfig, today: date) -> int:
    """The hourly watchdog's retry of digests a slot end could not queue;
    one older than PENDING_MAX_DAYS is dropped with an error. Never raises."""
    sent = 0
    try:
        marks = sorted(_pending_dir(config).glob("*_*"))
    except OSError:
        return 0
    for mark in marks:
        session, _, day = mark.name.partition("_")
        try:
            slot_date = date.fromisoformat(day)
        except ValueError:
            mark.unlink(missing_ok=True)
            continue
        if (today - slot_date).days > PENDING_MAX_DAYS:
            logger.error("research digest for %s %s dropped: not queued within %d days",
                         session, slot_date, PENDING_MAX_DAYS)
            mark.unlink(missing_ok=True)
            continue
        sent += send_digest(config, session, slot_date)
    return sent
