"""Command-line entry point for the Luk-style research backtest (SPEC.md §1, §9).

Run from the worktree root:

    .venv/bin/python -m research.luk_strategy.cli <command> [options]

Commands: fetch-daily, build-watchlists, fetch-intraday, backtest, ablate, report.
The CLI is deliberately thin: data / features / signals / engine / metrics do the
work. Stage modules other than `data` are imported lazily (`_stage`) so the fetch
commands stay light and tests can stub them through `sys.modules`.

Everything is written under `data_dir()` (env `LUK_DATA_DIR`), never in the repo:
  watchlists.pkl, watchlists.meta.json, intraday_manifest.json, cache_pf.pkl(+.key.json),
  results/<name>/{trades,equity,skips}.csv, summary.json, report.md,
  results/ablation_table.{csv,md}.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import logging
import pickle
import re
import sys
from collections import Counter
from dataclasses import fields
from datetime import date, timedelta
from pathlib import Path
from types import ModuleType
from typing import Any

from . import data
from .config import Config, data_dir, parse_override

log = logging.getLogger(__name__)

# Default symbol prefilter for the wide panel (data.load_daily_panel); lowered to the
# configured universe thresholds when those are looser.
PREFILTER_ADV = 5e6
PREFILTER_PRICE = 3.0

# Calendar days of daily history loaded before cfg.start (~275 sessions >= min_history 150,
# enough for ret126 / SMA50 / high63 at the first backtest session).
WARMUP_DAYS = 400
CACHE_VERSION = 1
SUMMARY_HEAD_LINES = 60

# Config fields that only the intraday trigger / engine / exits / sizing read. Changing them
# can never change the panel, the features, or the watchlists, so cached panel+features+
# watchlists stay valid across them. Every other field (including any field added later) is
# treated as watchlist-relevant and invalidates the cache: unknown == conservative.
ENGINE_ONLY_FIELDS = frozenset(
    {
        "entry_mode",
        "exit_mode",
        "setups",
        "regime_mode",
        "bo_max_extension",
        "bo_cutoff",
        "ep_min_first_bar_vol_frac",
        "ep_cutoff",
        "pb_cutoff",
        "min_stop_pct",
        "max_stop_pct",
        "max_stop_adr_mult",
        "ep_max_stop_pct",
        "ep_in_risk_off",
        "risk_off_ep_mult",
        "initial_equity",
        "risk_per_trade",
        "max_position_pct",
        "max_gross",
        "max_new_per_day",
        "max_open_positions",
        "slip_bps",
        "partial_day",
        "partial_frac",
        "trail_ma",
        "trail_after_partial",
        "max_hold",
        "data_gap_sessions",
        "contract_target_r",
        "contract_horizon",
        "contract_atr_floor",
    }
)

# Source files whose code determines the cached panel/features/watchlists objects.
_CACHE_SOURCES = ("data.py", "features.py", "signals.py", "models.py")

# SPEC §9 ablations. Values are raw strings fed through config.parse_override exactly like
# `--set k=v`; each variant is applied on top of the (optionally overridden) base config.
# All keys are in ENGINE_ONLY_FIELDS, so one panel/features/watchlists build serves all.
ABLATIONS: dict[str, dict[str, str]] = {
    "baseline": {},
    "bo_only": {"setups": "BO"},
    "ep_only": {"setups": "EP"},
    "pb_only": {"setups": "PB"},
    "contract_exits": {"exit_mode": "contract"},
    "daily_entry": {"entry_mode": "daily"},
    "regime_none": {"regime_mode": "none"},
    "regime_breadth": {"regime_mode": "breadth"},
    "slip25": {"slip_bps": "25"},
    "risk1pct": {"risk_per_trade": "0.01"},
    "trail_sma20": {"trail_ma": "sma20"},
    "trail_day1": {"trail_after_partial": "false"},
    "cutoff_1530": {"bo_cutoff": "15:30", "ep_cutoff": "15:30", "pb_cutoff": "15:30"},
    # Luk's stated typical stop is < 3%: only take entries whose LOD stop fits in 3%.
    "tight_stop3": {"max_stop_pct": "0.03", "ep_max_stop_pct": "0.03"},
}

_CHOICES: dict[str, set[str]] = {
    "entry_mode": {"intraday", "daily"},
    "exit_mode": {"luk", "contract"},
    "regime_mode": {"index", "breadth", "both", "none"},
    "trail_ma": {"sma10", "ema10", "sma20", "ema21"},
}
_SETUPS = {"EP", "BO", "PB"}
_HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
_NAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]*$")


class CliError(Exception):
    """User-facing failure (missing prerequisite file etc.): logged, exit code 1."""


# --------------------------------------------------------------------------- helpers
def _stage(name: str) -> ModuleType:
    """Lazily import a sibling stage module (features, signals, engine, metrics)."""
    return importlib.import_module(f"{__package__}.{name}")


def validate_config(cfg: Config) -> Config:
    """Reject values the engine would silently misread (typos in --set)."""
    for key, allowed in _CHOICES.items():
        if getattr(cfg, key) not in allowed:
            raise ValueError(f"{key}={getattr(cfg, key)!r}; expected one of {sorted(allowed)}")
    if not cfg.setups or not set(cfg.setups) <= _SETUPS:
        raise ValueError(f"setups={cfg.setups!r}; expected a non-empty subset of {sorted(_SETUPS)}")
    for key in ("bo_cutoff", "ep_cutoff", "pb_cutoff"):
        if not _HHMM.match(getattr(cfg, key)):
            raise ValueError(f"{key}={getattr(cfg, key)!r}; expected HH:MM (ET)")
    if not isinstance(cfg.start, date):
        raise ValueError("start must be a YYYY-MM-DD date")
    if cfg.end is not None and cfg.end < cfg.start:
        raise ValueError(f"end {cfg.end} is before start {cfg.start}")
    return cfg


def build_config(overrides: list[str] | None = None, base: Config | None = None) -> Config:
    """Apply `k=v` overrides (config.parse_override) on top of `base` (default Config())."""
    cfg = base or Config()
    for item in overrides or []:
        key, sep, raw = item.partition("=")
        if not sep or not key.strip():
            raise ValueError(f"override must look like key=value, got {item!r}")
        cfg = parse_override(cfg, key.strip(), raw.strip())
    return validate_config(cfg)


def select_variants(only: str | None) -> list[str]:
    """ABLATIONS names to run, in canonical order; `only` is a comma list."""
    if not only:
        return list(ABLATIONS)
    wanted = [v.strip() for v in only.split(",") if v.strip()]
    unknown = [v for v in wanted if v not in ABLATIONS]
    if unknown or not wanted:
        raise ValueError(f"unknown ablation(s) {unknown or only!r}; choose from {list(ABLATIONS)}")
    return [v for v in ABLATIONS if v in wanted]


def apply_ablation(base: Config, variant: str) -> Config:
    cfg = base
    for key, raw in ABLATIONS[variant].items():
        cfg = parse_override(cfg, key, raw)
    return validate_config(cfg)


def results_dir(name: str) -> Path:
    if not _NAME_RE.match(name) or ".." in name:
        raise ValueError(f"invalid run name {name!r} (letters, digits, _ . - only)")
    return data_dir() / "results" / name


def watchlist_cfg(cfg: Config) -> dict[str, Any]:
    """The config fields that can influence panel / features / watchlists."""
    return {k: v for k, v in cfg.to_dict().items() if k not in ENGINE_ONLY_FIELDS}


def _daily_signature(root: Path) -> list[list[Any]]:
    out = []
    for sub in ("daily", "daily_raw"):
        for p in sorted((root / sub).glob("batch_*.pkl")):
            st = p.stat()
            out.append([f"{sub}/{p.name}", st.st_mtime_ns, st.st_size])
    return out


def _source_hash() -> str:
    h = hashlib.sha256()
    here = Path(__file__).resolve().parent
    for name in _CACHE_SOURCES:
        p = here / name
        h.update(name.encode())
        h.update(p.read_bytes() if p.exists() else b"<missing>")
    return h.hexdigest()


def cache_key(cfg: Config, root: Path | None = None) -> dict[str, Any]:
    """Staleness key for cache_pf.pkl: daily batch files (name, mtime, size), a hash of the
    watchlist-relevant cfg fields, and a hash of the stage source files."""
    root = root or data_dir()
    cfg_json = json.dumps(watchlist_cfg(cfg), sort_keys=True, default=str)
    return {
        "version": CACHE_VERSION,
        "daily": _daily_signature(root),
        "cfg_hash": hashlib.sha256(cfg_json.encode()).hexdigest(),
        "src_hash": _source_hash(),
    }


def _key_digest(key: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(key, sort_keys=True).encode()).hexdigest()[:16]


def _atomic_pickle(obj: Any, path: Path) -> None:
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "wb") as f:
        pickle.dump(obj, f, protocol=pickle.HIGHEST_PROTOCOL)
    tmp.replace(path)


def _atomic_text(text: str, path: Path) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text)
    tmp.replace(path)


def build_panel_features_watchlists(cfg: Config):
    """Load the daily panel (with warmup), compute features, build all watchlists."""
    features, signals = _stage("features"), _stage("signals")
    warm_start = cfg.start - timedelta(days=WARMUP_DAYS)
    log.info("loading daily panel from %s (warmup) to %s", warm_start, cfg.end or "latest")
    # The symbol prefilter must never be stricter than the configured universes, or a
    # lowered threshold override would silently drop eligible names.
    panel = data.load_daily_panel(
        start=warm_start,
        end=cfg.end,
        prefilter_adv=min(PREFILTER_ADV, cfg.min_adv, cfg.ep_min_adv),
        prefilter_price=min(PREFILTER_PRICE, cfg.min_price),
    )
    log.info("panel: %d sessions x %d symbols", len(panel.dates), len(panel.symbols))
    feats = features.compute_features(panel, cfg)
    log.info("features computed")
    wl = signals.build_watchlists(panel, feats, cfg)
    log.info("watchlists: %d sessions", len(wl))
    return panel, feats, wl


def load_or_build(cfg: Config, rebuild: bool = False):
    """(panel, features, watchlists, key) from cache_pf.pkl if its key matches, else rebuilt.

    The small sidecar `cache_pf.key.json` is checked first so a stale multi-GB pickle is
    never loaded; the key embedded in the pickle is re-checked after loading. The pickle is
    written before the sidecar, so a crash in between leaves a mismatch (rebuild), never a
    stale pickle under a fresh key."""
    root = data_dir()
    path, key_path = root / "cache_pf.pkl", root / "cache_pf.key.json"
    key = cache_key(cfg, root)
    if not rebuild and path.exists() and key_path.exists():
        try:
            side = json.loads(key_path.read_text())
        except (OSError, ValueError):
            side = None
        if side == key:
            with open(path, "rb") as f:
                blob = pickle.load(f)
            if blob.get("key") == key:
                log.info("using cached panel/features/watchlists (%s)", _key_digest(key))
                return blob["panel"], blob["features"], blob["watchlists"], key
            log.warning("cache_pf.pkl embedded key mismatch; rebuilding")
        else:
            log.info("cache_pf.pkl stale; rebuilding")
    panel, feats, wl = build_panel_features_watchlists(cfg)
    key_path.unlink(missing_ok=True)
    _atomic_pickle({"key": key, "panel": panel, "features": feats, "watchlists": wl}, path)
    _atomic_text(json.dumps(key, sort_keys=True), key_path)
    return panel, feats, wl, key


def manifest_to_json(manifest: dict[date, list[str]]) -> dict[str, list[str]]:
    return {d.isoformat(): sorted(set(syms)) for d, syms in sorted(manifest.items())}


def load_watchlists(path: Path | None = None) -> dict:
    path = path or data_dir() / "watchlists.pkl"
    if not path.exists():
        raise CliError(f"{path} not found; run `cli build-watchlists` first")
    with open(path, "rb") as f:
        return pickle.load(f)


def intraday_coverage(manifest: dict[date, list[str]], root: Path | None = None) -> dict:
    """Cheap pre-flight check: manifest days with no intraday cache file, and candidate
    symbol-days absent from the intraday_manifest.json written by build-watchlists (the list
    fetch-intraday works from). Either shows up later as `no_data` skips; logging it up
    front keeps a partial intraday cache from passing silently."""
    root = root or data_dir()
    days = sorted(manifest)
    missing_days = [d for d in days if not data._intraday_path(root, d).exists()]
    pairs = {(d.isoformat(), s) for d, syms in manifest.items() for s in syms}
    built_path = root / "intraday_manifest.json"
    if built_path.exists():
        built = json.loads(built_path.read_text())
        known = {(d, s) for d, syms in built.items() for s in syms}
        not_in_built = len(pairs - known)
    else:
        not_in_built = len(pairs)
    return {
        "manifest_days": len(days),
        "manifest_symbol_days": len(pairs),
        "days_without_cache_file": len(missing_days),
        "first_days_without_cache_file": [d.isoformat() for d in missing_days[:5]],
        "symbol_days_not_in_built_manifest": not_in_built,
    }


def run_variant(name: str, cfg: Config, panel, feats, wl, intraday_store=None) -> dict:
    """Run one backtest, write results/<name>/, return the summary dict."""
    engine, metrics = _stage("engine"), _stage("metrics")
    out = results_dir(name)
    store = None
    coverage = None
    if cfg.entry_mode == "intraday":
        store = intraday_store if intraday_store is not None else data.IntradayStore()
        coverage = intraday_coverage(_stage("signals").intraday_manifest(wl))
        if coverage["days_without_cache_file"] or coverage["symbol_days_not_in_built_manifest"]:
            log.warning("intraday coverage gaps (will appear as no_data skips): %s", coverage)
    log.info("[%s] running backtest (%s entry, %s exits)", name, cfg.entry_mode, cfg.exit_mode)
    result = engine.run_backtest(panel, feats, wl, cfg, intraday_store=store)
    summary = metrics.summarize(result, panel, cfg)
    if coverage is not None:
        summary["intraday_coverage"] = coverage
    out.mkdir(parents=True, exist_ok=True)
    metrics.write_results(result, summary, out)
    log.info("[%s] results written to %s", name, out)
    return summary


def _print_head(md: str, lines: int = SUMMARY_HEAD_LINES) -> None:
    rows = md.splitlines()
    print("\n".join(rows[:lines]))
    if len(rows) > lines:
        print(f"... ({len(rows) - lines} more lines in report.md)")


# --------------------------------------------------------------------------- commands
def cmd_fetch_daily(args: argparse.Namespace) -> int:
    end = args.end
    assets = data.fetch_assets(refresh=args.refresh_assets)
    symbols = data.filter_symbols(assets)
    log.info("fetch-daily: %d symbols, %s -> %s", len(symbols), args.start, end)
    data.fetch_daily(symbols, args.start, end)
    data.fetch_daily(symbols, args.start, end, raw=True)  # as-traded closes for price floors
    print(f"daily bars cached for {len(symbols)} symbols, {args.start} -> {end}")
    return 0


def cmd_build_watchlists(args: argparse.Namespace) -> int:
    cfg = args.cfg
    _, _, wl, key = load_or_build(cfg, rebuild=args.rebuild)
    manifest = _stage("signals").intraday_manifest(wl)
    root = data_dir()
    _atomic_pickle(wl, root / "watchlists.pkl")
    counts = Counter(c.setup for cands in wl.values() for c in cands)
    symbol_days = sum(len(set(v)) for v in manifest.values())
    meta = {
        "cache_key": key,
        "config": cfg.to_dict(),
        "sessions": len(wl),
        "counts": dict(sorted(counts.items())),
        "manifest_days": len(manifest),
        "manifest_symbol_days": symbol_days,
    }
    _atomic_text(json.dumps(meta, indent=2, sort_keys=True), root / "watchlists.meta.json")
    _atomic_text(json.dumps(manifest_to_json(manifest)), root / "intraday_manifest.json")
    print(f"watchlists: {len(wl)} sessions -> {root / 'watchlists.pkl'}")
    for setup in ("EP", "BO", "PB"):
        print(f"  {setup}: {counts.get(setup, 0)} candidates")
    print(f"intraday manifest: {len(manifest)} days, {symbol_days} symbol-days")
    return 0


def cmd_fetch_intraday(args: argparse.Namespace) -> int:
    wl = load_watchlists()
    manifest = _stage("signals").intraday_manifest(wl)
    if args.since:
        manifest = {d: s for d, s in manifest.items() if d >= args.since}
    if args.until:
        manifest = {d: s for d, s in manifest.items() if d <= args.until}
    symbol_days = sum(len(set(v)) for v in manifest.values())
    log.info("fetch-intraday: %d days, %d symbol-days", len(manifest), symbol_days)
    data.fetch_calendar()  # session closes, so half-day after-hours bars are dropped on load
    data.fetch_intraday(manifest, workers=args.workers)
    print(f"intraday bars cached for {len(manifest)} days ({symbol_days} symbol-days)")
    return 0


def cmd_backtest(args: argparse.Namespace) -> int:
    cfg = args.cfg
    panel, feats, wl, _ = load_or_build(cfg, rebuild=args.rebuild)
    summary = run_variant(args.name, cfg, panel, feats, wl)
    _print_head(_stage("metrics").render_markdown(summary, args.name))
    print(f"\nresults: {results_dir(args.name)}")
    return 0


def cmd_ablate(args: argparse.Namespace) -> int:
    base, cfgs = args.cfg, args.variant_cfgs
    variants = list(cfgs)
    panel, feats, wl, _ = load_or_build(base, rebuild=args.rebuild)
    store = data.IntradayStore()
    rows: list[tuple[str, dict]] = []
    for v in variants:
        cfg = cfgs[v]
        if watchlist_cfg(cfg) != watchlist_cfg(base):  # defensive; ABLATIONS never do this
            log.warning("[%s] changes watchlist inputs; building its own panel/features", v)
            p, f, w, _ = load_or_build(cfg)
        else:
            p, f, w = panel, feats, wl
        rows.append((v, run_variant(v, cfg, p, f, w, intraday_store=store)))
    metrics = _stage("metrics")
    table = metrics.ablation_table(rows)
    out = data_dir() / "results"
    out.mkdir(parents=True, exist_ok=True)
    table.to_csv(out / "ablation_table.csv", index=False)
    md = metrics.ablation_markdown(table)
    _atomic_text(md, out / "ablation_table.md")
    print(md)
    print(f"\nablation table: {out / 'ablation_table.csv'}")
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    out = results_dir(args.name)
    path = out / "summary.json"
    if not path.exists():
        raise CliError(f"{path} not found; run `cli backtest --name {args.name}` first")
    summary = json.loads(path.read_text())
    md = _stage("metrics").render_markdown(summary, args.name)
    _atomic_text(md, out / "report.md")
    print(md)
    return 0


# --------------------------------------------------------------------------- parser
def _iso_date(s: str) -> date:
    try:
        return date.fromisoformat(s)
    except ValueError as e:
        raise argparse.ArgumentTypeError(f"expected YYYY-MM-DD, got {s!r}") from e


def _add_set(p: argparse.ArgumentParser) -> None:
    names = ", ".join(f.name for f in fields(Config))
    p.add_argument(
        "--set",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help=f"override a Config field (repeatable). Fields: {names}",
    )
    p.add_argument(
        "--rebuild", action="store_true", help="ignore cache_pf.pkl and rebuild features"
    )


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="python -m research.luk_strategy.cli",
        description="Luk-style momentum swing strategy: isolated research backtest.",
    )
    sub = ap.add_subparsers(dest="command", required=True)

    p = sub.add_parser("fetch-daily", help="fetch asset list + split-adjusted and raw daily bars")
    p.add_argument("--start", type=_iso_date, default=date(2016, 1, 1))
    p.add_argument("--end", type=_iso_date, default=None, help="default: last complete session")
    p.add_argument("--refresh-assets", action="store_true", help="re-download the asset list")
    p.set_defaults(func=cmd_fetch_daily)

    p = sub.add_parser("build-watchlists", help="features + per-session candidate lists")
    _add_set(p)
    p.set_defaults(func=cmd_build_watchlists)

    p = sub.add_parser("fetch-intraday", help="5-min bars for every watchlist symbol-day")
    p.add_argument("--workers", type=int, default=4)
    p.add_argument("--since", type=_iso_date, default=None, help="only days >= this date")
    p.add_argument("--until", type=_iso_date, default=None, help="only days <= this date")
    p.set_defaults(func=cmd_fetch_intraday)

    p = sub.add_parser("backtest", help="run one backtest into results/<name>/")
    p.add_argument("--name", required=True)
    _add_set(p)
    p.set_defaults(func=cmd_backtest)

    p = sub.add_parser("ablate", help="run the SPEC §9 ablation variants")
    p.add_argument("--only", default=None, help=f"comma list from: {','.join(ABLATIONS)}")
    _add_set(p)
    p.set_defaults(func=cmd_ablate)

    p = sub.add_parser("report", help="re-render results/<name>/report.md from summary.json")
    p.add_argument("--name", required=True)
    p.set_defaults(func=cmd_report)
    return ap


def _prepare(args: argparse.Namespace) -> None:
    """Validate user input up front (before any expensive load). Raises KeyError/ValueError,
    which main() turns into a usage error; errors raised later are not masked that way."""
    if args.command == "fetch-daily":
        args.end = args.end or data.last_complete_session()
        if args.end < args.start:
            raise ValueError(f"--end {args.end} is before --start {args.start}")
    if hasattr(args, "set"):
        args.cfg = build_config(args.set)
    if getattr(args, "name", None) is not None:
        results_dir(args.name)
    if args.command == "ablate":
        args.variant_cfgs = {v: apply_ablation(args.cfg, v) for v in select_variants(args.only)}
    if args.command == "fetch-intraday" and args.workers < 1:
        raise ValueError("--workers must be >= 1")


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        _prepare(args)
    except (KeyError, ValueError) as e:
        parser.error(str(e.args[0]) if isinstance(e, KeyError) and e.args else str(e))
    try:
        return int(args.func(args) or 0)
    except CliError as e:
        log.error("%s", e)
        return 1


if __name__ == "__main__":
    sys.exit(main())
