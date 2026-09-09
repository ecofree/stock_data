from __future__ import annotations

import duckdb

from scripts.audit_source_conflicts import _duplicate_groups, _provider_counts


def test_source_conflict_audit_reads_pragma_column_names(tmp_path):
    db = tmp_path / "conflicts.duckdb"
    con = duckdb.connect(str(db))
    con.execute(
        "CREATE TABLE multi_source_stock_flow ("
        "source_date DATE, stock_code VARCHAR, provider VARCHAR, is_stale BOOLEAN)"
    )
    con.execute(
        "INSERT INTO multi_source_stock_flow VALUES "
        "('2026-08-28', '000001', 'eastmoney_intraday_clist', false),"
        "('2026-08-28', '000001', 'tushare', false)"
    )

    assert _provider_counts(con, "multi_source_stock_flow") == [
        {"provider": "eastmoney_intraday_clist", "rows": 1},
        {"provider": "tushare", "rows": 1},
    ]
    groups = _duplicate_groups(
        con, "multi_source_stock_flow", ("source_date", "stock_code")
    )
    assert len(groups) == 1
    assert groups[0]["provider_count"] == 2
    assert groups[0]["providers"] == "eastmoney_intraday_clist, tushare"
    con.close()
