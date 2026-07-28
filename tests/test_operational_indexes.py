from __future__ import annotations

import duckdb

from scripts.ensure_operational_indexes import ensure_indexes


def test_ensure_operational_indexes_is_idempotent(tmp_path):
    db = tmp_path / "indexes.duckdb"
    con = duckdb.connect(str(db))
    con.execute("CREATE TABLE realtime_candidate_pool_snapshot(trade_date DATE, fetched_at TIMESTAMP)")
    con.execute("CREATE TABLE operator_trade_outcome(trade_date VARCHAR, stock_code VARCHAR)")
    con.close()

    first = ensure_indexes(db)
    second = ensure_indexes(db)

    assert first["created"] == 2
    assert second["created"] == 0
