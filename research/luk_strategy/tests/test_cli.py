"""Offline tests for cli.py: ablation definitions, argument wiring, cache staleness.

Stage modules (features / signals / engine / metrics) are replaced by fakes injected
into sys.modules (cli imports them lazily via importlib), and data-layer network
functions are monkeypatched, so nothing here touches the network or the real HOME."""

from __future__ import annotations

import ast
import json
import os
import pickle
import subprocess
import sys
import types
from dataclasses import fields
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from research.luk_strategy import cli, data
from research.luk_strategy.config import Config, parse_override
from research.luk_strategy.models import BacktestResult, Candidate, DailyPanel, Features

PKG = "research.luk_strategy"
PKG_DIR = Path(cli.__file__).resolve().parent
D1, D2, D3 = date(2017, 1, 3), date(2017, 1, 4), date(2017, 1, 5)


def _cand(day: date, symbol: str, setup: str) -> Candidate:
    return Candidate(
        day=day,
        symbol=symbol,
        setup=setup,
        level=None if setup == "EP" else 10.0,
        prev_close=9.5,
        prev_high=10.0,
        prev_low=9.0,
        open_today=9.6,
        adr=0.05,
        atr=0.5,
        adv_shares=1e6,
        rank_key=0.97,
        risk_on_index=True,
        risk_on_breadth=False,
    )


FAKE_WL = {
    D1: [_cand(D1, "AAA", "BO"), _cand(D1, "BBB", "PB"), _cand(D1, "AAA", "PB")],
    D2: [_cand(D2, "CCC", "EP")],
    D3: [],
}


@pytest.fixture
def env(tmp_path, monkeypatch):
    root = tmp_path / "luk"
    monkeypatch.setenv("LUK_DATA_DIR", str(root))
    monkeypatch.delenv("ALPACA_API_KEY_ID", raising=False)
    monkeypatch.delenv("ALPACA_API_SECRET_KEY", raising=False)
    monkeypatch.setenv("LUK_DOTENV", str(tmp_path / "absent.env"))

    def _forbidden(*_a, **_k):
        raise AssertionError("network/credential access in an offline test")

    for fn in ("_data_client", "load_credentials", "fetch_assets", "fetch_daily", "fetch_intraday"):
        monkeypatch.setattr(data, fn, _forbidden)
    # stage source files may be edited concurrently; pin their hash for determinism
    monkeypatch.setattr(cli, "_source_hash", lambda: "src-v1")

    rec = SimpleNamespace(panel_calls=[], features=[], watchlists=[], engine=[], written=[])
    idx = pd.DatetimeIndex(pd.bdate_range("2016-12-28", "2017-01-05"))
    frame = pd.DataFrame(10.0, index=idx, columns=["AAA", "BBB", "CCC", "QQQ", "SPY"])
    panel = DailyPanel(open=frame, high=frame + 1, low=frame - 1, close=frame, volume=frame * 1e5)

    def load_daily_panel(**kw):
        rec.panel_calls.append(kw)
        return panel

    monkeypatch.setattr(data, "load_daily_panel", load_daily_panel)
    daily = root / "daily"
    daily.mkdir(parents=True)
    (daily / "batch_0000.pkl").write_bytes(b"batch0")

    def compute_features(p, cfg):
        rec.features.append(cfg)
        s = pd.Series(True, index=p.dates)
        return Features(
            frames={"sma10": p.close}, risk_on_index=s, leaders_count=s * 1.0, risk_on_breadth=s
        )

    def build_watchlists(p, f, cfg):
        rec.watchlists.append(cfg)
        return FAKE_WL

    def intraday_manifest(wl):
        return {d: sorted({c.symbol for c in cands}) for d, cands in wl.items() if cands}

    def run_backtest(p, f, wl, cfg, intraday_store=None):
        rec.engine.append(SimpleNamespace(cfg=cfg, store=intraday_store, wl=wl))
        return BacktestResult(
            trades=pd.DataFrame(), equity=pd.DataFrame(), skips=pd.DataFrame(), config=cfg.to_dict()
        )

    def summarize(result, p, cfg):
        return {"overall": {"total_return": cfg.slip_bps / 100}, "config": cfg.to_dict()}

    def render_markdown(summary, name):
        return f"# {name}\n\ntotal_return={summary['overall']['total_return']}\n"

    def write_results(result, summary, out_dir):
        rec.written.append(Path(out_dir))
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "summary.json").write_text(json.dumps(summary))
        (out_dir / "report.md").write_text("old report")

    def ablation_table(rows):
        return pd.DataFrame(
            [{"variant": n, "total_return": s["overall"]["total_return"]} for n, s in rows]
        )

    def ablation_markdown(df):
        return "| variant |\n|---|\n" + "".join(f"| {v} |\n" for v in df["variant"])

    for name, attrs in {
        "features": {"compute_features": compute_features},
        "signals": {"build_watchlists": build_watchlists, "intraday_manifest": intraday_manifest},
        "engine": {"run_backtest": run_backtest},
        "metrics": {
            "summarize": summarize,
            "render_markdown": render_markdown,
            "write_results": write_results,
            "ablation_table": ablation_table,
            "ablation_markdown": ablation_markdown,
        },
    }.items():
        mod = types.ModuleType(f"{PKG}.{name}")
        for k, v in attrs.items():
            setattr(mod, k, v)
        monkeypatch.setitem(sys.modules, mod.__name__, mod)
    rec.root = root
    return rec


# --------------------------------------------------------------------------- ABLATIONS
EXPECTED_ABLATIONS = [
    "baseline", "bo_only", "ep_only", "pb_only", "contract_exits", "daily_entry",
    "regime_none", "regime_breadth", "slip25", "risk1pct", "trail_sma20", "trail_day1",
    "cutoff_1530", "tight_stop3",
]  # fmt: skip


def test_ablations_match_spec_and_parse_against_config():
    assert list(cli.ABLATIONS) == EXPECTED_ABLATIONS
    base = Config()
    expected = {
        "baseline": {},
        "bo_only": {"setups": ("BO",)},
        "ep_only": {"setups": ("EP",)},
        "pb_only": {"setups": ("PB",)},
        "contract_exits": {"exit_mode": "contract"},
        "daily_entry": {"entry_mode": "daily"},
        "regime_none": {"regime_mode": "none"},
        "regime_breadth": {"regime_mode": "breadth"},
        "slip25": {"slip_bps": 25.0},
        "risk1pct": {"risk_per_trade": 0.01},
        "trail_sma20": {"trail_ma": "sma20"},
        "trail_day1": {"trail_after_partial": False},
        "cutoff_1530": {"bo_cutoff": "15:30", "ep_cutoff": "15:30", "pb_cutoff": "15:30"},
        "tight_stop3": {"max_stop_pct": 0.03, "ep_max_stop_pct": 0.03},
    }
    for name, overrides in cli.ABLATIONS.items():
        cfg = base
        for k, raw in overrides.items():
            cfg = parse_override(cfg, k, raw)  # must not raise
        assert cfg == cli.apply_ablation(base, name)
        changed = {
            f.name: getattr(cfg, f.name)
            for f in fields(Config)
            if getattr(cfg, f.name) != getattr(base, f.name)
        }
        # pb_cutoff already defaults to 15:30, so only check values + "nothing else moved"
        assert {k: getattr(cfg, k) for k in expected[name]} == expected[name], name
        assert set(changed) <= set(expected[name]), name
        for k, v in changed.items():
            assert type(v) is type(getattr(base, k)), (name, k)
        # every variant reuses the baseline panel/features/watchlists
        assert set(overrides) <= cli.ENGINE_ONLY_FIELDS, name
        assert cli.watchlist_cfg(cfg) == cli.watchlist_cfg(base), name


def test_engine_only_fields_are_real_and_exclude_watchlist_inputs():
    names = {f.name for f in fields(Config)}
    assert names >= cli.ENGINE_ONLY_FIELDS
    must_invalidate = {
        "start", "end", "min_price", "min_adv", "min_adr", "min_history", "ep_min_adv",
        "leader_rs", "leader_min_ret", "bo_max_candidates", "ep_min_gap",
        "ep_require_neglect", "ep_neglect_max_ret63", "ep_max_candidates",
        "pb_max_candidates",
    }  # fmt: skip
    assert must_invalidate <= names
    assert not must_invalidate & cli.ENGINE_ONLY_FIELDS


def test_select_variants_and_build_config_validation():
    assert cli.select_variants(None) == EXPECTED_ABLATIONS
    assert cli.select_variants("slip25, baseline") == ["baseline", "slip25"]
    with pytest.raises(ValueError):
        cli.select_variants("baseline,nope")
    cfg = cli.build_config(["setups=BO,PB", "end=2020-12-31", "slip_bps=5"])
    assert cfg.setups == ("BO", "PB") and cfg.end == date(2020, 12, 31) and cfg.slip_bps == 5.0
    for bad in (
        ["nokey"],
        ["=1"],
        ["exit_mode=contrat"],
        ["setups=BO,XX"],
        ["bo_cutoff=1100"],
        ["trail_ma=sma50"],
        ["end=2016-01-01"],
        ["slip_bps=abc"],
    ):
        with pytest.raises(ValueError):
            cli.build_config(bad)
    with pytest.raises(KeyError):
        cli.build_config(["not_a_field=1"])
    with pytest.raises(ValueError):
        cli.results_dir("../escape")


# --------------------------------------------------------------------------- parser wiring
def test_parser_wiring_and_defaults():
    p = cli.build_parser()
    a = p.parse_args(["fetch-daily"])
    assert a.func is cli.cmd_fetch_daily and a.start == date(2016, 1, 1) and a.end is None
    a = p.parse_args(["fetch-daily", "--start", "2015-06-01", "--end", "2024-01-05"])
    assert (a.start, a.end) == (date(2015, 6, 1), date(2024, 1, 5))
    a = p.parse_args(["build-watchlists", "--set", "a=1", "--set", "b=2"])
    assert a.func is cli.cmd_build_watchlists and a.set == ["a=1", "b=2"] and not a.rebuild
    a = p.parse_args(["fetch-intraday"])
    assert a.func is cli.cmd_fetch_intraday and a.workers == 4
    a = p.parse_args(["backtest", "--name", "x", "--rebuild"])
    assert a.func is cli.cmd_backtest and a.name == "x" and a.set == [] and a.rebuild
    a = p.parse_args(["ablate", "--only", "baseline"])
    assert a.func is cli.cmd_ablate and a.only == "baseline"
    a = p.parse_args(["report", "--name", "x"])
    assert a.func is cli.cmd_report
    for argv in ([], ["backtest"], ["report"], ["fetch-daily", "--start", "2016/01/01"]):
        with pytest.raises(SystemExit) as e:
            p.parse_args(argv)
        assert e.value.code == 2


def test_fetch_daily_wiring(env, monkeypatch, capsys):
    seen = {}
    assets = pd.DataFrame({"symbol": ["AAA"], "name": ["A"], "exchange": ["NYSE"]})

    def fetch_assets(refresh=False):
        seen["refresh"] = refresh
        return assets

    def filter_symbols(df):
        assert df is assets
        return ["AAA", "QQQ", "SPY"]

    monkeypatch.setattr(data, "fetch_assets", fetch_assets)
    monkeypatch.setattr(data, "filter_symbols", filter_symbols)
    monkeypatch.setattr(data, "last_complete_session", lambda today=None: date(2026, 10, 12))
    monkeypatch.setattr(
        data,
        "fetch_daily",
        lambda syms, start, end, raw=False: (
            seen.update(syms=syms, start=start, end=end),
            seen.setdefault("raw_calls", []).append(raw),
        ),
    )
    assert cli.main(["fetch-daily"]) == 0
    assert seen.pop("raw_calls") == [False, True]  # adjusted + as-traded bars
    assert seen == {
        "refresh": False,
        "syms": ["AAA", "QQQ", "SPY"],
        "start": date(2016, 1, 1),
        "end": date(2026, 10, 12),
    }
    assert (
        cli.main(
            ["fetch-daily", "--start", "2018-01-01", "--end", "2019-06-30", "--refresh-assets"]
        )
        == 0
    )
    assert (seen["start"], seen["end"], seen["refresh"]) == (
        date(2018, 1, 1),
        date(2019, 6, 30),
        True,
    )
    with pytest.raises(SystemExit) as e:
        cli.main(["fetch-daily", "--start", "2020-01-01", "--end", "2019-01-01"])
    assert e.value.code == 2


def test_build_watchlists_writes_pickle_manifest_and_counts(env, capsys):
    assert cli.main(["build-watchlists", "--set", "bo_max_candidates=10"]) == 0
    assert env.panel_calls == [
        {
            "start": Config().start - timedelta(days=400),
            "end": None,
            "prefilter_adv": 5e6,
            "prefilter_price": 3.0,
        }
    ]
    assert len(env.features) == 1 and env.features[0].bo_max_candidates == 10
    with open(env.root / "watchlists.pkl", "rb") as f:
        assert pickle.load(f) == FAKE_WL
    manifest = json.loads((env.root / "intraday_manifest.json").read_text())
    assert manifest == {"2017-01-03": ["AAA", "BBB"], "2017-01-04": ["CCC"]}
    meta = json.loads((env.root / "watchlists.meta.json").read_text())
    assert meta["counts"] == {"BO": 1, "EP": 1, "PB": 2}
    assert meta["manifest_symbol_days"] == 3 and meta["config"]["bo_max_candidates"] == 10
    out = capsys.readouterr().out
    assert "BO: 1 candidates" in out and "EP: 1 candidates" in out and "PB: 2 candidates" in out
    assert "3 symbol-days" in out


def test_panel_prefilter_never_stricter_than_configured_universe(env):
    """Codex P2: lowering min_adv / ep_min_adv / min_price below the loader's default
    prefilter must loosen the prefilter too, or eligible names are silently dropped."""
    cmd = ["build-watchlists", "--set", "min_adv=2e6", "--set", "ep_min_adv=2e6"]
    assert cli.main([*cmd, "--set", "min_price=1"]) == 0
    assert env.panel_calls[-1]["prefilter_adv"] == 2e6
    assert env.panel_calls[-1]["prefilter_price"] == 1.0


def test_warmup_covers_a_raised_min_history(env):
    """Codex P2: min_history beyond the default warmup must extend the loaded history."""
    assert cli.warmup_days(Config()) == cli.WARMUP_DAYS
    assert cli.main(["build-watchlists", "--set", "min_history=400"]) == 0
    loaded = env.panel_calls[-1]["start"]
    sessions = len(pd.bdate_range(loaded, Config().start)) - 1
    assert sessions >= 400 * 1.05  # weekdays, with room for ~10 holidays a year


def test_fetch_intraday_uses_saved_watchlists(env, monkeypatch):
    assert cli.main(["fetch-intraday"]) == 1  # no watchlists.pkl yet
    cli.main(["build-watchlists"])
    seen = {}
    monkeypatch.setattr(data, "fetch_calendar", lambda *a, **k: {})
    monkeypatch.setattr(
        data, "fetch_intraday", lambda req, workers=4: seen.update(req=req, workers=workers)
    )
    assert cli.main(["fetch-intraday", "--workers", "2"]) == 0
    assert seen == {"req": {D1: ["AAA", "BBB"], D2: ["CCC"]}, "workers": 2}
    assert cli.main(["fetch-intraday", "--since", "2017-01-04"]) == 0
    assert seen["req"] == {D2: ["CCC"]} and seen["workers"] == 4
    with pytest.raises(SystemExit):
        cli.main(["fetch-intraday", "--workers", "0"])


# --------------------------------------------------------------------------- backtest + cache
def test_backtest_wiring_and_outputs(env, capsys):
    assert cli.main(["backtest", "--name", "t1", "--set", "slip_bps=25"]) == 0
    (call,) = env.engine
    assert call.cfg.slip_bps == 25.0 and call.wl == FAKE_WL
    assert isinstance(call.store, data.IntradayStore)
    out_dir = env.root / "results" / "t1"
    assert env.written == [out_dir]
    summary = json.loads((out_dir / "summary.json").read_text())
    cov = summary["intraday_coverage"]  # no intraday files / manifest in tmp -> all gaps
    assert cov["manifest_days"] == 2 and cov["days_without_cache_file"] == 2
    assert cov["symbol_days_not_in_built_manifest"] == 3
    assert "# t1" in capsys.readouterr().out

    assert cli.main(["backtest", "--name", "t2", "--set", "entry_mode=daily"]) == 0
    assert env.engine[-1].store is None and env.engine[-1].cfg.entry_mode == "daily"
    assert "intraday_coverage" not in json.loads(
        (env.root / "results" / "t2" / "summary.json").read_text()
    )


def test_backtest_cache_reuse_and_invalidation(env, monkeypatch):
    def run(*sets):
        argv = ["backtest", "--name", "c"]
        for s in sets:
            argv += ["--set", s]
        assert cli.main(argv) == 0
        return len(env.features)

    assert run() == 1
    assert (env.root / "cache_pf.pkl").exists() and (env.root / "cache_pf.key.json").exists()
    assert run() == 1  # fresh cache reused
    assert run("slip_bps=25", "exit_mode=contract", "trail_ma=sma20") == 1  # engine-only
    assert run("bo_max_candidates=5") == 2  # watchlist input changed
    assert run("bo_max_candidates=5") == 2
    assert run("end=2017-01-05") == 3  # period changes the panel
    assert env.panel_calls[-1]["end"] == date(2017, 1, 5)
    assert run() == 4
    (env.root / "daily" / "batch_0000.pkl").write_bytes(b"refetched batch")  # size/mtime
    assert run() == 5
    (env.root / "daily" / "batch_0001.pkl").write_bytes(b"new batch")
    assert run() == 6
    monkeypatch.setattr(cli, "_source_hash", lambda: "src-v2")  # stage code edited
    assert run() == 7
    assert run() == 7
    assert cli.main(["backtest", "--name", "c", "--rebuild"]) == 0
    assert len(env.features) == 8


def test_cache_embedded_key_is_rechecked(env):
    cfg = Config()
    _, _, _, key = cli.load_or_build(cfg)
    path = env.root / "cache_pf.pkl"
    with open(path, "rb") as f:
        blob = pickle.load(f)
    blob["key"] = {**key, "cfg_hash": "tampered"}  # sidecar says fresh, pickle disagrees
    with open(path, "wb") as f:
        pickle.dump(blob, f)
    cli.load_or_build(cfg)
    assert len(env.features) == 2
    (env.root / "cache_pf.key.json").write_text("{not json")
    cli.load_or_build(cfg)
    assert len(env.features) == 3


def test_intraday_coverage_counts_partial_cache(env):
    manifest = {D1: ["AAA", "BBB"], D2: ["CCC"]}
    (env.root / "intraday").mkdir()
    data._intraday_path(env.root, D1).write_bytes(b"x")
    (env.root / "intraday_manifest.json").write_text(json.dumps({"2017-01-03": ["AAA", "BBB"]}))
    cov = cli.intraday_coverage(manifest)
    assert cov == {
        "manifest_days": 2,
        "manifest_symbol_days": 3,
        "days_without_cache_file": 1,
        "first_days_without_cache_file": ["2017-01-04"],
        "symbol_days_not_in_built_manifest": 1,
    }


def test_errors_inside_stages_are_not_turned_into_usage_errors(env):
    def boom(*_a, **_k):
        raise KeyError("bug inside the engine")

    sys.modules[f"{PKG}.engine"].run_backtest = boom
    with pytest.raises(KeyError):
        cli.main(["backtest", "--name", "boom"])


@pytest.mark.parametrize(
    "argv",
    [
        ["backtest", "--name", "x", "--set", "nope=1"],
        ["backtest", "--name", "x", "--set", "slip_bps"],
        ["backtest", "--name", "x", "--set", "exit_mode=contrat"],
        ["backtest", "--name", "../x"],
        ["build-watchlists", "--set", "regime_mode=sometimes"],
        ["ablate", "--only", "baseline,bogus"],
        ["ablate", "--set", "max_hold=ten"],
        ["report", "--name", "a/b"],
    ],
)
def test_bad_input_is_a_usage_error_before_any_work(env, argv):
    with pytest.raises(SystemExit) as e:
        cli.main(argv)
    assert e.value.code == 2
    assert env.panel_calls == [] and env.engine == []


# --------------------------------------------------------------------------- ablate / report
def test_ablate_runs_selected_variants_on_one_build(env, capsys):
    assert (
        cli.main(["ablate", "--only", "slip25,daily_entry,baseline", "--set", "end=2017-01-05"])
        == 0
    )
    assert len(env.features) == 1 and len(env.watchlists) == 1
    cfgs = {c.cfg.slip_bps: c for c in env.engine}
    assert [c.cfg.entry_mode for c in env.engine] == ["intraday", "daily", "intraday"]
    assert all(c.cfg.end == date(2017, 1, 5) for c in env.engine)  # base override applied
    assert set(cfgs) == {10.0, 25.0}
    assert env.engine[1].store is None
    assert isinstance(env.engine[0].store, data.IntradayStore)
    assert env.engine[0].store is env.engine[2].store
    res = env.root / "results"
    assert env.written == [res / "baseline", res / "daily_entry", res / "slip25"]
    table = pd.read_csv(res / "ablation_table.csv")
    assert table["variant"].tolist() == ["baseline", "daily_entry", "slip25"]
    assert table["total_return"].tolist() == [0.1, 0.1, 0.25]
    md = (res / "ablation_table.md").read_text()
    assert "| slip25 |" in md and "| slip25 |" in capsys.readouterr().out


def test_ablate_all_variants(env):
    assert cli.main(["ablate"]) == 0
    assert len(env.features) == 1
    assert [p.name for p in env.written] == EXPECTED_ABLATIONS


def test_report_rerenders_from_summary(env, capsys):
    assert cli.main(["report", "--name", "never_ran"]) == 1
    cli.main(["backtest", "--name", "r1"])
    out_dir = env.root / "results" / "r1"
    s = json.loads((out_dir / "summary.json").read_text())
    s["overall"]["total_return"] = 0.42
    (out_dir / "summary.json").write_text(json.dumps(s))
    capsys.readouterr()
    assert cli.main(["report", "--name", "r1"]) == 0
    assert (out_dir / "report.md").read_text() == "# r1\n\ntotal_return=0.42\n"
    assert "total_return=0.42" in capsys.readouterr().out


# --------------------------------------------------------------------------- isolation
FORBIDDEN = {"pipeline", "tradingagents", "cli", "webui", "compare", "devtools"}


def test_package_never_imports_repo_components():
    """SPEC §1: no module in the package may import production repo packages."""
    for path in sorted(PKG_DIR.rglob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                tops = {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                tops = {node.module.split(".")[0]}
            else:
                continue
            assert not tops & FORBIDDEN, f"{path.name}:{node.lineno} imports {tops & FORBIDDEN}"


def test_module_entrypoint_help(tmp_path):
    root = PKG_DIR.parents[1]
    proc = subprocess.run(
        [sys.executable, "-m", f"{PKG}.cli", "--help"],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=60,
        env={**os.environ, "LUK_DATA_DIR": str(tmp_path), "HOME": str(tmp_path)},
    )
    assert proc.returncode == 0, proc.stderr
    for cmd in (
        "fetch-daily",
        "build-watchlists",
        "fetch-intraday",
        "backtest",
        "ablate",
        "report",
    ):
        assert cmd in proc.stdout
