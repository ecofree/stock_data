import duckdb

from trade_system.integration.external_audit import (
    audit_external_projects,
    render_capability_matrix_markdown,
    render_external_audit_markdown,
    write_phase11_outputs,
)


def _make_duckdb(path, table_sql, insert_sql=None):
    con = duckdb.connect(str(path))
    con.execute(table_sql)
    if insert_sql:
        con.execute(insert_sql)
    con.close()


def test_audit_external_projects_classifies_capabilities_and_counts_database_rows(tmp_path):
    stock_root = tmp_path / "stock_data"
    legacy_root = tmp_path / "kpl-qds"
    tickflow_root = tmp_path / "tickflow-stock-panel"
    vibe_root = tmp_path / "Vibe-Research"

    (stock_root / "reports").mkdir(parents=True)
    (stock_root / "reports" / "data_quality_latest.md").write_text("# data quality\n", encoding="utf-8")
    _make_duckdb(
        stock_root / "kpl_data.duckdb",
        "CREATE TABLE auction_tick(symbol VARCHAR)",
    )

    (legacy_root / "db").mkdir(parents=True)
    (legacy_root / "engine").mkdir(parents=True)
    (legacy_root / "config").mkdir(parents=True)
    _make_duckdb(
        legacy_root / "db" / "kpl_qds.duckdb",
        "CREATE TABLE daily_watchlist(symbol VARCHAR)",
        "INSERT INTO daily_watchlist VALUES ('000001')",
    )
    (legacy_root / "engine" / "signal_fusion.py").write_text("# signal fusion\n", encoding="utf-8")
    (legacy_root / "engine" / "qmt_bridge.py").write_text("# auto trade bridge\n", encoding="utf-8")
    (legacy_root / "config" / "settings.yaml").write_text("api_key: SECRET_VALUE\n", encoding="utf-8")

    (tickflow_root / "backend" / "app" / "strategy").mkdir(parents=True)
    (tickflow_root / "frontend" / "src" / "pages").mkdir(parents=True)
    (tickflow_root / "backend" / "app" / "strategy" / "engine.py").write_text("# strategy\n", encoding="utf-8")
    (tickflow_root / "frontend" / "src" / "pages" / "Dashboard.tsx").write_text("// ui\n", encoding="utf-8")

    (vibe_root / "backend").mkdir(parents=True)
    (vibe_root / "a-stock-data").mkdir(parents=True)
    (vibe_root / "backend" / "newsradar.py").write_text("# news radar\n", encoding="utf-8")
    (vibe_root / "backend" / "app.py").write_text("# fastapi app\n", encoding="utf-8")
    (vibe_root / "a-stock-data" / "SKILL.md").write_text("# a stock data\n", encoding="utf-8")

    audit = audit_external_projects(stock_root, legacy_root, tickflow_root, vibe_root)

    assert audit["projects"]["stock_data"]["database"]["tables"]["auction_tick"]["row_count"] == 0
    assert audit["projects"]["kpl_qds"]["database"]["tables"]["daily_watchlist"]["row_count"] == 1

    decisions = {item["capability_id"]: item["decision"] for item in audit["capability_matrix"]}
    assert decisions["kpl_qds.signal_fusion"] == "port"
    assert decisions["kpl_qds.qmt_bridge"] == "forbid"
    assert decisions["tickflow.strategy_engine"] == "port"
    assert decisions["tickflow.react_workbench"] == "reference"
    assert decisions["vibe.news_radar"] == "port"
    assert decisions["vibe.fastapi_app"] == "reference"

    forbidden = {item["capability_id"] for item in audit["forbidden_capabilities"]}
    assert "kpl_qds.qmt_bridge" in forbidden
    assert "kpl_qds.hardcoded_settings" in forbidden


def test_rendered_phase11_outputs_include_operator_sections_without_secret_values(tmp_path):
    stock_root = tmp_path / "stock_data"
    legacy_root = tmp_path / "kpl-qds"
    tickflow_root = tmp_path / "tickflow-stock-panel"
    vibe_root = tmp_path / "Vibe-Research"
    for root in (stock_root, legacy_root, tickflow_root, vibe_root):
        root.mkdir(parents=True)
    (legacy_root / "config").mkdir()
    (legacy_root / "config" / "settings.yaml").write_text("api_key: SECRET_VALUE\n", encoding="utf-8")

    audit = audit_external_projects(stock_root, legacy_root, tickflow_root, vibe_root)

    report = render_external_audit_markdown(audit)
    matrix = render_capability_matrix_markdown(audit)

    assert "# Phase 11 三项目深度审计报告" in report
    assert "职业操盘" in report
    assert "禁止迁入" in report
    assert "能力矩阵" in matrix
    assert "SECRET_VALUE" not in report
    assert "SECRET_VALUE" not in matrix

    outputs = write_phase11_outputs(
        audit,
        report_path=stock_root / "reports" / "external_project_audit_latest.md",
        matrix_path=stock_root / "docs" / "integration" / "external_capability_matrix.md",
    )

    assert outputs["report"].exists()
    assert outputs["matrix"].exists()
    assert outputs["report"].stat().st_size > 0
    assert outputs["matrix"].stat().st_size > 0
