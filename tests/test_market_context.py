from datetime import datetime

import duckdb

from schema import init_schema
from scripts.derive_market_context import derive_market_context


def test_market_context_fallback_uses_same_date_stock_flow(tmp_path):
    db = tmp_path / "fallback.duckdb"
    con = duckdb.connect(str(db))
    init_schema(con)
    con.executemany(
        """
        INSERT INTO multi_source_stock_flow
        (source_date,stock_code,main_net,change_pct,turnover,provider,is_stale)
        VALUES (?,?,?,?,?,?,false)
        """,
        [
            ["2026-07-16", "000001", 1, 10.0, 100, "eastmoney_intraday_clist"],
            ["2026-07-16", "000002", 1, -2.0, 200, "eastmoney_intraday_clist"],
            ["2026-07-16", "000003", 1, 0.0, 300, "eastmoney_intraday_clist"],
        ],
    )
    con.commit()
    con.close()

    result = derive_market_context(db, "2026-07-16")
    assert result["status"] == "fallback_written"
    assert result["coverage_codes"] == 3

    con = duckdb.connect(str(db), read_only=True)
    assert con.execute("SELECT rise_count,fall_count,limit_up_count FROM market_mood WHERE date='2026-07-16'").fetchone() == (1, 1, 1)
    assert con.execute("SELECT rise_count,fall_count FROM daily_summary WHERE date='2026-07-16'").fetchone() == (1, 1)
    con.close()


def test_verified_full_market_flow_is_promoted_but_does_not_claim_kpl_source(tmp_path):
    db = tmp_path / "derived-current.duckdb"
    con = duckdb.connect(str(db))
    init_schema(con)
    con.executemany(
        """
        INSERT INTO multi_source_stock_flow
        (source_date,stock_code,main_net,change_pct,turnover,provider,is_stale)
        VALUES (?,?,?,?,?,?,false)
        """,
        [
            ["2026-07-27", "000001", 1, 1.0, 100, "eastmoney_intraday_clist_delay"],
            ["2026-07-27", "920992", 1, -1.0, 200, "eastmoney_intraday_clist_delay"],
        ],
    )
    con.execute(
        """
        CREATE TABLE intraday_stock_flow_batch(
          trade_date DATE,provider VARCHAR,expected_rows INTEGER,
          fetched_rows INTEGER,coverage_pct DOUBLE,status VARCHAR,
          updated_at TIMESTAMP
        )
        """
    )
    con.execute(
        """
        INSERT INTO intraday_stock_flow_batch
        (trade_date,provider,expected_rows,fetched_rows,coverage_pct,status,updated_at)
        VALUES (
          '2026-07-27','eastmoney_intraday_clist_delay',2,2,100,'success',
          '2026-07-27 10:00:00'
        )
        """
    )
    # Exercise promotion of an earlier, low-coverage fallback row.
    con.execute(
        "INSERT INTO daily_summary(date,rise_count,fall_count,source_kind) "
        "VALUES ('2026-07-27',0,0,'fallback')"
    )
    con.commit()
    con.close()

    result = derive_market_context(
        db, "2026-07-27", now=datetime(2026, 7, 27, 10, 5)
    )
    assert result["status"] == "secondary_verified_written"
    assert result["source_kind"] == "secondary_verified"
    assert result["coverage_pct"] == 100.0

    con = duckdb.connect(str(db), read_only=True)
    try:
        assert con.execute(
            "SELECT rise_count,fall_count,source_kind FROM daily_summary "
            "WHERE date='2026-07-27'"
        ).fetchone() == (1, 1, "secondary_verified")
        payload = con.execute(
            "SELECT raw_json FROM daily_summary WHERE date='2026-07-27'"
        ).fetchone()[0]
        assert '"fallback": false' in payload
        assert '"batch_provider": "eastmoney_intraday_clist_delay"' in payload
    finally:
        con.close()
