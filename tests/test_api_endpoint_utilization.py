import duckdb

from base import DuckDBStore
from collect_index import (
    collect_index_full_info,
    collect_index_intraday,
    collect_index_kline,
    collect_index_list,
)
from collect_l2 import (
    collect_l2_realtime_index_trend,
    collect_l2_sector_intraday,
    collect_l2_sector_volume,
    collect_l2_stock_intraday,
    collect_l2_tick_history,
    collect_l2_tick_orders,
    collect_l2_tick_orders_all,
)
from collect_sector import (
    collect_sector_all_stocks,
    collect_sector_son_plates,
    collect_sector_strength_batch,
    collect_sector_sub_concepts,
)
from schema import init_schema
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
            "/l2/tick-history": {"data": [{"time": "09:31", "price": 10.1, "volume": 100, "direction": "buy"}]},
            "/l2/tick-orders": {"data": [{"time": "09:31", "price": 10.1, "volume": 100, "order_type": "buy"}]},
            "/l2/tick-orders-all": {"data": [{"time": "09:31", "price": 10.1, "volume": 100, "order_type": "buy"}]},
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
