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
    for chunk in body.split("\n## ")[1:]:
        title, _, section = chunk.partition("\n")
        name = MACRO_SECTION_NAMES.get(title.strip())
        impact = _last_impact(section) if name else None
        if impact:
            reads.append(f"{name} {impact[0]}")
    verdict = eval_verdict(config, brief)
    head = f"宏观（评估 {verdict}）" if verdict != "missing" else "宏观（未评估）"
    return f"{head}：{' · '.join(reads) or '无影响判断'}"


def _decision(stage: dict[str, Any] | None, session: str) -> str | None:
    if not stage:
        return None
    code = stage.get("code")
    if code == "ERROR":
        return "出错"  # never the error text: it carries API error bodies
    if code not in ("BUY", "SELL", "HOLD"):
        return None
    rating = (stage.get("details") or {}).get("pm_rating")
    text = f"{code}（PM {rating}）" if rating else str(code)
    return text + ("，不下单" if session == "cn" and code != "HOLD" else "")


def _verdict_text(stage: dict[str, Any] | None) -> str:
    code = (stage or {}).get("code")
    if code in ("pass", "warn", "fail"):
        return f"评估 {code}"
    return "未评估"


def ticker_line(config: PipelineConfig, entry: dict[str, Any], slot_date: date, session: str) -> tuple[tuple, str] | None:
    ticker = str(entry.get("ticker") or "")
    stages = entry.get("stages") or {}
    if (stages.get("collect") or {}).get("code") != "written":
        return None
    text = _read(config.ticker_brief_dir / ticker / f"{slot_date.isoformat()}.md")
    if text is None:
        return None
    meta, body, _errors = split_frontmatter(text)
    meta = meta or {}
    try:
        score = float(meta.get("catalyst_score"))
    except (TypeError, ValueError):
        score = None
    catalyst = " ".join(
        str(part) for part in (meta.get("catalyst_type"), meta.get("catalyst_window"))
        if part not in (None, "", "none", "other")
    )
    impact = _last_impact(body)
    parts = [f"• {ticker} {score:.1f}" if score is not None else f"• {ticker} –"]
    if catalyst:
        parts.append(catalyst)
    line = " ".join(parts)
    if impact:
        line += f" · {impact[0]}" + (f" — {impact[1]}" if impact[1] else "")
    tail = [_verdict_text(stages.get("evaluate"))]
    decision = _decision(stages.get("decide"), session)
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
            snapshot = json.loads(raw)
        except json.JSONDecodeError:
            snapshot = {}
    rows = [row for entry in snapshot.get("tickers") or [] if isinstance(entry, dict)
            if (row := ticker_line(config, entry, slot_date, session)) is not None]
    rows.sort(key=lambda row: row[0])
    analysed = sum(1 for order, _line in rows if not order[0])
    lines.append(f"个股简报 {len(rows)} 份，分析 {analysed} 只：" if rows else "个股：无简报")
    lines.extend(line for _order, line in rows)
    return "\n".join(lines)


def send_digest(config: PipelineConfig, session: str, slot_date: date) -> int:
    """Queue the slot's digest on the trades channel (once per key) and flush;
    nothing when notifications are off or the trades webhook is unset.
    Returns messages sent; never raises."""
    if not config.notifications_enabled or not config.lark_trades_webhook_url:
        return 0
    try:
        from pipeline import trade_notify
        from pipeline.trade_monitor import make_trades_sender

        text = build_digest(config, session, slot_date)
        return trade_notify.enqueue(config.state_dir, digest_key(session, slot_date), text,
                                    make_trades_sender(config))
    except Exception as exc:  # never the URL: LarkError text is scrubbed
        reason = str(exc) if exc.__class__.__name__ == "LarkError" else type(exc).__name__
        logger.warning("research digest for %s %s not sent: %s", session, slot_date, reason)
        return 0
