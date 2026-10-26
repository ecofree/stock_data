"""Read-only cutoff inventory. Destructive cold-storage split is retired.

The old algorithm erased previously archived history on subsequent moves.
--execute is always refused. --cold-db is accepted only for old callers and is
never opened or created. Future migration needs a separate preservation contract.
"""
from __future__ import annotations

import argparse
from datetime import date
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
    cols = {r[0] for r in con.execute(f'DESCRIBE "{table.replace(chr(34), chr(34)*2)}"').fetchall()}
    for candidate in ("date", "trade_date", "source_date"):
        if candidate in cols:
            return candidate
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(PROJECT_ROOT / "kpl_data.duckdb"))
    parser.add_argument("--cold-db", default=str(PROJECT_ROOT / "cold_storage.duckdb"))
    parser.add_argument(
        "--before", required=True, type=date.fromisoformat,
        help="Inventory rows strictly older than this YYYY-MM-DD cutoff.",
    )
    parser.add_argument("--tables", default=",".join(DEFAULT_CANDIDATES))
    parser.add_argument("--execute", action="store_true", help="Retired: always refused before accessing either database.")
    args = parser.parse_args()

    if args.execute:
        print('RETIRED: destructive cold-storage split disabled; use read-only inventory.', file=sys.stderr)
        return 2
    if not Path(args.db).is_file():
        print(f'database not found: {args.db}', file=sys.stderr)
        return 1

    from trade_system.db_utils import legacy_connect
    con = legacy_connect(args.db, read_only=True)
    try:
        for table in [t.strip() for t in args.tables.split(",") if t.strip()]:
            if not _table_exists(con, table):
                print(f"skip {table}: not present")
                continue
            date_col = _date_column(con, table)
            if date_col is None:
                print(f"skip {table}: no date column")
                continue
            quoted_table = table.replace(chr(34), chr(34)*2)
            old_rows = con.execute(
                f'SELECT count(*) FROM "{quoted_table}" WHERE {date_col} < ?', [args.before]
            ).fetchone()[0]
            total_rows = con.execute(f'SELECT count(*) FROM "{quoted_table}"').fetchone()[0]
            print(f"{table}: {old_rows}/{total_rows} rows older than {args.before}")
        print('read-only inventory complete; no cold database accessed, no rows moved')
        return 0
    finally:
        con.close()


if __name__ == "__main__":
    sys.exit(main())
