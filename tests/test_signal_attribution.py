"""Tests for stage-signal attribution math and the advisory phase cap."""
from __future__ import annotations

import duckdb
import pytest

from trade_system.migrations import apply_pending
from trade_system.signal_attribution import (
    PHASE_POSITION_CAP_PCT,
    compute_stage_attribution,
    latest_phase,
)


@pytest.fixture()
def con():
    conn = duckdb.connect(":memory:")
    conn.execute(
        "CREATE TABLE v_kline_daily(trade_date VARCHAR, stock_code VARCHAR,"
        " open DOUBLE, high DOUBLE, low DOUBLE, close DOUBLE, volume BIGINT,"
        " turnover BIGINT, change_pct DOUBLE, ktype VARCHAR, source_table VARCHAR,"
        " is_fallback BOOLEAN, fetched_at TIMESTAMP)"
    )
    apply_pending(conn)
    # sessions: 08-10 (signal day), 08-11 (outcome day)
    conn.execute(
        "INSERT INTO v_kline_daily VALUES"
        " ('2026-08-10','000001',10,11,9.5,10.0,1,1,null,'D','t',false,now()),"
        " ('2026-08-11','000001',10.0,12,9.9,11.0,1,1,10.0,'D','t',false,now())"
    )
    conn.execute(
        """CREATE TABLE stock_candidate_stage_signal(
               trade_date VARCHAR, stage VARCHAR, stock_code VARCHAR,
               stock_name VARCHAR, score DOUBLE, decision VARCHAR,
               evidence_json VARCHAR, generated_at TIMESTAMP,
               source_trade_date VARCHAR, as_of_time TIMESTAMP, run_id VARCHAR,
               is_actionable BOOLEAN, readiness_json VARCHAR,
               reference_price DOUBLE, reference_price_type VARCHAR,
               feature_version VARCHAR, data_complete BOOLEAN,
               signal_triggered BOOLEAN)"""
    )
    conn.execute(
        """INSERT INTO stock_candidate_stage_signal
           (trade_date, stage, stock_code, signal_triggered, is_actionable,
            data_complete)
           VALUES ('2026-08-10','intraday_strength','000001',true,true,true)"""
    )
    yield conn
    conn.close()


def test_open_to_close_and_close_to_close(con):
    rows = {r["horizon"]: r for r in compute_stage_attribution(con)}
    oc = rows["oc_next"]
    cc = rows["cc_next"]
    # next day: open 10 -> close 11 => +10%; prior close 10 -> close 11 => +10%
    assert oc["n_signals"] == 1
    assert oc["avg_ret_pct"] == pytest.approx(10.0)
    assert oc["win_rate"] == pytest.approx(1.0)
    assert cc["avg_ret_pct"] == pytest.approx(10.0)


def test_phase_join_defaults_to_unclassified(con):
    rows = compute_stage_attribution(con)
    assert all(r["phase"] == "unclassified" for r in rows)


def test_phase_aware_grouping(con):
    con.execute(
        """INSERT INTO market_cycle_phase(trade_date, phase)
           VALUES ('2026-08-10','ferment')"""
    )
    rows = compute_stage_attribution(con)
    assert all(r["phase"] == "ferment" for r in rows)


def test_advisory_cap_mapping_is_total_and_ordered():
    phases = {"ice", "recovery", "ferment", "climax", "divergence", "retreat"}
    assert set(PHASE_POSITION_CAP_PCT) == phases
    caps = list(PHASE_POSITION_CAP_PCT.values())
    assert caps == sorted(caps)


def test_latest_phase_reads_table(con):
    con.execute(
        """INSERT INTO market_cycle_phase
           (trade_date, phase, score, rationale)
           VALUES ('2026-08-10','retreat',18.5,'test')"""
    )
    info = latest_phase(con)
    assert info["phase"] == "retreat"
    assert info["advisory_position_cap_pct"] == 20
