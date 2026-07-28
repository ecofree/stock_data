import duckdb

from base import DuckDBStore
from scripts.collect_professional_sources import select_stock_codes_for_professional_collection


def test_select_stock_codes_prioritizes_candidates_limit_pool_then_sector_stocks(tmp_path):
    db_path = tmp_path / "select.duckdb"
    con = duckdb.connect(str(db_path))
    con.execute(
        """
        CREATE TABLE stock_candidate_stage_signal (
            trade_date VARCHAR, stage VARCHAR, stock_code VARCHAR, stock_name VARCHAR, score DOUBLE, decision VARCHAR
        )
        """
    )
    con.execute(
        """
        INSERT INTO stock_candidate_stage_signal VALUES
        ('2026-07-06','premarket_pool','000001','候选A',90,'pool'),
        ('2026-07-06','premarket_pool','000002','候选B',80,'pool')
        """
    )
    con.execute(
        """
        CREATE VIEW v_limit_pool AS
        SELECT '2026-07-06' AS trade_date, 1 AS board_level, '000003' AS stock_code, '涨停C' AS stock_name, '09:31' AS limit_up_time
        """
    )
    con.execute("CREATE TABLE sector_stocks(date DATE, sector_code VARCHAR, stock_code VARCHAR)")
    con.execute("INSERT INTO sector_stocks VALUES ('2026-07-06','801001','000004')")
    con.close()

    store = DuckDBStore(str(db_path))
    try:
        codes = select_stock_codes_for_professional_collection(store, "2026-07-06", limit=4)
    finally:
        store.close()

    assert codes == ["000001", "000002", "000003", "000004"]
