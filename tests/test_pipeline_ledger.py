"""TradePlan validator + ledger record models (trade-plan-ledger-contract v1)."""

import math

import pytest
from pydantic import ValidationError

from pipeline.contracts import ledger

LAST_CLOSE = 283.0
ATR14 = 8.0

NAN = float("nan")
INF = float("inf")


def make_plan(**overrides):
    data = {
        "action": "BUY",
        "conviction": 0.7,
        "add_intent": False,
        "add_rationale": None,
        "entry_zone": [280.0, 285.0],
        "stop": 268.0,
        "targets": [301.0, 315.0],
        "horizon_days": 10,
        "invalidation": "daily close below the weekly mid-band",
        "sizing": {"risk_pct": 1.0},
        "source_levels": "entry = daily BOLL mid, stop = below daily lower band",
    }
    data.update(overrides)
    return ledger.TradePlan.model_validate(data)


@pytest.mark.unit
def test_baseline_buy_plan_is_valid():
    assert ledger.validate_trade_plan(make_plan(), LAST_CLOSE, ATR14) == []


@pytest.mark.unit
@pytest.mark.parametrize(
    ("overrides", "last_close", "atr14", "expected"),
    [
        # BUY ordering: stop < entry_zone[0] <= entry_zone[1] < min(targets)
        ({"stop": 281.0}, LAST_CLOSE, ATR14, "not below entry_zone[0]"),
        ({"entry_zone": [285.0, 280.0]}, LAST_CLOSE, ATR14, "not ordered"),
        ({"targets": [284.0]}, LAST_CLOSE, ATR14, "not below min(targets)"),
        # entry mid ±10% of last close
        ({}, 200.0, ATR14, "±10% of last close"),
        # stop distance in [0.5×ATR14, 15% of entry mid]
        ({}, LAST_CLOSE, 40.0, "below 0.5×ATR14"),
        ({"stop": 210.0}, LAST_CLOSE, ATR14, "above 15% of entry mid"),
        # sizing / horizon ranges
        ({"sizing": {"risk_pct": 2.5}}, LAST_CLOSE, ATR14, "outside [0.1, 2.0]"),
        ({"sizing": {"risk_pct": 0.05}}, LAST_CLOSE, ATR14, "outside [0.1, 2.0]"),
        ({"horizon_days": 0}, LAST_CLOSE, ATR14, "outside [1, 30]"),
        ({"horizon_days": 45}, LAST_CLOSE, ATR14, "outside [1, 30]"),
        # numeric hygiene
        ({"conviction": 1.5}, LAST_CLOSE, ATR14, "[0, 1]"),
        ({"conviction": NAN}, LAST_CLOSE, ATR14, "[0, 1]"),
        ({"entry_zone": [NAN, 285.0]}, LAST_CLOSE, ATR14, "non-finite"),
        ({"targets": [301.0, INF]}, LAST_CLOSE, ATR14, "non-finite"),
        ({"stop": -5.0}, LAST_CLOSE, ATR14, "finite positive"),
        ({"targets": [315.0, 301.0]}, LAST_CLOSE, ATR14, "strictly ascending"),
        ({"targets": [301.0, 301.0]}, LAST_CLOSE, ATR14, "strictly ascending"),
        ({"targets": []}, LAST_CLOSE, ATR14, "1-2 entries"),
        ({"targets": [301.0, 310.0, 320.0]}, LAST_CLOSE, ATR14, "1-2 entries"),
        ({"entry_zone": [280.0]}, LAST_CLOSE, ATR14, "exactly 2 prices"),
        # add-on semantics
        ({"add_intent": True}, LAST_CLOSE, ATR14, "add_rationale"),
        ({"add_intent": True, "add_rationale": "   "}, LAST_CLOSE, ATR14, "add_rationale"),
        # required BUY fields
        ({"stop": None}, LAST_CLOSE, ATR14, "missing required field 'stop'"),
        ({"targets": None}, LAST_CLOSE, ATR14, "missing required field 'targets'"),
        ({"sizing": None}, LAST_CLOSE, ATR14, "missing required field 'sizing'"),
        ({"horizon_days": None}, LAST_CLOSE, ATR14, "missing required field 'horizon_days'"),
        ({"entry_zone": None}, LAST_CLOSE, ATR14, "missing required field 'entry_zone'"),
        # unusable reference data is a violation, not a crash
        ({}, NAN, ATR14, "reference data unusable"),
        ({}, LAST_CLOSE, 0.0, "reference data unusable"),
    ],
)
def test_validate_trade_plan_violation_matrix(overrides, last_close, atr14, expected):
    violations = ledger.validate_trade_plan(make_plan(**overrides), last_close, atr14)
    assert any(expected in violation for violation in violations), violations


@pytest.mark.unit
def test_add_intent_with_rationale_passes():
    plan = make_plan(add_intent=True, add_rationale="guidance raised — new information")
    assert ledger.validate_trade_plan(plan, LAST_CLOSE, ATR14) == []


@pytest.mark.unit
def test_multiple_violations_all_reported():
    plan = make_plan(stop=281.0, horizon_days=45, add_intent=True)
    violations = ledger.validate_trade_plan(plan, 200.0, ATR14)
    text = "\n".join(violations)
    assert "not below entry_zone[0]" in text
    assert "outside [1, 30]" in text
    assert "add_rationale" in text
    assert "±10%" in text


@pytest.mark.unit
def test_sell_is_schema_and_hygiene_only():
    sell = ledger.TradePlan.model_validate({"action": "SELL", "conviction": 0.6})
    assert ledger.validate_trade_plan(sell, LAST_CLOSE, ATR14) == []
    # BUY-only range rules do not apply to SELL...
    sell_loose = ledger.TradePlan.model_validate(
        {"action": "SELL", "conviction": 0.6, "sizing": {"risk_pct": 5.0}, "horizon_days": 90}
    )
    assert ledger.validate_trade_plan(sell_loose, LAST_CLOSE, ATR14) == []
    # ...but numeric hygiene does.
    sell_bad = ledger.TradePlan.model_validate(
        {"action": "SELL", "conviction": 0.6, "entry_zone": [290.0, 280.0]}
    )
    violations = ledger.validate_trade_plan(sell_bad, LAST_CLOSE, ATR14)
    assert any("not ordered" in violation for violation in violations)


@pytest.mark.unit
def test_hold_is_trivially_valid_but_hygiene_still_applies():
    hold = ledger.TradePlan.model_validate({"action": "HOLD", "conviction": 0.5})
    assert ledger.validate_trade_plan(hold, LAST_CLOSE, ATR14) == []
    hold_nan = ledger.TradePlan.model_validate({"action": "HOLD", "conviction": NAN})
    assert ledger.validate_trade_plan(hold_nan, LAST_CLOSE, ATR14) != []


@pytest.mark.unit
def test_trade_plan_rejects_unknown_fields():
    with pytest.raises(ValidationError):
        make_plan(leverage=3)


# ---------------------------------------------------------------------------
# run_id / pair_id formats
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    "run_id",
    [
        "2026-08-03-us-NVDA-brief-1",
        "2026-08-03-cn-0700.HK-feeds-12",
        "2026-08-03-us-BRK.B-brief-2",
    ],
)
def test_run_id_regex_accepts(run_id):
    assert ledger.RUN_ID_RE.match(run_id)


@pytest.mark.unit
@pytest.mark.parametrize(
    "run_id",
    [
        "2026-08-03-eu-NVDA-brief-1",  # unknown session
        "2026-08-03-us-NVDA-brief-0",  # n is 1-based
        "2026-08-03-us-NVDA-1",  # missing arm
        "2026-8-3-us-NVDA-brief-1",  # unpadded date
        "2026-08-03-us-nvda-brief-1",  # lowercase ticker
        "2026-08-03-us-NVDA-brief-",
    ],
)
def test_run_id_regex_rejects(run_id):
    assert not ledger.RUN_ID_RE.match(run_id)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("pair_id", "ok"),
    [
        ("2026-08-03-us-NVDA-a1", True),
        ("2026-08-03-cn-600519.SS-a3", True),
        ("2026-08-03-us-NVDA-a0", False),
        ("2026-08-03-us-NVDA-b1", False),
        ("2026-08-03-us-NVDA-a", False),
    ],
)
def test_pair_id_regex(pair_id, ok):
    assert bool(ledger.PAIR_ID_RE.match(pair_id)) is ok


# ---------------------------------------------------------------------------
# jsonl record models
# ---------------------------------------------------------------------------


def decision_dict(**overrides):
    data = {
        "run_id": "2026-08-03-us-NVDA-brief-1",
        "pair_id": "2026-08-03-us-NVDA-a1",
        "date": "2026-08-03",
        "session": "us",
        "ticker": "NVDA",
        "arm": "brief",
        "preset": "claude-sub",
        "trigger": "core",
        "catalyst_score": 7.5,
        "macro_eval_verdict": "pass",
        "ticker_eval_verdict": "pass",
        "inputs": {
            "macro_brief": {
                "path": "2026-08-03.us.md",
                "generated_at": "2026-08-03T12:35:00Z",
                "sha256": "a" * 64,
            },
            "ticker_brief": {
                "path": "NVDA/2026-08-03.md",
                "generated_at": "2026-08-03T12:41:00Z",
                "sha256": "b" * 64,
            },
            "pool": {
                "path": "us/2026-08-03.json",
                "generated_at": "2026-08-03T12:31:00Z",
                "sha256": "c" * 64,
            },
            "config_digest": "d" * 64,
        },
        "decided_at": "2026-08-03T13:02:11Z",
        "decision": "BUY",
        "plan": {
            "action": "BUY",
            "conviction": 0.7,
            "entry_zone": [280.0, 285.0],
            "stop": 268.0,
            "targets": [301.0, 315.0],
            "horizon_days": 10,
            "invalidation": "daily close below the weekly mid-band",
            "sizing": {"risk_pct": 1.0},
            "source_levels": "entry = daily BOLL mid",
        },
        "plan_valid": True,
    }
    data.update(overrides)
    return data


@pytest.mark.unit
def test_decision_record_contract_example_parses():
    record = ledger.DecisionRecord.model_validate(decision_dict())
    assert record.plan.action == "BUY"
    assert record.inputs.pool.path == "us/2026-08-03.json"


@pytest.mark.unit
def test_decision_record_feeds_arm_null_inputs():
    record = ledger.DecisionRecord.model_validate(
        decision_dict(
            arm="feeds",
            run_id="2026-08-03-us-NVDA-feeds-1",
            inputs={"macro_brief": None, "ticker_brief": None, "pool": None, "config_digest": "x"},
            pair_id=None,
            plan=None,
            plan_valid=False,
            decision="ERROR",
        )
    )
    assert record.inputs.macro_brief is None
    assert record.pair_id is None


@pytest.mark.unit
def test_decision_record_rejects_bad_ids():
    with pytest.raises(ValidationError, match="run_id"):
        ledger.DecisionRecord.model_validate(decision_dict(run_id="not-a-run-id"))
    with pytest.raises(ValidationError, match="pair_id"):
        ledger.DecisionRecord.model_validate(decision_dict(pair_id="2026-08-03-us-NVDA-1"))


@pytest.mark.unit
def test_decision_record_lenient_parse_warns_on_unknown_field(caplog):
    data = decision_dict(experimental_note="from a future writer")
    with pytest.raises(ValidationError):
        ledger.DecisionRecord.model_validate(data)
    with caplog.at_level("WARNING", logger="pipeline.contracts"):
        record = ledger.DecisionRecord.parse_lenient(data)
    assert record.run_id == "2026-08-03-us-NVDA-brief-1"
    assert "experimental_note" in caplog.text


def order_dict(**overrides):
    data = {
        "run_id": "2026-08-03-us-NVDA-brief-1",
        "written_at": "2026-08-03T13:05:00Z",
        "session": "us",
        "ticker": "NVDA",
        "kind": "submitted",
        "dry_run": False,
        "reason": None,
        "client_order_id": "2026-08-03-us-NVDA-entry",
        "order_kind": "bracket",
        "tif": "gtc",
        "qty": 12,
        "limit_price": 285.0,
        "stop_price": 268.0,
        "target_price": 301.0,
        "broker_status": "filled",
        "filled_avg_price": 283.6,
        "filled_qty": 12,
    }
    data.update(overrides)
    return data


@pytest.mark.unit
def test_order_record_contract_example_parses():
    record = ledger.OrderRecord.model_validate(order_dict())
    assert record.kind == "submitted"
    assert record.order_kind == "bracket"


@pytest.mark.unit
def test_order_record_skip_requires_reason():
    with pytest.raises(ValidationError, match="reason"):
        ledger.OrderRecord.model_validate(order_dict(kind="skip", order_kind=None))
    record = ledger.OrderRecord.model_validate(
        order_dict(kind="skip", order_kind=None, reason="cap hit")
    )
    assert record.reason == "cap hit"


@pytest.mark.unit
def test_outcome_record_contract_example_parses():
    record = ledger.OutcomeRecord.model_validate(
        {
            "run_id": "2026-08-03-us-NVDA-brief-1",
            "settled_at": "2026-08-10T12:05:00Z",
            "returns": {"d1": 0.004, "d5": 0.021, "d20": None},
            "benchmark_returns": {"d1": 0.001, "d5": 0.008, "d20": None},
            "plan_replay": {
                "entry_hit": True,
                "stop_hit_first": False,
                "target_hit_first": True,
                "ambiguous": False,
                "realized_rr": 0.94,
            },
            "paper": {"realized_pnl": 214.7, "closed": True},
        }
    )
    assert record.returns.d20 is None
    assert record.plan_replay.realized_rr == pytest.approx(0.94)
    assert math.isfinite(record.paper.realized_pnl)


@pytest.mark.unit
def test_outcome_record_sell_hold_replay_is_null():
    record = ledger.OutcomeRecord.model_validate(
        {
            "run_id": "2026-08-03-us-NVDA-feeds-1",
            "settled_at": "2026-08-10T12:05:00Z",
            "returns": {"d1": 0.004},
            "benchmark_returns": {"d1": 0.001},
            "plan_replay": None,
            "paper": None,
        }
    )
    assert record.plan_replay is None and record.paper is None


@pytest.mark.unit
def test_positions_snapshot_contract_example_parses():
    snapshot = ledger.PositionsSnapshot.model_validate(
        {
            "as_of": "2026-08-04T12:20:00Z",
            "equity": 99985.65,
            "positions": [
                {
                    "ticker": "NVDA",
                    "qty": 12,
                    "avg_entry": 283.6,
                    "last_fill_at": "2026-08-03T14:32:00Z",
                    "market_value": 3520.0,
                    "unrealized_pl": 114.7,
                    "tranches": 1,
                    "open_orders": [
                        {
                            "client_order_id": "2026-08-03-us-NVDA-entry",
                            "leg": "stop",
                            "price": 268.0,
                        }
                    ],
                }
            ],
        }
    )
    assert snapshot.positions[0].open_orders[0].leg == "stop"


# ---------------------------------------------------------------------------
# Id calendar-date validity, degenerate tickers, field consistency
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_id_helpers_require_a_real_calendar_date():
    assert ledger.is_valid_run_id("2026-08-03-us-NVDA-brief-1")
    assert not ledger.is_valid_run_id("2026-99-99-us-NVDA-brief-1")
    assert ledger.is_valid_pair_id("2026-08-03-us-NVDA-a1")
    assert not ledger.is_valid_pair_id("2026-02-30-us-NVDA-a1")


@pytest.mark.unit
@pytest.mark.parametrize("ticker_part", ["-", ".", "--", ".-", "-NVDA", "NVDA-", "BRK..B"])
def test_run_id_regex_rejects_degenerate_tickers(ticker_part):
    assert not ledger.RUN_ID_RE.match(f"2026-08-03-us-{ticker_part}-brief-1")


@pytest.mark.unit
def test_run_id_regex_accepts_hyphenated_ticker():
    assert ledger.RUN_ID_RE.match("2026-08-03-us-BRK-B-brief-1")
    assert ledger.PAIR_ID_RE.match("2026-08-03-us-BRK-B-a1")


@pytest.mark.unit
def test_decision_record_ids_must_match_fields():
    # run_id/pair_id are derived from the row's own fields; an inconsistent
    # row would silently corrupt A/B aggregation joins.
    with pytest.raises(ValidationError, match="does not match this row"):
        ledger.DecisionRecord.model_validate(decision_dict(ticker="AMD"))
    with pytest.raises(ValidationError, match="does not match this row"):
        ledger.DecisionRecord.model_validate(decision_dict(arm="feeds"))
    with pytest.raises(ValidationError, match="does not match this row"):
        ledger.DecisionRecord.model_validate(decision_dict(date="2026-08-04"))
    with pytest.raises(ValidationError, match="does not match this row"):
        ledger.DecisionRecord.model_validate(decision_dict(pair_id="2026-08-03-us-AMD-a1"))


@pytest.mark.unit
def test_decision_record_rejects_impossible_run_id_date():
    with pytest.raises(ValidationError, match="run_id"):
        ledger.DecisionRecord.model_validate(decision_dict(run_id="2026-99-99-us-NVDA-brief-1"))


# ---------------------------------------------------------------------------
# Order-row kind rules
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_order_record_order_kind_null_for_skip_and_refresh():
    # Contract: order_kind is 'bracket | close ; null for skip/refresh'.
    with pytest.raises(ValidationError, match="order_kind"):
        ledger.OrderRecord.model_validate(order_dict(kind="skip", reason="cap hit"))
    with pytest.raises(ValidationError, match="order_kind"):
        ledger.OrderRecord.model_validate(order_dict(kind="refresh"))
    record = ledger.OrderRecord.model_validate(order_dict(kind="refresh", order_kind=None))
    assert record.order_kind is None


# ---------------------------------------------------------------------------
# Timestamps must be timezone-aware everywhere in the ledger
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_ledger_timestamps_must_be_timezone_aware():
    with pytest.raises(ValidationError, match="decided_at"):
        ledger.DecisionRecord.model_validate(decision_dict(decided_at="2026-08-03T13:02:11"))
    with pytest.raises(ValidationError, match="written_at"):
        ledger.OrderRecord.model_validate(order_dict(written_at="2026-08-03T13:05:00"))
    with pytest.raises(ValidationError, match="settled_at"):
        ledger.OutcomeRecord.model_validate(
            {
                "run_id": "2026-08-03-us-NVDA-brief-1",
                "settled_at": "2026-08-10T12:05:00",
                "returns": {"d1": 0.004},
                "benchmark_returns": {"d1": 0.001},
            }
        )
    with pytest.raises(ValidationError, match="as_of"):
        ledger.PositionsSnapshot.model_validate({"as_of": "2026-08-04T12:20:00", "equity": 1.0})
    with pytest.raises(ValidationError, match="generated_at"):
        ledger.InputRef.model_validate(
            {"path": "x.md", "generated_at": "2026-08-03T12:35:00", "sha256": "a" * 64}
        )
