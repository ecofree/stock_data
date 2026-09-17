
import duckdb

from base import DuckDBStore
from collect_index import sync_index_list_from_kline


def test_sync_index_list_from_real_kline_publishes_requested_date_only(tmp_path):
    db_path = tmp_path / "index.duckdb"
    con = duckdb.connect(str(db_path))
    con.execute(
        "CREATE TABLE index_kline("
        "date DATE,index_code VARCHAR,close DOUBLE,volume BIGINT,turnover BIGINT,"
        "change_pct DOUBLE,ktype VARCHAR)"
    )
    con.execute(
        "CREATE TABLE index_list("
        "date DATE,index_code VARCHAR,index_name VARCHAR,price DOUBLE,"
        "change_pct DOUBLE,change_amt DOUBLE,turnover BIGINT,volume BIGINT)"
    )
    con.execute(
        "INSERT INTO index_kline VALUES "
        "('2026-07-05','SH000001',4000,100,1000,-1,'D'),"
        "('2026-07-06','SH000001',4041.24,120,1432,1.03,'D')"
    )
    con.close()

    store = DuckDBStore(db_path)
    try:
        result = sync_index_list_from_kline(store, "2026-07-06", ["SH000001"])
        result_again = sync_index_list_from_kline(
            store, "2026-07-06", ["SH000001"]
        )
    finally:
        store.close()

    assert result == 1
    assert result_again == 1
    con = duckdb.connect(str(db_path))
    try:
        rows = con.execute(
            "SELECT CAST(date AS VARCHAR),index_code,index_name,price,"
            "round(change_amt,2) FROM index_list"
        ).fetchall()
    finally:
        con.close()
    assert rows == [("2026-07-06", "SH000001", "上证指数", 4041.24, 41.24)]

from trade_system.data_chain import assess_data_chains
from trade_system.quality import dedupe_table, find_duplicate_keys


def test_dedupe_table_archives_old_duplicate_rows(tmp_path):
    db_path = tmp_path / "sample.duckdb"
    con = duckdb.connect(str(db_path))
    con.execute(
        "CREATE TABLE market_mood("
        "date DATE, rise_count INTEGER, fetched_at TIMESTAMP, raw_json VARCHAR)"
    )
    con.execute(
        "INSERT INTO market_mood VALUES "
        "('2026-07-06', 100, '2026-07-06 09:00:00', '{\"old\":true}'), "
        "('2026-07-06', 200, '2026-07-06 15:00:00', '{\"new\":true}')"
    )
    con.close()

    result = dedupe_table(str(db_path), "market_mood", ["date"])

    assert result["removed_rows"] == 1
    assert find_duplicate_keys(str(db_path), "market_mood", ["date"])["duplicate_groups"] == 0
    con = duckdb.connect(str(db_path))
    kept = con.execute("SELECT rise_count FROM market_mood").fetchone()[0]
    archived = con.execute("SELECT rise_count FROM _dedupe_archive_market_mood").fetchone()[0]
    con.close()
    assert kept == 200
    assert archived == 100


def test_assess_data_chains_marks_missing_and_fallback_sources(tmp_path):
    db_path = tmp_path / "sample.duckdb"
    con = duckdb.connect(str(db_path))
    con.execute("CREATE TABLE sector_strength(date DATE, sector_code VARCHAR)")
    con.execute("INSERT INTO sector_strength VALUES ('2026-07-06', '801001')")
    con.execute("CREATE TABLE l2_realtime_all_boards(date DATE, stock_code VARCHAR)")
    con.execute("INSERT INTO l2_realtime_all_boards VALUES ('2026-07-06', '000001')")
    con.execute("CREATE TABLE l2_realtime_index_list(date DATE, index_code VARCHAR)")
    con.execute("INSERT INTO l2_realtime_index_list VALUES ('2026-07-06', 'SH000001')")
    con.execute("CREATE TABLE auction_bidding_anomaly(date DATE)")
    con.close()

    chains = assess_data_chains(str(db_path))

    by_name = {item["chain"]: item for item in chains}
    assert by_name["集合竞价"]["status"] == "missing"
    assert by_name["板块资金"]["status"] == "fallback"
    assert by_name["L2可用数据"]["status"] == "available"

def test_assess_data_chains_treats_realtime_index_list_as_index_source(tmp_path):
    db_path = tmp_path / "sample.duckdb"
    con = duckdb.connect(str(db_path))
    con.execute("CREATE TABLE l2_realtime_index_list(date DATE, index_code VARCHAR)")
    con.execute("INSERT INTO l2_realtime_index_list VALUES ('2026-07-06', 'SH000001')")
    con.close()

    chains = assess_data_chains(str(db_path))

    index_chain = next(item for item in chains if "index_kline" in item["required"])
    assert index_chain["status"] == "available"
    assert index_chain["required_present"] == ["l2_realtime_index_list"]


def test_assess_data_chains_marks_historical_rows_stale_for_requested_date(tmp_path):
    db_path = tmp_path / "stale.duckdb"
    con = duckdb.connect(str(db_path))
    con.execute("CREATE TABLE sector_capital(date DATE, sector_code VARCHAR)")
    con.execute("INSERT INTO sector_capital VALUES ('2026-07-08','801001')")
    con.close()

    chains = assess_data_chains(str(db_path), "2026-07-09")

    sector = next(item for item in chains if item["chain"] == "板块资金")
    assert sector["status"] == "stale"
    assert sector["latest_dates"]["sector_capital"] == "2026-07-08"
