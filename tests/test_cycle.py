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


def test_promotion_rate_counts_repeats(con):
    _add_kline(con, "2026-08-21", "999999", 1.0)  # ensure a next session exists
    _add_limit(con, "2026-08-20", "000001", 1)
    _add_limit(con, "2026-08-20", "000002", 1)
    _add_limit(con, "2026-08-21", "000001", 2)  # promoted

    rows = compute_promotion(con, "2026-08-20")
    first = next(r for r in rows if r["from_board"] == 1)
    assert first["candidates"] == 2 and first["promoted"] == 1
    assert first["rate"] == pytest.approx(0.5)


def test_next_session_skips_gaps(con):
    _add_kline(con, "2026-08-20", "000001", 1.0)
    _add_kline(con, "2026-08-25", "000001", 2.0)
    assert next_session(con, "2026-08-20") == "2026-08-25"
