from __future__ import annotations

import duckdb

from trade_system.flow_contract import ensure_stock_flow_contract, migrate_existing_stock_flow, normalize_stock_flow_row


def test_tushare_total_is_not_main_orders_net():
    row = normalize_stock_flow_row(
        {
            "net_mf_amount": 8,
            "buy_sm_amount": 1,
            "sell_sm_amount": 0,
            "buy_md_amount": 2,
            "sell_md_amount": 0,
            "buy_lg_amount": 3,
            "sell_lg_amount": 1,
            "buy_elg_amount": 4,
            "sell_elg_amount": 1,
        },
        "tushare_relay",
    )
    assert row["main_net"] == 50_000
    assert row["net_total"] == 80_000
    assert row["flow_definition"] == "main_orders_net"
    assert row["amount_unit"] == "yuan"


def test_migration_preserves_total_and_recalculates_tushare_main(tmp_path):
    db = tmp_path / "flow.duckdb"
    con = duckdb.connect(str(db))
    con.execute("create table multi_source_stock_flow(source_date date,stock_code varchar,main_net double,super_net double,large_net double,provider varchar,raw_json varchar)")
    con.execute("insert into multi_source_stock_flow values ('2026-07-10','000001',80000,30000,20000,'tushare','{}')")
    ensure_stock_flow_contract(con)
    result = migrate_existing_stock_flow(con)
    assert result["legacy_tushare_rows"] == 1
    assert con.execute("select main_net,net_total,flow_definition,field_mapping_version from multi_source_stock_flow").fetchone() == (50000, 80000, "main_orders_net", "stock_flow_v2")
    con.close()
