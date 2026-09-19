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


class ScopedFixture:
    def __init__(self):
        self.calls = []
        self.empty_codes = set()
        self.wrong_date = False

    def query_rows(self, api, params=None, fields=""):
        self.calls.append((api, dict(params)))
        assert api in {"daily", "daily_basic", "adj_factor", "index_daily"}
        if params["ts_code"] in self.empty_codes:
            return []
        return [{"ts_code": params["ts_code"],
                 "trade_date": "20260704" if self.wrong_date else params["trade_date"],
                 "close": 10, "adj_factor": 2, "pe": None}]


def seed_calendar(collector):
    collector.store.conn.execute("INSERT INTO tushare_trade_cal(exchange,cal_date,is_open) "
        "SELECT e,CAST(d AS DATE),opened FROM (VALUES ('SSE'),('SZSE')) x(e) CROSS JOIN "
        "(VALUES ('2026-07-01',true),('2026-07-02',true),('2026-07-03',true),('2026-07-04',false),('2026-07-05',false)) y(d,opened)")


def test_scoped_overlap_only_fetches_missing_sessions_and_preserves_old_done(tmp_path):
    client = ScopedFixture()
    with TushareHistoryCollector(tmp_path / "scope.duckdb", client=client) as c:
        seed_calendar(c)
        c.store.conn.execute("INSERT INTO tushare_backfill_task(task_id,status,rows_inserted) "
                             "VALUES ('old-task','done',1)")
        options = dict(datasets=["daily"], stock_codes=["1"])
        first = c.run("20260701", "20260702", **options)
        assert {r["status"] for r in first["results"]} == {"success"}
        before = c.store.conn.execute("SELECT * FROM tushare_daily ORDER BY date").fetchall()
        c.run("20260701", "20260702", **options)
        assert len(client.calls) == 2
        assert c.store.conn.execute("SELECT * FROM tushare_daily ORDER BY date").fetchall() == before
        c.run("20260702", "20260705", **options)
        assert len(client.calls) == 3
        assert client.calls[-1][1] == {"ts_code": "000001.SZ", "trade_date": "20260703"}
        # A removed fact makes a real gap even though an old checkpoint says success.
        c.store.conn.execute("DELETE FROM tushare_daily WHERE date='2026-07-02'")
        c.run("20260701", "20260702", **options)
        assert len(client.calls) == 4
        assert c.store.conn.execute("SELECT status,rows_inserted FROM tushare_backfill_task").fetchall() == [("done", 1)]


def test_partial_scope_retains_receipts_and_retries_only_uncovered_instrument(tmp_path):
    client = ScopedFixture()
    client.empty_codes = {"000002.SZ"}
    with TushareHistoryCollector(tmp_path / "partial.duckdb", client=client) as c:
        seed_calendar(c)
        options = dict(datasets=["daily"], stock_codes=["1", "2"])
        first = c.run("20260701", "20260701", **options)
        assert first["results"][0]["status"] == "error"
        assert c.store.conn.execute("SELECT status FROM history_fetch_checkpoint").fetchall() == [("error",)]
        assert c.store.conn.execute("SELECT count(*) FROM tushare_daily").fetchone()[0] == 1
        assert c.store.conn.execute("SELECT count(*) FROM multi_source_observation").fetchone()[0] == 2
        client.empty_codes.clear()
        second = c.run("20260701", "20260701", **options)
        assert second["results"][0]["status"] == "success"
        assert len(client.calls) == 3
        assert client.calls[-1][1]["ts_code"] == "000002.SZ"


def test_plan_excludes_prelisting_but_does_not_infer_suspension(tmp_path):
    client = ScopedFixture()
    with TushareHistoryCollector(tmp_path / "plan.duckdb", client=client) as c:
        seed_calendar(c)
        c.store.conn.execute("INSERT INTO tushare_stock_basic(ts_code,list_date,delist_date) VALUES ('000002.SZ','2026-07-02',NULL),('000003.SZ','2020-01-01','2026-07-01')")
        # Equal row counts on the wrong session cannot cover the requested session.
        c.store.conn.execute("INSERT INTO tushare_daily(ts_code,date,close) VALUES ('000001.SZ','2026-07-04',10)")
        result = c.run("20260701", "20260701", datasets=["daily"], stock_codes=["1", "2", "3"], plan_only=True)
        assert result["results"][0]["missing_codes"] == ["000001.SZ"]
        assert result["results"][0]["not_listed_codes"] == ["000002.SZ", "000003.SZ"]
        assert not client.calls
        assert c.store.conn.execute("SELECT count(*) FROM history_fetch_checkpoint").fetchone()[0] == 0


def test_wrong_session_is_rejected_with_raw_receipt_retained(tmp_path):
    client = ScopedFixture()
    client.wrong_date = True
    with TushareHistoryCollector(tmp_path / "wrong.duckdb", client=client) as c:
        seed_calendar(c)
        result = c.run("20260701", "20260701", datasets=["daily"], stock_codes=["1"])
        assert result["results"][0]["status"] == "error"
        assert c.store.conn.execute("SELECT count(*) FROM tushare_daily").fetchone()[0] == 0
        assert '20260704' in c.store.conn.execute("SELECT payload_json FROM multi_source_observation").fetchone()[0]


def test_forced_empty_refresh_cannot_reuse_old_rows_as_success(tmp_path):
    client = ScopedFixture()
    with TushareHistoryCollector(tmp_path / "refresh.duckdb", client=client) as c:
        seed_calendar(c)
        options = dict(datasets=["daily"], stock_codes=["1"])
        c.run("20260701", "20260701", **options)
        previous = c.store.conn.execute("SELECT * FROM tushare_daily").fetchall()
        client.empty_codes.add("000001.SZ")
        result = c.run("20260701", "20260701", force=True, **options)
        assert result["results"][0]["status"] == "error"
        assert c.store.conn.execute("SELECT * FROM tushare_daily").fetchall() == previous


def test_pagination_failure_keeps_all_received_pages_without_publishing(tmp_path):
    class Repeating(XiaodefaClient):
        def __init__(self):
            super().__init__(token="fixture")
        def query_rows(self, api, params=None, fields="", *, _deadline=None):
            return [{"ts_code": "000001.SZ", "trade_date": "20260701", "close": 10}] * 100
    with TushareHistoryCollector(tmp_path / "pages.duckdb", client=Repeating(), batch_limit=100) as c:
        seed_calendar(c)
        result = c.run("20260701", "20260701", datasets=["daily"], stock_codes=["1"])
        assert result["results"][0]["status"] == "error"
        assert "repeated page" in result["results"][0]["error"]
        assert c.store.conn.execute("SELECT count(*) FROM multi_source_observation").fetchone()[0] == 2
        assert c.store.conn.execute("SELECT count(*) FROM tushare_daily").fetchone()[0] == 0


def test_equal_count_with_wrong_instrument_cannot_certify_full_snapshot(tmp_path):
    import pytest
    from trade_system.xiaodefa_source import XiaodefaError
    class WrongUniverse(XiaodefaDailyFixture):
        def query_rows(self, *args, **kwargs):
            rows = super().query_rows(*args, **kwargs)
            rows[-1]["ts_code"] = "600999.SH"
            return rows
    with TushareHistoryCollector(tmp_path / "identity.duckdb", client=WrongUniverse()) as c:
        c.store.conn.execute("INSERT INTO tushare_stock_basic(ts_code,stock_code) "
            "SELECT '000' || lpad(CAST(i AS VARCHAR),3,'0') || '.SZ', "
            "'000' || lpad(CAST(i AS VARCHAR),3,'0') FROM range(1000) t(i)")
        with pytest.raises(XiaodefaError, match="1 missing instruments"):
            c._collect_daily("20260714")
        assert c.store.conn.execute("SELECT count(*) FROM tushare_daily").fetchone()[0] == 0
        assert c.store.conn.execute("SELECT count(*) FROM multi_source_observation").fetchone()[0] == 1


def test_offline_plan_needs_no_provider_credential(tmp_path, monkeypatch):
    from trade_system import xiaodefa_source
    monkeypatch.setattr(xiaodefa_source, "SETTINGS", {})
    with TushareHistoryCollector(tmp_path / "offline.duckdb", offline=True) as c:
        seed_calendar(c)
        result = c.run("20260701", "20260701", datasets=["daily"], stock_codes=["1"], plan_only=True)
        assert result["results"][0]["status"] == "planned"


def test_reference_failure_cannot_return_success_or_empty_skip(tmp_path):
    class Empty:
        def query_rows(self, *args, **kwargs):
            return []
    with TushareHistoryCollector(tmp_path / "reference.duckdb", client=Empty()) as c:
        seed_calendar(c)
        result = c.run("20260701", "20260701", datasets=["stock_basic"])
        assert result["results"][0]["status"] == "error"


def test_full_basic_response_with_only_identity_is_not_complete(tmp_path):
    client = ScopedFixture()
    with TushareHistoryCollector(tmp_path / "unknown.duckdb", client=client) as c:
        seed_calendar(c)
        c.store.conn.execute("INSERT INTO tushare_stock_basic(ts_code) VALUES ('000001.SZ')")
        c.store.conn.execute("INSERT INTO tushare_daily_basic(ts_code,stock_code,date,pe) "
                             "VALUES ('000001.SZ','000001','2026-07-01',10)")
        c._query_date_batch = lambda *a, **k: [{"ts_code":"000001.SZ","trade_date":"20260701"}]
        result = c.run("20260701", "20260701", datasets=["daily_basic"])
        assert result["results"][0]["status"] == "error"
        assert c.store.conn.execute("SELECT status FROM history_fetch_checkpoint").fetchone()[0] == 'error'
        assert c.store.conn.execute("SELECT pe FROM tushare_daily_basic").fetchone()[0] == 10


def test_stored_price_cannot_fill_an_unknown_calendar_day(tmp_path):
    import pytest
    from trade_system.xiaodefa_source import XiaodefaError
    class Unavailable:
        def query_rows(self, *a, **k):
            raise RuntimeError("calendar unavailable")
    with TushareHistoryCollector(tmp_path / "calendar.duckdb", client=Unavailable()) as c:
        c.store.conn.execute("INSERT INTO tushare_daily(ts_code,date,close) VALUES ('000001.SZ','2026-07-04',10)")
        with pytest.raises(XiaodefaError, match="trade_cal fetch failed"):
            c.ensure_calendar("20260704", "20260704")
