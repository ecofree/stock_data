"""Tests for the IC analysis math on a synthetic panel."""
from __future__ import annotations

import pandas as pd
import duckdb
import pytest

from scripts.feature_ic_analysis import ic_summary, load_panel


@pytest.fixture()
def con(tmp_path):
    conn = duckdb.connect(":memory:")
    conn.execute(
        "CREATE TABLE v_kline_daily(trade_date VARCHAR, stock_code VARCHAR,"
        " open DOUBLE, high DOUBLE, low DOUBLE, close DOUBLE, volume BIGINT,"
        " turnover BIGINT, change_pct DOUBLE, ktype VARCHAR, source_table VARCHAR,"
        " is_fallback BOOLEAN, fetched_at TIMESTAMP)"
    )
    # 3 sessions x 3 names; feature perfectly predicts next-session return.
    prices = {
        "2026-08-03": {"000001": 10.0, "000002": 20.0, "000003": 30.0},
        "2026-08-04": {"000001": 11.0, "000002": 19.0, "000003": 33.0},
        "2026-08-05": {"000001": 12.0, "000002": 18.0, "000003": 36.0},
        "2026-08-06": {"000001": 13.0, "000002": 17.0, "000003": 39.0},
    }
    for d, m in prices.items():
        for code, c in m.items():
            conn.execute(
                "INSERT INTO v_kline_daily VALUES (?, ?, 10, 40, 5, ?,"
                " 1, 1, null, 'D', 't', false, now())",
                [d, code, c],
            )
    conn.execute(
        """CREATE TABLE feat_panel(trade_date VARCHAR, stock_code VARCHAR,
                                   momentum DOUBLE)"""
    )
    # momentum on day t == that stock's return from t to t+1 (perfect IC)
    mom = {"2026-08-03": {"000001": 0.10, "000002": -0.05, "000003": 0.10},
           "2026-08-04": {"000001": 0.091, "000002": -0.053, "000003": 0.091}}
    for d, m in mom.items():
        for code, v in m.items():
            conn.execute(
                "INSERT INTO feat_panel VALUES (?, ?, ?)", [d, code, v]
            )
    yield conn
    conn.close()


def test_load_panel_computes_forward_return(con):
    panel = load_panel(con, "feat_panel", "trade_date", "stock_code",
                       ["momentum"], horizon=1)
    # 2 signal days x 3 names = 6 rows
    assert len(panel) == 6
    row = panel[(panel["d"] == pd.Timestamp("2026-08-03"))
                & (panel["stock_code"] == "000001")]
    assert row["fwd_ret"].iloc[0] == pytest.approx(11.0 / 10.0 - 1)


def test_load_panel_start_date_filters_rows(con):
    full = load_panel(con, "feat_panel", "trade_date", "stock_code",
                      ["momentum"], horizon=1)
    filtered = load_panel(con, "feat_panel", "trade_date", "stock_code",
                          ["momentum"], horizon=1,
                          start_date="2026-08-04")
    assert len(full) == 6
    # only the 08-04 signal day survives the filter
    assert len(filtered) == 3
    assert set(filtered["d"]) == {pd.Timestamp("2026-08-04")}


def test_ic_summary_perfect_feature_has_ic_one(con):
    panel = load_panel(con, "feat_panel", "trade_date", "stock_code",
                       ["momentum"], horizon=1)
    summary = ic_summary(panel, ["momentum"], min_names=3)
    row = summary.iloc[0]
    assert row["days"] == 2
    assert row["ic_mean"] == pytest.approx(1.0, abs=1e-6)
