"""Daily post-close review and data archive (specs/daily-review.md).

After the US close the day's research, decisions, orders and market moves
are archived by category and date under ``TRADINGAGENTS_ARCHIVE_DIR``
(default ``/mnt/d/massive/stock``), and one model call reviews the day from
structured facts only — what went wrong, and whether the strategy needs
optimising. The review goes to the Lark trades group and next to the
archive; it proposes, it never changes the strategy.
"""

from __future__ import annotations

import argparse
import contextlib
import csv
import io
import json
import logging
import os
import subprocess
import sys
from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from pipeline.common import atomic_write
from pipeline.config import PipelineConfig, load_config

logger = logging.getLogger("pipeline.daily_review")

ARCHIVE_ENV = "TRADINGAGENTS_ARCHIVE_DIR"
DEFAULT_ARCHIVE_DIR = Path("/mnt/d/massive/stock")
NY = ZoneInfo("America/New_York")
#: The review starts once the New York clock is past this, on a weekday.
REVIEW_AFTER = time(17, 0)
#: A started review without a done mark is retried after this.
STUCK_AFTER = timedelta(hours=2)
#: Sessions of history the rolling scoreboard reads.
SCOREBOARD_SESSIONS = 20
#: A move at least this large on the day makes a skipped name worth naming.
MISSED_MOVE_PCT = 5.0
REVIEW_TIMEOUT_S = 300.0
BENCHMARKS = {"us": "SPY", "cn": "000300.SS", "hk": "^HSI"}
VERDICTS = ("需要优化", "继续观察", "暂不调整")


def archive_root(config: PipelineConfig | None = None) -> Path:
    raw = os.environ.get(ARCHIVE_ENV, "").strip()
    return Path(raw).expanduser() if raw else DEFAULT_ARCHIVE_DIR


def _benchmark(ticker: str, session: str) -> str:
    return BENCHMARKS["hk"] if ticker.upper().endswith(".HK") else BENCHMARKS[session]


# ---------------------------------------------------------------------------
# R1 — archive
# ---------------------------------------------------------------------------


def _copy(src: Path, dest: Path, counts: dict[str, int], category: str) -> None:
    try:
        data = src.read_bytes()
    except OSError:
        counts[f"{category}:missing"] += 1
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and dest.read_bytes() == data:
        counts[category] += 1  # idempotent: the same content is already there
        return
    tmp = dest.with_name(dest.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, dest)
    counts[category] += 1


def _write(dest: Path, text: str, counts: dict[str, int], category: str) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(dest, text)
    counts[category] += 1


def _jsonl_rows(path: Path) -> list[dict]:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    rows = []
    for line in text.splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _day_tickers(config: PipelineConfig, day: date) -> dict[str, set[str]]:
    """session -> tickers of the day's funnel snapshots (and brief files)."""
    from pipeline.funnel import snapshot_path

    out: dict[str, set[str]] = defaultdict(set)
    for session in ("cn", "us"):
        try:
            snap = json.loads(snapshot_path(config.state_dir, session, day).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for entry in snap.get("tickers") or [] if isinstance(snap, dict) else []:
            if isinstance(entry, dict) and entry.get("ticker"):
                out[session].add(str(entry["ticker"]))
    return out


def closes(tickers: set[str], fetch=None) -> dict[str, list[tuple[date, float]]]:
    """ticker -> clean daily closes (the price tape's source)."""
    from pipeline import price_tape

    out = {}
    for ticker in sorted(tickers):
        try:
            bars = (fetch or price_tape.fetch_daily_closes)(ticker)
        except Exception as exc:
            logger.warning("daily review: closes for %s unavailable (%s)", ticker, type(exc).__name__)
            continue
        out[ticker] = [(d, c) for d, c in bars if c == c and c > 0]
    return out


def day_change(series: list[tuple[date, float]], day: date) -> float | None:
    """% change of the session on ``day`` vs the previous close."""
    for i, (d, close) in enumerate(series):
        if d == day and i > 0:
            return (close / series[i - 1][1] - 1) * 100
    return None


def archive_day(config: PipelineConfig, day: date, *, broker=None, fetch=None,
                prices: dict[str, list[tuple[date, float]]] | None = None) -> dict[str, int]:
    """Archive ``day`` under ``<root>/<category>/<day>/``; returns counts per
    category (``<category>:missing`` for skipped inputs). Never raises on a
    missing input."""
    root, iso = archive_root(config), day.isoformat()
    counts: dict[str, int] = defaultdict(int)
    for session in ("cn", "us"):
        base = config.macro_brief_dir / f"{iso}.{session}"
        _copy(Path(f"{base}.md"), root / "macro_briefs" / iso / f"{session}.md", counts, "macro_briefs")
        _copy(Path(f"{base}.eval.json"), root / "macro_briefs" / iso / f"{session}.eval.json", counts, "macro_briefs")
        from pipeline.funnel import snapshot_path

        _copy(snapshot_path(config.state_dir, session, day), root / "funnel" / iso / f"{session}.json", counts, "funnel")
        _copy(config.state_dir / "research_digest" / session / f"{iso}.txt",
              root / "digests" / iso / f"{session}.txt", counts, "digests")
    with contextlib.suppress(OSError):
        for brief in sorted(config.ticker_brief_dir.glob(f"*/{iso}.md")):
            ticker = brief.parent.name
            _copy(brief, root / "ticker_briefs" / iso / f"{ticker}.md", counts, "ticker_briefs")
            _copy(brief.with_name(f"{iso}.eval.json"), root / "ticker_briefs" / iso / f"{ticker}.eval.json",
                  counts, "ticker_briefs")
    decisions = [r for r in _jsonl_rows(config.ledger_dir / "decisions.jsonl") if r.get("date") == iso]
    _write(root / "decisions" / iso / "decisions.jsonl",
           "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in decisions), counts, "decisions")
    orders = [r for r in _jsonl_rows(config.ledger_dir / "orders.jsonl")
              if str(r.get("client_order_id") or r.get("run_id") or "").startswith(iso)]
    _write(root / "orders" / iso / "orders.jsonl",
           "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in orders), counts, "orders")
    cli = [r for r in _jsonl_rows(config.state_dir / "claude_usage.jsonl") if str(r.get("ts", "")).startswith(iso)]
    analysis = [r for r in _jsonl_rows(config.state_dir / "llm_usage.jsonl") if r.get("date") == iso]
    _write(root / "llm_usage" / iso / "cli.jsonl", "".join(json.dumps(r) + "\n" for r in cli), counts, "llm_usage")
    _write(root / "llm_usage" / iso / "analysis.jsonl", "".join(json.dumps(r) + "\n" for r in analysis),
           counts, "llm_usage")
    if prices is None:
        tickers = set().union(*_day_tickers(config, day).values()) | set(BENCHMARKS.values())
        prices = closes(tickers, fetch)
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["date", "ticker", "close", "change_pct"])
    for ticker, series in sorted(prices.items()):
        for d, close in series:
            if d == day:
                change = day_change(series, day)
                writer.writerow([iso, ticker, f"{close:.4f}", "" if change is None else f"{change:.2f}"])
    _write(root / "prices" / iso / "closes.csv", buf.getvalue(), counts, "prices")
    if broker is not None:
        for name, read in (("account", broker.account), ("positions", broker.positions)):
            try:
                value = read()
                data = value.model_dump() if hasattr(value, "model_dump") else (
                    [v.model_dump() if hasattr(v, "model_dump") else dict(v) for v in value]
                    if isinstance(value, list) else dict(value))
                _write(root / "broker" / iso / f"{name}.json", json.dumps(data, default=str, indent=1), counts,
                       "broker")
            except Exception as exc:
                logger.warning("daily review: broker %s unavailable (%s)", name, type(exc).__name__)
                counts["broker:missing"] += 1
        try:
            start = datetime(day.year, day.month, day.day, tzinfo=NY).astimezone(timezone.utc)
            fills = [f for f in broker.fills_since(start) if str(_fill_day(f)) == iso]
            _write(root / "broker" / iso / "fills.json", json.dumps(fills, default=str, indent=1), counts, "broker")
        except Exception as exc:
            logger.warning("daily review: fills unavailable (%s)", type(exc).__name__)
            counts["broker:missing"] += 1
    return dict(counts)


def _fill_day(fill: Any) -> date | None:
    raw = (fill.get("transaction_time") if isinstance(fill, dict) else None) or ""
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00")).astimezone(NY).date()
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# R2 — facts and review
# ---------------------------------------------------------------------------


def _names(config: PipelineConfig, session: str, day: date) -> list[dict]:
    """Structured per-name facts of a slot — never brief prose."""
    from pipeline import research_digest as rd

    tickers, _threshold, _executed, _readable = rd._tickers(config, session, day)
    briefs_used = {str(r.get("run_id")): str(((r.get("inputs") or {}).get("ticker_brief") or {}).get("path") or "")
                   for r in _jsonl_rows(config.ledger_dir / "decisions.jsonl") if r.get("date") == day.isoformat()}
    out = []
    for t in tickers:
        impact = rd._last_impact(t.body) if t.has_brief else None
        details = (t.decide or {}).get("details") or {}
        used = briefs_used.get(str(details.get("run_id")), "")
        trigger = t.stages.get("trigger") if isinstance(t.stages.get("trigger"), dict) else {}
        out.append({
            "ticker": t.ticker, "impact": impact[0] if impact else None, "score": t.score,
            "eval": ((t.stages.get("evaluate") or {}).get("code")),
            # the runner's own code (core / core-pair / catalyst / below-threshold …)
            "trigger": trigger.get("code") or t.trigger,
            # a decision made on an older brief (served stale, contract gap = 1)
            "brief_used": used.split("/")[-1].removesuffix(".md") if used and day.isoformat() not in used else None,
            "decision": (t.decide or {}).get("code"), "pm_rating": details.get("pm_rating"),
            "arm": details.get("arm"), "final": t.final,
            "execute": (t.execute or {}).get("code") if isinstance(t.execute, dict) else None,
        })
    return out


def _with_moves(names: list[dict], session: str, day: date, prices: dict) -> list[dict]:
    for row in names:
        move = day_change(prices.get(row["ticker"], []), day)
        bench = day_change(prices.get(_benchmark(row["ticker"], session), []), day)
        row["move_pct"] = None if move is None else round(move, 2)
        row["excess_pct"] = None if move is None or bench is None else round(move - bench, 2)
    return names


def _scoreboard(config: PipelineConfig, day: date, prices: dict) -> dict:
    """Rolling: decision-day excess return by brief impact word and by PM
    rating, over the last SCOREBOARD_SESSIONS archived us sessions."""
    buckets: dict[str, dict[str, list[float]]] = {"impact": defaultdict(list), "pm_rating": defaultdict(list)}
    d, seen = day, 0
    while seen < SCOREBOARD_SESSIONS and (day - d).days < 60:
        if d.weekday() < 5:
            names = _with_moves(_names(config, "us", d), "us", d, prices)
            if names:
                seen += 1
            for row in names:
                if row["excess_pct"] is None:
                    continue
                if row["impact"]:
                    buckets["impact"][row["impact"]].append(row["excess_pct"])
                if row["pm_rating"]:
                    buckets["pm_rating"][row["pm_rating"]].append(row["excess_pct"])
        d -= timedelta(days=1)
    return {"sessions": seen, **{
        kind: {key: {"n": len(v), "mean_excess_pct": round(sum(v) / len(v), 2)} for key, v in group.items()}
        for kind, group in buckets.items()}}


def build_facts(config: PipelineConfig, day: date, *, prices: dict | None = None, broker=None) -> dict:
    """The day's structured facts for the review (specs/daily-review.md R2)."""
    from pipeline import research_digest as rd

    if prices is None:
        tickers: set[str] = set(BENCHMARKS.values())
        d = day
        for _ in range(60):  # the scoreboard window's names too
            for names in _day_tickers(config, d).values():
                tickers |= names
            d -= timedelta(days=1)
        prices = closes(tickers)
    facts: dict[str, Any] = {"date": day.isoformat()}
    for session in ("us", "cn"):
        names = _with_moves(_names(config, session, day), session, day, prices)
        macro, _text = rd.macro_read(config, session, day) if (
            config.macro_brief_dir / f"{day.isoformat()}.{session}.md").exists() else ("无宏观简报", None)
        facts[session] = {
            "macro": macro,
            "benchmark_move_pct": (lambda m: None if m is None else round(m, 2))(
                day_change(prices.get(BENCHMARKS[session], []), day)),
            "names": names,
            "missed_moves": [r for r in names if r["decision"] is None and r["move_pct"] is not None
                             and abs(r["move_pct"]) >= MISSED_MOVE_PCT],
            "analysis_errors": sum(1 for r in names if r["decision"] == "ERROR"),
            "execute_outcomes": dict(sorted(_count(r["execute"] for r in names if r["execute"]).items())),
        }
    if broker is not None:
        with contextlib.suppress(Exception):
            account = broker.account()
            acct = account.model_dump() if hasattr(account, "model_dump") else dict(account)
            facts["account"] = {k: acct.get(k) for k in ("equity", "last_equity", "cash", "buying_power")}
        with contextlib.suppress(Exception):
            facts["positions"] = [
                {k: (p.model_dump() if hasattr(p, "model_dump") else dict(p)).get(k)
                 for k in ("symbol", "qty", "avg_entry_price", "current_price", "unrealized_pl")}
                for p in broker.positions()]
    facts["scoreboard"] = _scoreboard(config, day, prices)
    return facts


def _count(values) -> dict[str, int]:
    out: dict[str, int] = defaultdict(int)
    for value in values:
        out[str(value)] += 1
    return out


REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "issues": {"type": "array", "items": {"type": "object", "properties": {
            "title": {"type": "string"}, "evidence": {"type": "string"},
            "severity": {"type": "string", "enum": ["高", "中", "低"]}},
            "required": ["title", "evidence", "severity"], "additionalProperties": False}},
        "verdict": {"type": "string", "enum": list(VERDICTS)},
        "proposals": {"type": "array", "items": {"type": "object", "properties": {
            "change": {"type": "string"}, "why": {"type": "string"}, "evidence": {"type": "string"},
            "risk": {"type": "string"}},
            "required": ["change", "why", "evidence", "risk"], "additionalProperties": False}},
        "watch": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["summary", "issues", "verdict", "proposals", "watch"],
    "additionalProperties": False,
}

REVIEW_PROMPT = """你是一个交易研究流水线的复盘员。下面是 {date} 这一天的结构化事实（JSON），
包括：美股（us）与 A股/港股（cn）每只股票的简报方向（impact: 偏多/偏空/中性/分歧）、催化剂分数、
评估结论、触发代码、分析结论（decision: BUY/SELL/HOLD/ERROR）与 PM 评级、执行结果、
当天涨跌（move_pct）和相对基准的超额（excess_pct；为 null 表示行情尚未更新，不是零）；
brief_used 不为空表示分析用的是那一天的旧简报（当天简报缺失时按合约沿用前一天的）；被跳过却大幅波动的股票（missed_moves）；
宏观判断；账户和持仓；以及最近 {sessions} 个交易日的滚动统计（按简报方向、按 PM 评级的超额收益均值与样本数）。

请用中文复盘：
1. summary：两三句话总结今天的操作和结果。
2. issues：今天暴露的问题，每条给出事实依据（引用具体股票和数字）和严重程度。没有问题就留空，不要编造。
3. verdict：策略是否需要优化——"需要优化"、"继续观察"或"暂不调整"。
4. proposals：如果需要优化，给出具体改动（改什么规则或阈值）、理由、依据（必须引用样本数和数字）、风险。
   样本数少于 10 的规律不足以支撑策略改动，只能列入 watch。
5. watch：需要继续积累数据观察的现象。

规则：只使用下面的事实，不要搜索、不要引用外部信息、不要编造任何数字；数字写阿拉伯数字。
你只提建议，任何改动都由人决定。

{facts}"""


def _run_review(prompt: str) -> dict:
    """One hermetic call on the evaluator's model, schema-bound JSON."""
    from pipeline.claude_cli import (
        EnvelopeError,
        call_env,
        claude_command,
        parse_envelope,
        record_failed_call,
        record_usage,
        timeout_stdout,
    )

    config = load_config()
    model, effort = config.claude_eval_model, config.claude_eval_effort
    command = claude_command(model, effort, "--json-schema", json.dumps(REVIEW_SCHEMA), role="eval")
    try:
        proc = subprocess.run(command, input=prompt, capture_output=True, text=True,
                              timeout=REVIEW_TIMEOUT_S, env=call_env(command))
    except subprocess.TimeoutExpired as exc:
        record_failed_call("daily_review", model, effort, timeout_stdout(exc))
        raise
    if proc.returncode != 0:
        record_failed_call("daily_review", model, effort, proc.stdout)
        raise RuntimeError(f"review call failed (exit {proc.returncode})")
    try:
        envelope = parse_envelope(proc.stdout)
    except EnvelopeError:
        record_failed_call("daily_review", model, effort, proc.stdout)
        raise
    output = envelope.get("structured_output")
    record_usage("daily_review", model, effort, envelope, outcome="ok" if isinstance(output, dict) else "error")
    if not isinstance(output, dict):
        raise RuntimeError("review call returned no structured output")
    return output


def render(day: date, facts: dict, review: dict | None) -> str:
    """The Lark message (and the archived markdown body)."""
    lines = [f"🧭 每日复盘 · {day.isoformat()}"]
    account = facts.get("account") or {}
    try:
        equity, last = float(account.get("equity")), float(account.get("last_equity"))
        lines.append(f"账户：权益 ${equity:,.2f}（今日 {equity - last:+,.2f}）｜持仓 {len(facts.get('positions') or [])} 只")
    except (TypeError, ValueError):
        pass
    us = facts.get("us") or {}
    if us.get("benchmark_move_pct") is not None:
        lines.append(f"大盘：SPY {us['benchmark_move_pct']:+.2f}%")
    if review is None:
        lines.append("复盘模型不可用，以下为当天事实：")
        for session, label in (("us", "美股"), ("cn", "A股/港股")):
            block = facts.get(session) or {}
            missed = ", ".join(f"{r['ticker']} {r['move_pct']:+.1f}%" for r in block.get("missed_moves") or [])
            lines.append(f"• {label}：分析出错 {block.get('analysis_errors', 0)} 次"
                         + (f"；未分析但大幅波动：{missed}" if missed else ""))
        return "\n".join(lines)
    lines += ["", f"总结：{review.get('summary', '').strip()}"]
    issues = review.get("issues") or []
    if issues:
        lines += ["", "问题："]
        lines += [f"• [{i.get('severity')}] {i.get('title')} — {i.get('evidence')}" for i in issues]
    lines += ["", f"策略：{review.get('verdict')}"]
    for p in review.get("proposals") or []:
        lines.append(f"• 建议：{p.get('change')}\n  理由：{p.get('why')}\n  依据：{p.get('evidence')}\n  风险：{p.get('risk')}")
    watch = review.get("watch") or []
    if watch:
        lines += ["", "继续观察："] + [f"• {w}" for w in watch]
    lines += ["", "（复盘只提建议，策略改动需你确认）"]
    return "\n".join(lines)


def review_day(config: PipelineConfig, day: date, *, broker=None, send=None, runner=None) -> str:
    """Archive, build facts, review, deliver, archive the review; returns the text."""
    from pipeline import trade_notify

    facts = build_facts(config, day, broker=broker)
    archive_day(config, day, broker=broker, prices=None)
    try:
        review = (runner or _run_review)(REVIEW_PROMPT.format(
            date=day.isoformat(), sessions=facts["scoreboard"]["sessions"],
            facts=json.dumps(facts, ensure_ascii=False, default=str)))
        if review.get("verdict") not in VERDICTS:
            raise ValueError("verdict outside the schema")
    except Exception as exc:
        logger.warning("daily review: model unavailable (%s) — facts only", type(exc).__name__)
        review = None
    text = render(day, facts, review)
    root = archive_root(config) / "reviews" / day.isoformat()
    root.mkdir(parents=True, exist_ok=True)
    atomic_write(root / "review.md", text + "\n")
    atomic_write(root / "review.json", json.dumps({"facts": facts, "review": review}, ensure_ascii=False,
                                                   default=str, indent=1))
    if send is not None:
        trade_notify.enqueue(config.state_dir, f"review:{day.isoformat()}", text, send)
    return text


# ---------------------------------------------------------------------------
# R3 — schedule (the hourly watchdog)
# ---------------------------------------------------------------------------


def _marks(config: PipelineConfig, day: date) -> tuple[Path, Path]:
    base = config.state_dir / "daily_review"
    return base / f"{day.isoformat()}.started", base / f"{day.isoformat()}.done"


def maybe_start(config: PipelineConfig, now: datetime, *, spawn=None) -> bool:
    """Start the day's review, detached, once per day after REVIEW_AFTER (NY)
    on a weekday whose us funnel snapshot exists. Returns whether it started."""
    from pipeline.funnel import snapshot_path

    local = now.astimezone(NY)
    day = local.date()
    if day.weekday() >= 5 or local.time() < REVIEW_AFTER:
        return False
    if not snapshot_path(config.state_dir, "us", day).exists():
        return False
    started, done = _marks(config, day)
    if done.exists():
        return False
    if started.exists():
        try:
            at = datetime.fromisoformat(started.read_text(encoding="utf-8").strip())
        except ValueError:
            at = now - STUCK_AFTER
        if now - at < STUCK_AFTER:
            return False
    started.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(started, now.astimezone(timezone.utc).isoformat())
    (spawn or _spawn)(day)
    return True


def _spawn(day: date) -> None:
    subprocess.Popen(  # noqa: S603 — our own module, fixed argv
        [sys.executable, "-m", "pipeline.daily_review", "run", "--date", day.isoformat()],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
        cwd=str(Path(__file__).resolve().parents[1]))


def run(config: PipelineConfig, day: date) -> int:
    from pipeline import trade_notify
    from pipeline.trade_monitor import make_trades_sender

    broker = None
    with contextlib.suppress(Exception):
        broker = trade_notify.AlpacaReadOnly(*trade_notify.alpaca_keys())
    send = make_trades_sender(config) if config.notifications_enabled else None
    review_day(config, day, broker=broker if day == datetime.now(NY).date() else None, send=send)
    _started, done = _marks(config, day)
    done.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(done, datetime.now(timezone.utc).isoformat())
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m pipeline.daily_review", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    one = sub.add_parser("run", help="archive and review one US trading date, then send the review")
    one.add_argument("--date", type=date.fromisoformat, required=True)
    back = sub.add_parser("archive", help="archive past dates (no review, no message)")
    back.add_argument("--since", type=date.fromisoformat, required=True)
    back.add_argument("--until", type=date.fromisoformat, default=None)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    config = load_config()
    if args.cmd == "run":
        return run(config, args.date)
    d, until = args.since, args.until or datetime.now(NY).date()
    while d <= until:
        if d.weekday() < 5:
            counts = archive_day(config, d)
            print(d.isoformat(), {k: v for k, v in sorted(counts.items()) if not k.endswith(":missing")})
        d += timedelta(days=1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
