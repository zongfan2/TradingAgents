"""Offline tests for data.py: symbol filter, daily bar parsing, panel prefilter,
intraday fetch resumability / session filtering (fake client), credentials."""

from __future__ import annotations

import logging
import pickle
import threading
from datetime import date, datetime
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from research.luk_strategy import data
from research.luk_strategy.config import BENCHMARKS

ET = data.ET


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    """Never touch the real ~/.tradingagents, the repo .env, or the network."""
    monkeypatch.setenv("LUK_DATA_DIR", str(tmp_path / "luk"))
    monkeypatch.delenv("ALPACA_API_KEY_ID", raising=False)
    monkeypatch.delenv("ALPACA_API_SECRET_KEY", raising=False)
    monkeypatch.setenv("LUK_DOTENV", str(tmp_path / "absent.env"))

    def _no_network(*_a, **_k):
        raise AssertionError("network/client access in an offline test")

    monkeypatch.setattr(data, "_data_client", _no_network)


# --------------------------------------------------------------------------- filter_symbols
def test_filter_symbols_exclusions_regex_and_benchmarks():
    rows = [
        ("AAPL", "Apple Inc. Common Stock", "NASDAQ"),
        ("F", "Ford Motor Company", "NYSE"),
        ("CELH", "Celsius Holdings, Inc.", "NASDAQ"),
        ("IBKR", "Interactive Brokers", "BATS"),
        ("SMLR", "Semler Scientific", "AMEX"),
        ("TRMK", "Trustmark Corporation", "NASDAQ"),  # " Trust" needs a leading space
        ("SPY", "SPDR S&P 500 ETF Trust", "ARCA"),  # excluded by name, kept as benchmark
        ("QQQ", "Invesco QQQ Trust, Series 1", "NASDAQ"),  # ditto
        ("GLD", "SPDR Gold Trust", "ARCA"),
        ("TQQQ", "ProShares UltraPro QQQ", "NASDAQ"),
        ("SOXL", "Direxion Daily Semiconductor Bull 3X Shares", "ARCA"),
        ("IWM", "iShares Russell 2000 ETF", "ARCA"),
        ("XYZW", "XYZ Corp Warrants", "NASDAQ"),
        ("ABCU", "ABC Acquisition Corp Unit", "NYSE"),
        ("ABCR", "ABC Acquisition Corp Rights", "NYSE"),
        ("PFDX", "Foo Bank 5.25% Depositary Shares", "NYSE"),
        ("PRFX", "Foo Corp Series A preferred", "NYSE"),  # case-insensitive
        ("NOTE", "Foo Senior Notes due 2030", "NYSE"),
        ("ETNX", "Barclays iPath ETN", "ARCA"),
        ("FUND", "Foo Closed End Fund", "NYSE"),
        ("LEV", "Foo Leveraged Shares", "NASDAQ"),
        ("INV", "Foo Inverse Shares", "NASDAQ"),
        ("ACME", "acme small cap etf", "ARCA"),  # " ETF" case-insensitive
        ("BRK.B", "Berkshire Hathaway Class B", "NYSE"),  # regex: dot
        ("GOOGLE", "Six Letter Symbol Inc", "NASDAQ"),  # regex: > 5 letters
        ("abc", "Lowercase Inc", "NYSE"),  # regex: lowercase
        ("AB1", "Digit Inc", "NYSE"),  # regex: digit
        ("OTCX", "Some OTC Company", "OTC"),  # exchange not allowed
        ("CRPT", "Some Crypto Company", "CRYPTO"),
    ]
    assets = pd.DataFrame(
        [{"symbol": s, "name": n, "exchange": e, "status": "active"} for s, n, e in rows]
    )
    out = data.filter_symbols(assets)
    assert out == sorted(out) and len(out) == len(set(out))
    assert set(out) == {"AAPL", "F", "CELH", "IBKR", "SMLR", "TRMK", "SPY", "QQQ"}


def test_filter_symbols_keeps_operating_companies_hit_by_bare_substrings():
    # F7: "Rights" in BrightSpring, " Unit" in United, " Trust" in a corporation/bank name,
    # " Fund" in Funding are operating companies; real rights/units/trusts/funds still go.
    rows = [
        ("BTSG", "BrightSpring Health Services, Inc. Common Stock", "NASDAQ"),
        ("BRSP", "BrightSpire Capital, Inc.", "NYSE"),
        ("MANU", "MANCHESTER UNITED PLC", "NYSE"),
        ("ACU", "Acme United Corporation", "AMEX"),
        ("NTRS", "Northern Trust Corporation Common Stock", "NASDAQ"),
        ("WASH", "Washington Trust Bancorp, Inc. Common Stock", "NASDAQ"),
        ("ASFI", "Asta Funding, Inc. Common Stock", "NASDAQ"),
        ("WDH", "Waterdrop Inc. ADS (each representing the right to receive 10 shares)", "NYSE"),
        ("GBTC", "Grayscale Bitcoin Trust (BTC)", "ARCA"),
        ("RLTY", "Foo Realty Trust, Inc.", "NYSE"),  # trust-named, no corporate marker
        ("ABCU", "ABC Acquisition Corp Units", "NYSE"),
        ("ABCR", "ABC Acquisition Corp Rights", "NYSE"),
        ("ABCW", "ABC Acquisition Corp Warrant", "NYSE"),
        ("CEF", "Foo Income Fund, Inc.", "NYSE"),
        ("VTN", "Foo Vietnam Holdings Inc", "NYSE"),  # "ETN" only as a word
    ]
    assets = pd.DataFrame(
        [{"symbol": s, "name": n, "exchange": e, "status": "active"} for s, n, e in rows]
    )
    kept = set(data.filter_symbols(assets)) - set(BENCHMARKS)
    assert kept == {"BTSG", "BRSP", "MANU", "ACU", "NTRS", "WASH", "ASFI", "WDH", "VTN"}


def test_filter_symbols_adds_benchmarks_even_if_absent_from_assets():
    assets = pd.DataFrame(
        [{"symbol": "AAPL", "name": "Apple Inc.", "exchange": "NASDAQ", "status": "active"}]
    )
    assert data.filter_symbols(assets) == sorted({"AAPL", *BENCHMARKS})


# --------------------------------------------------------------------------- _bars_to_long
def _alpaca_daily_df(rows: list[tuple[str, str, float]]) -> pd.DataFrame:
    """Mimic alpaca-py BarSet.df: MultiIndex (symbol, timestamp UTC)."""
    idx = pd.MultiIndex.from_arrays(
        [[r[0] for r in rows], pd.to_datetime([r[1] for r in rows], utc=True)],
        names=["symbol", "timestamp"],
    )
    px = np.array([r[2] for r in rows])
    return pd.DataFrame(
        {
            "open": px,
            "high": px + 1,
            "low": px - 1,
            "close": px + 0.5,
            "volume": np.full(len(rows), 1000, dtype="int64"),
            "trade_count": np.ones(len(rows)),
            "vwap": px,
        },
        index=idx,
    )


def test_bars_to_long_maps_midnight_et_timestamps_to_session_dates_across_dst():
    # Alpaca stamps daily bars at 00:00 ET: 05:00Z in EST, 04:00Z in EDT.
    rows = [
        ("AAA", "2024-03-08T05:00:00Z", 10.0),  # Fri, EST
        ("AAA", "2024-03-11T04:00:00Z", 11.0),  # Mon, EDT (DST began Sun 03-10)
        ("AAA", "2024-11-01T04:00:00Z", 12.0),  # Fri, EDT
        ("AAA", "2024-11-04T05:00:00Z", 13.0),  # Mon, EST (DST ended Sun 11-03)
        ("BBB", "2024-12-31T05:00:00Z", 20.0),  # year boundary, EST
        ("BBB", "2025-01-02T05:00:00Z", 21.0),
    ]
    out = data._bars_to_long(_alpaca_daily_df(rows))
    assert list(out.columns) == ["symbol", "date", "open", "high", "low", "close", "volume"]
    assert out["date"].dt.tz is None
    assert (out["date"] == out["date"].dt.normalize()).all()
    got = list(zip(out["symbol"], out["date"].dt.date, strict=True))
    assert got == [
        ("AAA", date(2024, 3, 8)),
        ("AAA", date(2024, 3, 11)),
        ("AAA", date(2024, 11, 1)),
        ("AAA", date(2024, 11, 4)),
        ("BBB", date(2024, 12, 31)),
        ("BBB", date(2025, 1, 2)),
    ]
    assert out["open"].tolist() == [10.0, 11.0, 12.0, 13.0, 20.0, 21.0]
    assert out["close"].tolist() == [10.5, 11.5, 12.5, 13.5, 20.5, 21.5]
    for col in ("open", "high", "low", "close", "volume"):
        assert out[col].dtype == np.float64


def test_bars_to_long_empty_or_none():
    for df in (None, pd.DataFrame()):
        out = data._bars_to_long(df)
        assert out.empty
        assert list(out.columns) == ["symbol", "date", "open", "high", "low", "close", "volume"]


# --------------------------------------------------------------------------- load_daily_panel
def _long_series(symbol: str, dates: pd.DatetimeIndex, close, volume) -> pd.DataFrame:
    close = np.broadcast_to(np.asarray(close, dtype=float), (len(dates),))
    volume = np.broadcast_to(np.asarray(volume, dtype=float), (len(dates),))
    return pd.DataFrame(
        {
            "symbol": symbol,
            "date": dates,
            "open": close,
            "high": close * 1.02,
            "low": close * 0.98,
            "close": close,
            "volume": volume,
        }
    )


def _write_batch(
    root, bi: int, long: pd.DataFrame, end: str = "2024-06-28", start: str | None = "2016-01-01"
) -> None:
    d = root / "daily"
    d.mkdir(parents=True, exist_ok=True)
    syms = sorted(long["symbol"].unique())
    meta = {"symbols": syms, "end": end, "bars": long}
    if start is not None:
        meta["start"] = start
    with open(d / f"batch_{bi:04d}.pkl", "wb") as f:
        pickle.dump(meta, f)


def test_load_daily_panel_prefilter_keeps_ever_passing_symbols_and_benchmarks(tmp_path):
    dates = pd.bdate_range("2024-01-02", periods=80)
    # AAA: dollar volume 10*1e6 = $10M only on sessions 30..54 (25 bars), else $0.1M.
    vol_a = np.full(80, 1e4)
    vol_a[30:55] = 1e6
    # BBB: $20M/day but price 2.5 < prefilter_price 3 -> dropped.
    # CCC: price 50, volume 50k -> $2.5M/day -> dropped.
    # DDD: $10M/day but only 15 bars (< 20 for the rolling ADV) -> dropped.
    # SPY/QQQ: tiny volume -> kept anyway (benchmarks).
    long = pd.concat(
        [
            _long_series("AAA", dates, 10.0, vol_a),
            _long_series("BBB", dates, 2.5, 8e6),
            _long_series("CCC", dates, 50.0, 5e4),
            _long_series("DDD", dates[-15:], 20.0, 5e5),
            _long_series("SPY", dates, 400.0, 1.0),
            _long_series("QQQ", dates[5:], 350.0, 1.0),  # starts later -> NaN pre-listing
        ],
        ignore_index=True,
    )
    # split across two batch files (as fetch_daily writes them)
    _write_batch(tmp_path, 0, long[long["symbol"].isin(["AAA", "BBB", "CCC"])])
    _write_batch(tmp_path, 1, long[~long["symbol"].isin(["AAA", "BBB", "CCC"])])

    panel = data.load_daily_panel(cache=tmp_path)
    assert panel.symbols == ["AAA", "QQQ", "SPY"]
    assert panel.dates.equals(pd.DatetimeIndex(dates))
    assert panel.dates.is_monotonic_increasing and panel.dates.is_unique
    for frame in (panel.open, panel.high, panel.low, panel.close, panel.volume):
        assert list(frame.columns) == panel.symbols
        assert frame.index.equals(panel.dates)
        assert all(dt == np.float64 for dt in frame.dtypes)
    # AAA kept in full (all rows, not just the passing window) -> no look-ahead trimming.
    assert panel.close["AAA"].notna().all()
    assert panel.volume["AAA"].iloc[0] == 1e4
    assert panel.close["QQQ"].iloc[:5].isna().all()
    assert panel.close["QQQ"].iloc[5:].notna().all()


def test_load_daily_panel_window_and_data_dir_default(tmp_path, monkeypatch):
    root = tmp_path / "dd"
    monkeypatch.setenv("LUK_DATA_DIR", str(root))
    dates = pd.bdate_range("2024-01-02", periods=60)
    vol = np.full(60, 1e4)
    vol[:25] = 1e6  # AAA passes only in the first 25 sessions
    long = pd.concat(
        [
            _long_series("AAA", dates, 10.0, vol),
            _long_series("SPY", dates, 400.0, 1.0),
            _long_series("QQQ", dates, 350.0, 1.0),
        ],
        ignore_index=True,
    )
    _write_batch(root, 0, long)

    full = data.load_daily_panel()  # cache defaults to data_dir() == LUK_DATA_DIR
    assert full.symbols == ["AAA", "QQQ", "SPY"]

    # Window starting after AAA's liquid period: the prefilter is computed on the
    # loaded window only, so AAA drops out; start/end bound the index.
    win = data.load_daily_panel(start=dates[30].date(), end=dates[50].date())
    assert win.symbols == ["QQQ", "SPY"]
    assert win.dates[0] == dates[30] and win.dates[-1] == dates[50]


def test_load_daily_long_dedupes_and_requires_cache(tmp_path):
    with pytest.raises(FileNotFoundError):
        data.load_daily_long(tmp_path)
    dates = pd.bdate_range("2024-01-02", periods=3)
    a = _long_series("AAA", dates, 10.0, 1e6)
    b = _long_series("AAA", dates[1:], 11.0, 1e6)
    _write_batch(tmp_path, 0, a)
    _write_batch(tmp_path, 1, b)
    out = data.load_daily_long(tmp_path).sort_values("date")
    assert len(out) == 3
    assert out["close"].tolist() == [10.0, 11.0, 11.0]  # later batch wins


def test_fetch_daily_removes_stale_batches_beyond_current_count(tmp_path, monkeypatch):
    """A shrunken symbol list must not leave old high-numbered batches that would win the
    keep="last" de-duplication in load_daily_long."""
    dates = pd.bdate_range("2024-01-02", periods=3)
    _write_batch(tmp_path, 0, _long_series("AAA", dates, 10.0, 1e6))
    _write_batch(tmp_path, 1, _long_series("BBB", dates, 20.0, 1e6))
    _write_batch(tmp_path, 2, _long_series("AAA", dates, 99.0, 1e6))  # stale, old adjustment
    monkeypatch.setattr(data, "_data_client", lambda: object())  # batches 0-1 are up to date
    data.fetch_daily(["AAA", "BBB"], date(2024, 1, 2), date(2024, 6, 28), cache=tmp_path, batch=1)
    names = sorted(p.name for p in (tmp_path / "daily").glob("batch_*.pkl"))
    assert names == ["batch_0000.pkl", "batch_0001.pkl"]
    out = data.load_daily_long(tmp_path)
    assert out.loc[out["symbol"] == "AAA", "close"].tolist() == [10.0, 10.0, 10.0]


def test_fetch_daily_refetches_batches_that_do_not_cover_the_requested_start(tmp_path, monkeypatch):
    """Codex P2: a cache fetched from 2024 must not satisfy a request from 2016; a batch
    with no recorded start (older cache format) is re-fetched too."""
    dates = pd.bdate_range("2024-01-02", periods=3)
    _write_batch(tmp_path, 0, _long_series("AAA", dates, 10.0, 1e6), start="2024-01-02")
    _write_batch(tmp_path, 1, _long_series("BBB", dates, 20.0, 1e6), start=None)
    _write_batch(tmp_path, 2, _long_series("CCC", dates, 30.0, 1e6), start="2015-06-01")
    requested = []

    class _Client:
        def get_stock_bars(self, req):
            requested.append((tuple(req.symbol_or_symbols), req.start.date()))
            return SimpleNamespace(df=None)

    monkeypatch.setattr(data, "_data_client", lambda: _Client())
    data.fetch_daily(
        ["AAA", "BBB", "CCC"], date(2016, 1, 1), date(2024, 6, 28), cache=tmp_path, batch=1
    )
    assert requested == [(("AAA",), date(2016, 1, 1)), (("BBB",), date(2016, 1, 1))]
    meta = pd.read_pickle(tmp_path / "daily" / "batch_0000.pkl")
    assert meta["start"] == "2016-01-01"
    assert pd.read_pickle(tmp_path / "daily" / "batch_0002.pkl")["start"] == "2015-06-01"


def test_fetch_daily_survives_interrupted_write_and_truncated_batch(tmp_path, monkeypatch):
    """Codex P2: an interrupted serialization must not destroy the previous valid batch,
    and a truncated batch left by an older version is re-fetched instead of crashing."""
    dates = pd.bdate_range("2024-01-02", periods=3)
    _write_batch(tmp_path, 0, _long_series("AAA", dates, 10.0, 1e6), start="2024-01-02")
    good = (tmp_path / "daily" / "batch_0000.pkl").read_bytes()
    (tmp_path / "daily" / "batch_0001.pkl").write_bytes(good[: len(good) // 3])  # truncated

    class _Client:
        def get_stock_bars(self, req):
            return SimpleNamespace(df=None)

    monkeypatch.setattr(data, "_data_client", lambda: _Client())

    def boom(obj, f, *a, **k):
        f.write(b"partial")
        raise KeyboardInterrupt

    monkeypatch.setattr(data.pickle, "dump", boom)
    with pytest.raises(KeyboardInterrupt):
        data.fetch_daily(["AAA", "BBB"], date(2016, 1, 1), date(2024, 6, 28), cache=tmp_path, batch=1)
    assert (tmp_path / "daily" / "batch_0000.pkl").read_bytes() == good  # untouched

    monkeypatch.undo()
    monkeypatch.setattr(data, "_data_client", lambda: _Client())
    data.fetch_daily(["AAA", "BBB"], date(2016, 1, 1), date(2024, 6, 28), cache=tmp_path, batch=1)
    for i in (0, 1):
        assert pd.read_pickle(tmp_path / "daily" / f"batch_000{i}.pkl")["start"] == "2016-01-01"
    with pytest.raises((EOFError, pickle.UnpicklingError)):  # backtests refuse an unreadable batch
        (tmp_path / "daily" / "batch_0002.pkl").write_bytes(good[:10])
        data.load_daily_long(tmp_path)


def test_fetch_daily_recovers_when_every_cached_batch_is_unreadable(tmp_path, monkeypatch):
    """Codex P2 (round 3): with no readable batch left there is no comparison baseline,
    but the scheduled downloads must still run."""
    dates = pd.bdate_range("2024-01-02", periods=3)
    _write_batch(tmp_path, 0, _long_series("AAA", dates, 10.0, 1e6))
    path = tmp_path / "daily" / "batch_0000.pkl"
    path.write_bytes(path.read_bytes()[:20])
    calls = []

    class _Client:
        def get_stock_bars(self, req):
            calls.append(tuple(req.symbol_or_symbols))
            return SimpleNamespace(df=None)

    monkeypatch.setattr(data, "_data_client", lambda: _Client())
    data.fetch_daily(["AAA"], date(2016, 1, 1), date(2024, 6, 28), cache=tmp_path)
    assert calls == [("AAA",)]
    assert pd.read_pickle(path)["start"] == "2016-01-01"


def test_load_daily_panel_carries_raw_open_without_back_fill(tmp_path):
    dates = pd.bdate_range("2024-01-02", periods=30)
    adj = pd.concat(
        [_long_series(s, dates, 10.0, 2e6) for s in ("AAA", "SPY", "QQQ")], ignore_index=True
    )
    _write_batch(tmp_path, 0, adj)
    raw = adj.copy()
    raw[["open", "high", "low", "close"]] *= 4.0
    raw = raw[~((raw["symbol"] == "AAA") & (raw["date"] == dates[5]))]  # one missing raw row
    d = tmp_path / "daily_raw"
    d.mkdir()
    with open(d / "batch_0000.pkl", "wb") as f:
        pickle.dump({"symbols": ["AAA", "QQQ", "SPY"], "end": "x", "bars": raw}, f)
    panel = data.load_daily_panel(cache=tmp_path)
    assert panel.raw_open is not None
    assert panel.raw_open["AAA"].iloc[0] == 40.0
    assert np.isnan(panel.raw_open["AAA"].iloc[5])  # never back-filled from adjusted
    assert panel.raw_close["AAA"].iloc[5] == 10.0  # raw_close keeps its documented fallback


def test_load_daily_panel_without_raw_cache_has_no_raw_open(tmp_path):
    dates = pd.bdate_range("2024-01-02", periods=30)
    adj = pd.concat(
        [_long_series(s, dates, 10.0, 2e6) for s in ("AAA", "SPY", "QQQ")], ignore_index=True
    )
    _write_batch(tmp_path, 0, adj)
    assert data.load_daily_panel(cache=tmp_path).raw_open is None


class _DailyClient:
    """Fake daily client: every requested symbol gets the same bars at `px`."""

    def __init__(self, px: dict[str, float], dates: pd.DatetimeIndex):
        self.px, self.dates = px, dates

    def get_stock_bars(self, req):
        rows = [
            (s, f"{d.date().isoformat()}T05:00:00Z", self.px[s])
            for s in req.symbol_or_symbols
            for d in self.dates
        ]
        return _Resp(_alpaca_daily_df(rows))


def _write_intraday(root, day: date, symbols: list[str]) -> None:
    d = root / "intraday"
    d.mkdir(parents=True, exist_ok=True)
    bars = pd.DataFrame(
        {
            "symbol": symbols,
            "ts": [pd.Timestamp(datetime(day.year, day.month, day.day, 9, 30), tz=ET)] * len(symbols),
            "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 1.0,
        }
    )  # fmt: skip
    with open(data._intraday_path(root, day), "wb") as f:
        pickle.dump({"bars": bars, "requested": sorted(symbols)}, f)


def test_daily_refetch_with_new_adjustment_invalidates_cached_intraday(tmp_path, monkeypatch):
    """F1: AAA splits 2:1 between fetches. Its cached intraday days are on the old scale, so
    they are dropped (bars and `requested`) for re-fetch; BBB's are untouched; the raw
    re-fetch never invalidates anything."""
    dates = pd.bdate_range("2024-01-02", periods=3)
    old = pd.concat(
        [_long_series("AAA", dates, 100.0, 1e6), _long_series("BBB", dates, 50.0, 1e6)],
        ignore_index=True,
    )
    old["close"] = old["open"] + 0.5  # matches the fake client's close = px + 0.5
    _write_batch(tmp_path, 0, old, end="2024-01-04")
    d0, d_other = dates[1].date(), date(2023, 12, 1)
    _write_intraday(tmp_path, d0, ["AAA", "BBB"])
    _write_intraday(tmp_path, d_other, ["AAA"])  # no daily change on that date
    client = _DailyClient({"AAA": 50.0, "BBB": 50.0}, dates)
    monkeypatch.setattr(data, "_data_client", lambda: client)

    data.fetch_daily(["AAA", "BBB"], date(2024, 1, 2), date(2024, 1, 5), cache=tmp_path)
    blob = pd.read_pickle(data._intraday_path(tmp_path, d0))
    assert blob["requested"] == ["BBB"]
    assert set(blob["bars"]["symbol"]) == {"BBB"}
    assert pd.read_pickle(data._intraday_path(tmp_path, d_other))["requested"] == ["AAA"]
    assert data.load_daily_long(tmp_path).query("symbol == 'AAA'")["close"].tolist() == [50.5] * 3

    _write_intraday(tmp_path, d0, ["AAA", "BBB"])
    data.fetch_daily(["AAA", "BBB"], date(2024, 1, 2), date(2024, 1, 5), cache=tmp_path, raw=True)
    assert pd.read_pickle(data._intraday_path(tmp_path, d0))["requested"] == ["AAA", "BBB"]


def test_adjustment_changes_ignores_rounding_and_missing():
    dates = pd.bdate_range("2024-01-02", periods=3)
    old = _long_series("AAA", dates, 100.0, 1e6)
    new = old.copy()
    new.loc[0, "close"] = 100.04  # 0.04%: rounding, not an adjustment
    new.loc[1, "close"] = np.nan
    assert data.adjustment_changes(old, new) == {}
    new.loc[2, "close"] = 90.0
    assert data.adjustment_changes(old, new) == {"AAA": {dates[2].date()}}


# --------------------------------------------------------------------------- intraday
class _Resp:
    def __init__(self, df: pd.DataFrame):
        self.df = df


class FakeClient:
    """Stands in for StockHistoricalDataClient. Returns 5-min bars from 08:00 to 19:55 ET
    (pre-market + regular + after-hours) for every requested symbol except `no_data`."""

    def __init__(self, no_data: frozenset[str] = frozenset()):
        self.calls: list[tuple[date, list[str]]] = []
        self.no_data = no_data
        self.lock = threading.Lock()

    def get_stock_bars(self, req):
        syms = list(req.symbol_or_symbols)
        start = pd.Timestamp(req.start)
        start = start.tz_localize("UTC") if start.tzinfo is None else start
        day = start.tz_convert(ET).date()
        with self.lock:
            self.calls.append((day, sorted(syms)))
        ets = pd.date_range(
            datetime(day.year, day.month, day.day, 8, 0), periods=144, freq="5min", tz=ET
        )
        frames = []
        # reverse order on purpose: the cache must come back sorted by (symbol, ts)
        for s in sorted(syms, reverse=True):
            if s in self.no_data:
                continue
            n = len(ets)
            frames.append(
                pd.DataFrame(
                    {
                        "symbol": s,
                        "timestamp": ets.tz_convert("UTC")[::-1],
                        "open": np.arange(n, dtype=float)[::-1] + 10,
                        "high": np.arange(n, dtype=float)[::-1] + 11,
                        "low": np.arange(n, dtype=float)[::-1] + 9,
                        "close": np.arange(n, dtype=float)[::-1] + 10.5,
                        "volume": np.full(n, 100, dtype="int64"),
                    }
                )
            )
        if not frames:
            return _Resp(pd.DataFrame())
        df = pd.concat(frames, ignore_index=True).set_index(["symbol", "timestamp"])
        return _Resp(df)


@pytest.fixture
def fake_client(monkeypatch):
    client = FakeClient(no_data=frozenset({"HALT"}))
    monkeypatch.setattr(data, "_data_client", lambda: client)
    return client


def _load_day(root, day: date) -> dict:
    with open(data._intraday_path(root, day), "rb") as f:
        return pickle.load(f)


@pytest.mark.parametrize("day", [date(2024, 7, 1), date(2024, 12, 2), date(2024, 3, 11)])
def test_fetch_intraday_keeps_regular_session_only_in_et(tmp_path, fake_client, day):
    data.fetch_intraday({day: ["BBB", "AAA"]}, cache=tmp_path, workers=1, per_min=60_000)
    blob = _load_day(tmp_path, day)
    bars = blob["bars"]
    assert blob["requested"] == ["AAA", "BBB"]
    assert list(bars.columns) == ["symbol", "ts", "open", "high", "low", "close", "volume"]
    assert str(bars["ts"].dt.tz) == "America/New_York"
    for sym, g in bars.groupby("symbol"):
        assert g["ts"].is_monotonic_increasing, sym
        assert g["ts"].iloc[0] == pd.Timestamp(datetime(day.year, day.month, day.day, 9, 30), tz=ET)
        assert g["ts"].iloc[-1] == pd.Timestamp(
            datetime(day.year, day.month, day.day, 15, 55), tz=ET
        )
        assert len(g) == 78  # 6.5h of 5-min bars
        assert (g["ts"].dt.date == day).all()
    assert bars["symbol"].tolist() == sorted(bars["symbol"].tolist())

    store = data.IntradayStore(tmp_path)
    got = store(day)
    assert set(got) == {"AAA", "BBB"}
    for sym, g in got.items():
        assert (g["symbol"] == sym).all()
        assert g["ts"].is_monotonic_increasing
        assert g.index.tolist() == list(range(len(g)))
    assert store(date(2024, 7, 2)) is None  # no file for that day


def test_fetch_intraday_is_resumable_per_day(tmp_path, fake_client):
    d1, d2 = date(2024, 7, 1), date(2024, 7, 2)
    data.fetch_intraday({d1: ["AAA", "BBB", "HALT"]}, cache=tmp_path, workers=2, per_min=60_000)
    assert fake_client.calls == [(d1, ["AAA", "BBB", "HALT"])]
    first = _load_day(tmp_path, d1)
    # HALT returned no bars but is recorded as requested, so it is not re-fetched.
    assert first["requested"] == ["AAA", "BBB", "HALT"]
    assert set(first["bars"]["symbol"]) == {"AAA", "BBB"}

    # Superset for d1 + a new day: only the missing symbols / day are requested.
    fake_client.calls.clear()
    data.fetch_intraday(
        {d1: ["AAA", "BBB", "CCC", "HALT"], d2: ["AAA"]},
        cache=tmp_path,
        workers=2,
        per_min=60_000,
    )
    assert sorted(fake_client.calls) == [(d1, ["CCC"]), (d2, ["AAA"])]
    second = _load_day(tmp_path, d1)
    assert second["requested"] == ["AAA", "BBB", "CCC", "HALT"]
    assert sorted(second["bars"]["symbol"].unique()) == ["AAA", "BBB", "CCC"]
    # original bars preserved, no duplicates introduced by the merge
    assert not second["bars"].duplicated(["symbol", "ts"]).any()
    pd.testing.assert_frame_equal(
        second["bars"][second["bars"]["symbol"] != "CCC"].reset_index(drop=True),
        first["bars"].reset_index(drop=True),
    )
    assert not list((tmp_path / "intraday").glob("*.tmp"))

    # Fully cached request: no client calls at all.
    fake_client.calls.clear()
    data.fetch_intraday({d1: ["CCC", "AAA"], d2: ["AAA"]}, cache=tmp_path, per_min=60_000)
    assert fake_client.calls == []

    # IntradayStore after the merge returns each symbol sorted by ts
    got = data.IntradayStore(tmp_path)(d1)
    assert set(got) == {"AAA", "BBB", "CCC"}
    assert all(g["ts"].is_monotonic_increasing for g in got.values())


def test_fetch_intraday_empty_response_writes_empty_frame(tmp_path, fake_client):
    day = date(2024, 7, 3)
    data.fetch_intraday({day: ["HALT"]}, cache=tmp_path, workers=1, per_min=60_000)
    blob = _load_day(tmp_path, day)
    assert blob["requested"] == ["HALT"]
    assert blob["bars"].empty
    assert data.IntradayStore(tmp_path)(day) == {}


# --------------------------------------------------------------------------- credentials
KEY, SECRET = "PKTESTKEY-not-real-123", "test-secret-not-real-456"


def test_load_credentials_prefers_environment(tmp_path, monkeypatch):
    env_file = tmp_path / "creds.env"
    env_file.write_text("ALPACA_API_KEY_ID=file-key\nALPACA_API_SECRET_KEY=file-secret\n")
    monkeypatch.setenv("LUK_DOTENV", str(env_file))
    monkeypatch.setenv("ALPACA_API_KEY_ID", KEY)
    monkeypatch.setenv("ALPACA_API_SECRET_KEY", SECRET)
    assert data.load_credentials() == (KEY, SECRET)


def test_load_credentials_falls_back_to_luk_dotenv_without_git(tmp_path, monkeypatch):
    env_file = tmp_path / "creds.env"
    env_file.write_text(f"ALPACA_API_KEY_ID={KEY}\nALPACA_API_SECRET_KEY={SECRET}\n")
    monkeypatch.setenv("LUK_DOTENV", str(env_file))
    monkeypatch.setenv("ALPACA_API_KEY_ID", "only-key-in-env")  # incomplete env -> file

    def _no_git(*_a, **_k):
        raise AssertionError("LUK_DOTENV set: must not shell out to git")

    monkeypatch.setattr(data.subprocess, "run", _no_git)
    assert data.load_credentials() == (KEY, SECRET)


def test_load_credentials_missing_does_not_expose_values(tmp_path, monkeypatch, caplog):
    env_file = tmp_path / "partial.env"
    env_file.write_text(f"ALPACA_API_KEY_ID={KEY}\n")  # secret missing
    monkeypatch.setenv("LUK_DOTENV", str(env_file))
    monkeypatch.setenv("ALPACA_API_SECRET_KEY", SECRET)  # only half in env
    caplog.set_level(logging.DEBUG)
    with pytest.raises(RuntimeError) as exc:
        data.load_credentials()
    msg = str(exc.value)
    assert "credentials not found" in msg
    assert KEY not in msg and SECRET not in msg
    assert KEY not in caplog.text and SECRET not in caplog.text


def test_last_complete_session_skips_weekends():
    assert data.last_complete_session(date(2024, 7, 2)) == date(2024, 7, 1)  # Tue -> Mon
    assert data.last_complete_session(date(2024, 7, 1)) == date(2024, 6, 28)  # Mon -> Fri
    assert data.last_complete_session(date(2024, 7, 7)) == date(2024, 7, 5)  # Sun -> Fri
