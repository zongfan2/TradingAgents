"""Strategy/backtest configuration. All defaults mirror SPEC.md (a-priori, not fitted)."""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass, fields, replace
from datetime import date
from pathlib import Path
from typing import Any

BENCHMARKS = ("SPY", "QQQ")
SETUP_PRIORITY = {"EP": 0, "BO": 1, "PB": 2}


@dataclass(frozen=True)
class Config:
    # --- period / mode ---
    start: date = date(2017, 1, 3)
    end: date | None = None  # None -> latest cached session
    entry_mode: str = "intraday"  # "intraday" | "daily"
    exit_mode: str = "luk"  # "luk" | "contract"
    setups: tuple[str, ...] = ("EP", "BO", "PB")
    regime_mode: str = "index"  # "index" | "breadth" | "both" | "none"

    # --- universe ---
    min_price: float = 5.0
    min_adv: float = 10e6
    min_adr: float = 0.035
    min_history: int = 150
    ep_min_adv: float = 5e6

    # --- leader / setups ---
    leader_rs: float = 0.95
    leader_min_ret: float = 0.30
    bo_max_candidates: int = 25
    bo_max_extension: float = 1.0  # x ADR20 above pivot
    bo_cutoff: str = "11:00"
    ep_min_gap: float = 0.10
    ep_require_neglect: bool = False
    ep_neglect_max_ret63: float = 0.30
    ep_max_candidates: int = 15
    ep_min_first_bar_vol_frac: float = 0.10
    ep_cutoff: str = "11:00"
    pb_max_candidates: int = 25
    pb_cutoff: str = "15:30"

    # --- stops ---
    min_stop_pct: float = 0.01
    max_stop_pct: float = 0.08
    max_stop_adr_mult: float = 1.0
    ep_max_stop_pct: float = 0.08

    # --- regime ---
    ep_in_risk_off: bool = True
    risk_off_ep_mult: float = 0.5

    # --- sizing / portfolio ---
    initial_equity: float = 100_000.0
    risk_per_trade: float = 0.005
    max_position_pct: float = 0.25
    max_gross: float = 1.0
    max_new_per_day: int = 6
    max_open_positions: int = 12
    slip_bps: float = 10.0

    # --- exits ---
    partial_day: int = 3
    partial_frac: float = 1 / 3
    trail_ma: str = "sma10"  # sma10 | ema10 | sma20 | ema21
    # True: the MA trail applies from the partial_day-th session on (after the partial /
    # breakeven, the Qullamaggie/Luk order); False: from the session after entry (ablation).
    trail_after_partial: bool = True
    max_hold: int = 60
    data_gap_sessions: int = 5
    contract_target_r: float = 2.0
    contract_horizon: int = 10
    contract_atr_floor: float = 0.5

    def with_overrides(self, **kw: Any) -> Config:
        return replace(self, **kw)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        for k, v in d.items():
            if isinstance(v, date):
                d[k] = v.isoformat()
            elif isinstance(v, tuple):
                d[k] = list(v)
        return d


def parse_override(cfg: Config, key: str, raw: str) -> Config:
    """Apply one `key=value` CLI override, coercing to the field's type."""
    ftypes = {f.name: f.type for f in fields(Config)}
    if key not in ftypes:
        raise KeyError(f"unknown config field: {key}")
    cur = getattr(cfg, key)
    if isinstance(cur, bool):
        val: Any = raw.lower() in ("1", "true", "yes")
    elif isinstance(cur, int):
        val = int(raw)
    elif isinstance(cur, float):
        val = float(raw)
    elif isinstance(cur, tuple):
        val = tuple(x for x in raw.split(",") if x)
    elif key in ("start", "end"):
        val = None if raw.lower() == "none" else date.fromisoformat(raw)
    else:
        val = raw
    return replace(cfg, **{key: val})


def data_dir() -> Path:
    """Root for caches/results; never inside the repo."""
    root = os.environ.get("LUK_DATA_DIR")
    p = Path(root) if root else Path.home() / ".tradingagents" / "research" / "luk"
    p.mkdir(parents=True, exist_ok=True)
    return p
