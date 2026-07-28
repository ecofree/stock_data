import duckdb

from trade_system.integration.retirement import (
    build_retirement_checklist,
    persist_retirement_checklist,
    render_retirement_checklist_markdown,
)


def test_build_retirement_checklist_marks_projects_not_delete_safe_by_default(tmp_path):
    stock_root = tmp_path / "stock_data"
    legacy_root = tmp_path / "kpl-qds"
    tickflow_root = tmp_path / "tickflow"
    vibe_root = tmp_path / "vibe"
    for root in (stock_root, legacy_root, tickflow_root, vibe_root):
        root.mkdir()
    (stock_root / "reports").mkdir()
    (stock_root / "reports" / "external_project_audit_latest.md").write_text("audit", encoding="utf-8")
    (stock_root / "docs" / "integration").mkdir(parents=True)
    (stock_root / "docs" / "integration" / "external_capability_matrix.md").write_text("matrix", encoding="utf-8")

    rows = build_retirement_checklist(stock_root, legacy_root, tickflow_root, vibe_root)

    projects = {row["project"] for row in rows}
    assert {"kpl_qds", "tickflow", "vibe"} <= projects
    assert all(row["delete_safe"] is False for row in rows)
    assert any(row["remaining_dependency"] for row in rows)


def test_retirement_checklist_persists_and_renders(tmp_path):
    db_path = tmp_path / "retirement.duckdb"
    rows = [
        {
            "project": "tickflow",
            "capability": "strategy_engine",
            "migrated_to": "trade_system.strategy",
            "verified": True,
            "remaining_dependency": "manual review required",
            "delete_safe": False,
            "notes": "reference only",
        }
    ]

    assert persist_retirement_checklist(db_path, rows) == 1
    markdown = render_retirement_checklist_markdown(rows)

    assert "# 旧项目淘汰检查清单" in markdown
    assert "tickflow" in markdown
    assert "delete_safe" in markdown
    con = duckdb.connect(str(db_path))
    try:
        assert con.execute("SELECT count(*) FROM external_project_retirement_check").fetchone()[0] == 1
    finally:
        con.close()
