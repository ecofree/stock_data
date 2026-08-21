"""Resumable, date-batched 2026 TuShare history backfill."""

from __future__ import annotations

import argparse
from datetime import date
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from trade_system.tushare_history import TushareHistoryCollector, render_report  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Batch TuShare 2026 daily/basic/moneyflow history with checkpoints.")
    parser.add_argument("--db", default="kpl_data.duckdb")
    parser.add_argument("--start-date", default="20260101")
    parser.add_argument("--end-date", default=date.today().strftime("%Y%m%d"))
    parser.add_argument("--datasets", default="stock_basic,daily,daily_basic,adj_factor,moneyflow,industry_flow",
                        help="Comma-separated: stock_basic,daily,daily_basic,adj_factor,moneyflow,industry_flow")
    parser.add_argument("--max-days", type=int, default=0, help="Limit this invocation; 0 means all open dates.")
    parser.add_argument("--gap-only", action="store_true", help="Only process dates whose requested dataset is not complete.")
    parser.add_argument("--budget-seconds", type=float, default=300.0)
    parser.add_argument("--request-timeout", type=int, default=20)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument(
        "--retry-passes",
        type=int,
        default=1,
        help="Retry only failed date/dataset checkpoints after the first pass.",
    )
    parser.add_argument(
        "--retry-delay-seconds",
        type=float,
        default=15.0,
        help="Backoff before each failed-checkpoint retry pass.",
    )
    parser.add_argument("--batch-limit", type=int, default=5000)
    parser.add_argument("--moneyflow-page-size", type=int, default=1000)
    parser.add_argument("--force", action="store_true", help="Re-fetch successful checkpoints.")
    parser.add_argument("--report", default="reports/tushare_2026_backfill_latest.md")
    args = parser.parse_args()
    datasets = [item.strip() for item in args.datasets.split(",") if item.strip()]
    with TushareHistoryCollector(
        args.db,
        request_timeout=args.request_timeout,
        retries=args.retries,
        batch_limit=args.batch_limit,
        moneyflow_page_size=args.moneyflow_page_size,
        budget_seconds=args.budget_seconds,
    ) as collector:
        result = collector.run(
            args.start_date,
            args.end_date,
            datasets=datasets,
            max_days=args.max_days or None,
            force=args.force,
            gap_only=args.gap_only,
            retry_passes=args.retry_passes,
            retry_delay_seconds=args.retry_delay_seconds,
        )
    report = render_report(args.db, result, args.report)
    summary = {}
    for item in result["results"]:
        summary[item["status"]] = summary.get(item["status"], 0) + 1
    print(
        f"tushare_history start={result['start_date']} end={result['end_date']} "
        f"dates={len(result['dates'])} results={len(result['results'])} summary={summary} report={report}"
    )
    incomplete = summary.get("error", 0) + summary.get("budget_exhausted", 0)
    return 0 if not incomplete else 2


if __name__ == "__main__":
    raise SystemExit(main())
