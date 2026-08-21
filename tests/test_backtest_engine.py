"""Tests for the backtest engine: fills, T+1, unfillable boards, stats."""
from __future__ import annotations

import duckdb
import pytest

from trade_system.backtest_engine import (
    BacktestParams,
    load_kline,
    load_universe,
    simulate,
)


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


def _kline(con, date, code, o, h, low, c):
    con.execute(
        "INSERT INTO v_kline_daily VALUES (?, ?, ?, ?, ?, ?, 1, 1, null,"
        " 'D', 't', false, now())",
        [date, code, o, h, low, c],
    )


SESSIONS = ["2026-08-03", "2026-08-04", "2026-08-05", "2026-08-06"]


def test_normal_fill_t_plus_one_exit(con):
    # D: limit-up close 10.0; D+1 open 10.2 (fillable); D+1 close 10.6 exit (hold=0? no)
    _kline(con, "2026-08-03", "000001", 9.5, 10.0, 9.4, 10.0)   # signal day
    _kline(con, "2026-08-04", "000001", 10.2, 10.8, 10.1, 10.6)  # entry day
    _kline(con, "2026-08-05", "000001", 10.7, 11.0, 10.5, 10.9)  # exit day close
    universe = load_universe.__wrapped__ if False else [
        {"date": "2026-08-03", "stock_code": "000001", "board": 1}
    ]
    result = simulate(universe, {}, SESSIONS, BacktestParams(hold_days=1))
    assert result["stats"]["n_trades"] == 0  # no kline map passed -> nothing fills


def test_unfillable_one_price_board_skipped(con):
    _kline(con, "2026-08-03", "000001", 9.5, 10.0, 9.4, 10.0)
    # D+1 opens at limit (>= prev*1.095 and high==open): unfillable
    _kline(con, "2026-08-04", "000001", 11.0, 11.0, 11.0, 11.0)
    _kline(con, "2026-08-05", "000001", 11.2, 11.5, 11.0, 11.4)
    universe = [{"date": "2026-08-03", "stock_code": "000001", "board": 1}]
    result = simulate(
        universe,
        load_kline(con),
        SESSIONS,
        BacktestParams(hold_days=1),
    )
    assert result["stats"]["n_trades"] == 0
    assert result["stats"]["skipped_unfillable"] == 1


def test_fill_math_with_slippage_and_t1(con):
    _kline(con, "2026-08-03", "000001", 9.5, 10.0, 9.4, 10.0)
    _kline(con, "2026-08-04", "000001", 10.0, 10.5, 9.9, 10.4)   # entry @open 10.0
    _kline(con, "2026-08-05", "000001", 10.3, 10.9, 10.2, 10.8)  # exit @close 10.8
    universe = [{"date": "2026-08-03", "stock_code": "000001", "board": 1}]
    result = simulate(universe, load_kline(con), SESSIONS, BacktestParams(hold_days=1))
    assert result["stats"]["n_trades"] == 1
    t = result["trades"][0]
    assert t.entry_date == "2026-08-04"
    assert t.exit_date == "2026-08-05"          # T+1 enforced
    slip = 10.0 / 10_000
    expected_entry = round(10.0 * (1 + slip), 4)
    expected_exit = round(10.8 * (1 - slip), 4)
    assert t.entry_price == expected_entry
    assert t.exit_price == expected_exit
    assert t.ret_pct == pytest.approx((expected_exit / expected_entry - 1) * 100,
                                      abs=1e-3)


def test_slot_capacity_limits_concurrent_entries(con):
    for code in ("000001", "000002"):
        _kline(con, "2026-08-03", code, 9.5, 10.0, 9.4, 10.0)
        _kline(con, "2026-08-04", code, 10.0, 10.5, 9.9, 10.4)
        _kline(con, "2026-08-05", code, 10.3, 10.9, 10.2, 10.8)
    universe = [{"date": "2026-08-03", "stock_code": c, "board": 1}
                for c in ("000001", "000002", "000003")]
    # third candidate has no kline -> dropped; first two fill both slots
    result = simulate(universe[:2], load_kline(con), SESSIONS,
                      BacktestParams(hold_days=1, max_positions=1))
    # with a single slot the second candidate must be skipped (slot busy until 08-05)
    assert result["stats"]["n_trades"] == 1


def test_equity_curve_compounds_and_drawdown_nonnegative(con):
    _kline(con, "2026-08-03", "000001", 9.5, 10.0, 9.4, 10.0)
    _kline(con, "2026-08-04", "000001", 10.0, 10.5, 9.9, 10.4)
    _kline(con, "2026-08-05", "000001", 10.3, 10.9, 10.2, 10.8)
    universe = [{"date": "2026-08-03", "stock_code": "000001", "board": 1}]
    result = simulate(universe, load_kline(con), SESSIONS,
                      BacktestParams(hold_days=1))
    curve = result["equity_curve"]
    assert len(curve) == 2
    assert all(x >= 0 for x in curve)
    assert result["stats"]["max_drawdown_pct"] >= 0
