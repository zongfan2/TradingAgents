"""Research digest for the Lark trades group (specs/research-digest.md).

One message per slot, after its end-of-slot summary: the macro brief's
per-section read and every ticker brief the slot wrote, with its catalyst,
its 48h impact line, its eval verdict and — when it was analysed — the
decision. Built only from files the slot already wrote (the funnel snapshot,
the briefs and their evals): no model call, no extra cost. Delivered through
the trades outbox (:func:`pipeline.trade_notify.enqueue`) under the key
``research:<session>:<date>``, so a re-run or the us execution reopen never
sends it twice and it is never sent ahead of a queued trade message.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import date
from pathlib import Path
from typing import Any

from pipeline.config import PipelineConfig
from pipeline.contracts.briefs import MACRO_IMPACT_SECTIONS, split_frontmatter

logger = logging.getLogger("pipeline.research_digest")

SESSION_NAMES = {"cn": "A股/港股", "us": "美股"}
IMPACT_NAMES = {"bullish": "偏多", "bearish": "偏空", "neutral": "中性", "mixed": "分歧"}
MACRO_SECTION_NAMES = dict(zip(MACRO_IMPACT_SECTIONS, ("利率", "增长", "地缘", "流动性", "大宗", "中国/亚洲"), strict=True))
#: A brief's impact rationale is cut to this many characters.
RATIONALE_CHARS = 110

_HEADING = re.compile(r"^##[ \t]+", re.MULTILINE)
_IMPACT = re.compile(r"^\*\*Impact\*\*:\s*([A-Za-z]+)\s*(?:[—–-]+\s*(.*))?$")


def digest_key(session: str, slot_date: date) -> str:
    return f"research:{session}:{slot_date.isoformat()}"


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _impact(line: str) -> tuple[str, str] | None:
    match = _IMPACT.match(line.strip())
    if not match:
        return None
    word = match.group(1).lower()
    rationale = " ".join((match.group(2) or "").split())
    if len(rationale) > RATIONALE_CHARS:
        rationale = rationale[: RATIONALE_CHARS - 1].rstrip() + "…"
    return IMPACT_NAMES.get(word, word), rationale


def _last_impact(body: str) -> tuple[str, str] | None:
    lines = [line for line in body.splitlines() if line.strip()]
    return _impact(lines[-1]) if lines else None


def macro_line(config: PipelineConfig, session: str, slot_date: date) -> str:
    from pipeline.funnel import eval_verdict

    brief = config.macro_brief_dir / f"{slot_date.isoformat()}.{session}.md"
    text = _read(brief)
    if text is None:
        return "宏观：无简报"
    _meta, body, _errors = split_frontmatter(text)
    reads = []
    # The contract's heading rule: any line "## Title" (the body may start
    # with one when no blank line follows the frontmatter).
    for chunk in _HEADING.split(body)[1:]:
        title, _, section = chunk.partition("\n")
        name = MACRO_SECTION_NAMES.get(title.strip())
        impact = _last_impact(section) if name else None
        if impact:
            reads.append(f"{name} {impact[0]}")
    verdict = eval_verdict(config, brief)
    head = f"宏观（评估 {verdict}）" if verdict != "missing" else "宏观（未评估）"
    return f"{head}：{' · '.join(reads) or '无影响判断'}"


def _decision(stage: dict[str, Any] | None, session: str) -> str | None:
    """The decision of the arm that trades: a paired ticker's decide stage
    keeps its strongest arm, so a feeds BUY would hide the brief arm's HOLD
    (specs/decision-funnel.md) — the brief arm's latest event is read when
    there is one. A BUY/SELL that cannot become an order says so."""
    if not stage:
        return None
    events = [event for event in stage.get("events") or [] if isinstance(event, dict)]
    brief = [event for event in events if (event.get("details") or {}).get("arm") == "brief"]
    chosen = brief[-1] if brief else stage
    code = chosen.get("code")
    details = chosen.get("details") or {}
    if code == "ERROR":
        return "出错"  # never the error text: it carries API error bodies
    if code not in ("BUY", "SELL", "HOLD"):
        return None
    rating = details.get("pm_rating")
    text = f"{code}（PM {rating}）" if rating else str(code)
    if details.get("arm") == "feeds":
        text = f"feeds 对照 {text}"
    if code != "HOLD":
        if details.get("plan_valid") is False:
            text += "，计划无效"
        # Only the brief arm trades; an absent execute_eligible (a stage
        # rebuilt from files) is unknown, not "no order".
        if session == "cn" or details.get("arm") == "feeds" or details.get("execute_eligible") is False:
            text += "，不下单"
    return text


def _verdict_text(stage: dict[str, Any] | None) -> str:
    code = (stage or {}).get("code")
    if code in ("pass", "warn", "fail"):
        return f"评估 {code}"
    return "未评估"


def ticker_line(config: PipelineConfig, entry: dict[str, Any], slot_date: date, session: str) -> tuple[tuple, str] | None:
    """Every ticker with a brief for the slot's date (written now, or
    already there on a re-fire) or a decision (a core ticker is analysed
    even when its collection failed)."""
    ticker = str(entry.get("ticker") or "")
    stages = entry.get("stages") or {}
    decision = _decision(stages.get("decide"), session)
    text = _read(config.ticker_brief_dir / ticker / f"{slot_date.isoformat()}.md") if ticker else None
    if text is None and decision is None:
        return None
    meta, body, _errors = split_frontmatter(text or "")
    meta = meta or {}
    try:
        score = float(meta.get("catalyst_score"))
    except (TypeError, ValueError):
        score = None
    catalyst = " ".join(
        str(part) for part in (meta.get("catalyst_type"), meta.get("catalyst_window"))
        if part not in (None, "", "none", "other")
    )
    impact = _last_impact(body) if text is not None else None
    parts = [f"• {ticker} {score:.1f}" if score is not None else f"• {ticker} –"]
    if text is None:
        parts.append("（无简报）")
    if catalyst:
        parts.append(catalyst)
    line = " ".join(parts)
    if impact:
        line += f" · {impact[0]}" + (f" — {impact[1]}" if impact[1] else "")
    tail = [_verdict_text(stages.get("evaluate"))]
    if decision:
        tail.append(f"分析 {decision}")
    line += "｜" + "｜".join(tail)
    # Analysed names first, then by catalyst score, then by ticker.
    order = (decision is None, -(score if score is not None else -1.0), ticker)
    return order, line


def build_digest(config: PipelineConfig, session: str, slot_date: date) -> str:
    """The digest text for one slot; never raises on a missing or partial input."""
    from pipeline.funnel import snapshot_path

    lines = [f"📊 研究摘要 · {SESSION_NAMES.get(session, session)} · {slot_date.isoformat()}",
             macro_line(config, session, slot_date)]
    snapshot: dict[str, Any] = {}
    raw = _read(snapshot_path(config.state_dir, session, slot_date))
    if raw is not None:
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            parsed = None
        snapshot = parsed if isinstance(parsed, dict) else {}
    entries = snapshot.get("tickers")
    rows = []
    for entry in entries if isinstance(entries, list) else []:
        try:
            row = ticker_line(config, entry, slot_date, session)
        except Exception as exc:  # one malformed row never costs the digest
            logger.warning("research digest: skipped a malformed funnel row (%s)", type(exc).__name__)
            continue
        if row is not None:
            rows.append(row)
    rows.sort(key=lambda row: row[0])
    analysed = sum(1 for order, _line in rows if not order[0])
    lines.append(f"个股 {len(rows)} 只，分析 {analysed} 只：" if rows else "个股：无简报")
    lines.extend(line for _order, line in rows)
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
