from __future__ import annotations

import argparse
from pathlib import Path
import sys

import duckdb

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from trade_system.daily_loop import run_daily_operator_loop


def _latest_signal_date(db_path: str) -> str:
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        row = con.execute("SELECT max(trade_date) FROM stock_candidate_score").fetchone()
        return row[0] if row and row[0] else ""
    finally:
        con.close()


def main() -> int:
    project_root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description="Build manual daily operator workflow records from generated signals.")
    parser.add_argument("--db", default=str(project_root / "kpl_data.duckdb"))
    parser.add_argument("--trade-date", default="", help="Defaults to latest stock_candidate_score trade_date.")
    parser.add_argument("--limit", type=int, default=20)
    args = parser.parse_args()

    trade_date = args.trade_date or _latest_signal_date(args.db)
    if not trade_date:
        raise SystemExit("No trade_date supplied and stock_candidate_score is empty.")
    result = run_daily_operator_loop(args.db, trade_date, args.limit)
    print(f"trade_date={trade_date}")
    for key, value in result.items():
        print(f"{key}={value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
