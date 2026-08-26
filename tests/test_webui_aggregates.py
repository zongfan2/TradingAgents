"""webui/aggregates.py: A/B aggregates against hand-computed fixture ledgers (R2).

Every expected number below is derived by hand from THE FIXTURE at the top of
this file; the arithmetic is spelled out in comments next to each assertion so
a failure can be re-checked without re-deriving the contract. Covers the
ledger contract's aggregate definitions: latest-complete-attempt pair
selection (and its dedup pair counting), direction agreement, hit rates and
excess returns at d1/d5/d20 with SELL negation and HOLD exclusion, plan
quality with ambiguous-replay exclusion, eval weighting (consumed-fail rows
out; feeds rows never excluded by verdicts they did not consume), the
paired-only restriction, per-preset filtering, per-ticker clustering /
per-date stratification, latest-outcome selection, and the derived
submitted-vs-filled execution states.
"""

import pytest

from webui import aggregates as ag

# ---------------------------------------------------------------------------
# Fixture ledger
# ---------------------------------------------------------------------------


def _decision(
    run_id,
    *,
    date,
    ticker,
    arm,
    decision,
    pair_id=None,
    session="us",
    preset="p1",
    trigger="core",
    macro_verdict="pass",
    ticker_verdict="pass",
    consumed_macro=True,
    consumed_ticker=True,
    plan_valid=False,
):
    ref = {"path": "x", "generated_at": "2026-08-03T12:00:00Z", "sha256": "aa"}
    return {
        "run_id": run_id,
        "pair_id": pair_id,
        "date": date,
        "session": session,
        "ticker": ticker,
        "arm": arm,
        "preset": preset,
        "trigger": trigger,
        "macro_eval_verdict": macro_verdict,
        "ticker_eval_verdict": ticker_verdict,
        "inputs": {
            "macro_brief": ref if consumed_macro else None,
            "ticker_brief": ref if consumed_ticker else None,
            "pool": ref,
            "config_digest": "d",
        },
        "decided_at": f"{date}T13:00:00Z",
        "decision": decision,
        "plan_valid": plan_valid,
    }


# NVDA pairing attempt a1 is superseded by a2 (both arms present in both);
# TSLA's only attempt never completed (brief arm alone) — excluded everywhere;
# MSFT is an unpaired manual HOLD; r9 is an unpaired 08-04 SELL on preset p2.
DECISIONS = [
    # r1/r2 — superseded attempt a1; r1 carries a poison outcome (+0.50 d1)
    # so any wrong inclusion breaks the means below.
    _decision(
        "2026-08-03-us-NVDA-brief-1", date="2026-08-03", ticker="NVDA", arm="brief",
        decision="BUY", pair_id="2026-08-03-us-NVDA-a1", plan_valid=True,
    ),
    _decision(
        "2026-08-03-us-NVDA-feeds-1", date="2026-08-03", ticker="NVDA", arm="feeds",
        decision="SELL", pair_id="2026-08-03-us-NVDA-a1",
        consumed_macro=False, consumed_ticker=False,
    ),
    # r3 — selected NVDA brief row: BUY, clean evals, valid plan, filled order.
    _decision(
        "2026-08-03-us-NVDA-brief-2", date="2026-08-03", ticker="NVDA", arm="brief",
        decision="BUY", pair_id="2026-08-03-us-NVDA-a2", plan_valid=True,
    ),
    # r4 — selected NVDA feeds row: fail verdicts RECORDED but briefs not
    # consumed (inputs null) ⇒ never excluded by eval weighting.
    _decision(
        "2026-08-03-us-NVDA-feeds-2", date="2026-08-03", ticker="NVDA", arm="feeds",
        decision="BUY", pair_id="2026-08-03-us-NVDA-a2", plan_valid=True,
        macro_verdict="fail", ticker_verdict="fail",
        consumed_macro=False, consumed_ticker=False,
    ),
    # r5 — AMD brief: consumed ticker brief with a fail verdict ⇒ excluded in
    # the excluding_fail weighting (and its pair with it).
    _decision(
        "2026-08-03-us-AMD-brief-1", date="2026-08-03", ticker="AMD", arm="brief",
        decision="BUY", pair_id="2026-08-03-us-AMD-a1", plan_valid=True,
        ticker_verdict="fail",
    ),
    # r6 — AMD feeds: SELL (disagreement with r5's BUY).
    _decision(
        "2026-08-03-us-AMD-feeds-1", date="2026-08-03", ticker="AMD", arm="feeds",
        decision="SELL", pair_id="2026-08-03-us-AMD-a1",
        consumed_macro=False, consumed_ticker=False,
    ),
    # r7 — TSLA: paired row whose attempt never completed ⇒ excluded
    # everywhere (poison outcome +0.99 d1 guards the exclusion).
    _decision(
        "2026-08-03-us-TSLA-brief-1", date="2026-08-03", ticker="TSLA", arm="brief",
        decision="BUY", pair_id="2026-08-03-us-TSLA-a1", plan_valid=True,
    ),
    # r8 — unpaired manual HOLD: in row counts, out of every return metric.
    _decision(
        "2026-08-03-us-MSFT-brief-1", date="2026-08-03", ticker="MSFT", arm="brief",
        decision="HOLD", trigger="manual",
    ),
    # r9 — unpaired 08-04 SELL, preset p2 (preset filter + date stratum).
    _decision(
        "2026-08-04-us-NVDA-feeds-1", date="2026-08-04", ticker="NVDA", arm="feeds",
        decision="SELL", preset="p2",
        consumed_macro=False, consumed_ticker=False,
    ),
]


def _outcome(run_id, settled_at, returns, bench, replay=None, paper=None):
    return {
        "run_id": run_id,
        "settled_at": settled_at,
        "returns": returns,
        "benchmark_returns": bench,
        "plan_replay": replay,
        "paper": paper,
    }


OUTCOMES = [
    # r3: an older settled row (d1 −0.05 → would be a miss) is superseded by
    # the 08-10 row — latest-settled_at selection is what makes d1 a hit.
    _outcome(
        "2026-08-03-us-NVDA-brief-2", "2026-08-04T12:00:00Z",
        {"d1": -0.05, "d5": None, "d20": None}, {"d1": 0.0, "d5": None, "d20": None},
    ),
    _outcome(
        "2026-08-03-us-NVDA-brief-2", "2026-08-10T12:00:00Z",
        {"d1": 0.02, "d5": 0.05, "d20": None}, {"d1": 0.01, "d5": 0.01, "d20": None},
        replay={
            "entry_hit": True, "stop_hit_first": False, "target_hit_first": True,
            "ambiguous": False, "realized_rr": 1.5,
        },
        paper={"realized_pnl": 100.0, "closed": True},
    ),
    # r4: BUY miss; ambiguous replay (both levels in one bar) — excluded from
    # R:R and target-first, still counted for entry-hit.
    _outcome(
        "2026-08-03-us-NVDA-feeds-2", "2026-08-10T12:00:00Z",
        {"d1": 0.00, "d5": None, "d20": None}, {"d1": 0.01, "d5": None, "d20": None},
        replay={
            "entry_hit": True, "stop_hit_first": True, "target_hit_first": True,
            "ambiguous": True, "realized_rr": None,
        },
    ),
    # r5: BUY hit; replay never entered.
    _outcome(
        "2026-08-03-us-AMD-brief-1", "2026-08-10T12:00:00Z",
        {"d1": 0.03, "d5": None, "d20": None}, {"d1": 0.01, "d5": None, "d20": None},
        replay={
            "entry_hit": False, "stop_hit_first": False, "target_hit_first": False,
            "ambiguous": False, "realized_rr": None,
        },
    ),
    # r6: SELL hit (−0.02 < 0.01); excess = b − r = 0.03.
    _outcome(
        "2026-08-03-us-AMD-feeds-1", "2026-08-10T12:00:00Z",
        {"d1": -0.02, "d5": None, "d20": None}, {"d1": 0.01, "d5": None, "d20": None},
    ),
    # Poison outcomes for rows that MUST be excluded by pair selection.
    _outcome(
        "2026-08-03-us-NVDA-brief-1", "2026-08-10T12:00:00Z",
        {"d1": 0.50, "d5": 0.50, "d20": 0.50}, {"d1": 0.0, "d5": 0.0, "d20": 0.0},
    ),
    _outcome(
        "2026-08-03-us-TSLA-brief-1", "2026-08-10T12:00:00Z",
        {"d1": 0.99, "d5": None, "d20": None}, {"d1": 0.0, "d5": None, "d20": None},
    ),
    # r8 HOLD: excluded from hit/excess despite having returns.
    _outcome(
        "2026-08-03-us-MSFT-brief-1", "2026-08-10T12:00:00Z",
        {"d1": 0.10, "d5": None, "d20": None}, {"d1": 0.0, "d5": None, "d20": None},
    ),
    # r9: SELL miss (0.02 > 0.01); excess = 0.01 − 0.02 = −0.01.
    _outcome(
        "2026-08-04-us-NVDA-feeds-1", "2026-08-10T12:00:00Z",
        {"d1": 0.02, "d5": None, "d20": None}, {"d1": 0.01, "d5": None, "d20": None},
    ),
]


def _order(run_id, kind, written_at, *, cid, dry_run=False, status=None, reason=None, **extra):
    return {
        "run_id": run_id,
        "written_at": written_at,
        "session": "us",
        "ticker": run_id.split("-")[4],
        "kind": kind,
        "dry_run": dry_run,
        "reason": reason,
        "client_order_id": cid,
        "broker_status": status,
        **extra,
    }


ORDERS = [
    # r3: live submit + later fill refresh ⇒ derived state "filled".
    _order("2026-08-03-us-NVDA-brief-2", "intent", "2026-08-03T13:04:00Z",
           cid="2026-08-03-us-NVDA-entry"),
    _order("2026-08-03-us-NVDA-brief-2", "submitted", "2026-08-03T13:05:00Z",
           cid="2026-08-03-us-NVDA-entry"),
    _order("2026-08-03-us-NVDA-brief-2", "refresh", "2026-08-03T14:00:00Z",
           cid="2026-08-03-us-NVDA-entry", status="filled",
           filled_avg_price=283.6, filled_qty=12),
    # r5: live submit; the ONLY "filled" refresh is EARLIER than the submit
    # (must not count — the join rule requires a LATER refresh row) and the
    # later refresh is merely accepted ⇒ derived state stays "submitted".
    _order("2026-08-03-us-AMD-brief-1", "submitted", "2026-08-03T13:06:00Z",
           cid="2026-08-03-us-AMD-entry"),
    _order("2026-08-03-us-AMD-brief-1", "refresh", "2026-08-03T13:00:00Z",
           cid="2026-08-03-us-AMD-entry", status="filled"),
    _order("2026-08-03-us-AMD-brief-1", "refresh", "2026-08-03T14:00:00Z",
           cid="2026-08-03-us-AMD-entry", status="accepted"),
    # r4: dry-run submission (never reached the broker).
    _order("2026-08-03-us-NVDA-feeds-2", "submitted", "2026-08-03T13:05:30Z",
           cid="2026-08-03-us-NVDA-entry", dry_run=True),
    # r6: skipped with a reason.
    _order("2026-08-03-us-AMD-feeds-1", "skip", "2026-08-03T13:05:40Z",
           cid="2026-08-03-us-AMD-entry", dry_run=True, reason="EXECUTION_HALT active"),
]


def _agg(**kwargs):
    return ag.compute_ab_aggregates(DECISIONS, ORDERS, OUTCOMES, **kwargs)


# ---------------------------------------------------------------------------
# Building blocks
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_latest_outcome_selection_uses_greatest_settled_at():
    latest = ag.latest_outcomes(OUTCOMES)
    # r3 has two rows; the 08-10 one (d1 = +0.02) supersedes the 08-04 one.
    assert latest["2026-08-03-us-NVDA-brief-2"]["returns"]["d1"] == 0.02


@pytest.mark.unit
def test_derived_execution_states_submitted_vs_filled():
    states = ag.derive_execution_states(ORDERS)
    # r3: submitted 13:05 + refresh 14:00 broker_status=filled ⇒ filled.
    assert states["2026-08-03-us-NVDA-brief-2"]["state"] == "filled"
    assert states["2026-08-03-us-NVDA-brief-2"]["filled_avg_price"] == 283.6
    # r5: the filled refresh predates the submit; the later one is only
    # accepted ⇒ submitted, NOT filled (contract: "plus a later refresh row").
    assert states["2026-08-03-us-AMD-brief-1"]["state"] == "submitted"
    # r4: dry-run submit never reached the broker.
    assert states["2026-08-03-us-NVDA-feeds-2"]["state"] == "dry_run"
    # r6: skip row with its reason surfaced.
    assert states["2026-08-03-us-AMD-feeds-1"]["state"] == "skipped"
    assert states["2026-08-03-us-AMD-feeds-1"]["skip_reasons"] == ["EXECUTION_HALT active"]
    # r8 wrote no order rows at all.
    assert "2026-08-03-us-MSFT-brief-1" not in states


@pytest.mark.unit
def test_pair_selection_latest_complete_attempt_and_dedup():
    pairs, kept = ag.select_latest_complete_pairs(DECISIONS)
    # NVDA has attempts a1 and a2 (both complete) ⇒ a2 selected; AMD has a1;
    # TSLA's attempt never completed ⇒ no pair. Dedup count: 2 pairs, not 3
    # attempts and not 4 paired-row couples.
    assert [(p["key"][2], p["attempt"]) for p in pairs] == [("AMD", 1), ("NVDA", 2)]
    kept_ids = {row["run_id"] for row in kept}
    # kept = both rows of each selected pair + the unpaired rows (r8, r9);
    # a1 rows (r1, r2) and the incomplete TSLA row (r7) are excluded everywhere.
    assert kept_ids == {
        "2026-08-03-us-NVDA-brief-2", "2026-08-03-us-NVDA-feeds-2",
        "2026-08-03-us-AMD-brief-1", "2026-08-03-us-AMD-feeds-1",
        "2026-08-03-us-MSFT-brief-1", "2026-08-04-us-NVDA-feeds-1",
    }


@pytest.mark.unit
def test_consumed_eval_failed_requires_consumption():
    # r5 consumed a ticker brief whose verdict is fail ⇒ excluded.
    assert ag.consumed_eval_failed(DECISIONS[4]) is True
    # r4 records fail verdicts but consumed neither brief (inputs null) ⇒ kept.
    assert ag.consumed_eval_failed(DECISIONS[3]) is False


# ---------------------------------------------------------------------------
# Full aggregate — all rows, no filters
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_pair_count_and_direction_agreement():
    result = _agg()
    assert result["pair_count"] == 2
    # NVDA a2: BUY vs BUY agree; AMD a1: BUY vs SELL disagree ⇒ 1/2.
    assert result["direction_agreement"]["all"] == pytest.approx(0.5)
    # excluding_fail drops the AMD pair (r5 consumed a fail brief) ⇒ NVDA
    # alone agrees ⇒ 1/1.
    assert result["pair_count_excluding_fail"] == 1
    assert result["direction_agreement"]["excluding_fail"] == pytest.approx(1.0)


@pytest.mark.unit
def test_brief_arm_hit_rates_and_excess_returns():
    m = _agg()["arms"]["brief"]["all"]
    # brief rows used: r3 (BUY), r5 (BUY), r8 (HOLD — excluded from returns).
    assert (m["n_rows"], m["n_buy"], m["n_hold"]) == (3, 2, 1)
    # d1: r3 0.02>0.01 hit, r5 0.03>0.01 hit ⇒ 2/2; excess mean =
    # ((0.02−0.01) + (0.03−0.01)) / 2 = (0.01 + 0.02)/2 = 0.015.
    assert m["hit_n"]["d1"] == 2
    assert m["hit_rate"]["d1"] == pytest.approx(1.0)
    assert m["excess_return"]["d1"] == pytest.approx(0.015)
    # d5: only r3 has both numbers (0.05 vs 0.01) ⇒ 1/1 hit, excess 0.04.
    assert m["hit_n"]["d5"] == 1
    assert m["excess_return"]["d5"] == pytest.approx(0.04)
    # d20 never computable ⇒ empty denominator, None metrics.
    assert m["hit_n"]["d20"] == 0
    assert m["hit_rate"]["d20"] is None and m["excess_return"]["d20"] is None


@pytest.mark.unit
def test_feeds_arm_sell_negation():
    m = _agg()["arms"]["feeds"]["all"]
    # feeds rows: r4 (BUY), r6 (SELL), r9 (SELL).
    # d1 hits: r4 0.00>0.01? no. r6 SELL −0.02<0.01 hit. r9 SELL 0.02<0.01? no.
    # ⇒ 1/3. excess (BUY r−b, SELL b−r): (0.00−0.01) + (0.01−(−0.02)) +
    # (0.01−0.02) = −0.01 + 0.03 − 0.01 = 0.01; mean = 0.01/3.
    assert m["hit_n"]["d1"] == 3
    assert m["hit_rate"]["d1"] == pytest.approx(1 / 3)
    assert m["excess_return"]["d1"] == pytest.approx(0.01 / 3)


@pytest.mark.unit
def test_plan_quality_with_ambiguity_exclusion():
    arms = _agg()["arms"]
    brief = arms["brief"]["all"]["plan_quality"]
    # brief replays: r3 (entry hit, rr 1.5, target first) and r5 (no entry).
    # entry-hit rate 1/2; rr over non-ambiguous with a number = [1.5] ⇒ 1.5;
    # target-first over non-ambiguous entry-hit replays = r3 alone ⇒ 1/1.
    assert brief["n_replays"] == 2
    assert brief["entry_hit_rate"] == pytest.approx(0.5)
    assert brief["mean_realized_rr"] == pytest.approx(1.5)
    assert brief["n_non_ambiguous_rr"] == 1
    assert brief["target_first_share"] == pytest.approx(1.0)
    feeds = arms["feeds"]["all"]["plan_quality"]
    # feeds' only replay (r4) is ambiguous: entry-hit still counts (1/1) but
    # it is excluded from R:R and target-first ⇒ both None.
    assert feeds["n_replays"] == 1
    assert feeds["entry_hit_rate"] == pytest.approx(1.0)
    assert feeds["mean_realized_rr"] is None
    assert feeds["target_first_share"] is None


@pytest.mark.unit
def test_paper_pnl_uses_filled_not_submitted():
    arms = _agg()["arms"]
    # r3 is filled with paper pnl 100.0; r5 is merely submitted so its run
    # contributes nothing even though it has an outcome row.
    assert arms["brief"]["all"]["paper"]["realized_pnl_total"] == pytest.approx(100.0)
    assert arms["brief"]["all"]["paper"]["n_filled_with_pnl"] == 1
    assert arms["feeds"]["all"]["paper"]["realized_pnl_total"] is None


# ---------------------------------------------------------------------------
# Eval weighting, paired-only, preset filter
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_eval_weighted_variant_excludes_consumed_fail_rows_only():
    arms = _agg()["arms"]
    brief_ex = arms["brief"]["excluding_fail"]
    # r5 drops (consumed ticker eval fail); r3 + r8 remain.
    # d1: r3 alone ⇒ hit 1/1, excess 0.02−0.01 = 0.01.
    assert brief_ex["n_rows"] == 2
    assert brief_ex["hit_n"]["d1"] == 1
    assert brief_ex["excess_return"]["d1"] == pytest.approx(0.01)
    # feeds rows recorded fail verdicts without consuming briefs ⇒ identical
    # in both weightings ("warn"/unconsumed shown both ways).
    assert arms["feeds"]["excluding_fail"]["n_rows"] == arms["feeds"]["all"]["n_rows"] == 3


@pytest.mark.unit
def test_paired_only_restricts_to_selected_pairs():
    result = _agg(paired_only=True)
    # Only r3/r4 (NVDA a2) and r5/r6 (AMD a1) remain: the unpaired HOLD (r8)
    # and the unpaired 08-04 SELL (r9) drop out.
    assert result["row_counts"]["used"] == 4
    brief = result["arms"]["brief"]["all"]
    assert (brief["n_rows"], brief["n_hold"]) == (2, 0)
    feeds = result["arms"]["feeds"]["all"]
    # feeds d1 now r4 miss + r6 hit ⇒ 1/2; excess mean = (−0.01 + 0.03)/2 = 0.01.
    assert feeds["hit_rate"]["d1"] == pytest.approx(0.5)
    assert feeds["excess_return"]["d1"] == pytest.approx(0.01)
    # Pair-based numbers are unchanged by the toggle.
    assert result["pair_count"] == 2
    assert result["direction_agreement"]["all"] == pytest.approx(0.5)


@pytest.mark.unit
def test_preset_filter():
    result = _agg(preset="p1")
    # r9 (preset p2) drops; feeds d1 = r4 miss + r6 hit ⇒ 1/2, excess 0.01.
    feeds = result["arms"]["feeds"]["all"]
    assert feeds["hit_n"]["d1"] == 2
    assert feeds["hit_rate"]["d1"] == pytest.approx(0.5)
    assert feeds["excess_return"]["d1"] == pytest.approx(0.01)
    # The preset roster is computed over the unfiltered ledger.
    assert result["presets"] == ["p1", "p2"]


# ---------------------------------------------------------------------------
# Clustered / stratified views + caveats
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_per_ticker_clustering():
    by_ticker = _agg()["by_ticker"]
    assert set(by_ticker) == {"NVDA", "AMD", "MSFT"}  # TSLA excluded entirely
    # NVDA cluster: brief r3 (hit, excess 0.01); feeds r4 + r9 (both misses,
    # excess mean = (−0.01 + −0.01)/2 = −0.01).
    nvda_brief = by_ticker["NVDA"]["brief"]["all"]
    assert nvda_brief["hit_rate"]["d1"] == pytest.approx(1.0)
    assert nvda_brief["excess_return"]["d1"] == pytest.approx(0.01)
    nvda_feeds = by_ticker["NVDA"]["feeds"]["all"]
    assert nvda_feeds["hit_rate"]["d1"] == pytest.approx(0.0)
    assert nvda_feeds["excess_return"]["d1"] == pytest.approx(-0.01)
    # AMD cluster: feeds r6 alone ⇒ SELL excess b − r = 0.03.
    assert by_ticker["AMD"]["feeds"]["all"]["excess_return"]["d1"] == pytest.approx(0.03)


@pytest.mark.unit
def test_per_date_stratification():
    by_date = _agg()["by_date"]
    assert set(by_date) == {"2026-08-03", "2026-08-04"}
    # 08-04 stratum holds only r9: feeds SELL miss, excess −0.01; brief empty.
    stratum = by_date["2026-08-04"]
    assert stratum["feeds"]["all"]["hit_rate"]["d1"] == pytest.approx(0.0)
    assert stratum["feeds"]["all"]["excess_return"]["d1"] == pytest.approx(-0.01)
    assert stratum["brief"]["all"]["n_rows"] == 0
    # 08-03 stratum: feeds r4 + r6 ⇒ hit 1/2, excess (−0.01 + 0.03)/2 = 0.01.
    assert by_date["2026-08-03"]["feeds"]["all"]["excess_return"]["d1"] == pytest.approx(0.01)


@pytest.mark.unit
def test_caveats_ship_with_the_numbers():
    result = _agg()
    assert result["caveats"] == list(ag.CAVEATS)
    joined = " ".join(result["caveats"])
    # The contract's four caveats: conditionality, non-independence, dedup
    # pair counting, and the paper-P&L / benchmark provenance note.
    for fragment in ("Conditional comparison", "not independent", "deduplicated", "benchmark_map"):
        assert fragment in joined


@pytest.mark.unit
def test_empty_ledgers_produce_empty_but_valid_aggregate():
    result = ag.compute_ab_aggregates([], [], [])
    assert result["pair_count"] == 0
    assert result["direction_agreement"]["all"] is None
    assert result["arms"]["brief"]["all"]["n_rows"] == 0
    assert result["arms"]["brief"]["all"]["hit_rate"]["d1"] is None


@pytest.mark.unit
def test_read_jsonl_tolerates_missing_and_junk(tmp_path):
    assert ag.read_jsonl(tmp_path / "absent.jsonl") == []
    path = tmp_path / "decisions.jsonl"
    path.write_text('{"run_id": "ok"}\nnot json\n[1,2]\n\n', encoding="utf-8")
    assert ag.read_jsonl(path) == [{"run_id": "ok"}]
