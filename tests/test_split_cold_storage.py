"""Tests for the cold-storage split script (mechanics on temp databases)."""
from __future__ import annotations

import sys
from pathlib import Path

import duckdb
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.split_cold_storage import main  # noqa: E402


@pytest.fixture()
def db(tmp_path):
    path = tmp_path / "main.duckdb"
    con = duckdb.connect(str(path))
    con.execute(
        "CREATE TABLE kline(date DATE, stock_code VARCHAR, close DOUBLE)"
    )
    con.execute(
        "INSERT INTO kline VALUES "
        "('2025-12-31','000001',10.0),"
        "('2026-01-05','000001',11.0),"
        "('2026-02-01','000002',12.0)"
    )
    con.close()
    return path


def test_dry_run_does_not_move_rows(db, tmp_path, capsys):
    rc = main_with_args(db, tmp_path, execute=False)
    assert rc == 0
    out = capsys.readouterr().out
    assert "3/3 rows" in out  # all rows older than 2026-06-01? use before=2026-06-01
    con = duckdb.connect(str(db), read_only=True)
    assert con.execute("SELECT count(*) FROM kline").fetchone()[0] == 3
    con.close()


def test_execute_moves_old_rows_and_keeps_recent(db, tmp_path):
    rc = main_with_args(db, tmp_path, execute=True, before="2026-01-01")
    assert rc == 0
    con = duckdb.connect(str(db), read_only=True)
    cold = duckdb.connect(str(tmp_path / "cold.duckdb"), read_only=True)
    assert con.execute("SELECT count(*) FROM kline").fetchone()[0] == 2
    assert cold.execute("SELECT count(*) FROM kline").fetchone()[0] == 1
    # recent row intact in the hot table
    assert con.execute("SELECT close FROM kline WHERE date='2026-01-05'").fetchone()[0] == 11.0
    # moved row queryable from cold via the standard attach name
    con.close()
    cold.close()


def test_rerun_is_idempotent(db, tmp_path):
    assert main_with_args(db, tmp_path, execute=True, before="2026-01-01") == 0
    # second run finds nothing new and leaves counts unchanged
    assert main_with_args(db, tmp_path, execute=True, before="2026-01-01") == 0
    con = duckdb.connect(str(db), read_only=True)
    cold = duckdb.connect(str(tmp_path / "cold.duckdb"), read_only=True)
    assert con.execute("SELECT count(*) FROM kline").fetchone()[0] == 2
    assert cold.execute("SELECT count(*) FROM kline").fetchone()[0] == 1
    con.close()
    cold.close()


def test_attach_readthrough_via_env_name(db, tmp_path):
    """KPL_COLD_DB_PATH attach (base.connect_duckdb) makes cold visible as ``cold``."""
    import base

    rc = main_with_args(db, tmp_path, execute=True, before="2026-01-01")
    assert rc == 0
    monkey = pytest.MonkeyPatch()
    monkey.setenv("KPL_COLD_DB_PATH", str(tmp_path / "cold.duckdb"))
    try:
        store = base.DuckDBStore(str(db))
        total = store.fetchall(
            "SELECT (SELECT count(*) FROM kline) + (SELECT count(*) FROM cold.kline)"
        )[0][0]
        assert total == 3
        store.close()
    finally:
        monkey.undo()


def main_with_args(db, tmp_path, *, execute, before="2026-06-01"):
    argv = [
        "split_cold_storage.py",
        "--db", str(db),
        "--cold-db", str(tmp_path / "cold.duckdb"),
        "--before", before,
        "--tables", "kline",
    ]
    if execute:
        argv.append("--execute")
    old = sys.argv
    sys.argv = argv
    try:
        return main()
    finally:
        sys.argv = old
