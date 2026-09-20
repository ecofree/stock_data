"""Read-only legacy audit and one-time import into a separate empty archive."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import duckdb


IMPORT_TABLES = [
    "daily_sentiment",
    "daily_watchlist",
    "sector_strength",
    "ladder_stocks",
    "limit_up_stocks",
    "broken_stocks",
    "lhb_detail",
    "stock_capital_flow",
    "sector_capital_flow",
    "yesterday_limitup_detail",
    "intraday_signals",
    "trade_log",
]

PORT_FILES = [
    "engine/backtester.py",
    "engine/risk_enforcer.py",
    "engine/performance_tracker.py",
    "engine/auction_analyzer.py",
    "designs/kpl-qds-dashboard/Dashboard.html",
]

DISCARD_FILES = [
    "qmt_bridge.py",
    "engine/qmt_bridge.py",
    "config/settings.yaml",
    "qlib_ext/model_trainer.py",
    "qlib_ext/inference_pipeline.py",
]


def _count_rows(con: duckdb.DuckDBPyConnection, table_name: str) -> int:
    try:
        return int(con.execute(f'SELECT count(*) FROM "{table_name}"').fetchone()[0])
    except Exception:
        return 0


def _date_range(con: duckdb.DuckDBPyConnection, table_name: str) -> dict[str, str | None]:
    try:
        cols = {row[1] for row in con.execute(f'PRAGMA table_info("{table_name}")').fetchall()}
        date_col = next((col for col in ("date", "trade_date", "created_at", "fetched_at") if col in cols), None)
        if not date_col:
            return {"min_date": None, "max_date": None}
        row = con.execute(f'SELECT min("{date_col}"), max("{date_col}") FROM "{table_name}"').fetchone()
        return {
            "min_date": str(row[0]) if row and row[0] is not None else None,
            "max_date": str(row[1]) if row and row[1] is not None else None,
        }
    except Exception:
        return {"min_date": None, "max_date": None}


def audit_legacy_project(legacy_root: str | Path) -> dict[str, Any]:
    root = Path(legacy_root)
    db_path = root / "db" / "kpl_qds.duckdb"
    result: dict[str, Any] = {
        "legacy_root": str(root),
        "database": {
            "path": str(db_path),
            "exists": db_path.exists(),
            "size": db_path.stat().st_size if db_path.exists() else 0,
        },
        "tables": {},
        "import_tables": IMPORT_TABLES,
        "port_files": PORT_FILES,
        "discard_files": DISCARD_FILES,
    }
    if not db_path.exists():
        return result

    con = duckdb.connect(str(db_path), read_only=True)
    try:
        table_names = [
            row[0]
            for row in con.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema='main' ORDER BY table_name"
            ).fetchall()
        ]
        for name in table_names:
            result["tables"][name] = {"row_count": _count_rows(con, name), **_date_range(con, name)}
    finally:
        con.close()
    return result


def _sql_string(value: str | Path) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def import_legacy_tables(
    stock_db_path: str | Path,
    legacy_root: str | Path,
    selected_import_tables: list[str] | None = None,
) -> dict[str, int]:
    root = Path(legacy_root)
    legacy_db = root / "db" / "kpl_qds.duckdb"
    selected_tables = selected_import_tables or IMPORT_TABLES
    if set(selected_tables) - set(IMPORT_TABLES):
        raise ValueError('only named historical tables may be archived')
    if not legacy_db.exists():
        raise FileNotFoundError(str(legacy_db))
    target_path = Path(stock_db_path).resolve()
    if target_path == legacy_db.resolve() or (target_path.exists() and target_path.samefile(legacy_db)):
        raise ValueError('historical archive must not alias its source')
    if target_path.exists():
        with duckdb.connect(str(target_path), read_only=True) as check:
            if check.execute('SHOW TABLES').fetchall():
                raise ValueError('historical import requires an empty archive; existing records are protected')

    from trade_system.db_utils import legacy_connect
    con = legacy_connect(str(stock_db_path))
    attached = False
    copied: dict[str, int] = {}
    try:
        con.execute(f"ATTACH {_sql_string(legacy_db)} AS legacy_db (READ_ONLY)")
        attached = True
        existing = {
            row[0]
            for row in con.execute(
                "SELECT table_name FROM duckdb_tables() WHERE database_name='legacy_db' AND schema_name='main'"
            ).fetchall()
        }
        con.execute('BEGIN TRANSACTION')
        for table in selected_tables:
            target = f"legacy_qds_{table}"
            if table not in existing:
                copied[target] = 0
                continue
            con.execute(
                f"""
                CREATE TABLE "{target}" AS
                SELECT *, '{table}' AS legacy_source_table, current_timestamp AS legacy_imported_at
                FROM legacy_db.main."{table}"
                """
            )
            copied[target] = int(con.execute(f'SELECT count(*) FROM "{target}"').fetchone()[0])
        con.execute('COMMIT')
    except Exception:
        con.rollback()
        raise
    finally:
        if attached:
            try:
                con.execute("DETACH legacy_db")
            except Exception:
                pass
        con.close()
    return copied
