"""Generate the 2026 coverage, missing-date and capital-flow quality report."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config import DB_PATH  # noqa: E402
from trade_system.data_gap_audit import write_data_gap_report  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit 2026 data coverage and gaps.")
    parser.add_argument("--db", default=DB_PATH)
    parser.add_argument("--start-date", default="20260101")
    parser.add_argument("--end-date", default="20260714")
    parser.add_argument("--out", default="reports/2026_data_quality_latest.md")
    args = parser.parse_args()
    audit = write_data_gap_report(args.db, args.out, args.start_date, args.end_date)
    print(f"data_gap_audit expected_days={audit['expected_trading_days']} issues={len(audit['issues'])} out={args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

