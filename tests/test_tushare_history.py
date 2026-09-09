from __future__ import annotations

import duckdb

from trade_system.tushare_history import TushareHistoryCollector
from trade_system.tushare_relay import TushareRelayClient
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


class RelayDailyFailure(TushareRelayClient):
    def __init__(self):
        super().__init__(token="test", runner=lambda _body, _timeout: {"code": 0, "data": {}})

    def query_rows(self, api_name, params=None, fields=""):
        raise RuntimeError("fast relay unavailable")


class XiaodefaDailyFallback(XiaodefaClient):
    def __init__(self):
        pass

    def query_rows(self, api_name, params=None, fields=""):
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


def test_close_snapshot_falls_back_to_xiaodefa_after_fast_relay_failure(tmp_path):
    db = tmp_path / "fallback.duckdb"
    with TushareHistoryCollector(db, client=RelayDailyFailure()) as collector:
        collector.store.conn.execute(
            "INSERT INTO tushare_stock_basic(ts_code,stock_code) "
            "SELECT '000' || lpad(CAST(i AS VARCHAR),3,'0') || '.SZ', "
            "'000' || lpad(CAST(i AS VARCHAR),3,'0') FROM range(1000) t(i)"
        )
        collector.store.conn.commit()
        collector._secondary_client = XiaodefaDailyFallback()
        assert collector._collect_daily("20260714") == 1000
        cert = collector.store.conn.execute(
            "SELECT provider,status,distinct_codes FROM close_snapshot_certification "
            "WHERE dataset='daily' AND trade_date='2026-07-14'"
        ).fetchone()
    assert cert == ("xiaodefa", "certified", 1000)
