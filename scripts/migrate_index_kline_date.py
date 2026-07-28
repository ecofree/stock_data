"""Normalize ``index_kline.date`` to DuckDB DATE.

The table was originally created with a VARCHAR date, unlike the stock K-line
table and every downstream date checkpoint.  This idempotent migration checks
for invalid values, refuses to silently discard them, and only then changes
the column type in a transaction.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

import duckdb

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def migrate(db_path: str | Path) -> dict[str, int | str]:
    con = duckdb.connect(str(db_path))
    try:
        exists = int(
            con.execute(
                "SELECT count(*) FROM information_schema.tables WHERE table_name='index_kline'"
            ).fetchone()[0]
        )
        if not exists:
            return {"status": "missing_table", "rows": 0, "invalid_rows": 0}
        column = con.execute("SELECT data_type FROM information_schema.columns WHERE table_name='index_kline' AND column_name='date'").fetchone()
        if not column:
            return {"status": "missing_date_column", "rows": 0, "invalid_rows": 0}
        rows = int(con.execute("SELECT count(*) FROM index_kline").fetchone()[0])
        if str(column[0]).upper() == "DATE":
            return {"status": "already_date", "rows": rows, "invalid_rows": 0}
        invalid = int(
            con.execute(
                "SELECT count(*) FROM index_kline WHERE date IS NOT NULL AND try_cast(date AS DATE) IS NULL"
            ).fetchone()[0]
        )
        if invalid:
            raise ValueError(f"index_kline contains {invalid} invalid date values; migration refused")
        dependent_indexes = [
            (str(row[4]), str(row[13]))
            for row in con.execute(
                "SELECT * FROM duckdb_indexes() WHERE table_name='index_kline'"
            ).fetchall()
            if row[4]
        ]
        # DuckDB keeps index dependencies visible until the DROP statement is
        # committed, so this is intentionally two short transactions.  The
        # index DDL is captured above and restored on any conversion error.
        con.execute("BEGIN")
        try:
            for index_name, _ in dependent_indexes:
                con.execute(f'DROP INDEX IF EXISTS "{index_name}"')
            con.execute("COMMIT")
            con.execute("BEGIN")
            con.execute(
                "ALTER TABLE index_kline ALTER COLUMN date SET DATA TYPE DATE USING try_cast(date AS DATE)"
            )
            for _, index_sql in dependent_indexes:
                con.execute(index_sql.rstrip(";"))
            con.execute("COMMIT")
        except Exception:
            try:
                con.execute("ROLLBACK")
            except Exception:
                pass
            # Restore captured indexes if the conversion failed after the
            # drop commit.  Existing indexes are ignored so a retry remains
            # safe and idempotent.
            for _, index_sql in dependent_indexes:
                try:
                    con.execute(index_sql.rstrip(";"))
                except Exception:
                    pass
            raise
        return {"status": "migrated", "rows": rows, "invalid_rows": 0}
    finally:
        con.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Normalize index_kline.date from VARCHAR to DATE.")
    parser.add_argument("--db", default=str(ROOT / "kpl_data.duckdb"))
    args = parser.parse_args()
    result = migrate(args.db)
    print(" ".join(f"{key}={value}" for key, value in result.items()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
