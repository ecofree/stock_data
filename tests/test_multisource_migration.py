from __future__ import annotations

import duckdb
import pytest

from trade_system import resilient_sources
from trade_system.adapters import kline_sources
from trade_system.quote_transport import market_prefix


def test_new_beijing_920_prefix_routes_to_bj_without_misrouting_shanghai_b_share():
    assert kline_sources._norm_code("920992") == ("bj", "920992")
    assert market_prefix("920992") + "920992" == "bj920992"
    assert kline_sources._norm_code("900948") == ("sh", "900948")
from trade_system.multi_source_store import MultiSourceStore
from trade_system.multi_source_audit import audit_multisource


def test_migrated_source_plan_contains_non_tushare_capital_flow_paths():
    assert "stock_flow" in resilient_sources.SOURCE_PLAN
    assert "sector_flow" in resilient_sources.SOURCE_PLAN
    stock_sources = [name for name, _ in resilient_sources.SOURCE_PLAN["stock_flow"]("000001")]
    assert stock_sources[:2] == ["eastmoney", "sina"]
    assert stock_sources[-1] == "xiaodefa"
    assert [name for name, _ in resilient_sources.SOURCE_PLAN["sector_flow"]()] == ["eastmoney", "xiaodefa"]


def test_sector_flow_parser_normalizes_all_order_buckets(monkeypatch):
    # Implementation moved to trade_system.adapters.eastmoney_dc; patch there.
    from trade_system.adapters import eastmoney_dc

    monkeypatch.setattr(
        eastmoney_dc,
        "_em_get_clist_json",
        lambda *args, **kwargs: {"data": {"diff": [{
            "f12": "BK0001", "f14": "测试板块", "f3": 2.5, "f62": 100,
            "f66": 70, "f72": 30, "f78": -10, "f84": -20,
        }]}},
    )
    row = eastmoney_dc._from_em_sector_flow(10)[0]
    assert row["sector_code"] == "BK0001"
    assert row["main_net"] == 100
    assert row["super_net"] == 70
    assert row["small_net"] == -20


def test_tushare_dc_sector_flow_keeps_yuan_and_direct_net_buckets(monkeypatch):
    # The implementation lives in trade_system.adapters.kline_sources after
    # the adapter split; patch it there so the real code path is exercised.
    from trade_system.adapters import kline_sources

    monkeypatch.setattr(
        kline_sources,
        "_xiaodefa_query",
        lambda api, params, fields="": [{
            "trade_date": "20260714", "content_type": "概念", "ts_code": "BK0001.DC",
            "name": "测试板块", "pct_change": 2.5, "close": 100,
            "net_amount": 3_000_000_000, "buy_elg_amount": 700_000_000,
            "buy_lg_amount": 300_000_000, "buy_md_amount": -100_000_000,
            "buy_sm_amount": -900_000_000,
        }] if api == "moneyflow_ind_dc" else [],
    )
    row = kline_sources._from_xiaodefa_sector_flow("20260714")[0]
    assert row["main_net"] == 3_000_000_000
    assert row["super_net"] == 700_000_000
    assert row["small_net"] == -900_000_000
    assert row["amount_unit"] == "yuan"
    assert row["sector_type"] == "概念"


def test_store_is_idempotent_and_stale_does_not_overwrite(tmp_path):
    db = tmp_path / "multi.duckdb"

    def fake_fetcher(data_type, code=None, **kwargs):
        if data_type == "stock_flow":
            return ([{"date": "2026-07-10", "main_net": 100, "super_net": 60,
                      "large_net": 40, "mid_net": -10, "small_net": -20, "_src": "fake"}],
                    {"source": "eastmoney", "status": "live", "latency": 0.01})
        if data_type == "sector_flow":
            return ([{"sector_code": "BK0001", "sector_name": "测试板块", "main_net": 999,
                      "super_net": 500, "large_net": 499, "mid_net": 0, "small_net": 0}],
                    {"source": "eastmoney", "status": "live"})
        return ([{"date": "2026-07-10", "open": 10, "high": 11, "low": 9,
                   "close": 10.5, "volume": 100, "amount": 1000, "_src": "sina"}],
                {"source": "sina", "status": "live"})

    with MultiSourceStore(db, fetcher=fake_fetcher) as store:
        _, meta = store.fetch("stock_flow", "000001")
        store.store("stock_flow", "000001", meta and fake_fetcher("stock_flow", "000001")[0], meta)
        store.store("stock_flow", "000001", fake_fetcher("stock_flow", "000001")[0], meta)
        store.store("sector_flow", None, fake_fetcher("sector_flow")[0], {"source": "eastmoney", "status": "live"}, trade_date="2026-07-10")
        store.store("kline", "000001", fake_fetcher("kline", "000001")[0], {"source": "sina", "status": "live"})
        store.store("stock_flow", "000001", [{"date": "2026-07-10", "main_net": -999}], {"source": "cache", "status": "stale"})
        assert store.con.execute("select count(*) from multi_source_stock_flow").fetchone()[0] == 1
        assert store.con.execute("select main_net from multi_source_stock_flow").fetchone()[0] == 100
        assert store.con.execute("select count(*) from multi_source_sector_flow").fetchone()[0] == 1
        assert store.con.execute("select count(*) from multi_source_kline").fetchone()[0] == 1


def test_store_skips_preopen_flow_placeholders(tmp_path):
    db = tmp_path / "placeholder.duckdb"
    with MultiSourceStore(db) as store:
        placeholder = [{"sector_code": "BK0001", "sector_name": "pre-open", "main_net": None,
                        "super_net": None, "large_net": None, "mid_net": None, "small_net": None}]
        result = store.store("sector_flow", None, placeholder,
                             {"source": "eastmoney", "status": "live"}, trade_date="2026-07-14")
        assert result["rows_written"] == 0
        assert store.con.execute("select count(*) from multi_source_sector_flow").fetchone()[0] == 0


def test_sector_writer_and_read_only_kline_projection(tmp_path):
    db = tmp_path / "core.duckdb"
    con = duckdb.connect(str(db))
    con.execute("create table sector_capital(date date, sector_code varchar, main_net_inflow bigint, super_net_inflow bigint, big_net_inflow bigint, mid_net_inflow bigint, small_net_inflow bigint, fetched_at timestamp)")
    con.execute("create table kline(date date, stock_code varchar, open double, high double, low double, close double, volume bigint, turnover bigint, change_pct double, ktype varchar, fetched_at timestamp, raw_json varchar)")
    con.close()
    with MultiSourceStore(db) as store:
        store.con.execute("CREATE UNIQUE INDEX sector_key ON multi_source_sector_flow(source_date,sector_code,provider)")
        for _ in range(2):
            store.store("sector_flow", None, [{"sector_code": "BK0001", "main_net": 100, "super_net": 50, "large_net": 50, "mid_net": 0, "small_net": 0}], {"source": "eastmoney", "status": "live", "received_at": 1000000000.0}, trade_date="2026-07-10")
        assert store.con.execute("SELECT fetched_at FROM multi_source_sector_flow").fetchone()[0].timestamp() == 1000000000.0
        store.store("kline", "000001", [{"date": "2026-07-10", "open": 10, "high": 11, "low": 9, "close": 10.5, "volume": 100, "amount": 1000}], {"source": "sina", "status": "live"})
        assert store.sync_sector_capital("2026-07-10") == 1
        from trade_system.normalize import _create_kline_daily
        _create_kline_daily(store.con)
        assert store.con.execute("SELECT close FROM v_kline_daily").fetchone()[0] == 10.5
        assert store.con.execute("SELECT COUNT(*) FROM kline").fetchone()[0] == 0
        assert store.con.execute("select main_net_inflow, big_net_inflow from sector_capital").fetchone() == (100, 50)
        assert store.con.execute("select stock_code, close from v_kline_daily").fetchone() == ("000001", 10.5)


def test_readiness_audit_separates_stock_and_sector_coverage(tmp_path):
    db = tmp_path / "audit.duckdb"
    with MultiSourceStore(db) as store:
        store.store("stock_flow", "000001", [{"date": "2026-07-10", "main_net": 1}], {"source": "sina", "status": "live"})
        store.store("sector_flow", None, [{"sector_code": "BK0001", "main_net": 2}], {"source": "eastmoney", "status": "live"}, trade_date="2026-07-10")
    result = audit_multisource(db, "2026-07-10")
    assert result["capital_flow"]["multi_source_stock_flow"]["coverage"] == 1
    assert result["capital_flow"]["multi_source_sector_flow"]["coverage"] == 1
    assert result["capital_flow"]["multi_source_stock_flow"]["latest"] == "2026-07-10"


def test_readiness_audit_never_uses_future_rows_or_historical_peak_as_denominator(tmp_path):
    db = tmp_path / "audit-asof.duckdb"
    con = duckdb.connect(str(db))
    con.execute(
        "CREATE TABLE multi_source_sector_flow(source_date DATE, sector_code VARCHAR, is_stale BOOLEAN)"
    )
    con.execute(
        "INSERT INTO multi_source_sector_flow VALUES "
        "('2026-07-10','BK0001',false),('2026-07-11','BK0001',false),"
        "('2026-07-11','BK0002',false),('2026-07-12','BK0001',false),"
        "('2026-07-12','BK0002',false),('2026-07-12','BK0003',false)"
    )
    con.execute(
        "CREATE TABLE intraday_sector_flow_batch(trade_date DATE, expected_rows INTEGER, updated_at TIMESTAMP)"
    )
    con.execute("INSERT INTO intraday_sector_flow_batch VALUES ('2026-07-11',2,'2026-07-11 15:00:00')")
    con.close()

    result = audit_multisource(db, "2026-07-11")
    flow = result["capital_flow"]["multi_source_sector_flow"]
    assert flow["latest"] == "2026-07-11"
    assert flow["latest_coverage"] == 2
    assert flow["expected_coverage"] == 2
    assert flow["coverage_pct_of_expected"] == 100.0
    assert flow["status"] == "available"


def test_kpl_intraday_flow_promotes_latest_cumulative_point(tmp_path):
    db = tmp_path / "kpl.duckdb"
    con = duckdb.connect(str(db))
    con.execute(
        "create table advanced_zjmm_min(date date, stock_code varchar, time varchar, "
        "main_net_inflow bigint, super_net_inflow bigint, big_net_inflow bigint, fetched_at timestamp)"
    )
    con.execute("insert into advanced_zjmm_min values ('2026-07-13','000001','13:00',10,2,1,current_timestamp)")
    con.execute("insert into advanced_zjmm_min values ('2026-07-13','000001','13:08',25,4,3,current_timestamp)")
    con.close()
    with MultiSourceStore(db) as store:
        assert store.sync_kpl_intraday_flow("2026-07-13") == 1
        assert store.con.execute(
            "select main_net,super_net,large_net,provider from multi_source_stock_flow"
        ).fetchone() == (25.0, 4.0, 3.0, "kpl")
        source_time = store.con.execute("SELECT max(fetched_at) FROM advanced_zjmm_min").fetchone()[0]
        assert store.con.execute("SELECT fetched_at,flow_unit FROM multi_source_stock_flow").fetchone() == (source_time, 'CNY')
        store.con.execute("CREATE UNIQUE INDEX flow_key ON multi_source_stock_flow(source_date,stock_code,provider)")
        before = store.con.execute("SELECT * FROM multi_source_stock_flow").fetchall()
        assert store.sync_kpl_intraday_flow("2026-07-13") == 1
        assert store.con.execute("SELECT * FROM multi_source_stock_flow").fetchall() == before


def test_empty_dc_sector_flow_does_not_substitute_ths_definition(monkeypatch):
    from trade_system.adapters import kline_sources
    calls = []
    def empty(api, params, fields=""):
        calls.append(api)
        return []
    monkeypatch.setattr(kline_sources, "_xiaodefa_query", empty)
    assert kline_sources._from_xiaodefa_sector_flow("20260714") is None
    assert calls == ["moneyflow_ind_dc"]


def history_args(**changes):
    from types import SimpleNamespace
    values = dict(date="2026-07-13", start="20260701", end="20260713", fq="qfq",
                  max_sectors=200, periods=8, resume=True, force=False)
    return SimpleNamespace(**(values | changes))


def test_explicit_history_request_resumes_without_network_or_fact_rewrite(tmp_path):
    from scripts.collect_multisource import _run_one
    calls = []
    def fetch(kind, code, **kwargs):
        calls.append((kind, code, kwargs))
        return [{"period": "2026Q1", "revenue": 100}], {"source": "fixture", "status": "live"}
    with MultiSourceStore(tmp_path / "resume.duckdb", fetcher=fetch) as store:
        first = _run_one(store, "financials", "000001", history_args())
        assert first[2]["status"] == "live"
        receipts = store.con.execute("SELECT * FROM multi_source_observation").fetchall()
        second = _run_one(store, "financials", "000001", history_args())
        assert second[2]["status"] == "skipped"
        assert len(calls) == 1
        assert store.con.execute("SELECT * FROM multi_source_observation").fetchall() == receipts
        _run_one(store, "financials", "000001", history_args(periods=12))
        _run_one(store, "financials", "000001", history_args(force=True))
        assert len(calls) == 3 and calls[-1][2]["ttl"] == 0


def test_empty_history_response_and_interrupted_request_remain_retryable(tmp_path):
    from scripts.collect_multisource import _run_one
    import pytest
    calls = []
    def fetch(kind, code, **kwargs):
        calls.append(kind)
        if len(calls) == 1:
            raise RuntimeError("interrupted request")
        return [], {"source": "fixture", "status": "live"}
    with MultiSourceStore(tmp_path / "partial.duckdb", fetcher=fetch) as store:
        with pytest.raises(RuntimeError, match="interrupted"):
            _run_one(store, "financials", "000001", history_args())
        for _ in range(2):
            assert _run_one(store, "financials", "000001", history_args())[2]["status"] == "failed"
        assert len(calls) == 3
        assert store.con.execute("SELECT status,attempts FROM multi_source_task_checkpoint").fetchone() == ("failed", 3)


def test_explicit_collector_refuses_backdated_current_snapshots(tmp_path):
    from scripts.collect_multisource import _run_one
    import pytest
    calls = []
    with MultiSourceStore(tmp_path / "historical.duckdb", fetcher=lambda *a, **k: calls.append(a)) as store:
        with pytest.raises(ValueError, match="relabelled"):
            _run_one(store, "valuation", "000001", history_args(date="2000-01-01"))
        assert not calls


def test_explicit_collector_dry_run_has_no_writer_or_network(tmp_path, monkeypatch):
    from scripts import collect_multisource as cli
    import sys
    db = tmp_path / "never-created.duckdb"
    monkeypatch.setattr(sys, "argv", ["collect", "--db", str(db), "--types", "financials,statements",
                                      "--stock-codes", "000001", "--dry-run"])
    def refuse(*a, **k):
        raise AssertionError("dry-run opened a writer")
    monkeypatch.setattr(cli, "MultiSourceStore", refuse)
    assert cli.main() == 0
    assert not db.exists()


def test_migrated_sina_statements_use_verified_shared_transport(monkeypatch):
    import json
    from trade_system.adapters import sina_sources
    calls = []
    def read(request, **kwargs):
        calls.append((request.full_url, kwargs))
        return json.dumps({"result":{"data":{"report_list":{"20260630":{"data":[
            {"item_title":"revenue","item_value":"100","item_tongbi":"2"}]}}}}}).encode()
    monkeypatch.setattr(sina_sources, "read_verified_once", read)
    rows = sina_sources.get_financial_statements("000001")
    assert rows == [{"报告期":"2026-06-30","revenue":"100","revenue_同比":"2"}]
    assert len(calls) == 1 and calls[0][1]["max_bytes"] == 4_000_000


def test_product_failures_do_not_substitute_other_contracts(monkeypatch, tmp_path):
    from trade_system import eastmoney_finance
    from trade_system.adapters import eastmoney_dc
    monkeypatch.setattr(eastmoney_finance, "get_fund_flow", lambda *a, **k: [])
    assert eastmoney_dc.get_fund_flow("000001") is None
    assert [name for name, _ in resilient_sources.SOURCE_PLAN['fund_flow']('000001')] == ['eastmoney', 'sina']
    assert [name for name, _ in resilient_sources.SOURCE_PLAN['statements']('000001', report_type='llb')] == ['sina']
    assert not hasattr(eastmoney_dc, '_from_em_statements')
    _window_fixture(monkeypatch,tmp_path)
    for name in ('_from_ths_hot_reason','_from_ths_hot_list','_from_em_hot_rank',
                 '_from_em_hot_concept','_from_sina_option_greeks'):
        monkeypatch.setattr(resilient_sources,name,lambda *a:None)
    for product,code,params,old_source in [
        ('hot_topics',None,{'date':'2026-09-18'},'eastmoney'),
        ('ths_hot_list',None,{'period':'hour'},'eastmoney'),
        ('em_hot_rank',None,{},'ths'),('hot_concept','000001',{},'ths'),
        ('option_greeks','000001',{},'eastmoney')]:
        request=resilient_sources._cache_key(product,code,params)
        key='receipt-v1:'+resilient_sources._cache_key(product,code,{'request_key':request})
        resilient_sources.cache.put(key,{'schema':1,'source':old_source,
            'received_at':resilient_sources.time.time(),'data':[{'wrong_product':True}]})
        data,meta=resilient_sources.get(product,code,**params)
        assert data is None and meta['status']=='failed',product


def _window_fixture(monkeypatch, tmp_path, *, adjustment="none"):
    import sqlite3
    cache_db = sqlite3.connect(":memory:", check_same_thread=False)
    cache_db.execute("CREATE TABLE cache(key TEXT PRIMARY KEY,value TEXT,ts REAL)")
    monkeypatch.setattr(resilient_sources, "cache", resilient_sources.CacheStore(cache_db))
    monkeypatch.setattr(resilient_sources, "CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(resilient_sources.rate, "acquire", lambda *a, **kw: None)
    monkeypatch.setattr(resilient_sources.rate, "release", lambda *a, **kw: None)
    monkeypatch.setattr(resilient_sources.health, "record", lambda *a, **kw: None)
    monkeypatch.setattr(resilient_sources.health, "is_cooldown", lambda *a: False)
    calls = []
    rows = [{"date": f"2026-09-{day:02d}", "close": 10+day, "volume_unit": "shares",
             "amount_unit": "yuan", "adjustment": adjustment} for day in (14,15,16,17)]
    def plan(code, start, end, **kwargs):
        def fetch():
            calls.append((code,start,end))
            return [dict(row) for row in rows if start <= row["date"].replace("-", "") <= end]
        return [("fixture", fetch)]
    monkeypatch.setitem(resilient_sources.SOURCE_PLAN, "kline", plan)
    return calls, rows


def test_overlapping_windows_fetch_only_verified_missing_sessions_and_store_once(tmp_path, monkeypatch):
    calls, _ = _window_fixture(monkeypatch, tmp_path)
    with MultiSourceStore(tmp_path/"window.duckdb") as store:
        store.con.execute("CREATE TABLE tushare_trade_cal(exchange VARCHAR,cal_date DATE,is_open BOOLEAN)")
        store.con.execute("INSERT INTO tushare_trade_cal VALUES ('SSE','2026-09-14',true),('SSE','2026-09-15',true),('SSE','2026-09-16',true)")
        def run(start,end):
            data, meta = store.fetch("kline", "000001", start=start, end=end, fq="")
            return meta, store.store("kline", "000001", data, meta)
        first, result = run("20260914","20260915")
        assert result["rows_written"] == 2
        received = store.con.execute("SELECT source_date,fetched_at FROM multi_source_kline ORDER BY source_date").fetchall()
        second, result = run("20260915","20260916")
        assert result["rows_written"] == 1 and not second["missing_sessions"]
        third, result = run("20260914","20260916")
        assert result["rows_written"] == 0 and result["receipt_reused"] and third["status"] == "fresh"
        assert calls == [("000001","20260914","20260915"),("000001","20260916","20260916")]
        assert store.con.execute("SELECT count(*) FROM multi_source_observation").fetchone()[0] == 2
        assert store.con.execute("SELECT source_date,fetched_at FROM multi_source_kline ORDER BY source_date").fetchall()[:2] == received
        assert third["received_at"] == first["received_at"]
        store.con.execute("DELETE FROM multi_source_kline WHERE source_date='2026-09-15'")
        _, result = run("20260914","20260916")
        assert result["status"] == "cache_unmaterialized" and result["rows_written"] == 0


def test_partial_response_keeps_receipt_but_never_claims_full_coverage(tmp_path, monkeypatch):
    calls, rows = _window_fixture(monkeypatch, tmp_path)
    rows[:] = [rows[0]]
    params = dict(start="20260914",end="20260915",fq="",expected_sessions=["2026-09-14","2026-09-15"])
    data, meta = resilient_sources.get("kline","000001",**params)
    assert meta["status"] == "partial" and meta["missing_sessions"] == ["20260915"]
    assert len(meta["receipts"]) == 1 and len(data) == 1
    resilient_sources.get("kline","000001",**params)
    assert calls[-1][1:] == ("20260915","20260915")


def test_adjusted_windows_reuse_one_snapshot_but_never_stitch_anchors(tmp_path, monkeypatch):
    calls, _ = _window_fixture(monkeypatch, tmp_path, adjustment="qfq")
    def get(start,end,days):
        return resilient_sources.get("kline","000001",start=start,end=end,fq="qfq",expected_sessions=days)
    first = get("20260914","20260916",["20260914","20260915","20260916"])
    second = get("20260915","20260916",["20260915","20260916"])
    assert second[1]["status"] == "fresh" and second[1]["received_at"] == first[1]["received_at"]
    get("20260915","20260917",["20260915","20260916","20260917"])
    assert [c[1:] for c in calls] == [("20260914","20260916"),("20260915","20260917")]


def test_window_scope_retired_source_and_offline_cache_are_not_relabelled(tmp_path, monkeypatch):
    calls, _ = _window_fixture(monkeypatch, tmp_path)
    params = dict(start="20260914",end="20260914",fq="",expected_sessions=["20260914"])
    resilient_sources.get("kline","000001",**params)
    assert resilient_sources.get("kline","000002",offline=True,**params)[1]["status"] == "partial"
    monkeypatch.setattr(resilient_sources,"XIAODEFA_TOKEN","different-authorized-scope")
    assert resilient_sources.get("kline","000001",offline=True,**params)[1]["status"] == "partial"
    assert len(calls) == 1
    monkeypatch.setitem(resilient_sources.SOURCE_PLAN,"kline",lambda *a, **kw: [])
    assert resilient_sources.get("kline","000001",**params)[1]["status"] == "partial"
    assert len(calls) == 1
    from scripts.collect_multisource import _offline_fetch
    assert _offline_fetch("stock_info","000001")[1]["status"] == "failed"


def test_window_ttl_zero_fetches_explicit_revision_and_calendar_unknown_fails(tmp_path, monkeypatch):
    import pytest
    calls, _ = _window_fixture(monkeypatch,tmp_path)
    params = dict(start="20260914",end="20260914",fq="",expected_sessions=["20260914"])
    resilient_sources.get("kline","000001",**params)
    resilient_sources.get("kline","000001",ttl=0,**params)
    assert len(calls) == 2
    with MultiSourceStore(tmp_path/"unknown.duckdb") as store:
        with pytest.raises(ValueError,match="calendar"):
            store.fetch("kline","000001",start="20260914",end="20260915",fq="")
    assert len(calls) == 2


def test_overlap_reuse_is_shared_across_retained_bar_products_without_mixing(tmp_path,monkeypatch):
    calls, _ = _window_fixture(monkeypatch,tmp_path)
    for kind in ("index_kline","etf_kline","cb_kline"):
        monkeypatch.setitem(resilient_sources.SOURCE_PLAN,kind,resilient_sources.SOURCE_PLAN["kline"])
        params=dict(start="20260914",end="20260915",fq="",expected_sessions=["20260914","20260915"])
        resilient_sources.get(kind,"000001",**params)
        params.update(start="20260915",expected_sessions=["20260915"])
        assert resilient_sources.get(kind,"000001",**params)[1]["status"] == "fresh"
    assert len(calls)==3


def test_adjustment_products_and_invalid_rows_never_overwrite_good_facts(tmp_path,monkeypatch):
    calls, rows = _window_fixture(monkeypatch,tmp_path)
    with MultiSourceStore(tmp_path/"facts.duckdb") as store:
        store.con.execute("CREATE TABLE tushare_trade_cal(exchange VARCHAR,cal_date DATE,is_open BOOLEAN)")
        store.con.execute("INSERT INTO tushare_trade_cal VALUES ('SSE','2026-09-14',true)")
        def run(fq):
            data,meta=store.fetch("kline","000001",start="20260914",end="20260914",fq=fq,ttl=0)
            return meta,store.store("kline","000001",data,meta)
        run("")
        for row in rows:row.update(adjustment="qfq",close=12)
        run("qfq")
        assert store.con.execute("SELECT adjustment FROM multi_source_kline ORDER BY adjustment").fetchall()==[("none",),("qfq",)]
        from trade_system.normalize import _create_kline_daily
        _create_kline_daily(store.con)
        assert store.con.execute("SELECT close,adjustment FROM v_kline_daily").fetchone() == (24,"none")
        for row in rows:row.update(stock_code="000001.SH",close=99)
        meta,result=run("qfq")
        assert meta["status"]=="partial" and result["rows_written"]==0
        assert store.con.execute("SELECT close FROM multi_source_kline WHERE adjustment='qfq'").fetchone()==(12,)
        assert store.con.execute("SELECT count(*) FROM multi_source_observation").fetchone()[0]==3


def _financial_fixture(monkeypatch, tmp_path, datatype="financials"):
    calls, _ = _window_fixture(monkeypatch, tmp_path)
    reports = [{'报告期' if datatype == 'statements' else 'date': day, 'revenue': None if index==0 else index}
               for index, day in enumerate(['2026-06-30','2026-03-31','2025-12-31','2025-09-30'])]
    def plan(code, periods, **kwargs):
        def fetch():
            calls.append((code, periods, kwargs))
            return [dict(row) for row in reports[:periods]]
        return [('fixture', fetch)]
    monkeypatch.setitem(resilient_sources.SOURCE_PLAN, datatype, plan)
    return calls, reports


def test_report_subset_reuses_original_receipt_and_does_not_write_again(tmp_path, monkeypatch):
    calls, _ = _financial_fixture(monkeypatch, tmp_path, "statements")
    with MultiSourceStore(tmp_path/'reports.duckdb') as store:
        data, first = store.fetch('statements', '000001', periods=4)
        store.store('statements', '000001', data, first)
        original = store.con.execute('SELECT * FROM multi_source_observation').fetchall()
        data, second = store.fetch('statements', '000001', periods=2, offline=True)
        assert len(data)==2 and data[0]['revenue'] is None
        assert second['status']=='fresh' and second['received_at']==first['received_at']
        result = store.store('statements', '000001', data, second)
        assert result['receipt_reused'] and result['rows_written']==0
        assert store.con.execute('SELECT * FROM multi_source_observation').fetchall()==original
        assert len(calls)==1
        # Report types and separate normalized financial products never borrow this receipt.
        assert store.fetch('statements','000001',periods=2,report_type='fzb',offline=True)[0] is None
        assert store.fetch('statements','000001',periods=2,report_type='llb',offline=True)[0] is None
        assert store.fetch('financials','000001',periods=2,offline=True)[0] is None
        assert store.fetch('statements','000001',periods=2,report_type='lrb',offline=True)[0] == data
        with pytest.raises(ValueError,match='statement type'):
            store.fetch('statements','000001',report_type='unknown')
        assert len(calls)==1
        data, short = store.fetch('statements', '000001', periods=5)
        assert short['status']=='partial' and short['periods_observed']==4
        assert len(calls)==2


def test_newer_report_revision_prevents_stitching_or_resurrecting_old_periods(tmp_path, monkeypatch):
    _window_fixture(monkeypatch, tmp_path)
    clock, calls = [10000.0], []
    reports = [dict(id=str(n), date='2026-09-18', title=str(n)) for n in range(4)]
    monkeypatch.setattr(resilient_sources.time, 'time', lambda: clock[0])
    def fetch(code, page_size):
        calls.append(page_size)
        return [dict(row) for row in reports[:page_size]]
    monkeypatch.setattr(resilient_sources, '_from_cninfo_announcements', fetch)
    original, first = resilient_sources.get('announcements','000001',page_size=4)
    subset, cached = resilient_sources.get('announcements','000001',page_size=2,offline=True)
    assert subset == original[:2] and cached['received_at'] == first['received_at'] and calls == [4]
    reports[0]['title']='revised'
    clock[0]+=1
    updated, _ = resilient_sources.get('announcements','000001',page_size=1,ttl=0)
    assert updated[0]['title']=='revised'
    data, meta = resilient_sources.get('announcements','000001',page_size=2,offline=True)
    assert data is None and meta['status']=='failed'
    data, meta = resilient_sources.get('announcements','000001',page_size=2)
    assert data[0]['title']=='revised' and calls == [4,1,2] and len(meta['receipts'])==1


@pytest.mark.parametrize('change',['duplicate','invalid_date','expired','source_retired','authorization'])
def test_report_reuse_rejects_unqualified_scope(tmp_path, monkeypatch, change):
    calls, reports = _financial_fixture(monkeypatch, tmp_path)
    if change=='duplicate':reports[1]['date']=reports[0]['date']
    if change=='invalid_date':reports[1]['date']='2026-02-30'
    resilient_sources.get('financials','000001',periods=4)
    kwargs = {'ttl':0} if change=='expired' else {}
    if change=='source_retired':monkeypatch.setitem(resilient_sources.SOURCE_PLAN,'financials',[('other',lambda:None)])
    if change=='authorization':monkeypatch.setattr(resilient_sources,'XIAODEFA_TOKEN','changed-synthetic-scope')
    data, meta = resilient_sources.get('financials','000001',periods=2,offline=True,**kwargs)
    assert data is None and meta['status']=='failed' and len(calls)==1


def test_identical_flow_aliases_share_fetch_and_receipt_materialization(tmp_path, monkeypatch):
    calls, _ = _window_fixture(monkeypatch, tmp_path)
    def fetch():
        calls.append(1)
        return [{'date':'2026-09-18','main_net':100,'amount_unit':'yuan'}]
    monkeypatch.setitem(resilient_sources.SOURCE_PLAN,'fund_flow_120d',[('eastmoney',fetch)])
    with MultiSourceStore(tmp_path/'flows.duckdb') as store:
        data, first = store.fetch('stock_flow','000001')
        store.store('stock_flow','000001',data,first)
        before=store.con.execute('SELECT * FROM multi_source_observation').fetchall()
        data, second = store.fetch('fund_flow_120d','000001',offline=True)
        result=store.store('fund_flow_120d','000001',data,second)
        assert len(calls)==1 and result['receipt_reused']
        assert store.con.execute('SELECT * FROM multi_source_observation').fetchall()==before


def test_source_fallback_uses_remaining_acquisition_budget(monkeypatch, tmp_path):
    from trade_system.http_transport import request_budget
    calls, _ = _window_fixture(monkeypatch,tmp_path)
    clock=[100.0]
    monkeypatch.setattr(resilient_sources.time,'monotonic',lambda:clock[0])
    def call(fn,timeout,pending):
        calls.append(timeout)
        clock[0]+=0.6
        return None
    monkeypatch.setattr(resilient_sources,'_call',call)
    monkeypatch.setitem(resilient_sources.SOURCE_PLAN,'stock_info',[(name,lambda:None) for name in ('a','b','c')])
    with request_budget(1):
        data, meta = resilient_sources.get('stock_info','000001',timeout_per=10)
    assert data is None and meta['status']=='failed'
    assert calls==pytest.approx([1,.4])


@pytest.mark.parametrize("product", ['zt_pool','zb_pool','dt_pool','yzt_pool','limit_up_sentiment','ths_limit_up'])
def test_pool_failure_never_substitutes_other_product_or_old_contract(tmp_path, monkeypatch, product):
    _window_fixture(monkeypatch, tmp_path)
    calls = []
    for name in ('zt','zb','dt','yzt'):
        monkeypatch.setattr(resilient_sources, '_from_em_'+name+'_pool', lambda *a: None)
    monkeypatch.setattr(resilient_sources, '_from_ths_limit_up', lambda *a: calls.append('ths') or None)
    day='20260918'
    old_request=resilient_sources._cache_key(product,None,{'date':day})
    old_key='receipt-v1:'+resilient_sources._cache_key(product,None,{'request_key':old_request})
    resilient_sources.cache.put(old_key,{'schema':1,'source':'ths' if product=='ths_limit_up' else 'eastmoney',
        'received_at':resilient_sources.time.time(),'data':[{'code':'wrong-product'}]})
    data, meta = resilient_sources.get(product, date=day)
    assert data is None and meta['status']=='failed'
    assert calls == (['ths'] if product=='ths_limit_up' else [])
    assert resilient_sources.adaptive_ttl(product)==60


def test_sentiment_reuses_original_pool_receipts_without_new_writes(tmp_path, monkeypatch):
    _window_fixture(monkeypatch,tmp_path)
    calls=[]
    clock=[1000000000.0]
    monkeypatch.setattr(resilient_sources.time,'time',lambda:clock[0])
    def fetch(name):
        calls.append(name)
        return [{'code':'000001','limit_days':2}]
    for name in ('zt','zb','dt'):
        monkeypatch.setattr(resilient_sources,'_from_em_'+name+'_pool',lambda *a,n=name:fetch(n))
    from scripts.collect_multisource import ALL_TYPES
    assert 'limit_up_sentiment' not in ALL_TYPES
    from scripts import collect_realtime_limit_pool as cli
    from pathlib import Path
    monkeypatch.setattr(cli, 'collect_l2_realtime_all_boards', lambda *a: 0)
    monkeypatch.setattr(cli, 'collect_ladder_realtime_boards', lambda *a: 0)
    monkeypatch.setattr(cli, 'init_schema', lambda con: con.execute(
        Path('migrations/0011_runtime_support_tables.sql').read_text(encoding='utf-8').split('CREATE TABLE IF NOT EXISTS auction_collection_batch')[0]))
    db = tmp_path/'pools.duckdb'
    with MultiSourceStore(db) as store:
        for name in ('zt','zb','dt'):
            data,meta=store.fetch(name+'_pool',date='20260918')
            store.store(name+'_pool',None,data,meta,trade_date='2026-09-18')
            clock[0]+=1
        before=store.con.execute('SELECT * FROM multi_source_observation ORDER BY observed_at, data_type').fetchall()
        clock[0]+=5
        first,meta=store.fetch('limit_up_sentiment',date='20260918',offline=True)
        second,again=store.fetch('limit_up_sentiment',date='20260918',offline=True)
        assert first==second and first['zt_count']==first['zb_count']==first['dt_count']==1
        assert first['max_height']==2 and first['break_rate']==50
        assert calls==['zt','zb','dt'] and meta['received_at']==again['received_at']==1000000000.0
        assert [m['product'] for m in meta['inputs']]==['zt_pool','zb_pool','dt_pool']
        assert all(m['receipt_key'] for m in meta['inputs']) and not meta['execution_ready']
        with pytest.raises(ValueError,match='not provider receipts'):
            store.store('limit_up_sentiment',None,first,meta)
        assert store.con.execute('SELECT * FROM multi_source_observation ORDER BY observed_at, data_type').fetchall()==before
        assert resilient_sources.cache.stat()==3
        projection = tmp_path/'projection.duckdb'
        def projected(query='SELECT * FROM eastmoney_limit_up_pool'):
            with duckdb.connect(str(projection), read_only=True) as con:
                return con.execute(query).fetchall()
        result = cli.collect_realtime_limit_pool(projection, '2026-09-18')
        assert result['status'] == 'success' and calls == ['zt','zb','dt']
        saved = projected()
        assert saved[0][1] == 2 and saved[0][5].timestamp() == 1000000000.0
        assert cli.collect_realtime_limit_pool(projection, '2026-09-18')['status'] == 'success'
        assert projected() == saved
        assert projected('SELECT fetched_at FROM realtime_candidate_pool_snapshot')[0][0] == saved[0][5]
        clock[0] += 61
        data, stale = resilient_sources.get('zt_pool', date='2026-09-18', offline=True)
        assert data is None and stale['status'] == 'failed' and calls == ['zt','zb','dt']
        monkeypatch.setattr(resilient_sources, '_from_em_zt_pool', lambda *a: None)
        assert cli.collect_realtime_limit_pool(projection, '2026-09-18')['status'] == 'stale'
        assert projected() == saved
        monkeypatch.setattr(resilient_sources, '_from_em_zt_pool', lambda *a: fetch('zt'))
        _,refreshed=store.fetch('limit_up_sentiment',date='20260918',ttl=0)
        assert refreshed['status']=='live' and not refreshed['cache_hit']
        assert calls==['zt','zb','dt']*2
        assert store.con.execute('SELECT * FROM multi_source_observation ORDER BY observed_at, data_type').fetchall()==before


@pytest.mark.parametrize('failure',['expired','duplicate','height','wrong_source','empty','empty_zb','empty_dt'])
def test_sentiment_rejects_unqualified_cached_inputs(tmp_path,monkeypatch,failure):
    _window_fixture(monkeypatch,tmp_path)
    clock=[1000000000.0]
    monkeypatch.setattr(resilient_sources.time,'time',lambda:clock[0])
    for name in ('zt','zb','dt'):
        rows=[{'code':'000001','limit_days':2}]
        if name=='zt' and failure=='duplicate':rows*=2
        if name=='zt' and failure=='height':rows[0]['limit_days']=None
        if failure == ('empty' if name == 'zt' else 'empty_'+name): rows=[]
        source='wrong' if name=='zt' and failure=='wrong_source' else 'eastmoney'
        monkeypatch.setitem(resilient_sources.SOURCE_PLAN,name+'_pool',[(source,lambda r=rows:r)])
        resilient_sources.get(name+'_pool',date='20260918')
    if failure=='expired':clock[0]+=61
    data,meta=resilient_sources.get('limit_up_sentiment',date='20260918',offline=True)
    assert data is None and meta['status']=='failed' and meta['derived']


def test_derived_collector_request_rejects_before_opening_writer(tmp_path,monkeypatch):
    from scripts import collect_multisource as cli
    import sys
    monkeypatch.setattr(sys,'argv',['collect','--types','limit_up_sentiment','--db',str(tmp_path/'absent.duckdb')])
    monkeypatch.setattr(cli,'MultiSourceStore',lambda *a,**kw:pytest.fail('derived collection opened database'))
    with pytest.raises(SystemExit) as error:cli.main()
    assert error.value.code==2 and not (tmp_path/'absent.duckdb').exists()
