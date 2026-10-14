"""Alpaca data access with on-disk pickle caches (SPEC.md §1, §3).

Daily bars: split-adjusted SIP, cached per symbol batch (resumable).
Intraday: 5-min split-adjusted SIP bars, regular session only, cached per day.

Adjustment consistency: intraday files carry the split adjustment that was current when
they were fetched. When a re-fetch of the daily cache shows that a symbol's adjusted
history moved (a split or other adjustment booked since the last fetch), the cached
intraday days of that symbol are forgotten (`invalidate_intraday`) so the next
`fetch-intraday` re-downloads them on the same basis as the daily bars. Do not run
fetch-daily and fetch-intraday concurrently.
"""

from __future__ import annotations

import logging
import os
import pickle
import re
import subprocess
import threading
import time
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from .config import BENCHMARKS, data_dir
from .models import DailyPanel

log = logging.getLogger(__name__)
ET = ZoneInfo("America/New_York")
EXCHANGES = {"NYSE", "NASDAQ", "AMEX", "ARCA", "BATS"}
_SYMBOL_RE = re.compile(r"^[A-Z]{1,5}$")
# SPEC §3 name exclusions. Word-anchored where a bare substring hits operating companies
# ("Rights" in BrightSpring, " Unit" in Manchester United, " Fund" in Asta Funding).
_NAME_EXCLUDE = re.compile(
    r" ETF|\bETNs?\b| Funds?\b|\bWarrants?\b|\bUnits?\b|\bRights\b|Preferred| Notes|%"
    r"|ProShares|Direxion|iShares|SPDR|Invesco|Leveraged|Inverse| 2X| 3X",
    re.IGNORECASE,
)
# " Trust" excludes grantor/statutory trusts and trust-named funds, but not a corporation
# or bank holding company that merely has "Trust" in its name (Northern Trust Corporation,
# Washington Trust Bancorp).
_NAME_TRUST = re.compile(r" Trust", re.IGNORECASE)
_NAME_CORPORATE = re.compile(r"\b(?:Corporation|Corp|Bancorp|Bancshares)\b", re.IGNORECASE)
DAILY_BATCH = 200
# A re-fetched split-adjusted close that differs from the cached one by more than this
# fraction means the adjustment changed (or the bar was corrected): cached intraday bars
# for that symbol-day are on another price scale and must be re-fetched.
ADJ_CHANGE_TOL = 0.005


# --------------------------------------------------------------------------- creds/clients
def load_credentials() -> tuple[str, str]:
    key, secret = os.environ.get("ALPACA_API_KEY_ID"), os.environ.get("ALPACA_API_SECRET_KEY")
    if key and secret:
        return key, secret
    from dotenv import dotenv_values

    env_path = os.environ.get("LUK_DOTENV")
    if not env_path:
        common = subprocess.run(
            ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
            capture_output=True,
            text=True,
            check=True,
            cwd=Path(__file__).resolve().parent,
        ).stdout.strip()
        env_path = str(Path(common).parent / ".env")
    vals = dotenv_values(env_path)
    key, secret = vals.get("ALPACA_API_KEY_ID"), vals.get("ALPACA_API_SECRET_KEY")
    if not (key and secret):
        raise RuntimeError(f"Alpaca credentials not found (looked in env and {env_path})")
    return key, secret


def _data_client():
    from alpaca.data.historical import StockHistoricalDataClient

    k, s = load_credentials()
    return StockHistoricalDataClient(k, s)


class _RateLimiter:
    """Simple shared pacing: at most `per_min` calls per minute."""

    def __init__(self, per_min: int = 180):
        self.interval = 60.0 / per_min
        self.lock = threading.Lock()
        self.next_t = 0.0

    def wait(self) -> None:
        with self.lock:
            now = time.monotonic()
            t = max(now, self.next_t)
            self.next_t = t + self.interval
        if t > now:
            time.sleep(t - now)


def _with_retry(fn: Callable, *, tries: int = 6, base: float = 2.0):
    for i in range(tries):
        try:
            return fn()
        except Exception as e:  # network/429/5xx
            if i == tries - 1:
                raise
            wait = base * (2**i)
            log.warning("alpaca call failed (%s); retry in %.0fs", type(e).__name__, wait)
            time.sleep(wait)
    return None


# --------------------------------------------------------------------------- assets
def fetch_assets(cache: Path | None = None, refresh: bool = False) -> pd.DataFrame:
    cache = cache or data_dir()
    path = cache / "assets.pkl"
    if path.exists() and not refresh:
        return pd.read_pickle(path)
    from alpaca.trading.client import TradingClient
    from alpaca.trading.enums import AssetClass, AssetStatus
    from alpaca.trading.requests import GetAssetsRequest

    k, s = load_credentials()
    tc = TradingClient(k, s, paper=True)
    rows = []
    for st in (AssetStatus.ACTIVE, AssetStatus.INACTIVE):
        for a in tc.get_all_assets(GetAssetsRequest(asset_class=AssetClass.US_EQUITY, status=st)):
            rows.append(
                {
                    "symbol": a.symbol,
                    "name": a.name or "",
                    "exchange": str(getattr(a.exchange, "value", a.exchange)),
                    "status": str(getattr(a.status, "value", a.status)),
                }
            )
    df = pd.DataFrame(rows).drop_duplicates("symbol")
    df.to_pickle(path)
    return df


def filter_symbols(assets: pd.DataFrame) -> list[str]:
    name = assets["name"].fillna("")
    excluded = name.str.contains(_NAME_EXCLUDE) | (
        name.str.contains(_NAME_TRUST) & ~name.str.contains(_NAME_CORPORATE)
    )
    ok = assets["exchange"].isin(EXCHANGES) & assets["symbol"].str.match(_SYMBOL_RE) & ~excluded
    syms = set(assets.loc[ok, "symbol"]) | set(BENCHMARKS)
    return sorted(syms)


# --------------------------------------------------------------------------- daily
def _bars_to_long(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame(columns=["symbol", "date", "open", "high", "low", "close", "volume"])
    df = df.reset_index()
    ts = pd.to_datetime(df["timestamp"], utc=True).dt.tz_convert(ET)
    out = pd.DataFrame(
        {
            "symbol": df["symbol"].astype(str),
            "date": ts.dt.tz_localize(None).dt.normalize(),
            "open": df["open"].astype("float64"),
            "high": df["high"].astype("float64"),
            "low": df["low"].astype("float64"),
            "close": df["close"].astype("float64"),
            "volume": df["volume"].astype("float64"),
        }
    )
    return out


def _read_batch(path: Path) -> dict | None:
    """A cached daily batch, or None if the file is truncated/corrupt (e.g. a write
    interrupted by an older version); such a batch is simply re-fetched."""
    try:
        return pd.read_pickle(path)
    except (EOFError, pickle.UnpicklingError, ValueError) as e:
        log.warning("unreadable daily batch %s (%s); will re-fetch", path.name, type(e).__name__)
        return None


def _write_batch_atomic(path: Path, meta: dict) -> None:
    """Serialize to a sibling temp file, then rename over the batch, so an interruption
    never leaves a truncated batch or destroys the previous valid one."""
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "wb") as f:
        pickle.dump(meta, f)
    tmp.replace(path)


def _batch_covers(meta: dict, syms: list[str], start: date, end: date) -> bool:
    """True if a cached batch already holds `syms` over [start, end]: same symbols and
    end, and a recorded start on or before the requested one."""
    cached_start = meta.get("start")
    return (
        meta.get("symbols") == syms
        and meta.get("end") == end.isoformat()
        and cached_start is not None
        and date.fromisoformat(cached_start) <= start
    )


def fetch_daily(
    symbols: list[str],
    start: date,
    end: date,
    cache: Path | None = None,
    batch: int = DAILY_BATCH,
    raw: bool = False,
) -> None:
    """Fetch split-adjusted (or, with raw=True, unadjusted) SIP daily bars; one
    pickle per symbol batch. Resumable: a batch whose file exists with the same
    symbols and end, and whose cached start is on or before `start`, is skipped (a
    batch without a recorded start is re-fetched). Unadjusted closes back the
    point-in-time price filter.

    Split-adjusted re-fetches are compared with the previous cache; symbol-days whose
    adjusted close moved are dropped from the intraday cache (`invalidate_intraday`)
    before the new batch is written, so a crash in between only repeats the check."""
    from alpaca.data.enums import Adjustment, DataFeed
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame

    root = cache or data_dir()
    cache = root / ("daily_raw" if raw else "daily")
    cache.mkdir(parents=True, exist_ok=True)
    client = _data_client()
    batches = [symbols[i : i + batch] for i in range(0, len(symbols), batch)]
    todo: list[int] = []
    for bi, syms in enumerate(batches):
        path = cache / f"batch_{bi:04d}.pkl"
        meta = _read_batch(path) if path.exists() else None
        if meta is not None and _batch_covers(meta, syms, start, end):
            continue
        todo.append(bi)
    # Previous adjusted closes (all batches, before any file is removed or replaced): the
    # reference for detecting adjustment changes, even for symbols that moved between batches.
    old_close = None
    if todo and not raw and any(cache.glob("batch_*.pkl")):
        old_close = load_daily_long(root, skip_unreadable=True)[["symbol", "date", "close"]]
    # Drop batch files beyond the current batch count (left over from a longer symbol list):
    # load_daily_long reads every batch_*.pkl and keeps the LAST duplicate, so a stale
    # high-numbered file would silently override fresh rows with old adjustments.
    for stale in cache.glob("batch_*.pkl"):
        idx = stale.stem.removeprefix("batch_")
        if idx.isdigit() and int(idx) >= len(batches):
            log.info("removing stale daily batch %s", stale.name)
            stale.unlink()
    for bi in todo:
        syms = batches[bi]
        path = cache / f"batch_{bi:04d}.pkl"
        req = StockBarsRequest(
            symbol_or_symbols=syms,
            timeframe=TimeFrame.Day,
            start=datetime(start.year, start.month, start.day),
            end=datetime(end.year, end.month, end.day, 23, 59),
            feed=DataFeed.SIP,
            adjustment=Adjustment.RAW if raw else Adjustment.SPLIT,
        )
        t0 = time.time()
        bars = _with_retry(lambda req=req: client.get_stock_bars(req).df)
        long = _bars_to_long(bars)
        if old_close is not None:
            changes = adjustment_changes(old_close[old_close["symbol"].isin(syms)], long)
            if changes:
                n = invalidate_intraday(changes, root)
                log.info(
                    "daily batch %d: adjustment changed for %d symbols; %d cached intraday "
                    "symbol-days dropped for re-fetch",
                    bi + 1,
                    len(changes),
                    n,
                )
        _write_batch_atomic(
            path, {"symbols": syms, "start": start.isoformat(), "end": end.isoformat(), "bars": long}
        )
        log.info(
            "daily batch %d/%d: %d rows in %.1fs", bi + 1, len(batches), len(long), time.time() - t0
        )


def adjustment_changes(old: pd.DataFrame, new: pd.DataFrame) -> dict[str, set[date]]:
    """symbol -> session dates whose split-adjusted close differs between two fetches by
    more than ADJ_CHANGE_TOL (a split/adjustment booked in between, or a corrected bar)."""
    if old is None or new is None or len(old) == 0 or len(new) == 0:
        return {}
    m = old[["symbol", "date", "close"]].merge(
        new[["symbol", "date", "close"]], on=["symbol", "date"], suffixes=("_old", "_new")
    )
    rel = (m["close_new"] / m["close_old"] - 1.0).abs()  # NaN/inf-safe: NaN never > tol
    hit = m[np.isfinite(rel) & (rel > ADJ_CHANGE_TOL)]
    out: dict[str, set[date]] = {}
    for sym, g in hit.groupby("symbol"):
        out[str(sym)] = {ts.date() for ts in pd.to_datetime(g["date"])}
    return out


def invalidate_intraday(changes: dict[str, set[date]], cache: Path | None = None) -> int:
    """Forget cached 5-min bars for the given symbol-days (bars and `requested` entry), so
    the next fetch-intraday re-downloads them on the current adjustment basis. Returns the
    number of symbol-days dropped. Atomic per file."""
    root = cache or data_dir()
    by_day: dict[date, set[str]] = {}
    for sym, days in changes.items():
        for d in days:
            by_day.setdefault(d, set()).add(sym)
    dropped = 0
    for d in sorted(by_day):
        path = _intraday_path(root, d)
        if not path.exists():
            continue
        blob = pd.read_pickle(path)
        hit = by_day[d] & set(blob["requested"])
        if not hit:
            continue
        bars = blob["bars"]
        bars = bars[~bars["symbol"].isin(hit)].reset_index(drop=True)
        requested = sorted(set(blob["requested"]) - hit)
        tmp = path.with_suffix(".tmp")
        with open(tmp, "wb") as f:
            pickle.dump({"bars": bars, "requested": requested}, f)
        tmp.replace(path)
        dropped += len(hit)
    return dropped


def load_daily_long(
    cache: Path | None = None, raw: bool = False, skip_unreadable: bool = False
) -> pd.DataFrame:
    """All cached daily bars. An unreadable batch raises (a backtest must not silently
    lose symbols) unless `skip_unreadable`, used only by the fetcher's own bookkeeping."""
    cache = (cache or data_dir()) / ("daily_raw" if raw else "daily")
    parts = []
    for p in sorted(cache.glob("batch_*.pkl")):
        meta = _read_batch(p) if skip_unreadable else pd.read_pickle(p)
        if meta is not None:
            parts.append(meta["bars"])
    if not parts:
        raise FileNotFoundError(f"no daily cache in {cache}; run `cli fetch-daily` first")
    df = pd.concat(parts, ignore_index=True)
    return df.drop_duplicates(["symbol", "date"], keep="last")


def drop_placeholder_bars(long: pd.DataFrame) -> pd.DataFrame:
    """Drop zero-volume flat bars (volume == 0 and high == low). Alpaca emits these as
    placeholders after delistings / before relistings; kept, they splice different
    issuers into one history and defeat hist_len / data_end (they are not trades)."""
    placeholder = (long["volume"] <= 0) & (long["high"] == long["low"])
    return long[~placeholder]


def load_daily_panel(
    cache: Path | None = None,
    start: date | None = None,
    end: date | None = None,
    prefilter_adv: float = 5e6,
    prefilter_price: float = 3.0,
) -> DailyPanel:
    """Wide panel restricted to symbols that ever had a 20d ADV >= prefilter_adv
    with close >= prefilter_price (a superset of every point-in-time universe in
    SPEC §3, so the prefilter introduces no look-ahead). Benchmarks always kept.

    Price thresholds use unadjusted closes from the `daily_raw` cache when present:
    split-adjusted history would encode future splits (e.g. NVDA ~$6 in 2018).
    Dollar volume is split-invariant, so it is computed from the adjusted bars."""
    long = drop_placeholder_bars(load_daily_long(cache))
    if start is not None:
        long = long[long["date"] >= pd.Timestamp(start)]
    if end is not None:
        long = long[long["date"] <= pd.Timestamp(end)]
    raw_dir = (cache or data_dir()) / "daily_raw"
    has_raw = any(raw_dir.glob("batch_*.pkl"))
    if has_raw:
        raw = load_daily_long(cache, raw=True)[["symbol", "date", "open", "close"]]
        raw = raw.rename(columns={"open": "raw_open", "close": "raw_close"})
        long = long.merge(raw, on=["symbol", "date"], how="left")
    else:
        log.warning("no daily_raw cache: price floors fall back to split-adjusted closes")
        long = long.assign(raw_close=long["close"], raw_open=float("nan"))
    long = long.sort_values(["symbol", "date"])
    dv = long["close"] * long["volume"]
    adv = dv.groupby(long["symbol"]).transform(lambda s: s.rolling(20, min_periods=20).mean())
    px = long["raw_close"].fillna(long["close"])
    keep = set(long.loc[(adv >= prefilter_adv) & (px >= prefilter_price), "symbol"])
    keep |= set(BENCHMARKS)
    long = long[long["symbol"].isin(keep)]
    fields = {}
    for col in ("open", "high", "low", "close", "volume", "raw_close", "raw_open"):
        fields[col] = long.pivot(index="date", columns="symbol", values=col).sort_index()
    cols = sorted(fields["close"].columns)
    idx = fields["close"].index
    aligned = {k: v.reindex(index=idx, columns=cols).astype("float64") for k, v in fields.items()}
    # a missing raw close (rare) falls back to the adjusted close for that cell; raw_open is
    # left NaN there (signals uses it only where the raw row exists)
    aligned["raw_close"] = aligned["raw_close"].fillna(aligned["close"])
    if not has_raw:
        aligned["raw_open"] = None  # type: ignore[assignment]
    return DailyPanel(**aligned)


# --------------------------------------------------------------------------- intraday
def _intraday_path(cache: Path, day: date) -> Path:
    return cache / "intraday" / f"{day.isoformat()}.pkl"


def _fetch_intraday_day(client, limiter: _RateLimiter, day: date, symbols: list[str]) -> pd.DataFrame:
    from alpaca.data.enums import Adjustment, DataFeed
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

    start = datetime(day.year, day.month, day.day, 9, 30, tzinfo=ET)
    end = datetime(day.year, day.month, day.day, 16, 0, tzinfo=ET)
    req = StockBarsRequest(
        symbol_or_symbols=symbols,
        timeframe=TimeFrame(5, TimeFrameUnit.Minute),
        start=start,
        end=end,
        feed=DataFeed.SIP,
        adjustment=Adjustment.SPLIT,
    )

    def call():
        limiter.wait()
        return client.get_stock_bars(req).df

    df = _with_retry(call)
    if df is None or df.empty:
        return pd.DataFrame(columns=["symbol", "ts", "open", "high", "low", "close", "volume"])
    df = df.reset_index()
    ts = pd.to_datetime(df["timestamp"], utc=True).dt.tz_convert(ET)
    out = pd.DataFrame(
        {
            "symbol": df["symbol"].astype(str),
            "ts": ts,
            "open": df["open"].astype("float64"),
            "high": df["high"].astype("float64"),
            "low": df["low"].astype("float64"),
            "close": df["close"].astype("float64"),
            "volume": df["volume"].astype("float64"),
        }
    )
    # regular session only: bar start in [09:30, 16:00)
    mins = out["ts"].dt.hour * 60 + out["ts"].dt.minute
    out = out[(mins >= 9 * 60 + 30) & (mins < 16 * 60)]
    return out.sort_values(["symbol", "ts"]).reset_index(drop=True)


def fetch_intraday(
    requests: dict[date, Iterable[str]],
    cache: Path | None = None,
    workers: int = 4,
    per_min: int = 180,
) -> None:
    """Fetch 5-min bars for each (day, symbols). Resumable per day: only symbols
    not yet requested for that day are fetched; the file records `requested`."""
    cache = cache or data_dir()
    (cache / "intraday").mkdir(parents=True, exist_ok=True)
    client = _data_client()
    limiter = _RateLimiter(per_min)
    todo: list[tuple[date, list[str], dict | None]] = []
    for day, syms in sorted(requests.items()):
        syms = sorted(set(syms))
        path = _intraday_path(cache, day)
        existing = pd.read_pickle(path) if path.exists() else None
        have = set(existing["requested"]) if existing else set()
        missing = [s for s in syms if s not in have]
        if missing:
            todo.append((day, missing, existing))
    log.info("intraday: %d days need fetching", len(todo))

    def work(item):
        day, missing, existing = item
        new = _fetch_intraday_day(client, limiter, day, missing)
        if existing:
            bars = pd.concat([existing["bars"], new], ignore_index=True)
            requested = sorted(set(existing["requested"]) | set(missing))
        else:
            bars, requested = new, sorted(missing)
        path = _intraday_path(cache, day)
        tmp = path.with_suffix(".tmp")
        with open(tmp, "wb") as f:
            pickle.dump({"bars": bars, "requested": requested}, f)
        tmp.replace(path)
        return day

    done = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(work, it) for it in todo]
        for fut in as_completed(futs):
            fut.result()
            done += 1
            if done % 50 == 0 or done == len(todo):
                log.info("intraday: %d/%d days", done, len(todo))


def fetch_calendar(
    cache: Path | None = None, start: date = date(2016, 1, 1), end: date | None = None
) -> dict[date, int]:
    """Session close (minutes after midnight ET) per trading day, from Alpaca's
    calendar; cached in calendar.pkl. Early closes (e.g. 13:00) matter because the
    5-min fetch window runs to 16:00 and would otherwise admit after-hours bars."""
    path = (cache or data_dir()) / "calendar.pkl"
    end = end or (datetime.now(ET).date() + timedelta(days=30))
    if path.exists():
        cal = pd.read_pickle(path)
        if cal and min(cal) <= start and max(cal) >= min(end, last_complete_session()):
            return cal
    from alpaca.trading.client import TradingClient
    from alpaca.trading.requests import GetCalendarRequest

    k, s = load_credentials()
    days = TradingClient(k, s, paper=True).get_calendar(GetCalendarRequest(start=start, end=end))
    cal = {c.date: c.close.hour * 60 + c.close.minute for c in days}
    with open(path, "wb") as f:
        pickle.dump(cal, f)
    return cal


class IntradayStore:
    """Lazy per-day loader: store(day) -> {symbol: bars DataFrame} (sorted by ts).

    When calendar.pkl exists, bars starting at/after that day's session close are
    dropped (half-days close at 13:00; later bars are after-hours trading)."""

    def __init__(self, cache: Path | None = None):
        self.cache = cache or data_dir()
        cal_path = self.cache / "calendar.pkl"
        self.closes: dict[date, int] | None = pd.read_pickle(cal_path) if cal_path.exists() else None
        if self.closes is None:
            log.warning("no calendar.pkl: half-day sessions keep after-hours bars")

    def __call__(self, day: date) -> dict[str, pd.DataFrame] | None:
        path = _intraday_path(self.cache, day)
        if not path.exists():
            return None
        d = pd.read_pickle(path)
        bars = d["bars"]
        close_min = (self.closes or {}).get(day)
        if close_min is not None and close_min < 16 * 60 and not bars.empty:
            mins = bars["ts"].dt.hour * 60 + bars["ts"].dt.minute
            bars = bars[mins < close_min]
        return {s: g.reset_index(drop=True) for s, g in bars.groupby("symbol", sort=False)}


def last_complete_session(today: date | None = None) -> date:
    """Conservative end date: yesterday (SIP requires >15 min delay)."""
    today = today or datetime.now(ET).date()
    d = today - timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


__all__ = [
    "IntradayStore",
    "drop_placeholder_bars",
    "fetch_calendar",
    "fetch_assets",
    "fetch_daily",
    "fetch_intraday",
    "filter_symbols",
    "last_complete_session",
    "load_credentials",
    "load_daily_long",
    "load_daily_panel",
]
