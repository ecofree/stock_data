from __future__ import annotations

import duckdb

from trade_system import resilient_sources
from trade_system import stock_data_sources


def test_new_beijing_920_prefix_routes_to_bj_without_misrouting_shanghai_b_share():
    assert stock_data_sources._norm_code("920992") == ("bj", "920992")
    assert stock_data_sources._qt_code("920992") == "bj920992"
    assert stock_data_sources._norm_code("900948") == ("sh", "900948")
from trade_system.multi_source_store import MultiSourceStore
from trade_system.multi_source_audit import audit_multisource


def test_migrated_source_plan_contains_non_tushare_capital_flow_paths():
    assert "stock_flow" in resilient_sources.SOURCE_PLAN
    assert "sector_flow" in resilient_sources.SOURCE_PLAN
    stock_sources = [name for name, _ in resilient_sources.SOURCE_PLAN["stock_flow"]("000001")]
    assert stock_sources[:2] == ["eastmoney", "sina"]
    assert stock_sources[-1] == "tushare_relay"
    assert [name for name, _ in resilient_sources.SOURCE_PLAN["sector_flow"]()] == ["eastmoney", "tushare_relay"]


def test_sector_flow_parser_normalizes_all_order_buckets(monkeypatch):
    monkeypatch.setattr(
        stock_data_sources,
        "_em_get_clist_json",
        lambda *args, **kwargs: {"data": {"diff": [{
            "f12": "BK0001", "f14": "测试板块", "f3": 2.5, "f62": 100,
            "f66": 70, "f72": 30, "f78": -10, "f84": -20,
        }]}},
    )
    row = stock_data_sources._from_em_sector_flow(10)[0]
    assert row["sector_code"] == "BK0001"
    assert row["main_net"] == 100
    assert row["super_net"] == 70
    assert row["small_net"] == -20


def test_tushare_dc_sector_flow_keeps_yuan_and_direct_net_buckets(monkeypatch):
    monkeypatch.setattr(
        stock_data_sources,
        "_tushare_query",
        lambda api, params, fields="": [{
            "trade_date": "20260714", "content_type": "概念", "ts_code": "BK0001.DC",
            "name": "测试板块", "pct_change": 2.5, "close": 100,
            "net_amount": 3_000_000_000, "buy_elg_amount": 700_000_000,
            "buy_lg_amount": 300_000_000, "buy_md_amount": -100_000_000,
            "buy_sm_amount": -900_000_000,
        }] if api == "moneyflow_ind_dc" else [],
    )
    row = stock_data_sources._from_tushare_sector_flow("20260714")[0]
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


def test_sync_sector_flow_and_kline_to_existing_core_tables(tmp_path):
    db = tmp_path / "core.duckdb"
    con = duckdb.connect(str(db))
    con.execute("create table sector_capital(date date, sector_code varchar, main_net_inflow bigint, super_net_inflow bigint, big_net_inflow bigint, mid_net_inflow bigint, small_net_inflow bigint, fetched_at timestamp)")
    con.execute("create table kline(date date, stock_code varchar, open double, high double, low double, close double, volume bigint, turnover bigint, change_pct double, ktype varchar, fetched_at timestamp, raw_json varchar)")
    con.close()
    with MultiSourceStore(db) as store:
        store.store("sector_flow", None, [{"sector_code": "BK0001", "main_net": 100, "super_net": 50, "large_net": 50, "mid_net": 0, "small_net": 0}], {"source": "eastmoney", "status": "live"}, trade_date="2026-07-10")
        store.store("kline", "000001", [{"date": "2026-07-10", "open": 10, "high": 11, "low": 9, "close": 10.5, "volume": 100, "amount": 1000}], {"source": "sina", "status": "live"})
        assert store.sync_sector_capital("2026-07-10") == 1
        assert store.sync_core_klines() == 1
        assert store.con.execute("select main_net_inflow, big_net_inflow from sector_capital").fetchone() == (100, 50)
        assert store.con.execute("select stock_code, close from kline").fetchone() == ("000001", 10.5)


def test_readiness_audit_separates_stock_and_sector_coverage(tmp_path):
    db = tmp_path / "audit.duckdb"
    with MultiSourceStore(db) as store:
        store.store("stock_flow", "000001", [{"date": "2026-07-10", "main_net": 1}], {"source": "sina", "status": "live"})
        store.store("sector_flow", None, [{"sector_code": "BK0001", "main_net": 2}], {"source": "eastmoney", "status": "live"}, trade_date="2026-07-10")
    result = audit_multisource(db, "2026-07-10")
    assert result["capital_flow"]["multi_source_stock_flow"]["coverage"] == 1
    assert result["capital_flow"]["multi_source_sector_flow"]["coverage"] == 1
    assert result["capital_flow"]["multi_source_stock_flow"]["latest"] == "2026-07-10"


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
