"""Pool contract models + reading rule (specs/pool-data-contract.md v1)."""

import json
from datetime import date

import pytest
from pydantic import ValidationError

from pipeline.contracts import ContractError, pool


def opportunity_entry(ticker="AVGO", **overrides):
    entry = {
        "ticker": ticker,
        "score": 7.5,
        "catalyst_type": "earnings",
        "rationale": "one-line reason with the catalyst and timing",
        "citations": ["https://example.com/a"],
        "entered_on": "2026-08-01",
        "low_score_streak": 0,
        "gate_fail_streak": 0,
        "technical": {
            "gate": "pass",
            "boll_daily": {"close": 291.2, "mid": 285.1, "upper": 301.4, "lower": 268.8},
            "boll_weekly": {"close": 291.2, "mid": 262.0, "upper": 315.5, "lower": 208.4},
        },
    }
    entry.update(overrides)
    return entry


def watch_entry(ticker="MRVL", **overrides):
    entry = {
        "ticker": ticker,
        "score": 6.2,
        "catalyst_type": "product",
        "rationale": "watching",
        "citations": ["https://example.com/w"],
        "technical": {"gate": "watch"},
    }
    entry.update(overrides)
    return entry


def pool_dict(**overrides):
    data = {
        "as_of_date": "2026-08-03",
        "session": "us",
        "generated_at": "2026-08-03T12:31:00Z",
        "generator": "claude-deep-search",
        "carried_forward": False,
        "core": [{"ticker": "NVDA", "note": "holding", "score": 6.8}],
        "opportunity": [opportunity_entry()],
        "watch": [watch_entry()],
        "removed": [{"ticker": "SMCI", "reason": "score<4.0 for 3 sessions", "last_score": 3.1}],
    }
    data.update(overrides)
    return data


@pytest.mark.unit
def test_valid_pool_file_parses():
    parsed = pool.PoolFile.model_validate(pool_dict())
    assert parsed.session == "us"
    assert parsed.opportunity[0].entered_on == date(2026, 8, 1)
    assert parsed.opportunity[0].technical.gate == "pass"
    assert parsed.carried_forward is False


@pytest.mark.unit
def test_carried_forward_defaults_false():
    data = pool_dict()
    data.pop("carried_forward")
    assert pool.PoolFile.model_validate(data).carried_forward is False


@pytest.mark.unit
def test_opportunity_requires_hysteresis_state():
    entry = opportunity_entry()
    entry.pop("entered_on")
    with pytest.raises(ValidationError, match="entered_on"):
        pool.PoolFile.model_validate(pool_dict(opportunity=[entry]))


@pytest.mark.unit
def test_opportunity_requires_citation_and_gate():
    with pytest.raises(ValidationError, match="citations"):
        pool.PoolFile.model_validate(pool_dict(opportunity=[opportunity_entry(citations=[])]))
    entry = opportunity_entry()
    entry["technical"] = {}
    with pytest.raises(ValidationError, match="gate"):
        pool.PoolFile.model_validate(pool_dict(opportunity=[entry]))


@pytest.mark.unit
def test_opportunity_cap_is_hard():
    entries = [opportunity_entry(ticker=f"OPP{i}") for i in range(6)]
    with pytest.raises(ValidationError, match="cap 5"):
        pool.PoolFile.model_validate(pool_dict(opportunity=entries))


@pytest.mark.unit
def test_watch_cap_is_hard():
    entries = [watch_entry(ticker=f"WCH{i}") for i in range(11)]
    with pytest.raises(ValidationError, match="cap 10"):
        pool.PoolFile.model_validate(pool_dict(watch=entries))


@pytest.mark.unit
def test_core_cap_is_soft_warn_only(caplog):
    core = [{"ticker": f"CORE{i}"} for i in range(11)]
    with caplog.at_level("WARNING", logger="pipeline.contracts.pool"):
        parsed = pool.PoolFile.model_validate(pool_dict(core=core))
    assert len(parsed.core) == 11
    assert "soft cap" in caplog.text


@pytest.mark.unit
def test_cross_layer_duplicates_rejected():
    with pytest.raises(ValidationError, match="'NVDA' appears in both 'core' and 'opportunity'"):
        pool.PoolFile.model_validate(pool_dict(opportunity=[opportunity_entry(ticker="NVDA")]))


@pytest.mark.unit
def test_within_layer_duplicates_rejected():
    entries = [watch_entry(ticker="MRVL"), watch_entry(ticker="MRVL")]
    with pytest.raises(ValidationError, match="duplicate ticker 'MRVL' within layer 'watch'"):
        pool.PoolFile.model_validate(pool_dict(watch=entries))


@pytest.mark.unit
def test_tickers_must_match_session_market():
    with pytest.raises(ValidationError, match="belongs to session 'cn'"):
        pool.PoolFile.model_validate(pool_dict(core=[{"ticker": "0700.HK"}]))


@pytest.mark.unit
def test_pool_lenient_parse_warns_on_unknown_fields(caplog):
    data = pool_dict(builder_debug="not in the contract")
    with pytest.raises(ValidationError):
        pool.PoolFile.model_validate(data)
    with caplog.at_level("WARNING", logger="pipeline.contracts"):
        parsed = pool.PoolFile.parse_lenient(data)
    assert parsed.session == "us"
    assert "builder_debug" in caplog.text


# ---------------------------------------------------------------------------
# Reading rule
# ---------------------------------------------------------------------------


def write_pool(base_dir, date_str, session="us"):
    session_dir = base_dir / session
    session_dir.mkdir(parents=True, exist_ok=True)
    payload = pool_dict(as_of_date=date_str, session=session)
    (session_dir / f"{date_str}.json").write_text(json.dumps(payload), encoding="utf-8")


@pytest.mark.unit
def test_read_pool_same_day_is_fresh(tmp_path):
    write_pool(tmp_path, "2026-08-03")
    result = pool.read_pool(tmp_path, "us", date(2026, 8, 3))
    assert result.staleness == "fresh"
    assert result.gap_days == 0
    assert result.pool.as_of_date == date(2026, 8, 3)


@pytest.mark.unit
def test_read_pool_picks_newest_on_or_before_date(tmp_path):
    write_pool(tmp_path, "2026-07-30")
    write_pool(tmp_path, "2026-08-01")
    write_pool(tmp_path, "2026-08-05")  # future — never served
    result = pool.read_pool(tmp_path, "us", date(2026, 8, 3))
    assert result.as_of_date == date(2026, 8, 1)
    assert result.staleness == "warn"
    assert result.gap_days == 2


@pytest.mark.unit
def test_read_pool_gap_beyond_staleness_days_is_absent(tmp_path):
    write_pool(tmp_path, "2026-07-30")
    result = pool.read_pool(tmp_path, "us", date(2026, 8, 3))
    assert result.gap_days == 4
    assert result.staleness == "absent"
    # The file itself is still surfaced so the ledger can record what was on disk.
    assert result.path is not None


@pytest.mark.unit
def test_read_pool_staleness_days_configurable(tmp_path):
    write_pool(tmp_path, "2026-07-30")
    result = pool.read_pool(tmp_path, "us", date(2026, 8, 3), pool_max_staleness_days=5)
    assert result.staleness == "warn"


@pytest.mark.unit
def test_read_pool_no_files_is_absent(tmp_path):
    (tmp_path / "us").mkdir()
    result = pool.read_pool(tmp_path, "us", date(2026, 8, 3))
    assert result == pool.PoolReadResult(None, None, None, None, "absent")
    # A missing session directory behaves the same.
    assert pool.read_pool(tmp_path, "cn", date(2026, 8, 3)).staleness == "absent"


@pytest.mark.unit
def test_read_pool_ignores_non_pool_files(tmp_path):
    session_dir = tmp_path / "us"
    session_dir.mkdir()
    (session_dir / "core.us.yaml").write_text("", encoding="utf-8")
    (session_dir / "notes.txt").write_text("", encoding="utf-8")
    (session_dir / "2026-08-03.json.bak").write_text("", encoding="utf-8")
    assert pool.read_pool(tmp_path, "us", date(2026, 8, 3)).staleness == "absent"


@pytest.mark.unit
def test_read_pool_corrupt_json_raises_contract_error(tmp_path):
    session_dir = tmp_path / "us"
    session_dir.mkdir()
    (session_dir / "2026-08-03.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(ContractError, match="not valid JSON"):
        pool.read_pool(tmp_path, "us", date(2026, 8, 3))


# ---------------------------------------------------------------------------
# Entry field constraints (review round: enforced-but-untested branches)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_pool_citation_must_be_http_url():
    with pytest.raises(ValidationError, match="is not a URL"):
        pool.PoolFile.model_validate(
            pool_dict(watch=[watch_entry(citations=["ftp://example.com/x"])])
        )


@pytest.mark.unit
def test_pool_invalid_gate_value_rejected():
    with pytest.raises(ValidationError, match="gate"):
        pool.PoolFile.model_validate(pool_dict(watch=[watch_entry(technical={"gate": "maybe"})]))


@pytest.mark.unit
def test_pool_negative_streaks_and_score_range_rejected():
    with pytest.raises(ValidationError, match="low_score_streak"):
        pool.PoolFile.model_validate(pool_dict(opportunity=[opportunity_entry(low_score_streak=-1)]))
    with pytest.raises(ValidationError, match="gate_fail_streak"):
        pool.PoolFile.model_validate(pool_dict(opportunity=[opportunity_entry(gate_fail_streak=-2)]))
    with pytest.raises(ValidationError, match="score"):
        pool.PoolFile.model_validate(pool_dict(watch=[watch_entry(score=10.5)]))


@pytest.mark.unit
def test_pool_removed_layer_tickers_must_match_session_market():
    with pytest.raises(ValidationError, match="removed ticker '0700.HK' belongs to session 'cn'"):
        pool.PoolFile.model_validate(pool_dict(removed=[{"ticker": "0700.HK", "reason": "x"}]))


@pytest.mark.unit
def test_pool_generated_at_must_be_timezone_aware():
    with pytest.raises(ValidationError, match="generated_at"):
        pool.PoolFile.model_validate(pool_dict(generated_at="2026-08-03T12:31:00"))


# ---------------------------------------------------------------------------
# Reading-rule edge cases
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_read_pool_gap_equal_to_staleness_limit_is_warn(tmp_path):
    # Boundary: the contract makes the pool absent only when gap > 3 — a gap
    # of exactly pool_max_staleness_days must still serve with a warning.
    write_pool(tmp_path, "2026-07-31")
    result = pool.read_pool(tmp_path, "us", date(2026, 8, 3))
    assert result.gap_days == 3
    assert result.staleness == "warn"


@pytest.mark.unit
def test_read_pool_schema_invalid_file_raises_contract_error(tmp_path):
    session_dir = tmp_path / "us"
    session_dir.mkdir()
    payload = pool_dict(session="not-a-session")
    (session_dir / "2026-08-03.json").write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ContractError, match="session"):
        pool.read_pool(tmp_path, "us", date(2026, 8, 3))


@pytest.mark.unit
def test_read_pool_skips_pool_named_file_with_impossible_date(tmp_path, caplog):
    # A stray 2026-99-99.json must not take down every read of the directory.
    write_pool(tmp_path, "2026-08-03")
    (tmp_path / "us" / "2026-99-99.json").write_text("{}", encoding="utf-8")
    with caplog.at_level("WARNING", logger="pipeline.contracts.pool"):
        result = pool.read_pool(tmp_path, "us", date(2026, 8, 3))
    assert result.staleness == "fresh"
    assert result.as_of_date == date(2026, 8, 3)
    assert "2026-99-99.json" in caplog.text
    # Alone, such a file is plain absence — not a crash.
    (tmp_path / "cn").mkdir()
    (tmp_path / "cn" / "2026-99-99.json").write_text("{}", encoding="utf-8")
    assert pool.read_pool(tmp_path, "cn", date(2026, 8, 3)).staleness == "absent"
