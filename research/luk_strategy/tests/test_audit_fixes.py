"""Regression tests for the independent-audit findings (float ties, placeholder bars,
half-day after-hours bars)."""

import pickle
from datetime import date

import pandas as pd

from research.luk_strategy import data
from research.luk_strategy.intraday import touched_down, touched_up


def test_touch_helpers_count_exact_float_prints():
    level = 17.01 + 0.01  # 17.020000000000003 in binary floating point
    assert level > 17.02
    assert touched_up(17.02, level)
    stop = 196.09 - 0.01  # 196.07999999999998
    assert touched_down(196.08, stop)
    assert not touched_up(17.01, level)
    assert not touched_down(196.10, stop)
    assert not touched_up(float("nan"), level)


def test_placeholder_bars_dropped_before_panel():
    long = pd.DataFrame(
        {
            "symbol": ["OLD"] * 3,
            "date": pd.to_datetime(["2020-01-02", "2020-01-03", "2020-01-06"]),
            "open": [10.0, 9.0, 9.0],
            "high": [10.5, 9.0, 9.4],
            "low": [9.5, 9.0, 8.8],
            "close": [10.0, 9.0, 9.1],
            "volume": [1e6, 0.0, 0.0],
        }
    )
    out = data.drop_placeholder_bars(long)
    # flat zero-volume bar dropped; a zero-volume bar with a range is kept
    assert list(out["date"].dt.day) == [2, 6]


def test_intraday_store_drops_after_hours_on_half_days(tmp_path, monkeypatch):
    monkeypatch.setenv("LUK_DATA_DIR", str(tmp_path))
    day = date(2024, 11, 29)
    ts = pd.date_range("2024-11-29 09:30", "2024-11-29 15:55", freq="5min", tz="America/New_York")
    bars = pd.DataFrame(
        {"symbol": "AAA", "ts": ts, "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 1.0}
    )
    (tmp_path / "intraday").mkdir()
    with open(tmp_path / "intraday" / f"{day.isoformat()}.pkl", "wb") as f:
        pickle.dump({"bars": bars, "requested": ["AAA"]}, f)
    with open(tmp_path / "calendar.pkl", "wb") as f:
        pickle.dump({day: 13 * 60}, f)
    got = data.IntradayStore(tmp_path)(day)["AAA"]
    assert got["ts"].max().strftime("%H:%M") == "12:55"
    assert len(got) == 42
