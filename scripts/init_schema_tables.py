from __future__ import annotations

import argparse
from pathlib import Path
import sys

import duckdb

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from schema import init_schema


def main() -> int:
    parser = argparse.ArgumentParser(description="Create all schema.py tables in the DuckDB database.")
    parser.add_argument("--db", default="kpl_data.duckdb")
    args = parser.parse_args()
    con = duckdb.connect(args.db)
    try:
        init_schema(con)
    finally:
        con.close()
    print(f"Schema tables initialized in {args.db}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
