import duckdb
from datetime import datetime
import io
import json

import pytest

from trade_system.data_store import DuckDBStore
from trade_system import data_store as base
from collectors.collect_index import (
    collect_index_full_info,
    collect_index_intraday,
    collect_index_kline,
    collect_index_list,
)
from collectors.collect_l2 import (
    collect_l2_realtime_index_trend,
    collect_l2_sector_intraday,
    collect_l2_sector_volume,
    collect_l2_stock_intraday,
    collect_l2_tick_history,
    collect_l2_tick_orders,
    collect_l2_tick_orders_all,
)
from collectors.collect_sector import (
    collect_sector_all_stocks,
    collect_sector_son_plates,
    collect_sector_strength_batch,
    collect_sector_sub_concepts,
)
from trade_system.schema import init_schema
from trade_system.normalize import build_normalized_views


class FakeClient:
    def __init__(self, payloads):
        self.payloads = payloads
        self.calls = []

    def get(self, endpoint, params=None, **_kwargs):
        self.calls.append((endpoint, params or {}))
        value = self.payloads.get(endpoint)
        if callable(value):
            return value(params or {})
        return value


def _store(tmp_path):
    db_path = tmp_path / "endpoint_utilization.duckdb"
    store = DuckDBStore(str(db_path))
    init_schema(store.conn)
    return store, db_path


def _count(db_path, table):
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        return con.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0]
    finally:
        con.close()


def test_collect_l2_tick_history_and_orders_are_idempotent(tmp_path):
    store, db_path = _store(tmp_path)
    client = FakeClient(
        {
            "/l2/tick-history": {"date": "20260706", "data": [{"time": "09:31", "price": 10.1, "volume": 100, "direction": "buy"}]},
            "/l2/tick-orders": {"date": "20260706", "data": [{"time": "09:31", "price": 10.1, "volume": 100, "order_type": "buy"}]},
            "/l2/tick-orders-all": {"date": "20260706", "data": [{"time": "09:31", "price": 10.1, "volume": 100, "order_type": "buy"}]},
        }
    )
    try:
        assert collect_l2_tick_history(client, store, "2026-07-06", ["000001"]) == 1
        assert collect_l2_tick_orders(client, store, "2026-07-06", ["000001"]) == 1
        assert collect_l2_tick_orders_all(client, store, "2026-07-06", ["000001"]) == 1
        assert collect_l2_tick_history(client, store, "2026-07-06", ["000001"]) == 1
        assert collect_l2_tick_orders(client, store, "2026-07-06", ["000001"]) == 1
        assert collect_l2_tick_orders_all(client, store, "2026-07-06", ["000001"]) == 1
    finally:
        store.close()

    assert _count(db_path, "l2_tick_history") == 1
    assert _count(db_path, "l2_tick_orders") == 1
    assert _count(db_path, "l2_tick_orders_all") == 1


@pytest.mark.parametrize("endpoint,collector,table", [
    ("/l2/tick-history", collect_l2_tick_history, "l2_tick_history"),
    ("/l2/tick-orders", collect_l2_tick_orders, "l2_tick_orders"),
    ("/l2/tick-orders-all", collect_l2_tick_orders_all, "l2_tick_orders_all"),
])
def test_l2_preserves_full_response_before_withholding_same_second_events(tmp_path, endpoint, collector, table):
    store, _ = _store(tmp_path)
    payload = {
        "stock_code": "000001", "date": "20260706", "total_count": 2, "fetched_count": 3,
        "data": [
            {"time": "09:31", "order_id": "order-A", "price": 10.1, "volume": 100, "flag1": "2"},
            {"time": "09:31:00.200", "order_id": "order-B", "price": 10.1, "volume": 100, "flag1": "1"},
            {"time": "09:31:00", "price": 10.2, "volume": 200},
        ],
    }
    client = FakeClient({endpoint: payload})
    try:
        assert collector(client, store, "2026-07-06", ["000001"]) == 0
        assert store.fetchall(f"SELECT count(*) FROM {table}")[0][0] == 0
        saved_endpoint, raw = store.fetchall("SELECT endpoint,raw_json FROM raw_api_data")[0]
        receipt = json.loads(raw)
        assert saved_endpoint == endpoint
        assert receipt["response"] == payload
        assert "order_id" not in receipt["response"]["data"][2]
        assert receipt["request_params"] == {"code": "000001", "date": "2026-07-06"}
        assert receipt["requested_trade_date"] == "2026-07-06"
        assert receipt["source_trade_date"] == "20260706"
        requested = datetime.fromisoformat(receipt["requested_at"])
        observed = datetime.fromisoformat(receipt["response_observed_at"])
        assert requested.tzinfo is not None and observed.tzinfo is not None and observed >= requested
        assert receipt["qualification"]["pagination_complete"] is None
        assert receipt["qualification"]["same_definition_independent_funds_eligible"] is False
        assert "same_time_events_retained_in_raw" in store.fetchall("SELECT status FROM _collect_log")[-1][0]
        assert client.calls == [(endpoint, {"code": "000001", "date": "2026-07-06"})]
    finally:
        store.close()


@pytest.mark.parametrize("metadata,row_metadata,reason", [
    ({}, {}, "source_date_unverified"),
    ({"date": "20260707"}, {}, "response_date_mismatch"),
    ({"date": "20260706", "stock_code": "000002"}, {}, "response_security_mismatch"),
    ({"date": "not-a-date"}, {}, "invalid_response_date"),
    ({"date": "20260706"}, {"date": "20260707"}, "source_date_unverified_or_mismatch"),
    ({"date": "20260706"}, {"stock_code": "000002"}, "row_security_mismatch"),
])
def test_l2_never_assigns_requested_identity_or_day_to_conflicting_response(tmp_path, metadata, row_metadata, reason):
    store, _ = _store(tmp_path)
    payload = {**metadata, "data": [{"time": "09:31", "price": 10, "volume": 100, **row_metadata}]}
    try:
        assert collect_l2_tick_orders_all(FakeClient({"/l2/tick-orders-all": payload}), store, "2026-07-06", ["000001"]) == 0
        assert store.fetchall("SELECT count(*) FROM l2_tick_orders_all")[0][0] == 0
        receipt = json.loads(store.fetchall("SELECT raw_json FROM raw_api_data")[0][0])
        assert receipt["response"] == payload
        assert reason in store.fetchall("SELECT status FROM _collect_log")[-1][0]
    finally:
        store.close()


def test_l2_projection_conflict_keeps_historical_row_and_original_order_ids(tmp_path):
    store, _ = _store(tmp_path)
    store.conn.execute("INSERT INTO l2_tick_orders_all(date,stock_code,time,price,volume,order_type) "
                       "VALUES ('2026-07-06','000001','09:31:00',11,100,'buy')")
    payload = {"date": "20260706", "data": [
        {"time": "09:31:00", "order_id": "order-A", "price": 10, "volume": 100, "order_type": "buy"},
        {"time": "09:32:00", "order_id": "order-B", "price": 12, "volume": 200, "order_type": "sell"},
    ]}
    try:
        assert collect_l2_tick_orders_all(FakeClient({"/l2/tick-orders-all": payload}), store, "2026-07-06", ["000001"]) == 1
        assert store.fetchall("SELECT time,price FROM l2_tick_orders_all ORDER BY time") == [("09:31:00", 11), ("09:32:00", 12)]
        receipt = json.loads(store.fetchall("SELECT raw_json FROM raw_api_data")[0][0])
        assert receipt["response"] == payload
        assert "existing_projection_conflict_retained_in_raw" in store.fetchall("SELECT status FROM _collect_log")[-1][0]
    finally:
        store.close()


def test_l2_raw_persistence_precedes_projection_and_survives_projection_failure(tmp_path, monkeypatch):
    store, _ = _store(tmp_path)
    payload = {"date": "20260706", "data": [{"time": "09:31", "order_id": "order-A", "price": 10, "volume": 100}]}

    def fail_projection(*_args, **_kwargs):
        assert store.fetchall("SELECT count(*) FROM raw_api_data")[0][0] == 1
        raise RuntimeError("projection failed")

    monkeypatch.setattr(store, "insert_rows", fail_projection)
    try:
        with pytest.raises(RuntimeError, match="projection failed"):
            collect_l2_tick_orders(FakeClient({"/l2/tick-orders": payload}), store, "2026-07-06", ["000001"])
        assert json.loads(store.fetchall("SELECT raw_json FROM raw_api_data")[0][0])["response"] == payload
        assert store.fetchall("SELECT count(*) FROM l2_tick_orders")[0][0] == 0
        assert store.fetchall("SELECT rows_inserted,status FROM _collect_log")[-1] == (0, "projection_failed_raw_retained")
    finally:
        store.close()


@pytest.mark.parametrize("write_limit", [0, 1])
def test_l2_reports_actual_projection_write_count_when_writer_accepts_fewer_rows(tmp_path, monkeypatch, write_limit):
    store, _ = _store(tmp_path)
    payload = {"date": "20260706", "data": [
        {"time": "09:31", "order_id": "order-A", "price": 10, "volume": 100},
        {"time": "09:32", "order_id": "order-B", "price": 10, "volume": 100},
    ]}
    original_insert = store.insert_rows

    def partial_insert(table, rows, columns, **kwargs):
        return original_insert(table, rows[:write_limit], columns, **kwargs)

    monkeypatch.setattr(store, "insert_rows", partial_insert)
    try:
        assert collect_l2_tick_orders(FakeClient({"/l2/tick-orders": payload}), store, "2026-07-06", ["000001"]) == write_limit
        assert store.fetchall("SELECT count(*) FROM l2_tick_orders")[0][0] == write_limit
        written, status = store.fetchall("SELECT rows_inserted,status FROM _collect_log")[-1]
        assert written == write_limit
        assert "projection_write_incomplete_raw_retained" in status
        assert json.loads(store.fetchall("SELECT raw_json FROM raw_api_data")[0][0])["response"] == payload
    finally:
        store.close()


def test_l2_idless_return_stays_idless_and_invalid_numeric_fields_stay_raw(tmp_path):
    store, _ = _store(tmp_path)
    payload = {"date": "20260706", "data": [
        {"time": "09:31", "price": 10, "volume": 100},
        {"time": "09:32", "volume": 100},
        {"time": "09:33", "price": 10, "volume": 1.5},
    ]}
    try:
        assert collect_l2_tick_orders(FakeClient({"/l2/tick-orders": payload}), store, "2026-07-06", ["000001"]) == 1
        receipt = json.loads(store.fetchall("SELECT raw_json FROM raw_api_data")[0][0])
        assert receipt["response"] == payload
        assert all("order_id" not in row for row in receipt["response"]["data"])
        assert store.fetchall("SELECT time,price,volume,order_type FROM l2_tick_orders") == [("09:31:00", 10, 100, None)]
    finally:
        store.close()


@pytest.mark.parametrize("endpoint,collector,table", [
    ("/l2/tick-history", collect_l2_tick_history, "l2_tick_history"),
    ("/l2/tick-orders", collect_l2_tick_orders, "l2_tick_orders"),
    ("/l2/tick-orders-all", collect_l2_tick_orders_all, "l2_tick_orders_all"),
])
@pytest.mark.parametrize("source_date,expected_count", [("20260707", 0), (None, 0), ("20260706", 1)])
def test_real_l2_client_preserves_decoded_payload_before_date_validation(
    tmp_path, monkeypatch, endpoint, collector, table, source_date, expected_count,
):
    store, _ = _store(tmp_path)
    payload = {"stock_code": "000001", "data": [
        {"time": "09:31", "order_id": "provider-order-A", "price": 10, "volume": 100},
    ]}
    if source_date is not None:
        payload["date"] = source_date
    calls = []

    def response(*_args, **_kwargs):
        calls.append(1)
        return io.BytesIO(json.dumps(payload).encode("utf-8"))

    monkeypatch.setattr(base, "open_verified", response)
    monkeypatch.setattr(base.shared_host_limiter, "acquire", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(base, "API_KEY", "synthetic-header-key-must-not-be-retained")
    client = base.KPLClient(max_attempts=5, total_budget_seconds=30)
    try:
        assert collector(client, store, "2026-07-06", ["000001"]) == expected_count
        assert calls == [1]
        assert store.fetchall(f"SELECT count(*) FROM {table}")[0][0] == expected_count
        raw_rows = store.fetchall("SELECT raw_json FROM raw_api_data")
        assert len(raw_rows) == 1  # The collector must not save a successful callback twice.
        receipt = json.loads(raw_rows[0][0])
        assert receipt["response"] == payload
        assert receipt["request_params"] == {"code": "000001", "date": "2026-07-06"}
        assert receipt["arrival_time_basis"] == "after_client_json_decode_before_semantic_validation"
        assert datetime.fromisoformat(receipt["requested_at"]).tzinfo is not None
        assert datetime.fromisoformat(receipt["response_observed_at"]).tzinfo is not None
        assert "synthetic-header-key-must-not-be-retained" not in raw_rows[0][0]
        assert receipt["qualification"]["same_definition_independent_funds_eligible"] is False
        status = store.fetchall("SELECT status FROM _collect_log")[-1][0]
        if source_date == "20260707":
            assert client.stats["semantic_error"] == 1
            assert client.stats["success"] == 0
            assert status == "semantic_response_rejected_raw_retained"
        elif source_date is None:
            assert "source_date_unverified" in status
    finally:
        store.close()


def test_real_l2_client_raw_persistence_failure_stops_before_retry_or_projection(tmp_path, monkeypatch):
    store, _ = _store(tmp_path)
    payload = {"date": "20260706", "data": [
        {"time": "09:31", "order_id": "provider-order-A", "price": 10, "volume": 100},
    ]}
    calls = []

    def response(*_args, **_kwargs):
        calls.append(1)
        return io.BytesIO(json.dumps(payload).encode("utf-8"))

    def fail_raw(*_args, **_kwargs):
        raise OSError("isolated evidence persistence failed")

    def forbidden_retry(*_args, **_kwargs):
        raise AssertionError("raw persistence failure must not retry")

    monkeypatch.setattr(base, "open_verified", response)
    monkeypatch.setattr(base.shared_host_limiter, "acquire", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(store, "insert_raw", fail_raw)
    client = base.KPLClient(max_attempts=5, total_budget_seconds=30)
    monkeypatch.setattr(client, "_sleep_retry", forbidden_retry)
    try:
        with pytest.raises(base.KPLDecodedObserverError, match="evidence persistence failed") as raised:
            collect_l2_tick_orders(client, store, "2026-07-06", ["000001", "000002"])
        assert isinstance(raised.value.__cause__, OSError)
        assert calls == [1]
        assert store.fetchall(
            "SELECT count(*) FROM information_schema.tables WHERE table_name='raw_api_data'"
        )[0][0] == 0
        assert store.fetchall("SELECT count(*) FROM l2_tick_orders")[0][0] == 0
        assert client.stats["success"] == 0
        assert client._circuit_open_reason == "decoded_observer_failed"
        assert client.get("/l2/tick-orders", {"code": "000002", "date": "2026-07-06"}) is None
        assert calls == [1]
    finally:
        store.close()


def test_collect_l2_index_trend_and_sector_volume(tmp_path):
    store, db_path = _store(tmp_path)
    client = FakeClient(
        {
            "/l2/realtime/index-trend": {"data": [{"index_code": "SH000001", "time": "09:31", "price": 3200.5, "volume": 1000}]},
            "/l2/sector-volume": {"data": [{"time": "09:31", "volume": 888, "turnover": 9999}]},
        }
    )
    try:
        assert collect_l2_realtime_index_trend(client, store, "2026-07-06") == 1
        assert collect_l2_sector_volume(client, store, "2026-07-06", ["801001"]) == 1
    finally:
        store.close()

    assert _count(db_path, "l2_realtime_index_trend") == 1
    assert _count(db_path, "l2_sector_volume") == 1


def test_intraday_collectors_reject_mismatched_response_date(tmp_path):
    store, db_path = _store(tmp_path)
    client = FakeClient(
        {
            "/l2/stock-intraday": {
                "date": "20260708",
                "data": [{"time": "09:31", "price": 10.0}],
            },
            "/l2/sector-intraday": {
                "data": [{"datetime": "2026-07-08 09:31", "price": 100.0}],
            },
        }
    )
    try:
        assert collect_l2_stock_intraday(client, store, "2026-07-09", ["000001"]) == 0
        assert collect_l2_sector_intraday(client, store, "2026-07-09", ["801001"]) == 0
    finally:
        store.close()

    assert _count(db_path, "l2_stock_intraday") == 0
    assert _count(db_path, "l2_sector_intraday") == 0


def test_collect_sector_hierarchy_and_batch_strength(tmp_path):
    store, db_path = _store(tmp_path)
    client = FakeClient(
        {
            "/sector/all-stocks": {"stocks": [{"code": "000001", "name": "Alpha"}]},
            "/sector/son-plates": {"plates": [{"code": "801002", "name": "Sub"}]},
            "/sector/sub-concepts": {"concepts": [{"code": "C001", "name": "Concept"}]},
            "/sector/strength-batch": {"data": [{"code": "801001", "strength": 88.5}]},
        }
    )
    try:
        assert collect_sector_all_stocks(client, store, "2026-07-06", ["801001"]) == 1
        assert collect_sector_son_plates(client, store, "2026-07-06", ["801001"]) == 1
        assert collect_sector_sub_concepts(client, store, "2026-07-06", ["801001"]) == 1
        assert collect_sector_strength_batch(client, store, "2026-07-06", ["801001"]) == 1
        assert collect_sector_all_stocks(client, store, "2026-07-06", ["801001"]) == 1
    finally:
        store.close()

    assert _count(db_path, "sector_all_stocks") == 1
    assert _count(db_path, "sector_son_plates") == 1
    assert _count(db_path, "sector_sub_concepts") == 1
    assert _count(db_path, "sector_strength_batch") == 1


def test_collect_index_chain_persists_real_index_sources(tmp_path):
    store, db_path = _store(tmp_path)
    client = FakeClient(
        {
            "/index/list": {"indexes": [{"code": "SH000001", "name": "SSE", "price": 3200.5, "change_pct": 1.2, "turnover": 10000, "volume": 200}]},
            "/index/intraday": {"data": [{"time": "09:31", "price": 3201.0, "avg_price": 3200.7, "volume": 10, "turnover": 20}]},
            "/index/full-info": {"status": "ok", "name": "SSE"},
            "/index/zhishu-kline": {"data": [{"date": "20260706", "open": 3190, "high": 3220, "low": 3180, "close": 3200, "volume": 100, "turnover": 200, "change_pct": 1.1}]},
        }
    )
    try:
        assert collect_index_list(client, store, "2026-07-06") == 1
        assert collect_index_intraday(client, store, "2026-07-06", ["SH000001"]) == 1
        assert collect_index_full_info(client, store, "2026-07-06", ["SH000001"]) == 1
        assert collect_index_kline(client, store, "2026-07-06", ["SH000001"]) == 1
        assert collect_index_kline(client, store, "2026-07-06", ["SH000001"]) == 1
    finally:
        store.close()

    assert _count(db_path, "index_list") == 1
    assert _count(db_path, "index_intraday") == 1
    assert _count(db_path, "index_full_info") == 1
    assert _count(db_path, "index_kline") == 1


def test_normalized_views_expose_intraday_and_theme_evidence(tmp_path):
    db_path = tmp_path / "views.duckdb"
    con = duckdb.connect(str(db_path))
    con.execute("CREATE TABLE l2_stock_intraday(date DATE, stock_code VARCHAR, main_fund_net DOUBLE, turnover BIGINT)")
    con.execute("INSERT INTO l2_stock_intraday VALUES ('2026-07-06','000001',1000,2000)")
    con.execute("CREATE TABLE l2_stock_bigorder(date DATE, stock_code VARCHAR, big_net_amount DOUBLE)")
    con.execute("INSERT INTO l2_stock_bigorder VALUES ('2026-07-06','000001',3000)")
    con.execute("CREATE TABLE l2_tick_history(date DATE, stock_code VARCHAR, time VARCHAR, volume BIGINT)")
    con.execute("INSERT INTO l2_tick_history VALUES ('2026-07-06','000001','09:31',100)")
    con.execute("CREATE TABLE l2_tick_orders(date DATE, stock_code VARCHAR, time VARCHAR, volume BIGINT)")
    con.execute("INSERT INTO l2_tick_orders VALUES ('2026-07-06','000001','09:31',200)")
    con.execute("CREATE TABLE l2_tick_orders_all(date DATE, stock_code VARCHAR, time VARCHAR, volume BIGINT)")
    con.execute("INSERT INTO l2_tick_orders_all VALUES ('2026-07-06','000001','09:31',300)")
    con.execute("CREATE TABLE advanced_pankou(date DATE, stock_code VARCHAR, buy1_volume BIGINT, sell1_volume BIGINT)")
    con.execute("INSERT INTO advanced_pankou VALUES ('2026-07-06','000001',500,250)")
    con.execute("CREATE TABLE sector_strength(date DATE, sector_code VARCHAR, strength_value DOUBLE, zhangting INTEGER, fengban_rate DOUBLE, up_count INTEGER, down_count INTEGER)")
    con.execute("INSERT INTO sector_strength VALUES ('2026-07-06','801001',88,3,70,10,2)")
    con.execute("CREATE TABLE sector_ranking(date DATE, sector_code VARCHAR, sector_name VARCHAR, stock_count INTEGER)")
    con.execute("INSERT INTO sector_ranking VALUES ('2026-07-06','801001','Theme',5)")
    con.execute("CREATE TABLE sector_capital(date DATE, sector_code VARCHAR, main_net_inflow BIGINT, super_net_inflow BIGINT, big_net_inflow BIGINT, mid_net_inflow BIGINT, small_net_inflow BIGINT)")
    con.execute("INSERT INTO sector_capital VALUES ('2026-07-06','801001',100000000,1,2,3,4)")
    con.execute("CREATE TABLE sector_all_stocks(date DATE, sector_code VARCHAR, stock_code VARCHAR)")
    con.execute("INSERT INTO sector_all_stocks VALUES ('2026-07-06','801001','000001')")
    con.execute("CREATE TABLE sector_son_plates(parent_code VARCHAR, son_code VARCHAR)")
    con.execute("INSERT INTO sector_son_plates VALUES ('801001','801002')")
    con.execute("CREATE TABLE sector_sub_concepts(sector_code VARCHAR, concept_code VARCHAR)")
    con.execute("INSERT INTO sector_sub_concepts VALUES ('801001','C001')")
    con.execute("CREATE TABLE sector_boom_reason(date DATE, sector_code VARCHAR, reason VARCHAR)")
    con.execute("INSERT INTO sector_boom_reason VALUES ('2026-07-06','801001','policy catalyst')")
    con.close()

    views = build_normalized_views(db_path)

    con = duckdb.connect(str(db_path), read_only=True)
    try:
        assert "v_intraday_strength_evidence" in views
        assert "v_theme_mainline_evidence" in views
        intraday = con.execute(
            "SELECT tick_rows, tick_order_rows, tick_all_rows, pankou_net_volume FROM v_intraday_strength_evidence"
        ).fetchone()
        theme = con.execute(
            "SELECT component_count, son_plate_count, sub_concept_count, boom_reason FROM v_theme_mainline_evidence"
        ).fetchone()
    finally:
        con.close()

    assert intraday == (1, 1, 1, 250)
    assert theme == (1, 1, 1, "policy catalyst")


def test_normalized_views_expose_research_and_lhb_evidence(tmp_path):
    db_path = tmp_path / "research_lhb.duckdb"
    con = duckdb.connect(str(db_path))
    con.execute("CREATE TABLE advanced_news_flash(date DATE, time VARCHAR, news_title VARCHAR, news_source VARCHAR, news_url VARCHAR)")
    con.execute("INSERT INTO advanced_news_flash VALUES ('2026-07-06','09:45','AI catalyst','wire','https://example.test/a')")
    con.execute("CREATE TABLE news_plate(date DATE, sector_code VARCHAR, news_title VARCHAR, news_url VARCHAR, news_source VARCHAR)")
    con.execute("INSERT INTO news_plate VALUES ('2026-07-06','801001','sector event','https://example.test/b','plate')")
    con.execute("CREATE TABLE advanced_corporate_news(date DATE, stock_code VARCHAR, news_title VARCHAR, news_type VARCHAR)")
    con.execute("INSERT INTO advanced_corporate_news VALUES ('2026-07-06','000001','company risk','risk')")
    con.execute("CREATE TABLE lhb_list(date DATE, stock_code VARCHAR, stock_name VARCHAR, reason VARCHAR, buy_amount BIGINT, sell_amount BIGINT, net_amount BIGINT)")
    con.execute("INSERT INTO lhb_list VALUES ('2026-07-06','000001','Alpha','daily limit',1000,300,700)")
    con.execute("CREATE TABLE lhb_detail(date DATE, stock_code VARCHAR, broker_name VARCHAR, buy_amount BIGINT, sell_amount BIGINT, net_amount BIGINT)")
    con.execute("INSERT INTO lhb_detail VALUES ('2026-07-06','000001','broker',600,100,500)")
    con.execute("CREATE TABLE lhb_youzi_dongxiang(date DATE, broker_name VARCHAR, stock_code VARCHAR, stock_name VARCHAR, buy_amount BIGINT, sell_amount BIGINT)")
    con.execute("INSERT INTO lhb_youzi_dongxiang VALUES ('2026-07-06','hot money','000001','Alpha',400,50)")
    con.execute("CREATE TABLE advanced_on_the_lhb(date DATE, stock_code VARCHAR, stock_name VARCHAR, probability DOUBLE)")
    con.execute("INSERT INTO advanced_on_the_lhb VALUES ('2026-07-06','000001','Alpha',80)")
    con.close()

    views = build_normalized_views(db_path)

    con = duckdb.connect(str(db_path), read_only=True)
    try:
        research_count = con.execute("SELECT count(*) FROM v_research_event_evidence").fetchone()[0]
        lhb = con.execute(
            "SELECT stock_code, broker_count, youzi_buy_amount, on_lhb_probability FROM v_lhb_review_evidence"
        ).fetchone()
    finally:
        con.close()

    assert "v_research_event_evidence" in views
    assert "v_lhb_review_evidence" in views
    assert research_count == 3
    assert lhb == ("000001", 1, 400, 80)
