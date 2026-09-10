from __future__ import annotations

import duckdb

from trade_system.flow_features import (
    SECTOR_FEATURE_TABLE,
    STOCK_FEATURE_TABLE,
    build_flow_features,
)


def test_build_flow_features_uses_canonical_provider_and_trading_windows(tmp_path):
    db = tmp_path / "flow.duckdb"
    con = duckdb.connect(str(db))
    con.execute(
        """
        CREATE TABLE multi_source_stock_flow(
            source_date DATE, stock_code VARCHAR, main_net DOUBLE, net_total DOUBLE,
            super_net DOUBLE, large_net DOUBLE, mid_net DOUBLE, small_net DOUBLE,
            close DOUBLE, change_pct DOUBLE, turnover DOUBLE, provider VARCHAR,
            fetched_at TIMESTAMP, is_stale BOOLEAN
        )
        """
    )
    con.execute(
        """
        CREATE TABLE multi_source_sector_flow(
            source_date DATE, sector_code VARCHAR, sector_name VARCHAR, main_net DOUBLE,
            change_pct DOUBLE, provider VARCHAR, sector_type VARCHAR,
            fetched_at TIMESTAMP, is_stale BOOLEAN
        )
        """
    )
    for day, value in [("2026-01-02", 1.0), ("2026-01-05", 2.0), ("2026-01-06", 3.0)]:
        con.execute(
            "INSERT INTO multi_source_stock_flow VALUES (?, '000001', ?, ?, 0,0,0,0,10,1,100,'tushare',current_timestamp,false)",
            [day, value, value],
        )
        # Eastmoney has the stable canonical priority and must win over the
        # duplicate TuShare row for the same date/code.
        con.execute(
            "INSERT INTO multi_source_stock_flow VALUES (?, '000001', ?, ?, 0,0,0,0,10,1,100,'eastmoney_intraday_clist_delay',current_timestamp,false)",
            [day, value + 10, value + 10],
        )
        con.execute(
            "INSERT INTO multi_source_stock_flow VALUES (?, '000002', ?, ?, 0,0,0,0,10,1,100,'tushare',current_timestamp,false)",
            [day, -value, -value],
        )
        con.execute(
            "INSERT INTO multi_source_sector_flow VALUES (?, 'THS001', '概念A', ?, 1, 'ths', 'ths_concept', current_timestamp, false)",
            [day, value],
        )
    con.close()

    result = build_flow_features(db, start_date="2026-01-02", end_date="2026-01-06")
    assert result["stock_rows"] == 6
    assert result["sector_rows"] == 3

    con = duckdb.connect(str(db), read_only=True)
    try:
        row = con.execute(
            f"SELECT provider, main_net_1d, main_net_3d, observed_days_20d FROM {STOCK_FEATURE_TABLE} WHERE trade_date='2026-01-06' AND stock_code='000001'"
        ).fetchone()
        assert row[0] == "eastmoney_intraday_clist_delay"
        assert row[1] == 13.0
        assert row[2] == 36.0
        assert row[3] == 3
        sector = con.execute(
            f"SELECT main_net_5d, positive_days_5d FROM {SECTOR_FEATURE_TABLE} WHERE trade_date='2026-01-06'"
        ).fetchone()
        assert sector == (6.0, 3)
    finally:
        con.close()


def test_equal_windows_source_switch_and_old_history_preserved(tmp_path):
    db=tmp_path/'segmented.duckdb'
    with duckdb.connect(str(db)) as con:
        con.execute('''CREATE TABLE multi_source_stock_flow(source_date DATE,stock_code VARCHAR,
            provider VARCHAR,main_net DOUBLE,turnover DOUBLE,close DOUBLE,change_pct DOUBLE,
            fetched_at TIMESTAMP,is_stale BOOLEAN,flow_definition VARCHAR,turnover_unit VARCHAR,flow_unit VARCHAR)''')
        con.execute("CREATE TABLE qlib_stock_flow_features(history VARCHAR); INSERT INTO qlib_stock_flow_features VALUES ('frozen v1')")
        for day in range(1,23):
            con.execute("INSERT INTO multi_source_stock_flow VALUES (?, '000001', ?,100,1000,10,0,current_timestamp,false,'same-definition','CNY','CNY')",
                        [f'2026-01-{day:02d}','tushare' if day<22 else 'hithink'])
    build_flow_features(db)
    with duckdb.connect(str(db),read_only=True) as con:
        assert con.execute(f"SELECT flow_acceleration_5d FROM {STOCK_FEATURE_TABLE} WHERE trade_date='2026-01-20'").fetchone()==(0.0,)
        assert con.execute(f"SELECT observed_days_20d,flow_acceleration_5d FROM {STOCK_FEATURE_TABLE} WHERE trade_date='2026-01-22'").fetchone()==(1,None)
        assert con.execute("SELECT * FROM qlib_stock_flow_features").fetchall()==[('frozen v1',)]

