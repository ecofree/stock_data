from __future__ import annotations

import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from trade_system.web_report import write_dashboard


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate a static HTML inspection dashboard.")
    parser.add_argument("--db", default="kpl_data.duckdb")
    parser.add_argument("--out", default="reports/trading_dashboard_latest.html")
    parser.add_argument("--date", default=None, help="Pin every market-state lookup to one trade date.")
    args = parser.parse_args()
    path = write_dashboard(args.db, args.out, trade_date=args.date)
    print(f"Web dashboard: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
