"""Pure A/B aggregate computation over the trade-plan ledger (webui page 4).

Implements the "A/B aggregate definitions" and "Statistical caveats" sections
of specs/trade-plan-ledger-contract.md as pure functions over parsed jsonl
rows (plain dicts). Deliberately pipeline-free (stdlib only): the webui is a
read-only renderer, and these functions never write anything — no aggregate is
ever persisted (webui-pages spec, page 4).

Row semantics implemented here, straight from the contract:

- current outcome per ``run_id`` = the row with the greatest ``settled_at``;
- current order state per ``client_order_id`` = greatest ``written_at``;
- derived execution states by joining ``orders.jsonl`` on ``run_id``:
  **submitted** = a ``submitted`` row with ``dry_run: false``; **filled** =
  submitted plus a *later* ``refresh`` row (same client id) with
  ``broker_status`` ``filled``/``partially_filled``. Aggregates use *filled*,
  not *submitted*, wherever fills matter (the paper P&L column);
- pair selection: per ``(date, session, ticker)`` only the latest **complete**
  pairing attempt (greatest ``a<k>`` with both arms present) is used; every
  other paired row — earlier attempts and never-completed attempts alike — is
  excluded everywhere. Pair counting is therefore deduplicated by
  construction: one pair per selected attempt;
- eval weighting: all metrics reported twice — over all rows, and excluding
  rows whose **consumed** brief eval verdict is ``fail`` (a ``fail`` verdict
  recorded on a row whose ``inputs`` show the brief was not consumed — the
  feeds arm — excludes nothing; ``warn`` rows appear in both variants).
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path

HORIZONS = ("d1", "d5", "d20")
ARMS = ("brief", "feeds")

#: Broker states that make a submitted order count as a fill (contract join rule).
FILL_STATUSES = frozenset({"filled", "partially_filled"})

#: Contract "Statistical caveats" — rendered WITH the numbers, never buried.
CAVEATS = (
    "Conditional comparison: catalyst-triggered pairs enter the sample because "
    "the brief arm's catalyst_score fired, so triggered-pair results are "
    "conditioned on the brief arm's selection — only core-rotation pairs "
    "approximate an unconditional comparison.",
    "Same-ticker rows across days are not independent samples: aggregates are "
    "also shown clustered per ticker and stratified per date.",
    "“≥ 100 pairs” counts deduplicated (date, session, ticker) "
    "pairs — one per latest complete pairing attempt.",
    "Returns are close-to-close on the decision date's series (plan replay uses "
    "OHLC); realized paper P&L is its own column, never mixed into the "
    "close-to-close metrics. Benchmarks come from the existing benchmark_map "
    "(SPY for US, ^HSI for .HK, index per suffix otherwise).",
)


# ---------------------------------------------------------------------------
# jsonl reading (tolerant — R1: pre-first-run state must render, never 500)
# ---------------------------------------------------------------------------


def parse_jsonl(text: str) -> list[dict]:
    """Parse jsonl text into dict rows, silently skipping junk lines."""
    rows: list[dict] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def read_jsonl(path: str | Path) -> list[dict]:
    """Read a ledger jsonl file; a missing/unreadable file is an empty ledger."""
    try:
        return parse_jsonl(Path(path).read_text(encoding="utf-8"))
    except OSError:
        return []


def _instant_key(value: object) -> str:
    """Ordering key for contract UTC instants (ISO-8601 ``...Z`` strings sort
    lexicographically); junk sorts first so a malformed row never wins."""
    return value if isinstance(value, str) else ""


def _number(value: object) -> float | None:
    """A usable metric number — bools are JSON junk here, not numbers."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


def _mean(values: Sequence[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _share(hits: int, total: int) -> float | None:
    return hits / total if total else None


# ---------------------------------------------------------------------------
# Current-state projections (outcomes, orders)
# ---------------------------------------------------------------------------


def latest_outcomes(outcome_rows: Iterable[Mapping]) -> dict[str, dict]:
    """Per ``run_id``, the outcome row with the greatest ``settled_at``
    (contract: the settle step refreshes records; the current one is the
    latest). Ties keep the later physical row — at least as current."""
    best: dict[str, dict] = {}
    for row in outcome_rows:
        run_id = row.get("run_id")
        if not isinstance(run_id, str) or not run_id:
            continue
        current = best.get(run_id)
        if current is None or _instant_key(row.get("settled_at")) >= _instant_key(
            current.get("settled_at")
        ):
            best[run_id] = dict(row)
    return best


def derive_execution_states(order_rows: Iterable[Mapping]) -> dict[str, dict]:
    """Per ``run_id``, the derived execution state from the contract join rule.

    ``state`` ∈ ``filled`` | ``submitted`` | ``dry_run`` | ``skipped`` |
    ``none``: *submitted* = a ``submitted`` row with ``dry_run: false``;
    *filled* = submitted plus a strictly later ``refresh`` row for the same
    ``client_order_id`` with ``broker_status`` in :data:`FILL_STATUSES`.
    ``dry_run``/``skipped`` are the informational fallbacks for runs that
    produced order rows without reaching the broker. The detail fields come
    from the latest refresh row (per-client-id current-state rule).
    """
    by_run: dict[str, list[dict]] = {}
    for row in order_rows:
        run_id = row.get("run_id")
        if isinstance(run_id, str) and run_id:
            by_run.setdefault(run_id, []).append(dict(row))

    states: dict[str, dict] = {}
    for run_id, rows in by_run.items():
        submitted = [
            r for r in rows if r.get("kind") == "submitted" and r.get("dry_run") is False
        ]
        filled = False
        for sub in submitted:
            cid = sub.get("client_order_id")
            sub_at = _instant_key(sub.get("written_at"))
            for r in rows:
                if (
                    r.get("kind") == "refresh"
                    and r.get("client_order_id") == cid
                    and _instant_key(r.get("written_at")) > sub_at
                    and r.get("broker_status") in FILL_STATUSES
                ):
                    filled = True
                    break
        if filled:
            state = "filled"
        elif submitted:
            state = "submitted"
        elif any(r.get("kind") == "submitted" and r.get("dry_run") for r in rows):
            state = "dry_run"
        elif any(r.get("kind") == "skip" for r in rows):
            state = "skipped"
        else:
            state = "none"

        refreshes = sorted(
            (r for r in rows if r.get("kind") == "refresh"),
            key=lambda r: _instant_key(r.get("written_at")),
        )
        latest_refresh = refreshes[-1] if refreshes else {}
        skip_reasons = [
            str(r.get("reason")) for r in rows if r.get("kind") == "skip" and r.get("reason")
        ]
        states[run_id] = {
            "state": state,
            "broker_status": latest_refresh.get("broker_status"),
            "filled_avg_price": latest_refresh.get("filled_avg_price"),
            "filled_qty": latest_refresh.get("filled_qty"),
            "skip_reasons": skip_reasons,
            "dry_run": state == "dry_run",
        }
    return states


# ---------------------------------------------------------------------------
# Pair selection (latest complete attempt only)
# ---------------------------------------------------------------------------


def _pair_attempt(pair_id: object) -> int | None:
    """``<date>-<session>-<ticker>-a<k>`` → ``k``, else None."""
    if not isinstance(pair_id, str):
        return None
    _head, sep, tail = pair_id.rpartition("-a")
    if not sep or not tail.isdigit():
        return None
    return int(tail)


def select_latest_complete_pairs(
    decision_rows: Sequence[Mapping],
) -> tuple[list[dict], list[dict]]:
    """Apply the contract's pair-selection rule.

    Returns ``(pairs, kept_rows)``. ``pairs`` — one entry per ``(date,
    session, ticker)`` that has at least one complete attempt: the greatest
    ``a<k>`` with both arms present, as ``{"key", "attempt", "brief",
    "feeds"}``, sorted by key (deduplicated pair counting = ``len(pairs)``).
    ``kept_rows`` — unpaired rows (``pair_id`` null: manual/single-arm runs)
    plus both rows of every selected pair; rows of earlier attempts and of
    never-completed attempts are excluded everywhere.
    """
    unpaired: list[dict] = []
    grouped: dict[tuple[str, str, str], dict[int, dict[str, dict]]] = {}
    for row in decision_rows:
        attempt = _pair_attempt(row.get("pair_id"))
        if attempt is None:
            unpaired.append(dict(row))
            continue
        key = (str(row.get("date")), str(row.get("session")), str(row.get("ticker")))
        arm = str(row.get("arm"))
        # Later physical row wins on a duplicate arm (contract: a pair_id owns
        # exactly two rows; a malformed ledger degrades to the newest).
        grouped.setdefault(key, {}).setdefault(attempt, {})[arm] = dict(row)

    pairs: list[dict] = []
    kept: list[dict] = list(unpaired)
    for key in sorted(grouped):
        attempts = grouped[key]
        complete = [k for k, arms in attempts.items() if "brief" in arms and "feeds" in arms]
        if not complete:
            continue
        attempt = max(complete)
        arms = attempts[attempt]
        pairs.append(
            {"key": key, "attempt": attempt, "brief": arms["brief"], "feeds": arms["feeds"]}
        )
        kept.extend((arms["brief"], arms["feeds"]))
    return pairs, kept


# ---------------------------------------------------------------------------
# Eval weighting
# ---------------------------------------------------------------------------


def consumed_eval_failed(row: Mapping) -> bool:
    """True when a brief this row actually CONSUMED carries a ``fail`` verdict.

    Consumption is visible in ``inputs`` (null = not consumed — the feeds arm
    or a withheld brief), so a feeds row recording the day's ``fail`` verdicts
    is never excluded: it did not read the failing content.
    """
    inputs = row.get("inputs") if isinstance(row.get("inputs"), Mapping) else {}
    return (
        inputs.get("macro_brief") is not None and row.get("macro_eval_verdict") == "fail"
    ) or (
        inputs.get("ticker_brief") is not None and row.get("ticker_eval_verdict") == "fail"
    )


# ---------------------------------------------------------------------------
# Metrics over a set of decision rows
# ---------------------------------------------------------------------------


def compute_metrics(
    rows: Sequence[Mapping],
    outcomes_by_run: Mapping[str, Mapping],
    exec_states: Mapping[str, Mapping],
) -> dict:
    """The contract's per-arm metric block over one set of decision rows.

    - hit rate @ h: share of BUY rows with ``returns.h > benchmark_returns.h``
      (SELL rows ``<``; HOLD and ERROR excluded); rows lacking either number
      at h drop out of that horizon's denominator (null until computable);
    - excess return @ h: mean of ``returns.h − benchmark_returns.h``, signed
      positive for BUY and negated for SELL;
    - plan quality (BUY rows with ``plan_valid`` and a replay): entry-hit
      rate over all replays; mean ``realized_rr`` over non-ambiguous replays
      (ambiguous = both levels touched in one bar — excluded from R:R);
      target-first share over non-ambiguous entry-hit replays (first-touch
      ordering is undefined for the others);
    - paper P&L: its own column, summed over rows whose derived execution
      state is *filled* (fills matter, not submissions).
    """
    decisions = [str(r.get("decision")) for r in rows]
    hit_rate: dict[str, float | None] = {}
    hit_n: dict[str, int] = {}
    excess: dict[str, float | None] = {}
    for horizon in HORIZONS:
        hits = 0
        excesses: list[float] = []
        for row in rows:
            direction = row.get("decision")
            if direction not in ("BUY", "SELL"):
                continue  # HOLD/ERROR rows never enter return metrics
            outcome = outcomes_by_run.get(str(row.get("run_id"))) or {}
            returns = outcome.get("returns") or {}
            bench = outcome.get("benchmark_returns") or {}
            r, b = _number(returns.get(horizon)), _number(bench.get(horizon))
            if r is None or b is None:
                continue
            if direction == "BUY":
                hits += 1 if r > b else 0
                excesses.append(r - b)
            else:
                hits += 1 if r < b else 0
                excesses.append(b - r)
        hit_n[horizon] = len(excesses)
        hit_rate[horizon] = _share(hits, len(excesses))
        excess[horizon] = _mean(excesses)

    replays: list[Mapping] = []
    for row in rows:
        if row.get("decision") != "BUY" or not row.get("plan_valid"):
            continue
        outcome = outcomes_by_run.get(str(row.get("run_id"))) or {}
        replay = outcome.get("plan_replay")
        if isinstance(replay, Mapping):
            replays.append(replay)
    non_ambiguous = [r for r in replays if not r.get("ambiguous")]
    rr_values = [
        rr for r in non_ambiguous if (rr := _number(r.get("realized_rr"))) is not None
    ]
    resolved = [r for r in non_ambiguous if r.get("entry_hit")]

    paper_pnl: list[float] = []
    for row in rows:
        state = exec_states.get(str(row.get("run_id"))) or {}
        if state.get("state") != "filled":
            continue  # contract: aggregates use *filled*, never *submitted*
        outcome = outcomes_by_run.get(str(row.get("run_id"))) or {}
        paper = outcome.get("paper")
        if isinstance(paper, Mapping) and (pnl := _number(paper.get("realized_pnl"))) is not None:
            paper_pnl.append(pnl)

    return {
        "n_rows": len(rows),
        "n_buy": decisions.count("BUY"),
        "n_sell": decisions.count("SELL"),
        "n_hold": decisions.count("HOLD"),
        "n_error": decisions.count("ERROR"),
        "hit_rate": hit_rate,
        "hit_n": hit_n,
        "excess_return": excess,
        "plan_quality": {
            "n_replays": len(replays),
            "entry_hit_rate": _share(sum(1 for r in replays if r.get("entry_hit")), len(replays)),
            "mean_realized_rr": _mean(rr_values),
            "n_non_ambiguous_rr": len(rr_values),
            "target_first_share": _share(
                sum(1 for r in resolved if r.get("target_hit_first")), len(resolved)
            ),
        },
        "paper": {
            "n_filled_with_pnl": len(paper_pnl),
            "realized_pnl_total": sum(paper_pnl) if paper_pnl else None,
            "realized_pnl_mean": _mean(paper_pnl),
        },
    }


# ---------------------------------------------------------------------------
# The full on-demand aggregate (page 4)
# ---------------------------------------------------------------------------


def _direction_agreement(pairs: Sequence[Mapping]) -> float | None:
    """Share of selected pairs whose two arms produced the same decision."""
    return _share(
        sum(1 for p in pairs if p["brief"].get("decision") == p["feeds"].get("decision")),
        len(pairs),
    )


def compute_ab_aggregates(
    decision_rows: Sequence[Mapping],
    order_rows: Sequence[Mapping],
    outcome_rows: Sequence[Mapping],
    *,
    preset: str | None = None,
    paired_only: bool = False,
) -> dict:
    """The whole page-4 payload, computed on demand and never persisted.

    Every metric block appears twice (``all`` / ``excluding_fail`` — the eval
    weighting), grouped by arm overall plus clustered per ticker and
    stratified per date (the caveats' non-independence views). ``preset``
    filters rows before pair selection; ``paired_only`` restricts the row set
    to the selected pairs' rows (direction agreement is pair-based either
    way).
    """
    presets = sorted({str(r.get("preset")) for r in decision_rows if r.get("preset")})
    if preset:
        decision_rows = [r for r in decision_rows if r.get("preset") == preset]

    outcomes_by_run = latest_outcomes(outcome_rows)
    exec_states = derive_execution_states(order_rows)
    pairs, kept_rows = select_latest_complete_pairs(decision_rows)
    rows = (
        [row for pair in pairs for row in (pair["brief"], pair["feeds"])]
        if paired_only
        else kept_rows
    )
    pairs_excluding_fail = [
        p
        for p in pairs
        if not consumed_eval_failed(p["brief"]) and not consumed_eval_failed(p["feeds"])
    ]

    def both_weightings(subset: Sequence[Mapping]) -> dict:
        return {
            "all": compute_metrics(subset, outcomes_by_run, exec_states),
            "excluding_fail": compute_metrics(
                [r for r in subset if not consumed_eval_failed(r)],
                outcomes_by_run,
                exec_states,
            ),
        }

    def by_arm(subset: Sequence[Mapping]) -> dict:
        return {
            arm: both_weightings([r for r in subset if r.get("arm") == arm]) for arm in ARMS
        }

    def grouped_view(field: str) -> dict:
        groups = sorted({str(r.get(field)) for r in rows})
        return {value: by_arm([r for r in rows if str(r.get(field)) == value]) for value in groups}

    return {
        # Deduplicated pair counting: one per latest complete attempt.
        "pair_count": len(pairs),
        "pair_count_excluding_fail": len(pairs_excluding_fail),
        "direction_agreement": {
            "all": _direction_agreement(pairs),
            "excluding_fail": _direction_agreement(pairs_excluding_fail),
        },
        "arms": by_arm(rows),
        "by_ticker": grouped_view("ticker"),
        "by_date": grouped_view("date"),
        "row_counts": {"ledger": len(decision_rows), "used": len(rows)},
        "presets": presets,
        "preset": preset,
        "paired_only": paired_only,
        "caveats": list(CAVEATS),
    }
