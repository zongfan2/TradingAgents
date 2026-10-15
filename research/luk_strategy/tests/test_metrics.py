"""Offline tests for metrics.py (synthetic data only; no network, no home dir)."""

from __future__ import annotations

import json
import math
from datetime import date

import numpy as np
import pandas as pd
import pytest

from research.luk_strategy import metrics as M
from research.luk_strategy.config import Config
from research.luk_strategy.models import BacktestResult, DailyPanel


@pytest.fixture(autouse=True)
def _sandbox_data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("LUK_DATA_DIR", str(tmp_path / "luk_data"))


def make_panel(dates, closes: dict[str, list[float]]) -> DailyPanel:
    close = pd.DataFrame(closes, index=pd.DatetimeIndex(dates), dtype=float)
    return DailyPanel(
        open=close.copy(),
        high=close.copy(),
        low=close.copy(),
        close=close,
        volume=close * 0 + 1e6,
    )


def make_equity(dates, equity: list[float], gross=None, npos=None) -> pd.DataFrame:
    n = len(equity)
    gross = gross if gross is not None else [0.0] * n
    return pd.DataFrame(
        {
            "equity": equity,
            "cash": [e - g for e, g in zip(equity, gross, strict=True)],
            "gross": gross,
            "n_positions": npos if npos is not None else [0] * n,
        },
        index=pd.DatetimeIndex(dates, name="date"),
    )


def make_trades(pnls, setups=None, reasons=None, entry_dates=None, symbols=None) -> pd.DataFrame:
    n = len(pnls)
    rows = []
    for i, p in enumerate(pnls):
        rows.append(
            {
                "trade_id": i + 1,
                "symbol": symbols[i] if symbols else f"S{i}",
                "setup": setups[i] if setups else "BO",
                "entry_date": entry_dates[i] if entry_dates else date(2020, 1, 2),
                "entry_ts": pd.Timestamp("2020-01-02 09:45", tz="America/New_York"),
                "entry_fill": 100.0,
                "initial_stop": 99.0,
                "qty": 100,
                "risk_dollars": 100.0,
                "exit_date": date(2020, 1, 8),
                "exit_price_avg": 100.0 + p / 100,
                "exit_reason": reasons[i] if reasons else "trail",
                "pnl": float(p),
                "r_multiple": p / 100.0,
                "return_pct": p / 10_000.0,
                "hold_sessions": i + 1,
                "legs": "[]",
            }
        )
    assert len(rows) == n
    return pd.DataFrame(rows)


def empty_skips() -> pd.DataFrame:
    return pd.DataFrame(columns=["day", "symbol", "setup", "reason"])


def result_of(equity, trades=None, skips=None, cfg: Config | None = None) -> BacktestResult:
    cfg = cfg or Config()
    return BacktestResult(
        trades=trades if trades is not None else pd.DataFrame(),
        equity=equity,
        skips=skips if skips is not None else empty_skips(),
        config=cfg.to_dict(),
    )


SESSIONS = list(pd.bdate_range("2020-01-02", periods=5))  # Jan 2, 3, 6, 7, 8
PRIOR = pd.Timestamp("2019-12-31")
EQ_VALUES = [101_000.0, 99_990.0, 98_990.0, 102_000.0, 103_020.0]


def standard_panel() -> DailyPanel:
    return make_panel(
        [PRIOR, *SESSIONS],
        {
            "SPY": [100, 101, 102, 100, 103, 104],
            "QQQ": [200, 202, 204, 198, 206, 210],
            "AAA": [10, 11, 12, 13, 14, 15],
        },
    )


# --------------------------------------------------------------------------- curve metrics
def test_curve_metrics_known_values():
    cfg = Config()
    res = result_of(make_equity(SESSIONS, EQ_VALUES))
    s = M.summarize(res, standard_panel(), cfg)
    o = s["overall"]

    path = np.array([1.0] + [v / 100_000 for v in EQ_VALUES])
    rets = path[1:] / path[:-1] - 1
    years = (SESSIONS[-1] - PRIOR).days / 365.25

    assert o["total_return"] == pytest.approx(0.0302)
    assert o["cagr"] == pytest.approx(1.0302 ** (1 / years) - 1)
    assert o["max_dd"] == pytest.approx(0.9899 / 1.01 - 1)
    assert o["max_dd_duration_sessions"] == 2
    assert o["max_dd_peak"] == "2020-01-02"
    assert o["max_dd_trough"] == "2020-01-06"
    assert o["max_dd_recovery"] == "2020-01-07"
    assert o["ann_vol"] == pytest.approx(rets.std(ddof=1) * math.sqrt(252))
    assert o["sharpe"] == pytest.approx(rets.mean() / rets.std(ddof=1) * math.sqrt(252))
    downside = math.sqrt(np.mean(np.minimum(rets, 0) ** 2))
    assert o["sortino"] == pytest.approx(rets.mean() / downside * math.sqrt(252))
    assert o["mar"] == pytest.approx(o["cagr"] / abs(o["max_dd"]))
    assert o["final_equity"] == pytest.approx(103_020.0)
    assert s["period"]["base_date"] == "2019-12-31"
    assert s["period"]["n_sessions"] == 5


def test_drawdown_counts_day_one_loss_and_unrecovered():
    # Day 1 already below the initial equity: the drawdown peak is the base point.
    eq = make_equity(SESSIONS, [99_000.0, 98_000.0, 99_500.0, 97_000.0, 98_000.0])
    s = M.summarize(result_of(eq), standard_panel(), Config())
    o = s["overall"]
    assert o["max_dd"] == pytest.approx(-0.03)
    assert o["max_dd_peak"] == "2019-12-31"  # base date (start of the period)
    assert o["max_dd_trough"] == "2020-01-07"
    assert o["max_dd_recovery"] is None
    assert o["max_dd_duration_sessions"] == 5


def test_drawdown_peak_is_last_session_at_peak():
    eq = make_equity(SESSIONS, [101_000.0, 101_000.0, 99_000.0, 102_000.0, 102_500.0])
    o = M.summarize(result_of(eq), standard_panel(), Config())["overall"]
    assert o["max_dd_peak"] == "2020-01-03"
    assert o["max_dd_trough"] == "2020-01-06"
    assert o["max_dd_duration_sessions"] == 1


def test_flat_equity_gives_nan_ratios_not_crash():
    eq = make_equity(SESSIONS, [100_000.0] * 5)
    s = M.summarize(result_of(eq), standard_panel(), Config())
    o = s["overall"]
    assert o["total_return"] == 0.0
    assert o["max_dd"] == 0.0
    assert o["max_dd_duration_sessions"] == 0
    assert o["ann_vol"] == 0.0
    assert o["sharpe"] is None and o["sortino"] is None and o["mar"] is None
    json.dumps(s, allow_nan=False)


# --------------------------------------------------------------------------- benchmarks
def test_benchmarks_buy_at_prior_close():
    s = M.summarize(result_of(make_equity(SESSIONS, EQ_VALUES)), standard_panel(), Config())
    spy, qqq = s["benchmarks"]["SPY"], s["benchmarks"]["QQQ"]
    assert spy["total_return"] == pytest.approx(0.04)  # 104 / 100 (prior close), not 104 / 101
    assert qqq["total_return"] == pytest.approx(0.05)
    years = (SESSIONS[-1] - PRIOR).days / 365.25
    assert spy["cagr"] == pytest.approx(1.04 ** (1 / years) - 1)
    assert spy["max_dd"] == pytest.approx(100 / 102 - 1)
    assert spy["sharpe"] is not None
    assert s["coverage"]["benchmarks_available"] == ["SPY", "QQQ"]
    assert s["coverage"]["n_panel_symbols"] == 1


def test_benchmarks_without_prior_session_and_missing_symbol():
    panel = make_panel(SESSIONS, {"SPY": [101, 102, 100, 103, 104], "AAA": [1, 2, 3, 4, 5]})
    s = M.summarize(result_of(make_equity(SESSIONS, EQ_VALUES)), panel, Config())
    assert s["period"]["base_date"] is None
    assert s["benchmarks"]["SPY"]["total_return"] == pytest.approx(104 / 101 - 1)
    assert s["benchmarks"]["QQQ"] is None
    years = (SESSIONS[-1] - SESSIONS[0]).days / 365.25
    assert s["overall"]["cagr"] == pytest.approx(1.0302 ** (1 / years) - 1)
    assert s["by_year"]["2020"]["QQQ"] is None
    json.dumps(s, allow_nan=False)
    md = M.render_markdown(s, "no_prior")
    assert "n/a" in md


def test_benchmark_gaps_are_forward_filled():
    panel = make_panel(
        [PRIOR, *SESSIONS],
        {"SPY": [100, 101, np.nan, np.nan, 103, 104], "QQQ": [200, 202, 204, 198, 206, 210]},
    )
    s = M.summarize(result_of(make_equity(SESSIONS, EQ_VALUES)), panel, Config())
    assert s["benchmarks"]["SPY"]["total_return"] == pytest.approx(0.04)
    assert s["benchmarks"]["SPY"]["max_dd"] == 0.0


# --------------------------------------------------------------------------- trade stats
PNLS = [500, -100, -100, 300, -50, 1000, -200, 0, 150, -100]


def test_trade_stats_known_values():
    trades = make_trades(PNLS, symbols=[f"T{i}" for i in range(10)])
    eq = make_equity(SESSIONS, EQ_VALUES, gross=[50_500.0, 0, 0, 51_000.0, 0])
    s = M.summarize(result_of(eq, trades), standard_panel(), Config())
    o = s["overall"]
    r = np.array(PNLS) / 100
    wins = r[np.array(PNLS) > 0]
    losses = r[np.array(PNLS) <= 0]

    assert o["trades"] == 10 and o["n_open"] == 0
    assert o["n_wins"] == 4 and o["n_losses"] == 6
    assert o["win_rate"] == pytest.approx(0.4)
    assert o["avg_win_r"] == pytest.approx(wins.mean())
    assert o["avg_loss_r"] == pytest.approx(losses.mean())
    assert o["avg_win_pct"] == pytest.approx(wins.mean() / 100)
    assert o["avg_loss_pct"] == pytest.approx(losses.mean() / 100)
    assert o["expectancy_r"] == pytest.approx(1.4)
    # expectancy decomposes exactly because loss = not win
    decomposed = o["win_rate"] * o["avg_win_r"] + (1 - o["win_rate"]) * o["avg_loss_r"]
    assert decomposed == pytest.approx(o["expectancy_r"])
    assert o["profit_factor"] == pytest.approx(1950 / 550)
    assert o["total_pnl"] == pytest.approx(1400)
    assert o["top10_share"] == pytest.approx(1000 / 1950)
    assert o["top10_share_of_net"] == pytest.approx(1000 / 1400)
    assert o["avg_hold_sessions"] == pytest.approx(5.5)
    years = (SESSIONS[-1] - PRIOR).days / 365.25
    assert o["trades_per_year"] == pytest.approx(10 / years)
    assert o["best_worst_by"] == "r_multiple"
    assert o["best_trade"]["symbol"] == "T5" and o["best_trade"]["r_multiple"] == 10.0
    assert o["worst_trade"]["symbol"] == "T6" and o["worst_trade"]["pnl"] == -200.0
    assert o["best_trade"]["entry_date"] == "2020-01-02"
    assert o["avg_exposure"] == pytest.approx(
        np.mean(np.array([50_500.0, 0, 0, 51_000.0, 0]) / np.array(EQ_VALUES))
    )


def test_top10_uses_integer_ceiling():
    # 30 trades -> top 3 (float 0.1*30 would ceil to 4).
    pnls = [100.0] * 27 + [1000.0, 900.0, 800.0]
    st = M.trade_stats(make_trades(pnls), years=1.0)
    assert st["top10_share"] == pytest.approx(2700 / (2700 + 2700))


def test_top10_share_clips_losers_in_top_bucket():
    # one winner among 20 trades: top 2 includes a loser, positive share stays <= 1
    pnls = [500.0] + [-10.0] * 19
    st = M.trade_stats(make_trades(pnls), years=1.0)
    assert st["top10_share"] == pytest.approx(1.0)
    assert st["top10_share_of_net"] == pytest.approx((500 - 10) / (500 - 190))


def test_profit_factor_without_losses_is_null():
    s = M.summarize(
        result_of(make_equity(SESSIONS, EQ_VALUES), make_trades([100, 200])),
        standard_panel(),
        Config(),
    )
    assert s["overall"]["profit_factor"] is None
    assert s["overall"]["avg_loss_r"] is None
    json.dumps(s, allow_nan=False)


def test_open_trades_and_string_columns_from_csv(tmp_path):
    trades = make_trades([100, -50, 30])
    trades.loc[2, ["pnl", "exit_date", "exit_price_avg", "r_multiple", "return_pct"]] = np.nan
    path = tmp_path / "t.csv"
    trades.to_csv(path, index=False)
    reloaded = pd.read_csv(path)
    st = M.trade_stats(reloaded, years=2.0)
    assert st["trades"] == 2 and st["n_open"] == 1
    assert st["win_rate"] == pytest.approx(0.5)
    assert st["trades_per_year"] == pytest.approx(1.0)


def test_return_pct_derived_when_missing():
    trades = make_trades([100, -50]).drop(columns=["return_pct"])
    st = M.trade_stats(trades, years=1.0)
    assert st["avg_win_pct"] == pytest.approx(100 / (100 * 100.0))
    assert st["avg_loss_pct"] == pytest.approx(-50 / (100 * 100.0))


# --------------------------------------------------------------------------- groupings
def test_by_year_chains_to_total_and_counts_trades_by_entry_year():
    dates = [pd.Timestamp(d) for d in ("2020-12-30", "2020-12-31", "2021-01-04", "2021-01-05")]
    prior = pd.Timestamp("2020-12-29")
    panel = make_panel(
        [prior, *dates],
        {"SPY": [100, 101, 103, 99, 105], "QQQ": [50, 49, 52, 53, 55], "AAA": [1, 1, 1, 1, 1]},
    )
    eq = make_equity(dates, [100_500.0, 99_000.0, 101_000.0, 103_000.0])
    trades = make_trades(
        [200, -100, 300],
        entry_dates=[date(2020, 12, 30), date(2020, 12, 31), date(2021, 1, 4)],
    )
    s = M.summarize(result_of(eq, trades), panel, Config())
    by = s["by_year"]
    assert list(by) == ["2020", "2021"]
    assert by["2020"]["strategy"] == pytest.approx(0.99 - 1)
    assert by["2021"]["strategy"] == pytest.approx(1.03 / 0.99 - 1)
    total = (1 + by["2020"]["strategy"]) * (1 + by["2021"]["strategy"]) - 1
    assert total == pytest.approx(s["overall"]["total_return"])
    assert by["2020"]["SPY"] == pytest.approx(1.03 - 1)
    assert by["2021"]["SPY"] == pytest.approx(105 / 103 - 1)
    spy_total = (1 + by["2020"]["SPY"]) * (1 + by["2021"]["SPY"]) - 1
    assert spy_total == pytest.approx(s["benchmarks"]["SPY"]["total_return"])
    assert by["2020"]["QQQ"] == pytest.approx(52 / 50 - 1)
    assert by["2020"]["max_dd"] == pytest.approx(0.99 / 1.005 - 1)
    # 2021 drawdown is measured from the 2020 year-end level; it only rises -> 0
    assert by["2021"]["max_dd"] == 0.0
    assert by["2020"]["trades"] == 2 and by["2020"]["win_rate"] == pytest.approx(0.5)
    assert by["2021"]["trades"] == 1 and by["2021"]["win_rate"] == pytest.approx(1.0)


def test_by_setup_exit_reason_and_skips():
    trades = make_trades(
        [100, -50, 300, -20],
        setups=["PB", "BO", "EP", "PB"],
        reasons=["trail", "stop", "trail", "stop"],
    )
    skips = pd.DataFrame(
        {
            "day": [date(2020, 1, 2)] * 5,
            "symbol": list("ABCDE"),
            "setup": ["BO", "BO", "EP", "PB", "BO"],
            "reason": ["no_trigger", "no_trigger", "ep_volume", "regime", "cap_new"],
        }
    )
    s = M.summarize(
        result_of(make_equity(SESSIONS, EQ_VALUES), trades, skips), standard_panel(), Config()
    )
    assert list(s["by_setup"]) == ["EP", "BO", "PB"]
    pb = s["by_setup"]["PB"]
    assert pb["count"] == 2 and pb["win_rate"] == pytest.approx(0.5)
    assert pb["expectancy_r"] == pytest.approx((1.0 - 0.2) / 2)
    assert pb["total_pnl"] == pytest.approx(80)
    assert s["by_exit_reason"]["stop"]["count"] == 2
    assert s["by_exit_reason"]["trail"]["total_pnl"] == pytest.approx(400)
    assert s["skips_by_reason"] == {"no_trigger": 2, "cap_new": 1, "ep_volume": 1, "regime": 1}
    assert list(s["skips_by_reason"])[0] == "no_trigger"
    assert s["skips_by_setup"]["BO"] == {"no_trigger": 2, "cap_new": 1}
    assert list(s["skips_by_setup"]) == ["EP", "BO", "PB"]


# --------------------------------------------------------------------------- degenerate inputs
def test_empty_trades_and_single_row_equity():
    eq = make_equity(SESSIONS[:1], [100_500.0])
    res = BacktestResult(
        trades=pd.DataFrame(), equity=eq, skips=pd.DataFrame(), config=Config().to_dict()
    )
    s = M.summarize(res, standard_panel(), Config())
    text = json.dumps(s, allow_nan=False)
    assert "NaN" not in text
    o = s["overall"]
    assert o["trades"] == 0
    assert o["win_rate"] is None and o["expectancy_r"] is None and o["profit_factor"] is None
    assert o["best_trade"] is None
    assert o["total_return"] == pytest.approx(0.005)
    assert o["sharpe"] is None and o["ann_vol"] is None and o["sortino"] is None
    assert o["cagr"] is not None  # base date (prior session) gives a positive span
    assert o["trades_per_year"] == 0.0
    assert s["by_setup"] == {} and s["by_exit_reason"] == {} and s["skips_by_reason"] == {}
    assert s["by_year"]["2020"]["trades"] == 0
    md = M.render_markdown(s, "degenerate")
    assert "_No closed trades._" in md and "_No skips recorded._" in md


def test_empty_equity_does_not_crash(tmp_path):
    res = BacktestResult(
        trades=pd.DataFrame(),
        equity=pd.DataFrame(columns=["equity", "cash", "gross", "n_positions"]),
        skips=pd.DataFrame(),
        config={},
    )
    s = M.summarize(res, standard_panel(), Config())
    json.dumps(s, allow_nan=False)
    assert s["overall"]["total_return"] is None
    assert s["overall"]["trades_per_year"] is None
    assert s["period"]["start"] is None
    assert s["by_year"] == {}
    assert s["benchmarks"]["SPY"] is None
    M.write_results(res, s, tmp_path / "empty_run")
    assert (tmp_path / "empty_run" / "report.md").read_text(encoding="utf-8").startswith("# ")
    assert "trade_id" in (tmp_path / "empty_run" / "trades.csv").read_text()


def test_equity_index_of_date_objects_and_csv_roundtrip(tmp_path):
    eq = make_equity(SESSIONS, EQ_VALUES)
    eq_dates = eq.copy()
    eq_dates.index = [d.date() for d in SESSIONS]
    s1 = M.summarize(result_of(eq_dates), standard_panel(), Config())
    eq.to_csv(tmp_path / "e.csv", index_label="date")
    s2 = M.summarize(result_of(pd.read_csv(tmp_path / "e.csv")), standard_panel(), Config())
    s3 = M.summarize(
        result_of(pd.read_csv(tmp_path / "e.csv", index_col="date")), standard_panel(), Config()
    )
    for s in (s1, s2, s3):
        assert s["overall"]["total_return"] == pytest.approx(0.0302)
        assert s["benchmarks"]["SPY"]["total_return"] == pytest.approx(0.04)


# --------------------------------------------------------------------------- JSON / outputs
def test_to_jsonable_converts_everything():
    obj = {
        "f32": np.float32(1.5),
        "i64": np.int64(3),
        "nan": np.nan,
        "inf": float("inf"),
        "ninf": np.float64(-np.inf),
        "ts": pd.Timestamp("2020-01-02"),
        "ts_tz": pd.Timestamp("2020-01-02 09:30", tz="America/New_York"),
        "d": date(2020, 1, 2),
        "b": np.bool_(True),
        "nat": pd.NaT,
        "na": pd.NA,
        "arr": np.array([1.0, np.nan]),
        "tup": (1, 2),
        date(2021, 5, 6): "date-key",
        7: "int-key",
    }
    out = M.to_jsonable(obj)
    assert out["f32"] == 1.5 and isinstance(out["i64"], int)
    assert out["nan"] is None and out["inf"] is None and out["ninf"] is None
    assert out["ts"] == "2020-01-02"
    assert out["ts_tz"].startswith("2020-01-02T09:30:00")
    assert out["d"] == "2020-01-02" and out["b"] is True
    assert out["nat"] is None and out["na"] is None
    assert out["arr"] == [1.0, None] and out["tup"] == [1, 2]
    assert out["2021-05-06"] == "date-key" and out["7"] == "int-key"
    json.dumps(out, allow_nan=False)


def test_summary_has_required_sections_and_config_note():
    cfg = Config(entry_mode="daily", exit_mode="contract")
    res = result_of(make_equity(SESSIONS, EQ_VALUES), make_trades(PNLS), cfg=Config())
    s = M.summarize(res, standard_panel(), cfg)
    for key in (
        "overall",
        "by_year",
        "by_setup",
        "by_exit_reason",
        "skips_by_reason",
        "benchmarks",
        "limitations",
        "config",
        "notes",
        "period",
        "coverage",
    ):
        assert key in s
    assert s["limitations"] == M.LIMITATIONS and len(s["limitations"]) == 8
    assert s["config"] == M.to_jsonable(cfg.to_dict())
    assert s["config"]["setups"] == ["EP", "BO", "PB"]
    joined = " ".join(s["notes"])
    assert "Daily entry mode" in joined and "Contract exits" in joined
    assert "result.config differs" in joined


def test_render_markdown_content():
    s = M.summarize(
        result_of(make_equity(SESSIONS, EQ_VALUES), make_trades(PNLS)), standard_panel(), Config()
    )
    md = M.render_markdown(s, "baseline")
    assert md.startswith("# Luk")
    assert "baseline" in md
    for heading in (
        "## 总览 / Overview",
        "## 交易统计 / Trade statistics",
        "## 分年度 / By year",
        "## 按形态 / By setup",
        "## 按离场原因 / By exit reason",
        "## 跳过原因 / Skipped triggers by reason",
        "## 已知局限 / Known limitations",
        "## 配置 / Config snapshot",
    ):
        assert heading in md
    for lim in M.LIMITATIONS:
        assert lim in md
    assert "+3.02%" in md  # total return, 2 decimals, signed
    assert "+4.00%" in md  # SPY total return
    assert "40.0%" in md  # win rate
    assert '"risk_per_trade": 0.005' in md
    assert "| 2020 |" in md
    # valid markdown table separator rows
    assert "|:---|---:|" in md


def test_coverage_discloses_early_endings_survivor_only_years_and_no_data_split():
    # LA-3: per-year count of symbols whose data ends early, survivor-only years, and the
    # no_data rate split by data status, in summary["coverage"] and report.md.
    dates = pd.bdate_range("2019-01-02", "2021-12-31")
    n = len(dates)

    def ends(k: int) -> list[float]:
        return [10.0] * k + [np.nan] * (n - k)

    closes = {"SPY": [100.0] * n, "QQQ": [200.0] * n, "LIVE": [10.0] * n}
    for j in range(15):  # fifteen names whose data ends in 2019
        closes[f"D19_{j}"] = ends(100 + j)
    closes["D20"] = ends(300)  # one in 2020
    closes["LATE"] = ends(n - 5)  # within EARLY_END_SESSIONS of the end: not "early"
    panel = make_panel(dates, closes)
    eq = make_equity(dates[1:], [100_000.0] * (n - 1))
    skips = pd.DataFrame(
        {
            "day": [date(2019, 3, 1)] * 6,
            "symbol": ["D19_0", "D19_1", "LIVE", "LIVE", "LIVE", "LIVE"],
            "setup": ["BO"] * 6,
            "reason": ["no_data", "chase", "no_data", "regime", "too_wide", "setup_disabled"],
        }
    )
    trades = make_trades([100.0], symbols=["LIVE"])
    s = M.summarize(result_of(eq, trades, skips), panel, Config())
    cov = s["coverage"]
    assert cov["symbols_ending_early_by_year"] == {"2019": 15, "2020": 1}
    assert cov["survivor_only_years"] == [2020, 2021]  # < 10% of 2019's count
    nd = cov["no_data_by_data_status"]
    assert nd["ended_early"] == {"evaluated": 2, "no_data": 1, "no_data_rate": 0.5}
    assert nd["live"]["evaluated"] == 3 and nd["live"]["no_data"] == 1  # regime/disabled excluded
    md = M.render_markdown(s, "cov")
    assert "## 数据覆盖 / Data coverage" in md
    assert "Effectively survivor-only test years" in md and "2020, 2021" in md


def test_entry_day_stops_reported_per_setup():
    trades = make_trades(
        [-100.0, -100.0, 50.0, -100.0],
        setups=["BO", "BO", "BO", "EP"],
        reasons=["stop", "stop", "stop", "trail"],
    )
    trades["hold_sessions"] = [0, 2, 0, 0]
    trades.loc[2, "pnl"], trades.loc[2, "r_multiple"] = 50.0, 0.5
    s = M.summarize(result_of(make_equity(SESSIONS, EQ_VALUES), trades), standard_panel(), Config())
    eds = s["entry_day_stops"]
    assert list(eds) == ["EP", "BO"]
    assert eds["BO"]["count"] == 2 and eds["BO"]["share"] == pytest.approx(2 / 3)
    assert eds["BO"]["total_pnl"] == pytest.approx(-50.0)
    assert eds["EP"]["count"] == 0 and eds["EP"]["expectancy_r"] is None
    assert "## 入场当日止损 / Entry-day stops" in M.render_markdown(s, "x")


def test_write_results_files(tmp_path):
    trades = make_trades(PNLS)
    skips = pd.DataFrame(
        {"day": [date(2020, 1, 2)], "symbol": ["A"], "setup": ["BO"], "reason": ["chase"]}
    )
    res = result_of(make_equity(SESSIONS, EQ_VALUES), trades, skips)
    s = M.summarize(res, standard_panel(), Config())
    out = tmp_path / "results" / "my_run"
    M.write_results(res, s, out)
    for name in ("trades.csv", "equity.csv", "skips.csv", "summary.json", "report.md"):
        assert (out / name).is_file(), name
    loaded = json.loads((out / "summary.json").read_text(encoding="utf-8"))
    assert loaded == s
    eq = pd.read_csv(out / "equity.csv")
    assert list(eq.columns) == ["date", "equity", "cash", "gross", "n_positions"]
    assert eq["date"].iloc[0] == "2020-01-02"
    assert len(pd.read_csv(out / "trades.csv")) == 10
    assert pd.read_csv(out / "skips.csv")["reason"].tolist() == ["chase"]
    report = (out / "report.md").read_text(encoding="utf-8")
    assert "my_run" in report and "chase" in report


# --------------------------------------------------------------------------- ablations
def test_ablation_table_and_markdown():
    base = M.summarize(
        result_of(make_equity(SESSIONS, EQ_VALUES), make_trades(PNLS)), standard_panel(), Config()
    )
    empty = M.summarize(
        result_of(make_equity(SESSIONS[:1], [100_000.0])), standard_panel(), Config()
    )
    df = M.ablation_table([("baseline", base), ("empty", empty), ("bare", base["overall"])])
    assert list(df.columns) == M.ABLATION_COLUMNS
    assert df["variant"].tolist() == ["baseline", "empty", "bare"]
    row = df.iloc[0]
    assert row["total_return"] == pytest.approx(0.0302)
    assert row["trades"] == 10
    assert row["win_rate"] == pytest.approx(0.4)
    assert row["profit_factor"] == pytest.approx(1950 / 550)
    assert row["top10_share"] == pytest.approx(1000 / 1950)
    assert math.isnan(df.iloc[1]["sharpe"]) and math.isnan(df.iloc[1]["win_rate"])
    assert df.iloc[2]["cagr"] == pytest.approx(row["cagr"])
    md = M.ablation_markdown(df)
    lines = md.strip().splitlines()
    assert len(lines) == 2 + 3
    assert lines[0].startswith("| Variant")
    assert "baseline" in md and "n/a" in md and "+3.0%" in md and "40.0%" in md
