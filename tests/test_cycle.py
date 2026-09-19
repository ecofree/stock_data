"""Tests for the emotion-cycle phase classifier and premium/promotion math."""
from __future__ import annotations

import duckdb
import pytest

from trade_system.cycle import DayMetrics, classify_phase, compute_premium, \
    compute_promotion, next_session


def _m(**kw) -> DayMetrics:
    return DayMetrics(trade_date="2026-08-21", **kw)


def test_ice_when_limit_up_very_low():
    phase, score, why = classify_phase(_m(limit_up_count=22, premium_pct=1.0))
    assert phase == "ice"
    assert "limit_up" in why


def test_climax_requires_premium_board_and_broken_rate():
    hot = classify_phase(_m(limit_up_count=80, premium_pct=4.0, max_board=6, blown_rate=20))
    assert hot[0] == "climax"
    # high broken rate blocks climax even with great premium
    blocked = classify_phase(_m(limit_up_count=80, premium_pct=4.0, max_board=6,
                                blown_rate=40))
    assert blocked[0] != "climax"


def test_retreat_on_negative_premium():
    phase, _, why = classify_phase(_m(limit_up_count=60, premium_pct=-2.5, blown_rate=30))
    assert phase == "retreat"
    assert "premium" in why


def test_ferment_and_recovery_and_divergence():
    assert classify_phase(_m(limit_up_count=70, premium_pct=2.0, max_board=5))[0] == "ferment"
    assert classify_phase(_m(limit_up_count=70, premium_pct=0.5, max_board=3))[0] == "recovery"
    mixed = classify_phase(_m(limit_up_count=70, premium_pct=-0.5, blown_rate=30,
                              promotion_rate=0.30))
    assert mixed[0] in ("divergence",)


def test_score_is_bounded():
    for kwargs in ({"premium_pct": 9.9}, {"premium_pct": -9.9},
                   {"max_board": 12}, {"blown_rate": 90}):
        _, score, _ = classify_phase(_m(limit_up_count=50, **kwargs))
        assert 0.0 <= score <= 100.0


@pytest.fixture()
def con():
    conn = duckdb.connect(":memory:")
    conn.execute(
        "CREATE TABLE v_limit_pool(trade_date VARCHAR, board_level INTEGER,"
        " stock_code VARCHAR, stock_name VARCHAR, limit_up_time VARCHAR,"
        " fetched_at TIMESTAMP)"
    )
    conn.execute(
        "CREATE TABLE v_kline_daily(trade_date VARCHAR, stock_code VARCHAR,"
        " open DOUBLE, high DOUBLE, low DOUBLE, close DOUBLE, volume BIGINT,"
        " turnover BIGINT, change_pct DOUBLE, ktype VARCHAR, source_table VARCHAR,"
        " is_fallback BOOLEAN, fetched_at TIMESTAMP)"
    )
    conn.execute("CREATE TABLE tushare_trade_cal(cal_date DATE, is_open BOOLEAN)")
    conn.executemany("INSERT INTO tushare_trade_cal VALUES (?,?)", [
        ("2026-08-20", True), ("2026-08-21", True), ("2026-08-22", False),
        ("2026-08-23", False), ("2026-08-24", True), ("2026-08-25", True)])
    yield conn
    conn.close()


def _add_limit(con, date, code, board):
    con.execute(
        "INSERT INTO v_limit_pool VALUES (?, ?, ?, 'x', '0930', now())",
        [date, board, code],
    )


def _add_kline(con, date, code, pct):
    con.execute(
        "INSERT INTO v_kline_daily VALUES (?, ?, 10, 10, 10, 10, 1, 1, ?, 'D',"
        " 't', false, now())",
        [date, code, pct],
    )


def test_compute_premium_buckets(con):
    for code in ("000001", "000002"):
        _add_limit(con, "2026-08-20", code, 1)
    _add_limit(con, "2026-08-20", "600001", 4)
    _add_kline(con, "2026-08-21", "000001", 5.0)
    _add_kline(con, "2026-08-21", "000002", -2.0)
    _add_kline(con, "2026-08-21", "600001", 8.0)

    rows = {r["board_bucket"]: r for r in compute_premium(con, "2026-08-20")}
    assert rows["1"]["sample_size"] == 2
    assert rows["1"]["avg_pct"] == pytest.approx(1.5)
    assert rows["1"]["win_rate"] == pytest.approx(0.5)
    assert rows["4+"]["avg_pct"] == pytest.approx(8.0)
    assert rows["_all"]["sample_size"] == 3




def test_next_session_never_skips_missing_market_bars(con):
    _add_kline(con, "2026-08-20", "000001", 1.0)
    _add_kline(con, "2026-08-25", "000001", 2.0)
    assert next_session(con, "2026-08-20") == "2026-08-21"


@pytest.mark.parametrize("fault", ["missing_calendar", "null_state", "conflict", "closed_origin", "skipped_session"])
def test_cycle_calendar_unknown_cannot_use_observed_rows(con, fault):
    _add_limit(con, "2026-08-20", "000001", 1)
    _add_kline(con, "2026-08-25", "000001", 10)
    sessions = None
    if fault == "missing_calendar":
        con.execute("DROP TABLE tushare_trade_cal")
    elif fault == "null_state":
        con.execute("UPDATE tushare_trade_cal SET is_open=NULL WHERE cal_date='2026-08-21'")
    elif fault == "conflict":
        con.execute("INSERT INTO tushare_trade_cal VALUES ('2026-08-21',false)")
    elif fault == "closed_origin":
        con.execute("UPDATE tushare_trade_cal SET is_open=false WHERE cal_date='2026-08-20'")
    else:
        sessions = ["2026-08-20", "2026-08-25"]
    assert next_session(con, "2026-08-20", sessions) is None
    assert compute_premium(con, "2026-08-20", sessions=sessions) == []


def test_estimated_pool_never_enters_real_cohort_or_run_dates(con):
    from scripts.generate_cycle_analytics import _trading_days
    con.execute("CREATE TABLE derived_limit_up_daily(trade_date DATE, stock_code VARCHAR, board_level INTEGER)")
    con.execute("INSERT INTO derived_limit_up_daily VALUES ('2026-08-20','000002',8),('2026-08-19','000003',9)")
    _add_limit(con, "2026-08-20", "000001", 1)
    _add_kline(con, "2026-08-21", "000001", 5)
    _add_kline(con, "2026-08-21", "000002", -10)
    assert _trading_days(con) == ["2026-08-20"]
    rows = {r["board_bucket"]:r for r in compute_premium(con, "2026-08-20")}
    assert set(rows) == {"1", "_all"} and rows['_all']['sample_size'] == 1
    assert rows['_all']['avg_pct'] == 5
    assert con.execute("SELECT count(*) FROM derived_limit_up_daily").fetchone()[0] == 2


@pytest.mark.parametrize("bad_value", [None, float('nan'), float('inf')])
def test_premium_keeps_missing_cohort_unknown_without_replacing_target(con, bad_value):
    for code in ('000001','000002'):
        _add_limit(con, "2026-08-20", code, 1)
    _add_kline(con, "2026-08-21", "000001", 5)
    _add_kline(con, "2026-08-21", "000002", bad_value)
    _add_kline(con, "2026-08-24", "000002", 10)
    row = next(r for r in compute_premium(con, "2026-08-20") if r['board_bucket']=='_all')
    assert (row['sample_size'],row['observed_count'],row['missing_count']) == (2,1,1)
    assert row['avg_pct'] is row['median_pct'] is row['win_rate'] is None


def test_promotion_uses_max_height_and_requires_actual_increment(con):
    for code, board in [('000001',1),('000001',2),('000002',2)]:
        _add_limit(con, "2026-08-20", code, board)
    _add_limit(con, "2026-08-21", "000001", 3)
    _add_limit(con, "2026-08-21", "000002", 2)
    assert compute_promotion(con, "2026-08-20") == [dict(trade_date='2026-08-20',from_board=2,candidates=2,promoted=1,rate=.5)]
    con.execute("DELETE FROM v_limit_pool WHERE trade_date='2026-08-21'")
    assert compute_promotion(con, "2026-08-20") == []


def test_cycle_builder_rejects_unverified_dates_before_writing(con):
    from scripts.generate_cycle_analytics import build
    con.execute("DELETE FROM tushare_trade_cal WHERE cal_date='2026-08-21'")
    before = con.execute("SHOW TABLES").fetchall()
    with pytest.raises(ValueError, match='calendar'):
        build(con, ['2026-08-20','2026-08-21'])
    assert con.execute("SHOW TABLES").fetchall() == before
