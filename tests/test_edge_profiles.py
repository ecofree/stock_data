"""Tests for hot-money profile and auction pattern aggregation math."""
from __future__ import annotations

import duckdb
import pytest

from trade_system.edge_profiles import (
    build_auction_pattern_stats,
    build_hot_money_profile,
)
from trade_system.migrations import apply_pending


@pytest.fixture()
def con():
    conn = duckdb.connect(":memory:")
    # v_kline_daily / lhb tables are created here; analytics tables come from
    # the shipped migrations.
    conn.execute(
        "CREATE TABLE v_kline_daily(trade_date VARCHAR, stock_code VARCHAR,"
        " open DOUBLE, high DOUBLE, low DOUBLE, close DOUBLE, volume BIGINT,"
        " turnover BIGINT, change_pct DOUBLE, ktype VARCHAR, source_table VARCHAR,"
        " is_fallback BOOLEAN, fetched_at TIMESTAMP)"
    )
    apply_pending(conn)
    # Sessions 2026-08-03 .. 08-07 for stock A: closes 10 -> 11 -> 12 -> 13 -> 14
    closes = {"2026-08-03": 10.0, "2026-08-04": 11.0, "2026-08-05": 12.0,
              "2026-08-06": 13.0, "2026-08-07": 14.0}
    for d, c in closes.items():
        conn.execute(
            "INSERT INTO v_kline_daily VALUES (?, '000001', 10, 15, 9, ?,"
            " 100, 1000, 1.0, 'D', 't', false, now())",
            [d, c],
        )
    conn.execute(
        "CREATE TABLE lhb_youzi_dongxiang(date DATE, broker_name VARCHAR,"
        " stock_code VARCHAR, stock_name VARCHAR, buy_amount BIGINT,"
        " sell_amount BIGINT, fetched_at TIMESTAMP, raw_json VARCHAR)"
    )
    conn.execute(
        "CREATE TABLE auction_bidding_anomaly(date DATE, stock_code VARCHAR,"
        " anomaly_type VARCHAR, anomaly_value DOUBLE, fetched_at TIMESTAMP)"
    )
    yield conn
    conn.close()


def test_hot_money_forward_return_math(con):
    con.execute(
        "INSERT INTO lhb_youzi_dongxiang VALUES"
        " ('2026-08-03', 'seat-a', '000001', 'A', 500, 100, now(), '{}')"
    )
    rows = build_hot_money_profile(con, min_appearances=1)
    seat = next(r for r in rows if r["broker_name"] == "seat-a")
    # entry close on day0 =10; exit = lead(close,3) = 13 => +30%
    assert seat["appearances"] == 1
    assert seat["win_rate_3d"] == pytest.approx(1.0)
    assert seat["avg_ret_3d"] == pytest.approx(30.0)


def test_hot_money_net_buy_filter_and_min_appearances(con):
    # net sell (sell > buy) must be ignored
    con.execute(
        "INSERT INTO lhb_youzi_dongxiang VALUES"
        " ('2026-08-03', 'seat-b', '000001', 'A', 100, 900, now(), '{}')"
    )
    assert build_hot_money_profile(con, min_appearances=1) == []


def test_auction_pattern_open_to_close(con):
    # 08-03: open=10, close=10 -> flat (oc 0%, not a win)
    con.execute(
        "INSERT INTO auction_bidding_anomaly VALUES"
        " ('2026-08-03', '000001', 'flat_open', 3.0, now())"
    )
    # 08-04: open=10, close=11 -> +10% oc
    con.execute(
        "INSERT INTO auction_bidding_anomaly VALUES"
        " ('2026-08-04', '000001', 'gap_up_grab', 3.0, now())"
    )
    rows = {r["anomaly_type"]: r for r in build_auction_pattern_stats(con)}
    assert set(rows) == {"flat_open", "gap_up_grab"}
    flat = rows["flat_open"]
    grab = rows["gap_up_grab"]
    assert flat["occurrences"] == 1 and grab["occurrences"] == 1
    assert flat["day_win_rate"] == pytest.approx(0.0)
    assert flat["day_avg_oc_pct"] == pytest.approx(0.0)
    assert grab["day_win_rate"] == pytest.approx(1.0)
    assert grab["day_avg_oc_pct"] == pytest.approx(10.0)


def test_auction_without_matching_kline_is_skipped(con):
    con.execute(
        "INSERT INTO auction_bidding_anomaly VALUES"
        " ('2026-08-09', '999999', 'weekend_noise', 5.0, now())"
    )
    assert build_auction_pattern_stats(con) == []
