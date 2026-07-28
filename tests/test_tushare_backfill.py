import duckdb

from base import DuckDBStore
from schema import init_schema
from trade_system.tushare_backfill import _normalize_code, build_tushare_gap_list, plan_tushare_backfill_tasks


def _store(tmp_path):
    db_path = tmp_path / "tushare_backfill.duckdb"
    store = DuckDBStore(str(db_path))
    init_schema(store.conn)
    return store, db_path


def test_backfill_normalizes_short_stock_codes_with_leading_zeroes():
    assert _normalize_code("daily", "1") == "000001"
    assert _normalize_code("daily_basic", "66") == "000066"
    assert _normalize_code("adj_factor", "656") == "000656"


def test_build_tushare_gap_list_records_missing_rows(tmp_path):
    store, db_path = _store(tmp_path)
    store.insert_rows(
        "tushare_trade_cal",
        [
            ("SSE", "2026-07-01", True, "2026-06-30"),
            ("SSE", "2026-07-02", True, "2026-07-01"),
        ],
        ["exchange", "cal_date", "is_open", "pretrade_date"],
        replace_on=["exchange", "cal_date"],
    )
    store.insert_rows(
        "tushare_daily",
        [("000001.SZ", "000001", "2026-07-01", 10, 11, 9.5, 10.5, 1000, 1200, 2.1)],
        ["ts_code", "stock_code", "date", "open", "high", "low", "close", "volume", "turnover", "change_pct"],
        replace_on=["ts_code", "date"],
    )
    store.close()

    gaps = build_tushare_gap_list(
        db_path,
        stock_codes=["000001"],
        start_date="20260701",
        end_date="20260702",
        data_kinds=["daily"],
    )

    con = duckdb.connect(str(db_path), read_only=True)
    try:
        row = con.execute(
            """
            SELECT data_kind, code, expected_rows, existing_rows, missing_rows, status
            FROM tushare_gap_status
            """
        ).fetchone()
    finally:
        con.close()

    assert gaps == [
        {
            "data_kind": "daily",
            "code": "000001",
            "start_date": "2026-07-01",
            "end_date": "2026-07-02",
            "expected_rows": 2,
            "existing_rows": 1,
            "missing_rows": 1,
            "status": "missing",
            "source_table": "tushare_daily",
        }
    ]
    assert row == ("daily", "000001", 2, 1, 1, "missing")


def test_plan_tushare_backfill_tasks_keeps_completed_tasks_done(tmp_path):
    store, db_path = _store(tmp_path)
    init_schema(store.conn)
    store.close()
    gap_rows = [
        {
            "data_kind": "daily",
            "code": "000001",
            "start_date": "2026-07-01",
            "end_date": "2026-07-02",
            "expected_rows": 2,
            "existing_rows": 1,
            "missing_rows": 1,
            "status": "missing",
            "source_table": "tushare_daily",
        }
    ]

    planned = plan_tushare_backfill_tasks(db_path, gap_rows)
    con = duckdb.connect(str(db_path))
    try:
        con.execute("UPDATE tushare_backfill_task SET status='done', rows_inserted=1 WHERE task_id=?", [planned[0]["task_id"]])
    finally:
        con.close()

    planned_again = plan_tushare_backfill_tasks(db_path, gap_rows)
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        status = con.execute("SELECT status FROM tushare_backfill_task WHERE task_id=?", [planned[0]["task_id"]]).fetchone()[0]
    finally:
        con.close()

    assert planned[0]["status"] == "pending"
    assert planned_again == []
    assert status == "done"
