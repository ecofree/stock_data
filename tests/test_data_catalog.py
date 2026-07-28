import duckdb

from trade_system.integration.capability_registry import build_capability_registry_rows
from trade_system.integration.data_catalog import (
    build_default_data_sources,
    install_data_catalog,
    render_adapter_contract_markdown,
    render_data_source_catalog_markdown,
)
from trade_system.integration.external_audit import audit_external_projects


def test_build_default_data_sources_covers_operator_domains():
    sources = build_default_data_sources()

    source_ids = {row["source_id"] for row in sources}
    domains = {row["data_domain"] for row in sources}

    assert "kpl_api" in source_ids
    assert "legacy_qds" in source_ids
    assert "tickflow_custom" in source_ids
    assert "vibe_a_stock_data" in source_ids
    assert "qlib_shadow" in source_ids
    assert {"auction", "sector_capital", "kline", "research", "ml_shadow"} <= domains


def test_build_capability_registry_rows_reuses_phase11_decisions(tmp_path):
    stock_root = tmp_path / "stock_data"
    legacy_root = tmp_path / "kpl-qds"
    tickflow_root = tmp_path / "tickflow"
    vibe_root = tmp_path / "vibe"
    for root in (stock_root, legacy_root, tickflow_root, vibe_root):
        root.mkdir()

    audit = audit_external_projects(stock_root, legacy_root, tickflow_root, vibe_root)
    rows = build_capability_registry_rows(audit)

    by_id = {row["capability_id"]: row for row in rows}
    assert by_id["kpl_qds.qmt_bridge"]["decision"] == "forbid"
    assert by_id["tickflow.strategy_engine"]["owner_layer"] == "strategy"
    assert by_id["vibe.news_radar"]["owner_layer"] == "research"
    assert by_id["kpl_qds.qlib_shadow"]["owner_layer"] == "ml_shadow"


def test_install_data_catalog_creates_idempotent_tables_and_coverage_view(tmp_path):
    db_path = tmp_path / "catalog.duckdb"
    stock_root = tmp_path / "stock_data"
    legacy_root = tmp_path / "kpl-qds"
    tickflow_root = tmp_path / "tickflow"
    vibe_root = tmp_path / "vibe"
    for root in (stock_root, legacy_root, tickflow_root, vibe_root):
        root.mkdir()
    audit = audit_external_projects(stock_root, legacy_root, tickflow_root, vibe_root)

    first = install_data_catalog(db_path, audit)
    second = install_data_catalog(db_path, audit)

    assert first == second
    assert first["data_source_catalog"] >= 6
    assert first["external_capability_registry"] >= 10

    con = duckdb.connect(str(db_path))
    try:
        raw_tables = {
            row[0]
            for row in con.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema='main'"
            ).fetchall()
        }
        assert "data_source_catalog" in raw_tables
        assert "external_capability_registry" in raw_tables
        coverage = con.execute(
            "SELECT data_domain, source_count FROM v_data_source_coverage WHERE data_domain='research'"
        ).fetchone()
        assert coverage[0] == "research"
        assert coverage[1] >= 1
    finally:
        con.close()


def test_catalog_markdown_documents_sources_and_adapter_contract():
    catalog_doc = render_data_source_catalog_markdown(build_default_data_sources())
    contract_doc = render_adapter_contract_markdown()

    assert "# 统一数据源目录" in catalog_doc
    assert "kpl_api" in catalog_doc
    assert "vibe_a_stock_data" in catalog_doc
    assert "# Adapter Contract" in contract_doc
    assert "不读取或输出密钥" in contract_doc
