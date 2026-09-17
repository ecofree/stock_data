import duckdb

from base import DuckDBStore
from schema import init_schema
from trade_system.normalize import build_normalized_views
from trade_system.integration.data_catalog import build_default_data_sources
from trade_system.xiaodefa_source import XiaodefaClient
from trade_system.tushare_store import (collect_tushare_adj_factor, collect_tushare_daily, collect_tushare_daily_basic, collect_tushare_index_daily, collect_tushare_stock_basic, collect_tushare_trade_cal, stock_code_to_ts_code)


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
    client = XiaodefaClient(token="secret", runner=FakeRunner({
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


def test_xiaodefa_normalizes_connection_settings():
    client = XiaodefaClient(token="  secret-token ", url=" https://t.xiaodefa.top/ ", runner=FakeRunner({}))
    assert client.token == "secret-token"
    assert client.url == "https://t.xiaodefa.top/"





def test_stock_code_to_ts_code_preserves_leading_zero_stock_codes():
    assert stock_code_to_ts_code("1") == "000001.SZ"
    assert stock_code_to_ts_code("66") == "000066.SZ"
    assert stock_code_to_ts_code("600793") == "600793.SH"


def test_tushare_collectors_write_staging_tables_idempotently(tmp_path):
    store, db_path = _store(tmp_path)
    client = XiaodefaClient(
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

    client = XiaodefaClient(token="secret", runner=FakeRunner({"daily": daily_payload}))
    try:
        inserted = collect_tushare_daily(client, store, ["000001"], "20250101", "20260709")
    finally:
        store.close()

    assert len(client.runner.calls) > 1
    assert inserted == len(client.runner.calls)
    assert _count(db_path, "tushare_daily") == inserted


def test_ohlc_views_read_raw_facts_without_copying_or_relabeling(tmp_path):
    store, db_path = _store(tmp_path)
    store.insert_rows("tushare_daily", [
        ("000001.SZ", "000001", "2026-07-09", 10.5, 1000, 1200, "hands", "thousand_yuan", "none", "tushare_relay"),
        ("000002.SZ", "000002", "2026-07-09", 8.1, None, None, "hands", "thousand_yuan", "none", "tushare"),
    ], ["ts_code", "stock_code", "date", "close", "volume", "turnover", "volume_unit", "amount_unit", "adjustment", "provider"])
    store.insert_rows("tushare_index_daily", [
        ("000001.SH", "SH000001", "2026-07-09", 3010, 20000, "thousand_yuan"),
        ("399001.SZ", "SZ399001", "2026-07-09", 9040, 22000, None),
    ], ["ts_code", "index_code", "date", "close", "turnover", "amount_unit"])
    store.close()
    for _ in range(2):
        build_normalized_views(db_path)
        with duckdb.connect(str(db_path), read_only=True) as con:
            assert con.execute("SELECT stock_code, close, volume, turnover, provider FROM v_kline_daily ORDER BY stock_code").fetchall() == [
                ("000001", 10.5, 100000, 1200000, "tushare_relay"), ("000002", 8.1, None, None, "tushare")]
            assert con.execute("SELECT index_code, close, turnover FROM v_index_state ORDER BY index_code").fetchall() == [
                ("SH000001", 3010, 20000000), ("SZ399001", 9040, None)]
            assert con.execute("SELECT volume, turnover, provider FROM tushare_daily WHERE stock_code='000001'").fetchone() == (1000, 1200, "tushare_relay")
            assert con.execute("SELECT COUNT(*) FROM kline").fetchone()[0] == 0
            assert con.execute("SELECT COUNT(*) FROM index_kline").fetchone()[0] == 0
            assert con.execute("SELECT COUNT(*) FROM multi_source_kline").fetchone()[0] == 0


def test_index_view_prefers_raw_and_keeps_uncovered_legacy_dates(tmp_path):
    store, db_path = _store(tmp_path)
    store.insert_rows("index_kline", [
        ("2026-07-08", "SH000001", 3000, "D"),
        ("2026-07-09", "SH000001", 2999, "D"),
    ], ["date", "index_code", "close", "ktype"])
    store.insert_rows("tushare_index_daily", [
        ("000001.SH", "SH000001", "2026-07-09", 3010),
    ], ["ts_code", "index_code", "date", "close"])
    store.close()
    build_normalized_views(db_path)
    with duckdb.connect(str(db_path), read_only=True) as con:
        assert con.execute("SELECT trade_date, close, source_table FROM v_index_state ORDER BY trade_date").fetchall() == [
            ("2026-07-08", 3000, "index_kline"), ("2026-07-09", 3010, "tushare_index_daily")]
        assert con.execute("SELECT close FROM index_kline ORDER BY date").fetchall() == [(3000,), (2999,)]


def test_retired_ohlc_and_close_commands_cannot_open_database(tmp_path, monkeypatch):
    import pytest
    import sys
    from scripts import sync_tushare_ohlc, refresh_close_signals
    db = tmp_path / "must-not-exist.duckdb"
    monkeypatch.setattr(sys, "argv", ["old-command", "--db", str(db)])
    for command in (sync_tushare_ohlc, refresh_close_signals):
        with pytest.raises(SystemExit, match="retired"):
            command.main()
    assert not db.exists()


def test_missing_and_nonfinite_raw_values_remain_null(tmp_path):
    store, db_path = _store(tmp_path)
    client = XiaodefaClient(token="secret", runner=FakeRunner({"index_daily": {
        "code": 0, "data": {"fields": ["ts_code", "trade_date", "open", "high", "low", "close", "vol", "amount", "pct_chg"],
        "items": [["000001.SH", "20260709", None, None, None, 3010, "nan", None, None]]}}}))
    try:
        collect_tushare_index_daily(client, store, ["SH000001"], "20260709", "20260709")
    finally:
        store.close()
    build_normalized_views(db_path)
    with duckdb.connect(str(db_path), read_only=True) as con:
        assert con.execute("SELECT open, volume, turnover FROM tushare_index_daily").fetchone() == (None, None, None)
        assert con.execute("SELECT close, turnover FROM v_index_state").fetchone() == (3010, None)


def test_data_catalog_lists_tushare_relay_basic_data_source():
    source_ids = {row["source_id"] for row in build_default_data_sources()}

    assert "tushare_relay_basic" in source_ids
