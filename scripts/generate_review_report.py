from __future__ import annotations

import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from trade_system.review import write_daily_report


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate daily trading assistant report.")
    parser.add_argument("--db", default="kpl_data.duckdb")
    parser.add_argument("--date")
    parser.add_argument("--out", default="reports/daily_trading_report_latest.md")
    args = parser.parse_args()
    out_path = write_daily_report(args.db, args.out, args.date)
    print(f"Daily trading report: out={out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
