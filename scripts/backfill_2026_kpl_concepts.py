"""Backfill dated KPL concept ranking and constituent snapshots."""

from __future__ import annotations

import argparse
from datetime import date
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from trade_system.kpl_history import KPLHistoryCollector, render_report  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Backfill KPL concept/constituent history with checkpoints.")
    parser.add_argument("--db", default="kpl_data.duckdb")
    parser.add_argument("--start-date", default="20260101")
    parser.add_argument("--end-date", default=date.today().strftime("%Y%m%d"))
    parser.add_argument("--mode", choices=("ranking", "full"), default="ranking",
                        help="ranking is dated and efficient; full is a current all-plate snapshot labelled at the final date.")
    parser.add_argument("--max-days", type=int, default=0)
    parser.add_argument("--budget-seconds", type=float, default=300.0)
    parser.add_argument("--request-timeout", type=float, default=10.0)
    parser.add_argument("--max-attempts", type=int, default=1)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--report", default="reports/kpl_2026_concepts_latest.md")
    args = parser.parse_args()
    with KPLHistoryCollector(args.db, request_timeout=args.request_timeout,
                             max_attempts=args.max_attempts, budget_seconds=args.budget_seconds) as collector:
        result = collector.run(args.start_date, args.end_date, max_days=args.max_days or None,
                               force=args.force, mode=args.mode)
    report = render_report(args.db, result, args.report)
    print(f"kpl_history mode={args.mode} dates={len(result['dates'])} results={len(result['results'])} report={report} stats={result['stats']}")
    return 0 if not any(item["status"] == "error" for item in result["results"]) else 2


if __name__ == "__main__":
    raise SystemExit(main())

