"""Read-only legacy holdings inspection; current accounts use V2 snapshot imports.

The old add/remove/close writers are retired. Existing records are preserved;
no cost-price fallback, inferred empty account or current account qualification.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

import duckdb

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
from trade_system.quality import table_exists


def inspect_holdings(db_path):
    with duckdb.connect(str(db_path), read_only=True) as con:
        exists = table_exists(con, "holdings")
        rows = []
        if exists:
            cursor = con.execute("SELECT * FROM holdings ORDER BY stock_code, entry_date")
            columns = [c[0] for c in cursor.description]
            rows = [dict(zip(columns, row)) for row in cursor.fetchall()]
    return {"scope": "historical_holdings_not_current_account", "execution_ready": False,
            "database_writes": 0, "table_present": exists, "rows": rows}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(PROJECT_ROOT / "kpl_data.duckdb"))
    parser.add_argument("command", choices=["show"], nargs="?", default="show")
    args = parser.parse_args()
    print(json.dumps(inspect_holdings(args.db), ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
