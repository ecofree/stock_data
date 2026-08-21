"""Tests for the incremental migration runner."""
from __future__ import annotations

import duckdb
import pytest

from trade_system.migrations import (
    apply_pending,
    applied_versions,
    discover_migrations,
)


@pytest.fixture()
def con():
    connection = duckdb.connect(":memory:")
    yield connection
    connection.close()


def _write_migration(directory, name, sql):
    path = directory / name
    path.write_text(sql, encoding="utf-8")
    return path


def test_discover_ignores_non_numbered_files(tmp_path):
    (tmp_path / "README.md").write_text("docs", encoding="utf-8")
    (tmp_path / "notes.txt").write_text("x", encoding="utf-8")
    _write_migration(tmp_path, "0002_add_widget.sql", "SELECT 1")
    _write_migration(tmp_path, "bad_name.sql", "SELECT 2")

    found = discover_migrations(tmp_path)
    assert [version for version, _ in found] == [2]
    assert found[0][1].name == "0002_add_widget.sql"


def test_apply_pending_runs_once_and_records_version(tmp_path, con):
    _write_migration(
        tmp_path,
        "0007_create_demo.sql",
        "CREATE TABLE IF NOT EXISTS demo(id INTEGER)",
    )
    first = apply_pending(con, tmp_path)
    assert first == [7]
    assert con.execute("SELECT count(*) FROM demo").fetchone()[0] == 0
    assert applied_versions(con) == {7}

    second = apply_pending(con, tmp_path)
    assert second == []


def test_failed_migration_leaves_no_partial_state(tmp_path, con):
    _write_migration(
        tmp_path,
        "0003_two_steps.sql",
        "CREATE TABLE partial_a(id INTEGER);\n"
        "THIS IS NOT VALID SQL",
    )
    with pytest.raises(RuntimeError, match="0003"):
        apply_pending(con, tmp_path)

    tables = {
        row[0]
        for row in con.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema='main'"
        ).fetchall()
    }
    # The bookkeeping row must not record the failed version and the valid
    # first statement must be rolled back together with the invalid one.
    assert 3 not in applied_versions(con)
    assert "partial_a" not in tables
    assert "schema_migration" in tables


def test_applies_versions_in_sorted_order(tmp_path, con):
    _write_migration(tmp_path, "0010_late.sql", "CREATE TABLE t10(i INTEGER)")
    _write_migration(tmp_path, "0009_early.sql", "CREATE TABLE t09(i INTEGER)")

    applied = apply_pending(con, tmp_path)
    assert applied == [9, 10]


def test_dry_run_does_not_touch_database(tmp_path, con):
    _write_migration(tmp_path, "0005_future.sql", "CREATE TABLE t05(i INTEGER)")
    applied = apply_pending(con, tmp_path, dry_run=True)
    assert applied == [5]
    assert applied_versions(con) == set()
