import duckdb

from base import DuckDBStore
from collect_advanced_stock import (
    collect_advanced_dadan_kline,
    collect_advanced_main_activity_kline,
    collect_advanced_main_monitor,
    collect_advanced_zjmm_min,
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
    db_path = tmp_path / "intraday_capital_flow.duckdb"
    store = DuckDBStore(str(db_path))
    init_schema(store.conn)
    return store, db_path


def _count(db_path, table):
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        return con.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0]
    finally:
        con.close()


def test_advanced_intraday_capital_collectors_are_idempotent(tmp_path):
    store, db_path = _store(tmp_path)
    client = FakeClient(
        {
            "/advanced/zjmm-min": {
                "data": [
                    {
                        "time": "09:31",
                        "main_net_inflow": 1_000_000,
                        "super_net_inflow": 300_000,
                        "big_net_inflow": 500_000,
                    }
                ]
            },
            "/advanced/dadan-kline": {"data": [{"big_net_amount": 2_000_000, "ktype": "1"}]},
            "/advanced/main-activity-kline": {"data": [{"main_activity_score": 73.5, "ktype": "1"}]},
            "/advanced/main-monitor": {
                "data": [{"stock_name": "Alpha", "main_net_inflow": 3_000_000, "ranking": 1}]
            },
        }
    )
    try:
        assert collect_advanced_zjmm_min(client, store, "2026-07-09", ["000001"]) == 1
        assert collect_advanced_dadan_kline(client, store, "2026-07-09", ["000001"]) == 1
        assert collect_advanced_main_activity_kline(client, store, "2026-07-09", ["000001"]) == 1
        assert collect_advanced_main_monitor(client, store, "2026-07-09", ["000001"]) == 1

        assert collect_advanced_zjmm_min(client, store, "2026-07-09", ["000001"]) == 1
        assert collect_advanced_dadan_kline(client, store, "2026-07-09", ["000001"]) == 1
        assert collect_advanced_main_activity_kline(client, store, "2026-07-09", ["000001"]) == 1
        assert collect_advanced_main_monitor(client, store, "2026-07-09", ["000001"]) == 1
    finally:
        store.close()

    assert _count(db_path, "advanced_zjmm_min") == 1
    assert _count(db_path, "advanced_dadan_kline") == 1
    assert _count(db_path, "advanced_main_activity_kline") == 1
    assert _count(db_path, "advanced_main_monitor") == 1


def test_advanced_intraday_collectors_parse_kpl_list_shapes(tmp_path):
    store, db_path = _store(tmp_path)
    client = FakeClient(
        {
            "/advanced/zjmm-min": {"day": "20260709", "data": [["09:31", 1_000_000]]},
            "/advanced/main-activity-kline": {"ktype": "1", "data": [10, 35, 73.5]},
            "/advanced/main-monitor": {"stock_id": "000001", "data": [["3", "1783560828", "201", "8239999", "8.24"]]},
        }
    )
    try:
        assert collect_advanced_zjmm_min(client, store, "2026-07-09", ["000001"]) == 1
        assert collect_advanced_main_activity_kline(client, store, "2026-07-09", ["000001"]) == 1
        assert collect_advanced_main_monitor(client, store, "2026-07-09", ["000001"]) == 1
    finally:
        store.close()

    con = duckdb.connect(str(db_path), read_only=True)
    try:
        zjmm = con.execute("SELECT time, main_net_inflow FROM advanced_zjmm_min").fetchone()
        activity = con.execute("SELECT main_activity_score, ktype FROM advanced_main_activity_kline").fetchone()
        monitor = con.execute("SELECT main_net_inflow, ranking FROM advanced_main_monitor").fetchone()
    finally:
        con.close()

    assert zjmm == ("09:31", 1_000_000)
    assert activity == (10.0, "1")
    assert monitor == (8_239_999, 1)


def test_intraday_capital_flow_view_feeds_strength_evidence(tmp_path):
    db_path = tmp_path / "intraday_capital_view.duckdb"
    con = duckdb.connect(str(db_path))
    init_schema(con)
    con.execute(
        "INSERT INTO advanced_main_monitor(date, stock_code, stock_name, main_net_inflow, ranking) "
        "VALUES ('2026-07-09','000001','Alpha',3000000,1)"
    )
    con.execute(
        "INSERT INTO advanced_zjmm_min(date, stock_code, time, main_net_inflow, super_net_inflow, big_net_inflow) "
        "VALUES ('2026-07-09','000001','09:31',1000000,300000,500000)"
    )
    con.execute(
        "INSERT INTO advanced_dadan_kline(date, stock_code, big_net_amount, ktype) "
        "VALUES ('2026-07-09','000001',2000000,'1')"
    )
    con.execute(
        "INSERT INTO advanced_main_activity_kline(date, stock_code, main_activity_score, ktype) "
        "VALUES ('2026-07-09','000001',73.5,'1')"
    )
    con.execute(
        "INSERT INTO l2_tick_history(date, stock_code, time, price, volume, direction) "
        "VALUES ('2026-07-09','000001','09:31',10.1,100,'buy')"
    )
    con.execute(
        "INSERT INTO l2_tick_orders_all(date, stock_code, time, price, volume, order_type) "
        "VALUES ('2026-07-09','000001','09:31',10.1,200,'buy')"
    )
    con.execute(
        "INSERT INTO advanced_pankou(date, stock_code, buy1_volume, sell1_volume) "
        "VALUES ('2026-07-09','000001',500,250)"
    )
    con.close()

    views = build_normalized_views(db_path)

    con = duckdb.connect(str(db_path), read_only=True)
    try:
        assert "v_intraday_capital_flow_evidence" in views
        capital = con.execute(
            """
            SELECT
                main_monitor_net_inflow,
                zjmm_main_net_inflow,
                dadan_big_net_amount,
                main_activity_score,
                tick_volume,
                order_flow_rows,
                pankou_net_volume,
                capital_flow_score,
                source_tables
            FROM v_intraday_capital_flow_evidence
            WHERE trade_date = '2026-07-09' AND stock_code = '000001'
            """
        ).fetchone()
        strength = con.execute(
            """
            SELECT capital_flow_score, capital_flow_source_tables, strength_score
            FROM v_intraday_strength_evidence
            WHERE trade_date = '2026-07-09' AND stock_code = '000001'
            """
        ).fetchone()
    finally:
        con.close()

    assert capital[:7] == (3_000_000, 1_000_000, 2_000_000, 73.5, 100, 1, 250)
    assert capital[7] > 0
    assert "advanced_zjmm_min" in capital[8]
    assert strength[0] == capital[7]
    assert "advanced_main_activity_kline" in strength[1]
    assert strength[2] >= capital[7]
