from __future__ import annotations

import duckdb

from trade_system.tushare_history import TushareHistoryCollector
from trade_system.xiaodefa_source import XiaodefaClient


class FakeClient:
    def query_rows(self, api_name, params=None, fields=""):
        assert api_name == "adj_factor"
        return [{"ts_code": "000001.SZ", "trade_date": "20260714", "adj_factor": 139.008}]


def test_adj_factor_date_snapshot_is_persisted(tmp_path):
    db = tmp_path / "history.duckdb"
    with TushareHistoryCollector(db, client=FakeClient()) as collector:
        assert collector._collect_adj_factor("20260714") == 1
    con = duckdb.connect(str(db), read_only=True)
    try:
        assert con.execute("select ts_code,stock_code,cast(date as varchar),adj_factor from tushare_adj_factor").fetchone() == (
            "000001.SZ", "000001", "2026-07-14", 139.008
        )
    finally:
        con.close()





class XiaodefaDailyFixture(XiaodefaClient):
    def __init__(self):
        super().__init__(token="fixture")

    def query_rows(self, api_name, params=None, fields="", *, _deadline=None):
        assert api_name == "daily"
        return [
            {
                "ts_code": f"000{i:03d}.SZ",
                "trade_date": "20260714",
                "open": 10,
                "high": 11,
                "low": 9,
                "close": 10.5,
                "vol": 100,
                "amount": 1000,
                "pct_chg": 1,
            }
            for i in range(1000)
        ]


def test_retained_xiaodefa_close_snapshot_is_certified(tmp_path):
    db = tmp_path / "fallback.duckdb"
    with TushareHistoryCollector(db, client=XiaodefaDailyFixture()) as collector:
        collector.store.conn.execute(
            "INSERT INTO tushare_stock_basic(ts_code,stock_code) "
            "SELECT '000' || lpad(CAST(i AS VARCHAR),3,'0') || '.SZ', "
            "'000' || lpad(CAST(i AS VARCHAR),3,'0') FROM range(1000) t(i)"
        )
        collector.store.conn.commit()
        assert collector._collect_daily("20260714") == 1000
        cert = collector.store.conn.execute(
            "SELECT provider,status,distinct_codes FROM close_snapshot_certification "
            "WHERE dataset='daily' AND trade_date='2026-07-14'"
        ).fetchone()
    assert cert == ("xiaodefa", "certified", 1000)


def test_flow_normalization_preserves_missing_values_and_dc_net_definition(tmp_path):
    class FlowFixture:
        def query_rows(self, api_name, params=None, fields=""):
            if api_name == "moneyflow_ind_dc":
                assert not any(f.startswith("sell_") for f in fields.split(","))
                return [{"trade_date": "20260714", "ts_code": "BK001.DC", "close": 100,
                         "net_amount": 30, "buy_elg_amount": 10, "buy_lg_amount": -3}]
            assert api_name == "moneyflow"
            return [{"trade_date": "20260714", "ts_code": "000001.SZ",
                     "buy_elg_amount": 10, "sell_elg_amount": 4,
                     "buy_lg_amount": 8, "sell_lg_amount": 2,
                     "buy_sm_amount": 1, "sell_sm_amount": 1,
                     "buy_md_amount": 1, "sell_md_amount": 1}]
    with TushareHistoryCollector(tmp_path / "flow.duckdb", client=FlowFixture()) as collector:
        collector._collect_industry_flow("20260714")
        collector.sync_sector_flow("20260714")
        assert collector.store.conn.execute(
            "SELECT main_net,super_net,large_net,mid_net,small_net FROM multi_source_sector_flow"
        ).fetchone() == (30, 10, -3, None, None)
        collector._collect_moneyflow("20260714")
        collector.sync_stock_flow("20260714")
        assert collector.store.conn.execute(
            "SELECT main_net,super_net,large_net,mid_net,small_net,net_total FROM multi_source_stock_flow"
        ).fetchone() == (120000, 60000, 60000, 0, 0, None)
