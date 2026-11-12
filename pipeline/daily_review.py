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
import math
import os
import re
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
#: A move at least this large on the day makes a non-analysed name worth naming.
MISSED_MOVE_PCT = 5.0
REVIEW_TIMEOUT_S = 300.0
#: cn: the CSI 300 ETF — Yahoo serves the CSI 300 index (000300.SS) as a
#: single latest bar, so the index never had a day change (2026-11-09).
BENCHMARKS = {"us": "SPY", "cn": "510300.SS", "hk": "^HSI"}
#: Scoreboard horizons in sessions, from the close before the decision day.
HORIZONS = (1, 3, 5)
VERDICTS = ("需要优化", "继续观察", "暂不调整")
SEVERITIES = ("高", "中", "低")
#: Size bounds (prompt and check — not the schema: codex sends it in OpenAI
#: strict form, which has no maxLength): one Lark text message, well under
#: the webhook's body limit; a larger review falls back to the facts.
MAX_FIELD_CHARS = 300
MAX_ITEMS = {"issues": 8, "proposals": 5, "watch": 6}
MAX_MESSAGE_CHARS = 6000
#: Links or markup never reach the group (the review text is model output):
#: any scheme, www., a markdown link, an HTML/Lark tag — a plain comparison
#: ("超额>2%", "n<10") is fine — and any domain-like host (research_digest's
#: rule: only a numeric exchange symbol such as 0700.HK passes).
_UNSAFE_TEXT = re.compile(
    r"[A-Za-z][A-Za-z0-9+.-]*://|www\.|\]\("
    # a tag (an HTML or Lark element name, or <!): NVDA<SPY is a comparison
    # (attributes are name or name=value, so "NVDA<SPY 但 MU>SPY" is no tag)
    r"|</?\s*[A-Za-z][A-Za-z0-9-]*(?:\s+[A-Za-z][\w:-]*(?:\s*=\s*[^\s<>]*)?)*\s*/?>"
    r"|<\s*/?\s*(?:a|at|b|i|u|s|p|br|hr|em|font|img|div|span|script|style|iframe|svg|link|meta|form)\b|<\s*!"
    # research_digest's host rules: a host with a path (numeric or not), an
    # IP-like host, a common TLD in any script
    r"|[A-Za-z0-9-]\.[A-Za-z]{2,}/|\d+(?:\.\d+){2,}"
    r"|(?<!\d)\.(?:com|net|org|cn|io|ai|ly|me|co|info|news|top|xyz)(?![A-Za-z])",
    re.IGNORECASE)


def archive_root(config: PipelineConfig | None = None) -> Path:
    raw = os.environ.get(ARCHIVE_ENV, "").strip()
    return Path(raw).expanduser() if raw else DEFAULT_ARCHIVE_DIR


def _benchmark(ticker: str, session: str) -> str:
    return BENCHMARKS["hk"] if ticker.upper().endswith(".HK") else BENCHMARKS[session]


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------


def _jsonl_rows(path: Path) -> list[dict] | None:
    """The file's JSON-object rows; None when it is missing or cannot be
    read — the archive then keeps what it has (facts read None as no rows)."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    rows = []
    for line in text.splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _snapshot(config: PipelineConfig, session: str, day: date) -> list[dict]:
    from pipeline.funnel import snapshot_path

    try:
        snap = json.loads(snapshot_path(config.state_dir, session, day).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return []
    entries = snap.get("tickers") if isinstance(snap, dict) else None
    return [e for e in entries if isinstance(e, dict) and e.get("ticker")] if isinstance(entries, list) else []


def _day_tickers(config: PipelineConfig, day: date) -> dict[str, set[str]]:
    """session -> tickers of the day's funnel snapshots."""
    return {session: {str(e["ticker"]) for e in _snapshot(config, session, day)} for session in ("cn", "us")}


#: The price reads share this budget (the review runs inside the watchdog's
#: 15-minute task limit, with the model call's own 300 s): tickers left when
#: it is spent are skipped (their moves read null), never the review.
PRICE_BUDGET_S = 240.0


#: Per market: local zone, the regular close, and a time safely after it.
#: A quote counts as the day's close only when its own last-trade time is on
#: the day at or after the close (less a few minutes), read after the margin.
#: Read margins cover Yahoo's delayed Asian quotes (up to ~30 minutes).
_SESSION_CLOSE = {"cn": ("Asia/Shanghai", time(15, 0), time(16, 0)),
                  "hk": ("Asia/Hong_Kong", time(16, 0), time(17, 0)),
                  "us": ("America/New_York", time(16, 0), time(16, 30))}
#: How long before the close a last trade may be (a closing auction prints
#: just before the bell; a thin name's last trade can be earlier).
CLOSE_TRADE_SLACK = timedelta(minutes=10)
#: The quote's previous close must equal the series' last close before the
#: day within this fraction: else the series misses a session.
QUOTE_PREV_TOLERANCE = 0.002


def _market(ticker: str) -> str:
    upper = ticker.upper()
    if upper.endswith((".SS", ".SZ")):
        return "cn"
    return "hk" if upper.endswith(".HK") or upper == "^HSI" else "us"


def _yahoo_quote(ticker: str) -> tuple[float, datetime, float]:
    """(regular-market price, its trade time, previous close) from Yahoo's
    chart metadata."""
    import yfinance as yf

    handle = yf.Ticker(ticker)
    handle.history(period="5d", interval="1d", auto_adjust=False)
    meta = handle.history_metadata or {}
    return (float(meta["regularMarketPrice"]),
            datetime.fromtimestamp(int(meta["regularMarketTime"]), timezone.utc),
            float(meta["previousClose"]))


def fill_day_close(ticker: str, series: list, day: date, now: datetime, quote=None,
                   reasons: dict | None = None) -> tuple[list, bool]:
    """Yahoo often lists the latest session late (a NaN close, or no row at
    all — 510300.SS and ^HSI on 2026-11-10). After that market's close, the
    quote's regular-market price is the day's close when its own trade time
    is on ``day``, at or after the close less CLOSE_TRADE_SLACK (so a
    holiday, a suspension or an intraday price never passes), and its
    previous close equals the series' last close before the day (the series
    misses no session). Never with a later bar; a listed close is never
    replaced. Returns the series and whether it was filled; ``reasons``
    receives why a missing day close was not filled (the ticker's key)."""
    def unfilled(why: str) -> tuple[list, bool]:
        if reasons is not None:
            reasons[ticker] = why
        return series, False

    if any(d == day and c is not None for d, c in series):
        return series, False  # listed: nothing to fill
    if any(d > day for d, _c in series):
        # The market listed a later session but not this day: most likely it
        # did not trade (a holiday) — or a past day re-read; never filled.
        listed = any(d == day for d, _c in series)  # a row with an unusable close is a data fault
        return unfilled("no-session" if any(d < day for d, _c in series) and not listed else "later-bar")
    zone_name, close_at, read_after = _SESSION_CLOSE[_market(ticker)]
    zone = ZoneInfo(zone_name)
    if now.astimezone(zone) < datetime.combine(day, read_after, tzinfo=zone):
        return unfilled("before-close")
    prior = [c for d, c in series if d < day and c is not None]
    if not prior:
        return unfilled("no-prior-close")
    try:
        last, traded_at, previous = (quote or _yahoo_quote)(ticker)
        traded = traded_at.astimezone(zone)
    except Exception as exc:
        logger.warning("daily review: quote for %s unavailable (%s)", ticker, type(exc).__name__)
        return unfilled(f"quote-error:{type(exc).__name__}")
    if traded.date() != day:
        return unfilled(f"quote-trade-date:{traded.date().isoformat()}")
    if traded < datetime.combine(day, close_at, tzinfo=zone) - CLOSE_TRADE_SLACK:
        return unfilled(f"quote-before-close:{traded.strftime('%H:%M')}")
    if not all(math.isfinite(v) and v > 0 for v in (last, previous)):
        return unfilled("quote-unusable")
    if abs(previous / prior[-1] - 1) > QUOTE_PREV_TOLERANCE:
        return unfilled("missed-session")  # the series lacks the session before the day
    # Sorted by date only: a duplicate date with an unusable close (None)
    # must never be compared by its close.
    return sorted([(d, c) for d, c in series if d != day] + [(day, last)], key=lambda bar: bar[0]), True


def closes(tickers: set[str], fetch=None, budget_s: float = PRICE_BUDGET_S, *, first=(),
           stats: dict | None = None, day: date | None = None, quote=None,
           now: datetime | None = None, fill: set[str] | None = None) -> dict[str, list[tuple[date, float]]]:
    """ticker -> clean daily closes (the price tape's source); a ticker whose
    read fails is left out (never an empty series). ``first`` (in order) is
    read before the rest, so a spent budget skips the oldest names, never the
    benchmarks or the day's; ``stats`` receives the skipped, failed and
    quote-filled counts. With ``day``, a close Yahoo has not listed yet is
    filled from the quote after that market's close (fill_day_close) — for
    the ``fill`` names only when given (the benchmarks and the day's names:
    one quote round trip each, from the shared budget)."""
    import time as _time

    from pipeline import price_tape

    order = [t for t in dict.fromkeys(first) if t in tickers] + sorted(set(tickers) - set(first))
    out, failed, skipped, filled = {}, 0, 0, []
    failed_names: dict[str, str] = {}  # ticker -> why its read failed
    unfilled: dict[str, str] = {}  # ticker -> why its missing day close was not filled
    now = now or datetime.now(timezone.utc)
    deadline = _time.monotonic() + budget_s
    for i, ticker in enumerate(order):
        if _time.monotonic() > deadline:
            skipped = len(order) - i
            logger.warning("daily review: price budget spent — %d tickers skipped", skipped)
            break
        try:
            bars = (fetch or price_tape.fetch_daily_closes)(ticker)
        except Exception as exc:
            logger.warning("daily review: closes for %s unavailable (%s)", ticker, type(exc).__name__)
            failed += 1
            failed_names[ticker] = type(exc).__name__
            continue
        # An unusable close stays in place as None: a change never spans it.
        series = [(d, c if c == c and c > 0 else None) for d, c in bars]
        if day is not None and (fill is None or ticker in fill):
            series, was_filled = fill_day_close(ticker, series, day, now, quote, unfilled)
            if was_filled:
                filled.append(ticker)
        if any(c is not None for _d, c in series):
            out[ticker] = series
        else:
            failed += 1
            failed_names[ticker] = "no-usable-close"
    if stats is not None:
        stats.update(skipped=skipped, failed=failed, quote_filled=len(filled), quote_filled_tickers=sorted(filled),
                     failed_tickers=dict(sorted(failed_names.items())), unfilled=dict(sorted(unfilled.items())))
    return out


def day_change(series: list[tuple[date, float]], day: date) -> float | None:
    """% change of the session on ``day`` vs the previous session's close;
    None when either close is missing (never a move across a gap)."""
    for i, (d, close) in enumerate(series):
        if d == day and i > 0 and close is not None and series[i - 1][1] is not None:
            return (close / series[i - 1][1] - 1) * 100
    return None


def _previous_date(series: list[tuple[date, float]], day: date) -> date | None:
    dates = [d for d, _c in series]
    return dates[dates.index(day) - 1] if day in dates and dates.index(day) > 0 else None


def name_change(prices: dict, ticker: str, session: str, day: date) -> float | None:
    """The name's change on ``day``, None when its previous bar is not the
    benchmark's previous session (a suspension with no bar): never a move
    across a gap. Without the benchmark's bars the name's own stand."""
    series = prices.get(ticker, [])
    move = day_change(series, day)
    bench = prices.get(_benchmark(ticker, session), [])
    if move is None or ticker in BENCHMARKS.values() or day_change(bench, day) is None:
        return move
    return move if _previous_date(series, day) == _previous_date(bench, day) else None


def _ny_date(stamp: object) -> date | None:
    try:
        return datetime.fromisoformat(str(stamp).replace("Z", "+00:00")).astimezone(NY).date()
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# R1 — archive
# ---------------------------------------------------------------------------


def _copy(src: Path, dest: Path, counts: dict[str, int], category: str) -> None:
    try:
        data = src.read_bytes()
    except OSError:
        counts[f"{category}:missing"] += 1
        return
    if not data and dest.exists() and dest.stat().st_size:
        counts[f"{category}:missing"] += 1  # an empty source never replaces an archived copy
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    if not (dest.exists() and dest.read_bytes() == data):
        tmp = dest.with_name(dest.name + ".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, dest)
    counts[category] += 1


def _write(dest: Path, text: str | None, counts: dict[str, int], category: str) -> None:
    """Write ``text``; None means the input could not be read, and an empty
    text never replaces a non-empty archived file: the archived file is then
    kept as it is and the category counts as missing."""
    if text is None or (not text and dest.exists() and dest.stat().st_size):
        counts[f"{category}:missing"] += 1  # an empty read (a truncated ledger) never empties the archive
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(dest, text)
    counts[category] += 1


def _jsonl_text(rows: list[dict] | None) -> str | None:
    return None if rows is None else "".join(json.dumps(r, ensure_ascii=False, default=str) + "\n" for r in rows)


def _usage_log() -> Path:
    from pipeline.claude_cli import usage_log_path

    return usage_log_path()


def archive_day(config: PipelineConfig, day: date, *, broker=None, fetch=None,
                prices: dict[str, list[tuple[date, float]]] | None = None,
                quote_filled: list[str] | None = None) -> dict[str, int]:
    """Archive ``day`` under ``<root>/<category>/<day>/``; returns counts per
    category (``<category>:missing`` for an input skipped or unreadable — an
    archived file is never replaced by an empty one). Never raises on input.
    ``quote_filled`` names the closes in ``prices`` that came from the quote
    (the review's own read), so the marker records them too."""

    root, iso = archive_root(config), day.isoformat()
    counts: dict[str, int] = defaultdict(int)
    try:
        _archive_into(config, day, root, iso, counts, broker=broker, fetch=fetch, prices=prices,
                      quote_filled=quote_filled)
    except OSError as exc:  # the root itself is unwritable (a missing drive, a full disk)
        logger.warning("daily review: archive root unwritable (%s)", type(exc).__name__)
        counts["error"] += 1
    return dict(counts)


def _archive_into(config, day, root, iso, counts, *, broker, fetch, prices, quote_filled=None) -> None:
    from pipeline.funnel import snapshot_path

    for session in ("cn", "us"):
        base = config.macro_brief_dir / f"{iso}.{session}"
        _copy(Path(f"{base}.md"), root / "macro_briefs" / iso / f"{session}.md", counts, "macro_briefs")
        _copy(Path(f"{base}.eval.json"), root / "macro_briefs" / iso / f"{session}.eval.json", counts, "macro_briefs")
        _copy(snapshot_path(config.state_dir, session, day), root / "funnel" / iso / f"{session}.json", counts, "funnel")
        _copy(config.state_dir / "research_digest" / session / f"{iso}.txt",
              root / "digests" / iso / f"{session}.txt", counts, "digests")
    briefs: list[Path] = []
    try:
        briefs = sorted(config.ticker_brief_dir.glob(f"*/{iso}.md"))
    except OSError:
        counts["ticker_briefs:missing"] += 1
    for brief in briefs:  # a write failure propagates: the day is not done
        ticker = brief.parent.name
        _copy(brief, root / "ticker_briefs" / iso / f"{ticker}.md", counts, "ticker_briefs")
        _copy(brief.with_name(f"{iso}.eval.json"), root / "ticker_briefs" / iso / f"{ticker}.eval.json",
              counts, "ticker_briefs")
    decisions = _jsonl_rows(config.ledger_dir / "decisions.jsonl")
    _write(root / "decisions" / iso / "decisions.jsonl",
           _jsonl_text(None if decisions is None else [r for r in decisions if r.get("date") == iso]),
           counts, "decisions")
    orders = _jsonl_rows(config.ledger_dir / "orders.jsonl")
    _write(root / "orders" / iso / "orders.jsonl", _jsonl_text(None if orders is None else [
        r for r in orders if str(r.get("client_order_id") or r.get("run_id") or "").startswith(iso)]),
        counts, "orders")
    cli = _jsonl_rows(_usage_log())
    _write(root / "llm_usage" / iso / "cli.jsonl",
           _jsonl_text(None if cli is None else [r for r in cli if _ny_date(r.get("ts")) == day]), counts, "llm_usage")
    analysis = _jsonl_rows(config.state_dir / "llm_usage.jsonl")
    _write(root / "llm_usage" / iso / "analysis.jsonl",
           _jsonl_text(None if analysis is None else [r for r in analysis if r.get("date") == iso]),
           counts, "llm_usage")
    tickers = set().union(*_day_tickers(config, day).values()) | set(BENCHMARKS.values())
    fetched: dict = {}
    handed = prices is not None  # the review's own read: its quote fills are these prices'
    if prices is None:
        prices = closes(tickers, fetch, first=BENCHMARKS.values(), day=day, stats=fetched)
    # The names whose archived close came from the quote — this run's own
    # fills, or the review's (``quote_filled``) when it hands its prices
    # over: an older marker keeps a name until this run refreshes its row
    # from a listed close (closes.csv never drops a row, so neither does the
    # marker). Every archive run writes it, so no path leaves a row unnamed.
    marker = root / "prices" / iso / "quote_filled.json"
    filled = {t for t in (*fetched.get("quote_filled_tickers", []), *((quote_filled or []) if handed else []))
              if t in tickers}
    listed = {t for t, series in prices.items() if t in tickers and t not in filled
              and any(d == day and c is not None for d, c in series)}
    try:
        loaded = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        loaded = []
    older = {name for name in loaded if isinstance(name, str)} if isinstance(loaded, list) else set()
    _write(marker, json.dumps(sorted((older - listed) | filled)), counts, "prices")
    session_of = {t: session for session, names in _day_tickers(config, day).items() for t in names}
    rows = [[iso, ticker, f"{close:.4f}",
             "" if (c := name_change(prices, ticker, session_of.get(ticker, "us"), day)) is None else f"{c:.2f}"]
            for ticker, series in sorted(prices.items()) if ticker in tickers
            for d, close in series if d == day and close is not None]
    dest = root / "prices" / iso / "closes.csv"
    merged: dict[str, list[str]] = {}
    with contextlib.suppress(OSError, UnicodeDecodeError):
        for row in list(csv.reader(dest.read_text(encoding="utf-8").splitlines()))[1:]:
            if len(row) == 4:
                merged[row[1]] = row
    before = len(merged)
    merged.update({row[1]: row for row in rows})  # re-archiving adds or refreshes rows, never drops one
    if rows:
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(["date", "ticker", "close", "change_pct"])
        writer.writerows(row for _ticker, row in sorted(merged.items()))
        _write(dest, buf.getvalue(), counts, "prices")
        if len(rows) < before:
            counts["prices:missing"] += before - len(rows)
    else:  # no closes read (a network failure, a date older than the data): keep what is there
        _write(dest, None, counts, "prices")
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
        fills = _fills(broker, day)
        _write(root / "broker" / iso / "fills.json", None if fills is None else json.dumps(fills, default=str, indent=1),
               counts, "broker")


def _fills(broker, day: date) -> list[dict] | None:
    try:
        start = datetime(day.year, day.month, day.day, tzinfo=NY).astimezone(timezone.utc)
        return [f for f in broker.fills_since(start)
                if _ny_date((f if isinstance(f, dict) else {}).get("transaction_time")) == day]
    except Exception as exc:
        logger.warning("daily review: fills unavailable (%s)", type(exc).__name__)
        return None


# ---------------------------------------------------------------------------
# R2 — facts and review
# ---------------------------------------------------------------------------


def _code(stages: dict, name: str) -> str | None:
    stage = stages.get(name)
    return stage.get("code") if isinstance(stage, dict) else None


def _names(config: PipelineConfig, session: str, day: date) -> list[dict]:
    """Structured per-name facts of a slot — never brief prose. Every funnel
    entry of the slot's own session is listed (a name without a brief too:
    a failed collection or a watch name can still be a missed move)."""
    from pipeline import research_digest as rd
    from pipeline.common import session_for_ticker

    tickers = {t.ticker: t for t in rd._tickers(config, session, day)[0]}
    rows = _jsonl_rows(config.ledger_dir / "decisions.jsonl") or []
    briefs_used = {str(r.get("run_id")): str(((r.get("inputs") or {}).get("ticker_brief") or {}).get("path") or "")
                   for r in rows if r.get("date") == day.isoformat() and isinstance(r.get("inputs"), dict)}
    out = []
    for entry in _snapshot(config, session, day):
        ticker = str(entry["ticker"])
        try:
            if session_for_ticker(ticker) != session:
                continue
            stages = entry.get("stages") if isinstance(entry.get("stages"), dict) else {}
            t = tickers.get(ticker)
            impact = rd._last_impact(t.body) if t is not None and t.has_brief else None
            decide = t.decide if t is not None else None
            details = (decide or {}).get("details") or {}
            used = briefs_used.get(str(details.get("run_id")), "")
            out.append({
                "ticker": ticker, "impact": impact[0] if impact else None,
                "score": t.score if t is not None else None, "has_brief": bool(t is not None and t.has_brief),
                "layer": entry.get("layer"), "collect": _code(stages, "collect"),
                "eval": _code(stages, "evaluate"),
                # the runner's own code (core / core-pair / catalyst / below-threshold …)
                "trigger": _code(stages, "trigger") or (t.trigger if t is not None else None),
                # a decision made on an older brief (served stale, contract gap = 1)
                "brief_used": (used.split("/")[-1].removesuffix(".md")
                               if used and day.isoformat() not in used else None),
                "decision": (decide or {}).get("code"), "pm_rating": details.get("pm_rating"),
                "arm": details.get("arm"), "final": t.final if t is not None else entry.get("final"),
                "execute": (t.execute or {}).get("code") if t is not None and isinstance(t.execute, dict) else None,
            })
        except Exception as exc:  # one malformed row never costs the review
            logger.warning("daily review: skipped a malformed funnel row (%s)", type(exc).__name__)
    return out


def _with_moves(names: list[dict], session: str, day: date, prices: dict) -> list[dict]:
    for row in names:
        move = name_change(prices, row["ticker"], session, day)
        bench = day_change(prices.get(_benchmark(row["ticker"], session), []), day)
        row["move_pct"] = None if move is None else round(move, 2)
        row["excess_pct"] = None if move is None or bench is None else round(move - bench, 2)
    return names


def forward_excess(prices: dict, ticker: str, session: str, day: date, sessions: int) -> float | None:
    """% excess vs the benchmark from the close before ``day`` to the close
    of the ``sessions``-th session from ``day`` on (the benchmark's bars are
    the calendar). None until that session has closed, or when the start or
    end close (the name's or the benchmark's) is missing; only those two
    closes are needed, so a missing bar inside the span does not matter."""
    bench = prices.get(_benchmark(ticker, session), [])
    dates = [d for d, _c in bench]
    if day not in dates:
        return None
    i = dates.index(day)
    if i == 0 or i + sessions - 1 >= len(dates):
        return None
    start, end = bench[i - 1], bench[i + sessions - 1]
    closes_by_date = dict(prices.get(ticker, []))
    t0, t1 = closes_by_date.get(start[0]), closes_by_date.get(end[0])
    if None in (start[1], end[1], t0, t1):
        return None
    return ((t1 / t0 - 1) - (end[1] / start[1] - 1)) * 100


def _scoreboard(config: PipelineConfig, day: date, prices: dict) -> dict:
    """Rolling, over the last SCOREBOARD_SESSIONS us sessions with a funnel
    snapshot: the excess return after each decision over HORIZONS sessions
    (from the close before the pre-market decision, so the first includes
    the overnight gap before the 09:45 execution), by decision, brief impact
    word and PM rating. A horizon not yet closed is left out of its bucket."""
    kinds = ("decision", "impact", "pm_rating")
    buckets = {h: {kind: defaultdict(list) for kind in kinds} for h in HORIZONS}
    d, seen = day, 0
    while seen < SCOREBOARD_SESSIONS and (day - d).days < 60:
        if d.weekday() < 5:
            try:
                names = _names(config, "us", d)
            except Exception as exc:
                logger.warning("daily review: scoreboard skipped %s (%s)", d, type(exc).__name__)
                names = []
            if names:
                seen += 1
            for row in names:
                for h in HORIZONS:
                    excess = forward_excess(prices, row["ticker"], "us", d, h)
                    if excess is None:
                        continue
                    for kind in kinds:
                        if row[kind]:
                            buckets[h][kind][row[kind]].append(excess)
        d -= timedelta(days=1)
    return {"sessions": seen, "definition": "excess vs SPY over 1/3/5 sessions from the close before the decision",
            "horizons": {f"{h}d": {kind: {key: {"n": len(v), "mean_excess_pct": round(sum(v) / len(v), 2)}
                                          for key, v in sorted(group.items())}
                                   for kind, group in buckets[h].items()} for h in HORIZONS}}


def _error_kinds(config: PipelineConfig, day: date) -> dict[str, int]:
    from pipeline.llm_usage import error_kind

    rows = _jsonl_rows(config.state_dir / "llm_usage.jsonl") or []
    out: dict[str, int] = defaultdict(int)
    for r in rows:
        if r.get("date") == day.isoformat() and r.get("error"):
            out[error_kind(str(r["error"]))] += 1
    return dict(out)


def history_prices(config: PipelineConfig, day: date, fetch=None, stats: dict | None = None) -> dict:
    """Closes for the benchmarks, the day's names, then the scoreboard
    window's, newest day first (a spent budget skips the oldest). Only the
    benchmarks and the day's names get a late close filled from the quote."""
    fill = set(BENCHMARKS.values()).union(*_day_tickers(config, day).values())
    first: list[str] = list(BENCHMARKS.values())
    for back in range(60):
        for _session, names in sorted(_day_tickers(config, day - timedelta(days=back)).items(), reverse=True):
            first += sorted(names)
    return closes(set(first), fetch, first=first, stats=stats, day=day, fill=fill)


def build_facts(config: PipelineConfig, day: date, *, prices: dict | None = None, broker=None,
                price_stats: dict | None = None) -> dict:
    """The day's structured facts for the review (specs/daily-review.md R2)."""
    from pipeline import research_digest as rd

    if prices is None:
        price_stats = {}
        prices = history_prices(config, day, stats=price_stats)
    facts: dict[str, Any] = {"date": day.isoformat()}
    if price_stats:  # a truncated or failed price read is a fact, not a quiet null
        facts["price_reads"] = {"skipped": price_stats.get("skipped", 0), "failed": price_stats.get("failed", 0),
                                "quote_filled": price_stats.get("quote_filled", 0),
                                "quote_filled_tickers": price_stats.get("quote_filled_tickers", []),
                                # which reads failed and why a missing close was not filled (diagnosis)
                                "failed_tickers": price_stats.get("failed_tickers", {}),
                                "unfilled": price_stats.get("unfilled", {})}
    try:
        from pipeline.config import core_analysis_sessions

        analysed = core_analysis_sessions()
    except ValueError:
        analysed = None
    for session in ("us", "cn"):
        names = _with_moves(_names(config, session, day), session, day, prices)
        try:
            macro = rd.macro_read(config, session, day)[0]
        except Exception as exc:  # a damaged macro brief costs only this line
            logger.warning("daily review: macro brief unreadable (%s)", type(exc).__name__)
            macro = "宏观简报无法读取"
        bench = day_change(prices.get(BENCHMARKS[session], []), day)
        facts[session] = {
            # By configuration (specs/llm-cost.md R6), not a fault: a session
            # whose core is not analysed lists its core names core-session-off.
            "core_analysed": None if analysed is None else session in analysed,
            "macro": macro,
            "benchmark_move_pct": None if bench is None else round(bench, 2),
            "names": names,
            "missed_moves": [{k: r[k] for k in ("ticker", "move_pct", "layer", "collect", "trigger")}
                             for r in names if r["decision"] is None and r["move_pct"] is not None
                             and abs(r["move_pct"]) >= MISSED_MOVE_PCT],
            "analysis_errors": sum(1 for r in names if r["decision"] == "ERROR"),
            "execute_outcomes": dict(sorted(_count(r["execute"] for r in names if r["execute"]).items())),
        }
    kinds = _error_kinds(config, day)
    unrecorded = sum(facts[s]["analysis_errors"] for s in ("us", "cn")) - sum(kinds.values())
    if unrecorded > 0:  # a run refused before any LLM call (a 401) writes no usage row
        kinds["unrecorded"] = unrecorded
    facts["analysis_error_kinds"] = kinds
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
        fills = _fills(broker, day)
        if fills is not None:
            facts["fills"] = [{k: f.get(k) for k in ("symbol", "side", "qty", "price", "transaction_time")}
                              for f in fills if isinstance(f, dict)]
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
            "title": {"type": "string"},
            "evidence": {"type": "string"},
            "severity": {"type": "string", "enum": list(SEVERITIES)}},
            "required": ["title", "evidence", "severity"], "additionalProperties": False}},
        "verdict": {"type": "string", "enum": list(VERDICTS)},
        "proposals": {"type": "array", "items": {"type": "object", "properties": {
            **{k: {"type": "string"} for k in ("change", "why", "evidence", "risk")}},
            "required": ["change", "why", "evidence", "risk"], "additionalProperties": False}},
        "watch": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["summary", "issues", "verdict", "proposals", "watch"],
    "additionalProperties": False,
}

REVIEW_PROMPT = """你是一个交易研究流水线的复盘员。下面是 {date} 这一天的结构化事实（JSON），
包括：美股（us）与 A股/港股（cn）每只股票的简报方向（impact: 偏多/偏空/中性/分歧）、催化剂分数、
评估结论、触发代码、分析结论（decision: BUY/SELL/HOLD/ERROR）与 PM 评级、执行结果、
当天涨跌（move_pct）和相对基准的超额（excess_pct；为 null 表示没有读到行情，不是零——
price_reads 记录行情读取被跳过（skipped，时间预算用完）和失败（failed）的股票数，大于 0 时相关统计不完整，要在总结里说明；
quote_filled 是 Yahoo 日线尚未更新、用收盘后实时报价补上当天收盘的股票数，属正常情况；
failed_tickers / unfilled 列出读取失败和未能补价的股票及原因，提到行情缺失时引用它们；
unfilled 原因是 no-session，或 quote-trade-date 且日期早于当天的，多半是该市场当天休市，不是数据故障）；
core_analysed 为 false 的市场按配置只做催化剂触发的研究，核心股不采集、不分析
（trigger 为 core-session-off），这是有意设计，不要当作问题或遗漏报告；
brief_used 不为空表示分析用的是那一天的旧简报（当天简报缺失时按合约沿用前一天的）；
未分析却大幅波动的股票（missed_moves）；宏观判断；分析出错的原因分类（unrecorded 表示出错但没有留下用量记录、原因未知，常见于调用前就被拒，如认证失败）；账户、持仓与成交；
以及最近 {sessions} 个交易日的滚动统计（scoreboard.horizons：决策后 1d/3d/5d 个交易日相对基准的超额收益，
按分析结论 decision、简报方向 impact、PM 评级 pm_rating 分组，含样本数；尚未走完的区间不计入）。

请用中文复盘：
1. summary：两三句话总结今天的操作和结果。
2. issues：今天暴露的问题，每条给出事实依据（引用具体股票和数字）和严重程度。没有问题就留空，不要编造。
3. verdict：策略是否需要优化——"需要优化"、"继续观察"或"暂不调整"。
4. proposals：如果需要优化，给出具体改动（改什么规则或阈值）、理由、依据（必须引用样本数和数字）、风险。
   样本数少于 10 的规律不足以支撑策略改动，只能列入 watch。
   判断规律以 3d/5d 为准：1d 只反映一天的行情（板块普涨普跌时尤其失真），1d 与 3d/5d 方向相反时，
   不得据 1d 提出策略改动；引用滚动统计时写明区间（如"5d n=15 平均 -8.55%"）。
5. watch：需要继续积累数据观察的现象。

规则：只使用下面的事实，不要搜索、不要引用外部信息、不要编造任何数字；数字写阿拉伯数字；
不要写网址、域名或任何 HTML/标记标签（比较可以写"大于/小于"或 >、<）；
每段文字不超过 {max_chars} 字，问题最多 {max_issues} 条、建议最多 {max_proposals} 条、观察最多 {max_watch} 条。
你只提建议，任何改动都由人决定。

{facts}"""


def _run_review(prompt: str) -> dict:
    """One hermetic call on the evaluator's model, schema-bound JSON. A call
    that used a web tool is rejected: the review is the facts' alone."""
    from pipeline.claude_cli import (
        EnvelopeError,
        _web_search_requests,
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
    calls = envelope.get("_tool_calls") if isinstance(envelope.get("_tool_calls"), dict) else {}
    if (any(calls.get(field) for field in ("web_search_queries", "web_fetch_urls", "web_fetch_requests"))
            or _web_search_requests(envelope) > 0):
        raise RuntimeError("review call used web tools")
    return output


def checked_review(review: object) -> dict:
    """The review if every field has its schema shape and no text carries a
    link or markup; raises otherwise (the caller falls back to the facts)."""
    from pipeline.research_digest import _DOMAIN, _SYMBOL

    def text(value: object) -> str:
        if not isinstance(value, str) or _UNSAFE_TEXT.search(value):
            raise ValueError("a review field is not plain text")
        if len(value.strip()) > MAX_FIELD_CHARS:
            raise ValueError("a review field is too long")
        if any(not _SYMBOL.fullmatch(token) for token in _DOMAIN.findall(value)):
            raise ValueError("a review field carries a host")
        return value.strip()

    if not isinstance(review, dict) or review.get("verdict") not in VERDICTS:
        raise ValueError("verdict outside the schema")
    issues, proposals, watch = review.get("issues"), review.get("proposals"), review.get("watch")
    if not all(isinstance(v, list) for v in (issues, proposals, watch)):
        raise ValueError("a review list is not a list")
    if any(len(review[k]) > n for k, n in MAX_ITEMS.items()):
        raise ValueError("a review list is too long")
    for issue in issues:
        if not isinstance(issue, dict) or issue.get("severity") not in SEVERITIES:
            raise ValueError("an issue is malformed")
    return {
        "summary": text(review.get("summary")), "verdict": review["verdict"],
        "issues": [{"title": text(i.get("title")), "evidence": text(i.get("evidence")), "severity": i["severity"]}
                   for i in issues],
        "proposals": [{k: text((p if isinstance(p, dict) else {}).get(k)) for k in ("change", "why", "evidence", "risk")}
                      for p in proposals],
        "watch": [text(w) for w in watch],
    }


def render(day: date, facts: dict, review: dict | None) -> str:
    """The Lark message (and the archived markdown body)."""
    lines = [f"🧭 每日复盘 · {day.isoformat()}"]
    account = facts.get("account") or {}
    try:
        equity, last = float(account.get("equity")), float(account.get("last_equity"))
        lines.append(f"账户：权益 ${equity:,.2f}（今日 {equity - last:+,.2f}）｜持仓 {len(facts.get('positions') or [])} 只"
                     f"｜成交 {len(facts.get('fills') or [])} 笔")
    except (TypeError, ValueError):
        pass
    us = facts.get("us") or {}
    if us.get("benchmark_move_pct") is not None:
        lines.append(f"大盘：SPY {us['benchmark_move_pct']:+.2f}%")
    reads = facts.get("price_reads") or {}
    def names(found: dict) -> str:
        shown = "、".join(f"{t}（{why}）" for t, why in list(found.items())[:5])
        return shown + (f" 等 {len(found)} 只" if len(found) > 5 else "")

    if reads.get("skipped") or reads.get("failed"):
        lines.append(f"⚠️ 行情读取不完整：跳过 {reads.get('skipped', 0)} 只、失败 {reads.get('failed', 0)} 只，相关涨跌与统计缺失"
                     + (f"；失败：{names(reads['failed_tickers'])}" if reads.get("failed_tickers") else ""))
    unfilled = reads.get("unfilled") or {}
    # The quote's last trade on an earlier day: that market most likely did
    # not trade (a holiday) — a note, not a data fault.
    closed = {t: why for t, why in unfilled.items() if why == "no-session" or (
        str(why).startswith("quote-trade-date:") and str(why).split(":", 1)[1] < str(facts.get("date")))}
    faults = {t: why for t, why in unfilled.items() if t not in closed}
    if faults:
        lines.append(f"⚠️ 当天收盘未补上：{names(faults)}")
    if closed:
        lines.append(f"ℹ️ 可能休市（报价停在前一交易日）：{names(closed)}")
    if review is None:
        lines.append("复盘模型不可用，以下为当天事实：")
        for session, label in (("us", "美股"), ("cn", "A股/港股")):
            block = facts.get(session) or {}
            missed = ", ".join(f"{r['ticker']} {r['move_pct']:+.1f}%" for r in block.get("missed_moves") or [])
            lines.append(f"• {label}：分析出错 {block.get('analysis_errors', 0)} 次"
                         + (f"；未分析但大幅波动：{missed}" if missed else ""))
        if facts.get("error"):
            lines.append(f"• 事实整理失败：{facts['error']}")
        return "\n".join(lines)
    lines += ["", f"总结：{review['summary']}"]
    if review["issues"]:
        lines += ["", "问题："] + [f"• [{i['severity']}] {i['title']} — {i['evidence']}" for i in review["issues"]]
    lines += ["", f"策略：{review['verdict']}"]
    for p in review["proposals"]:
        lines.append(f"• 建议：{p['change']}\n  理由：{p['why']}\n  依据：{p['evidence']}\n  风险：{p['risk']}")
    if review["watch"]:
        lines += ["", "继续观察："] + [f"• {w}" for w in review["watch"]]
    lines += ["", "（复盘只提建议，策略改动需你确认）"]
    return "\n".join(lines)


def review_day(config: PipelineConfig, day: date, *, broker=None, send=None, runner=None,
               return_archived: bool = False):
    """Facts, review, deliver, archive; returns the text. Every step but the
    send is guarded: whatever fails, the day's message is still queued. The
    review as queued is kept in state first, so a retry (the archive failed)
    re-queues and archives that text — no second model call."""
    from pipeline import trade_notify

    prices: dict = {}
    price_stats: dict = {}  # the price read's own counts: they outlive a failed facts build
    saved = _load_queued(config, day)
    if saved is not None:
        facts, review, text = saved["facts"], saved["review"], saved["text"]
    else:
        try:
            prices = history_prices(config, day, stats=price_stats)
            facts = build_facts(config, day, prices=prices, broker=broker, price_stats=price_stats)
        except Exception as exc:
            logger.warning("daily review: facts failed (%s)", type(exc).__name__)
            facts = {"date": day.isoformat(), "error": type(exc).__name__}
        review = None
        if "error" not in facts:
            try:
                review = checked_review((runner or _run_review)(REVIEW_PROMPT.format(
                    date=day.isoformat(), sessions=facts["scoreboard"]["sessions"], max_chars=MAX_FIELD_CHARS,
                    max_issues=MAX_ITEMS["issues"], max_proposals=MAX_ITEMS["proposals"],
                    max_watch=MAX_ITEMS["watch"],
                    facts=json.dumps(facts, ensure_ascii=False, default=str))))
            except Exception as exc:
                logger.warning("daily review: model unavailable (%s) — facts only", type(exc).__name__)
        text = render(day, facts, review)
        if len(text) > MAX_MESSAGE_CHARS:  # a backstop: the facts, never a body the webhook may refuse
            logger.warning("daily review: message too long (%d chars) — facts only", len(text))
            review, text = None, render(day, facts, None)[:MAX_MESSAGE_CHARS]
        try:
            path = _queued_path(config, day)
            path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write(path, json.dumps({"facts": facts, "review": review, "text": text}, ensure_ascii=False,
                                          default=str))
        except OSError as exc:
            logger.warning("daily review: queued review not kept (%s)", type(exc).__name__)
    if send is not None:
        trade_notify.enqueue(config.state_dir, f"review:{day.isoformat()}", text, send)
    archived = False
    try:  # after the model call, so its usage row is in the day's archive
        counts = archive_day(config, day, broker=broker, prices=prices or None,
                             quote_filled=price_stats.get("quote_filled_tickers"))
        root = archive_root(config) / "reviews" / day.isoformat()
        root.mkdir(parents=True, exist_ok=True)
        atomic_write(root / "review.md", text + "\n")
        atomic_write(root / "review.json", json.dumps({"facts": facts, "review": review}, ensure_ascii=False,
                                                       default=str, indent=1))
        archived = not counts.get("error")
    except Exception as exc:
        logger.warning("daily review: archive failed (%s) — retried by the next watchdog run", type(exc).__name__)
    return text if not return_archived else (text, archived)


# ---------------------------------------------------------------------------
# R3 — schedule (the hourly watchdog)
# ---------------------------------------------------------------------------


def _queued_path(config: PipelineConfig, day: date) -> Path:
    return config.state_dir / "daily_review" / f"{day.isoformat()}.queued.json"


def _load_queued(config: PipelineConfig, day: date) -> dict | None:
    try:
        saved = json.loads(_queued_path(config, day).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if isinstance(saved, dict) and isinstance(saved.get("text"), str) and isinstance(saved.get("facts"), dict):
        return saved
    return None


def _marks(config: PipelineConfig, day: date) -> tuple[Path, Path]:
    base = config.state_dir / "daily_review"
    return base / f"{day.isoformat()}.started", base / f"{day.isoformat()}.done"


#: A day left undone (the host off all evening, the drive missing past
#: midnight) is still picked up this many days later — Monday reaches Friday.
LOOKBACK_DAYS = 3


def due(config: PipelineConfig, now: datetime) -> date | None:
    """The oldest New York date whose review is due now, else None: a
    weekday within LOOKBACK_DAYS (today only past REVIEW_AFTER) with a us
    funnel snapshot, not done, and not started within STUCK_AFTER."""
    from pipeline.funnel import snapshot_path

    local = now.astimezone(NY)
    for back in range(LOOKBACK_DAYS, -1, -1):
        day = local.date() - timedelta(days=back)
        if day.weekday() >= 5 or (back == 0 and local.time() < REVIEW_AFTER):
            continue
        if not snapshot_path(config.state_dir, "us", day).exists():
            continue
        started, done = _marks(config, day)
        if done.exists():
            continue
        if started.exists():
            try:
                at = datetime.fromisoformat(started.read_text(encoding="utf-8").strip())
            except ValueError:
                at = now - STUCK_AFTER
            if now - at < STUCK_AFTER:
                continue
        return day
    return None


def maybe_start(config: PipelineConfig, now: datetime, *, runner=None) -> bool:
    """The watchdog's share: run the due review inline (well inside the
    watchdog task's limit), with started/done marks. Returns whether it ran."""
    day = due(config, now)
    if day is None:
        return False
    started, _done = _marks(config, day)
    started.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(started, now.astimezone(timezone.utc).isoformat())
    (runner or run)(config, day)
    return True


def run(config: PipelineConfig, day: date) -> int:
    """Review ``day`` and deliver it. The done mark is written once the
    message is queued (or no trades channel exists) and the archive was
    written; an unwritable archive leaves the day for the stuck retry. A
    market holiday (the broker's calendar) is archived, never reviewed."""
    from pipeline import trade_notify
    from pipeline.trade_monitor import make_trades_sender

    broker = None  # read-only: the calendar for any day, the snapshot only for today
    with contextlib.suppress(Exception):
        broker = trade_notify.AlpacaReadOnly(*trade_notify.alpaca_keys())
    _started, done = _marks(config, day)
    trading = True
    if broker is not None:
        try:
            trading = broker.is_trading_day(day)
        except Exception as exc:  # no calendar: the weekday rule stands
            logger.warning("daily review: calendar unavailable (%s)", type(exc).__name__)
    if not trading:
        logger.info("daily review: %s is a market holiday — archived, not reviewed", day)
        if archive_day(config, day).get("error"):
            return 1
        done.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(done, datetime.now(timezone.utc).isoformat())
        return 0
    send = make_trades_sender(config) if config.notifications_enabled else None
    if send is None and config.notifications_enabled:
        logger.warning("daily review: no trades webhook configured — the review is archived, not sent")
    _text, archived = review_day(config, day, broker=broker if day == datetime.now(NY).date() else None,
                                 send=send, return_archived=True)
    if not archived:
        # The message is queued (key review:<day>, never resent); the archive
        # is retried by the next watchdog run after STUCK_AFTER.
        return 1
    done.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(done, datetime.now(timezone.utc).isoformat())
    return 0


def main(argv: list[str] | None = None) -> int:
    from pipeline.orchestrator import _load_repo_dotenv

    _load_repo_dotenv()  # like every job: the webhook, keys and routing live in .env
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
