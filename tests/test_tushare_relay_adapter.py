import duckdb

from base import DuckDBStore
from schema import init_schema
from trade_system.integration.data_catalog import build_default_data_sources
from trade_system.tushare_relay import (
    TushareRelayClient,
    collect_tushare_adj_factor,
    collect_tushare_daily,
    collect_tushare_daily_basic,
    collect_tushare_index_daily,
    collect_tushare_stock_basic,
    collect_tushare_trade_cal,
    stock_code_to_ts_code,
    sync_tushare_ohlc_to_core_tables,
)


class FakeRunner:
    def __init__(self, payloads):
        self.payloads = payloads
        self.calls = []

    def __call__(self, body, timeout):
        self.calls.append((body, timeout))
        value = self.payloads[body["api_name"]]
        if callable(value):
            return value(body)
        return value


def _store(tmp_path):
    db_path = tmp_path / "tushare_relay.duckdb"
    store = DuckDBStore(str(db_path))
    init_schema(store.conn)
    return store, db_path


def _count(db_path, table):
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        return con.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0]
    finally:
        con.close()


def test_tushare_relay_client_maps_fields_items_to_dicts():
    client = TushareRelayClient(token="secret", runner=FakeRunner({
        "daily": {
            "code": 0,
            "msg": "ok",
            "data": {
                "fields": ["ts_code", "trade_date", "close"],
                "items": [["000001.SZ", "20260709", 12.34]],
            },
        }
    }))

    rows = client.query_rows(
        "daily",
        {"ts_code": "000001.SZ", "start_date": "20260709", "end_date": "20260709"},
        fields="ts_code,trade_date,close",
    )

    assert rows == [{"ts_code": "000001.SZ", "trade_date": "20260709", "close": 12.34}]


def test_tushare_relay_normalizes_whitespace_in_connection_settings():
    client = TushareRelayClient(
        token="  secret-token \n",
        url=" https://relay.example.test/ ",
        resolve=" 1.2.3.4:443 ",
        runner=FakeRunner({}),
    )

    assert client.token == "secret-token"
    assert client.url == "https://relay.example.test/"
    assert client.resolve == "1.2.3.4:443"


def test_tushare_relay_test_runner_defaults_to_no_sleep():
    client = TushareRelayClient(token="secret", runner=FakeRunner({}))

    assert client.min_interval_seconds == 0


def test_stock_code_to_ts_code_preserves_leading_zero_stock_codes():
    assert stock_code_to_ts_code("1") == "000001.SZ"
    assert stock_code_to_ts_code("66") == "000066.SZ"
    assert stock_code_to_ts_code("600793") == "600793.SH"


def test_tushare_collectors_write_staging_tables_idempotently(tmp_path):
    store, db_path = _store(tmp_path)
    client = TushareRelayClient(
        token="secret",
        runner=FakeRunner(
            {
                "trade_cal": {
                    "code": 0,
                    "data": {
                        "fields": ["exchange", "cal_date", "is_open", "pretrade_date"],
                        "items": [["SSE", "20260709", 1, "20260708"]],
                    },
                },
                "stock_basic": {
                    "code": 0,
                    "data": {
                        "fields": ["ts_code", "symbol", "name", "area", "industry", "market", "list_date"],
                        "items": [["000001.SZ", "000001", "Ping An Bank", "Shenzhen", "Bank", "主板", "19910403"]],
                    },
                },
                "daily": {
                    "code": 0,
                    "data": {
                        "fields": ["ts_code", "trade_date", "open", "high", "low", "close", "vol", "amount", "pct_chg"],
                        "items": [["000001.SZ", "20260709", 10, 11, 9.5, 10.5, 1000, 1200, 2.1]],
                    },
                },
                "daily_basic": {
                    "code": 0,
                    "data": {
                        "fields": ["ts_code", "trade_date", "turnover_rate", "volume_ratio", "pe", "pb", "total_mv", "circ_mv"],
                        "items": [["000001.SZ", "20260709", 1.2, 1.5, 8.8, 0.9, 1000000, 800000]],
                    },
                },
                "adj_factor": {
                    "code": 0,
                    "data": {
                        "fields": ["ts_code", "trade_date", "adj_factor"],
                        "items": [["000001.SZ", "20260709", 123.45]],
                    },
                },
                "index_daily": {
                    "code": 0,
                    "data": {
                        "fields": ["ts_code", "trade_date", "open", "high", "low", "close", "vol", "amount", "pct_chg"],
                        "items": [["000001.SH", "20260709", 3000, 3020, 2990, 3010, 10000, 20000, 0.5]],
                    },
                },
            }
        ),
    )

    try:
        assert collect_tushare_trade_cal(client, store, "20260701", "20260709") == 1
        assert collect_tushare_stock_basic(client, store) == 1
        assert collect_tushare_daily(client, store, ["000001"], "20260701", "20260709") == 1
        assert collect_tushare_daily_basic(client, store, ["000001"], "20260701", "20260709") == 1
        assert collect_tushare_adj_factor(client, store, ["000001"], "20260701", "20260709") == 1
        assert collect_tushare_index_daily(client, store, ["SH000001"], "20260701", "20260709") == 1

        assert collect_tushare_daily(client, store, ["000001"], "20260701", "20260709") == 1
        assert collect_tushare_index_daily(client, store, ["SH000001"], "20260701", "20260709") == 1
    finally:
        store.close()

    assert _count(db_path, "tushare_trade_cal") == 1
    assert _count(db_path, "tushare_stock_basic") == 1
    assert _count(db_path, "tushare_daily") == 1
    assert _count(db_path, "tushare_daily_basic") == 1
    assert _count(db_path, "tushare_adj_factor") == 1
    assert _count(db_path, "tushare_index_daily") == 1


def test_tushare_daily_collector_splits_long_date_ranges(tmp_path):
    store, db_path = _store(tmp_path)

    def daily_payload(body):
        params = body["params"]
        return {
            "code": 0,
            "data": {
                "fields": ["ts_code", "trade_date", "open", "high", "low", "close", "vol", "amount", "pct_chg"],
                "items": [
                    ["000001.SZ", params["start_date"], 10, 11, 9.5, 10.5, 1000, 1200, 2.1]
                ],
            },
        }

    client = TushareRelayClient(token="secret", runner=FakeRunner({"daily": daily_payload}))
    try:
        inserted = collect_tushare_daily(client, store, ["000001"], "20250101", "20260709")
    finally:
        store.close()

    assert len(client.runner.calls) > 1
    assert inserted == len(client.runner.calls)
    assert _count(db_path, "tushare_daily") == inserted


def test_tushare_ohlc_can_sync_to_core_kline_tables(tmp_path):
    store, db_path = _store(tmp_path)
    store.insert_rows(
        "tushare_daily",
        [("000001.SZ", "000001", "2026-07-09", 10, 11, 9.5, 10.5, 1000, 1200, 2.1)],
        ["ts_code", "stock_code", "date", "open", "high", "low", "close", "volume", "turnover", "change_pct"],
        replace_on=["ts_code", "date"],
    )
    store.insert_rows(
        "tushare_index_daily",
        [("000001.SH", "SH000001", "2026-07-09", 3000, 3020, 2990, 3010, 10000, 20000, 0.5)],
        ["ts_code", "index_code", "date", "open", "high", "low", "close", "volume", "turnover", "change_pct"],
        replace_on=["ts_code", "date"],
    )
    store.close()

    result = sync_tushare_ohlc_to_core_tables(db_path)

    # Production adds business-key unique indexes.  Re-running the same close
    # recovery must update/merge in place rather than DELETE+INSERT and collide
    # with DuckDB's unique-index constraint inside one transaction.
    con = duckdb.connect(str(db_path))
    try:
        con.execute(
            "CREATE UNIQUE INDEX uq_test_kline_business "
            "ON kline(date,stock_code,ktype)"
        )
        con.execute(
            "CREATE UNIQUE INDEX uq_test_index_kline_business "
            "ON index_kline(date,index_code,ktype)"
        )
    finally:
        con.close()
    rerun = sync_tushare_ohlc_to_core_tables(db_path)

    con = duckdb.connect(str(db_path), read_only=True)
    try:
        stock_row = con.execute(
            "SELECT CAST(date AS VARCHAR), stock_code, close, ktype FROM kline WHERE stock_code='000001'"
        ).fetchone()
        index_row = con.execute(
            "SELECT CAST(date AS VARCHAR), index_code, close, ktype FROM index_kline WHERE index_code='SH000001'"
        ).fetchone()
    finally:
        con.close()

    assert result == {"kline": 1, "index_kline": 1}
    assert rerun == result
    assert stock_row == ("2026-07-09", "000001", 10.5, "D")
    assert index_row == ("2026-07-09", "SH000001", 3010.0, "D")


def test_tushare_ohlc_sync_can_be_scoped_by_code_and_date(tmp_path):
    store, db_path = _store(tmp_path)
    store.insert_rows(
        "tushare_daily",
        [
            ("000001.SZ", "000001", "2026-07-08", 10, 11, 9.5, 10.5, 1000, 1200, 2.1),
            ("000001.SZ", "000001", "2026-07-09", 10.5, 12, 10.4, 11.8, 2000, 2400, 4.2),
            ("000002.SZ", "000002", "2026-07-09", 8, 8.2, 7.9, 8.1, 3000, 3300, 1.0),
        ],
        ["ts_code", "stock_code", "date", "open", "high", "low", "close", "volume", "turnover", "change_pct"],
        replace_on=["ts_code", "date"],
    )
    store.insert_rows(
        "tushare_index_daily",
        [
            ("000001.SH", "SH000001", "2026-07-08", 3000, 3020, 2990, 3010, 10000, 20000, 0.5),
            ("399001.SZ", "SZ399001", "2026-07-09", 9000, 9050, 8950, 9040, 12000, 22000, 0.8),
        ],
        ["ts_code", "index_code", "date", "open", "high", "low", "close", "volume", "turnover", "change_pct"],
        replace_on=["ts_code", "date"],
    )
    store.close()

    result = sync_tushare_ohlc_to_core_tables(
        db_path,
        stock_codes=["000001"],
        index_codes=["SZ399001"],
        start_date="20260709",
        end_date="20260709",
    )

    con = duckdb.connect(str(db_path), read_only=True)
    try:
        stock_rows = con.execute("SELECT stock_code, CAST(date AS VARCHAR), close FROM kline ORDER BY stock_code, date").fetchall()
        index_rows = con.execute("SELECT index_code, CAST(date AS VARCHAR), close FROM index_kline ORDER BY index_code, date").fetchall()
    finally:
        con.close()

    assert result == {"kline": 1, "index_kline": 1}
    assert stock_rows == [("000001", "2026-07-09", 11.8)]
    assert index_rows == [("SZ399001", "2026-07-09", 9040.0)]


def test_tushare_ohlc_sync_keeps_existing_core_rows_when_source_is_empty(tmp_path):
    store, db_path = _store(tmp_path)
    store.insert_rows(
        "kline",
        [("2026-07-09", "000001", 10, 11, 9.5, 10.5, 1000, 1200, 2.1, "D")],
        [
            "date", "stock_code", "open", "high", "low", "close",
            "volume", "turnover", "change_pct", "ktype",
        ],
    )
    store.insert_rows(
        "index_kline",
        [("2026-07-09", "SH000001", 3000, 3020, 2990, 3010, 10000, 20000, 0.5, "D")],
        [
            "date", "index_code", "open", "high", "low", "close",
            "volume", "turnover", "change_pct", "ktype",
        ],
    )
    store.close()

    result = sync_tushare_ohlc_to_core_tables(
        db_path, start_date="20260709", end_date="20260709"
    )

    con = duckdb.connect(str(db_path), read_only=True)
    try:
        stock_rows = con.execute(
            "SELECT stock_code, close, ktype FROM kline"
        ).fetchall()
        index_rows = con.execute(
            "SELECT index_code, close, ktype FROM index_kline"
        ).fetchall()
    finally:
        con.close()

    assert result == {"kline": 0, "index_kline": 0}
    assert stock_rows == [("000001", 10.5, "D")]
    assert index_rows == [("SH000001", 3010.0, "D")]


def test_data_catalog_lists_tushare_relay_basic_data_source():
    source_ids = {row["source_id"] for row in build_default_data_sources()}

    assert "tushare_relay_basic" in source_ids
