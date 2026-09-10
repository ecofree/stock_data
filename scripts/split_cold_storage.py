"""EXPERIMENTAL cold/hot storage split for the main DuckDB.

Moves rows older than a cutoff date from chosen tables into a separate
``cold_storage.duckdb``.  The main table keeps the recent rows; consumers
that need full history must query both databases (attach
``cold_storage.duckdb`` manually or set ``KPL_COLD_DB_PATH`` so
``base.connect_duckdb`` attaches it as ``cold``).

Known limitation: there is no automatic read-through view yet — a UNION
view over ``cold.<table>`` plus the hot table would not accept new inserts,
and DuckDB has no partitioned tables.  Until that design lands, treat this
as an archival compaction tool for history you rarely query.

Default is a DRY-RUN report.  This is a downtime operation: take a fresh
backup, stop the scheduler, then run with --execute.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))


# Candidates are append-only history families that dominate size but are
# rarely needed for intraday decisions.  Extend deliberately.
DEFAULT_CANDIDATES = (
    "tushare_daily",
    "tushare_daily_basic",
    "tushare_index_daily",
    "index_kline",
)


def _table_exists(con, name: str) -> bool:
    return bool(con.execute(
        "SELECT count(*) FROM information_schema.tables "
        "WHERE table_schema='main' AND table_name=?", [name]
    ).fetchone()[0])


def _date_column(con, table: str) -> str | None:
    cols = {r[0] for r in con.execute(f'DESCRIBE "{table}"').fetchall()}
    for candidate in ("date", "trade_date", "source_date"):
        if candidate in cols:
            return candidate
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(PROJECT_ROOT / "kpl_data.duckdb"))
    parser.add_argument("--cold-db", default=str(PROJECT_ROOT / "cold_storage.duckdb"))
    parser.add_argument(
        "--before", required=True,
        help="Move rows strictly older than this YYYY-MM-DD cutoff.",
    )
    parser.add_argument("--tables", default=",".join(DEFAULT_CANDIDATES))
    parser.add_argument("--execute", action="store_true", help="Perform the move.")
    args = parser.parse_args()

    if not Path(args.db).exists():
        print(f"database not found: {args.db}")
        return 1
    lock = Path(args.db).parent / (Path(args.db).name + ".pipeline.lock")
    if lock.exists():
        print(f"refusing to run while pipeline lock exists: {lock}")
        return 1

    from trade_system.db_utils import legacy_connect
    con = legacy_connect(args.db)
    failed = False
    try:
        cold_lit = args.cold_db.replace("'", "''")
        con.execute(f"ATTACH '{cold_lit}' AS cold")
        total_moved = 0
        for table in [t.strip() for t in args.tables.split(",") if t.strip()]:
            if not _table_exists(con, table):
                print(f"skip {table}: not present")
                continue
            date_col = _date_column(con, table)
            if date_col is None:
                print(f"skip {table}: no date column")
                continue
            old_rows = con.execute(
                f'SELECT count(*) FROM "{table}" WHERE {date_col} < ?', [args.before]
            ).fetchone()[0]
            total_rows = con.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0]
            print(f"{table}: {old_rows}/{total_rows} rows older than {args.before}")
            if not args.execute or old_rows == 0:
                continue

            # DuckDB cannot write two attached databases inside one
            # transaction, so the move is a crash-safe ordered pair:
            # (1) refresh the cold copy for the cutoff range (idempotent on
            # retry), (2) delete those rows from main.  A crash in between
            # only means duplicated data that the next run cleans up.
            con.execute(
                f'CREATE TABLE IF NOT EXISTS cold."{table}" AS '
                f'SELECT * FROM "{table}" WHERE false'
            )
            con.execute(
                f'DELETE FROM cold."{table}" WHERE {date_col} < ?',
                [args.before],
            )
            con.execute(
                f'INSERT INTO cold."{table}" '
                f'SELECT * FROM "{table}" WHERE {date_col} < ?',
                [args.before],
            )
            moved = con.execute(
                f'SELECT count(*) FROM cold."{table}" '
                f'WHERE {date_col} < ?', [args.before]
            ).fetchone()[0]
            if moved < old_rows:
                print(f"  FAILED {table}: moved {moved} < expected {old_rows}")
                failed = True
                break
            con.execute(f'DELETE FROM "{table}" WHERE {date_col} < ?', [args.before])
            total_moved += old_rows
            print(f"  moved {old_rows} row(s)")
        print(f"done; rows moved this run: {total_moved}")
        return 1 if failed else 0
    finally:
        con.close()


if __name__ == "__main__":
    sys.exit(main())
