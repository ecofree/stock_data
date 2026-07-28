"""Run the 2026-to-date TuShare backfill in resumable trading-day batches.

This is intentionally a thin orchestrator around ``TushareHistoryCollector``:
each batch has its own timeout budget, while the collector's date/dataset
checkpoints make retries idempotent.  A failed date never advances the batch
cursor silently; the next invocation retries it first.
"""

from __future__ import annotations

import argparse
from datetime import date
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from trade_system.tushare_history import TushareHistoryCollector, render_report  # noqa: E402


def _success_dates(db: str, start_date: str, end_date: str) -> list[str]:
    """Return open dates from the stored TuShare calendar, if available."""
    import duckdb

    con = duckdb.connect(db, read_only=True)
    try:
        rows = con.execute(
            "SELECT CAST(cal_date AS VARCHAR) FROM tushare_trade_cal "
            "WHERE is_open AND cal_date BETWEEN CAST(? AS DATE) AND CAST(? AS DATE) ORDER BY cal_date",
            [start_date, end_date],
        ).fetchall()
        return [str(row[0])[:10] for row in rows]
    finally:
        con.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Resumable 2026-to-date TuShare backfill.")
    parser.add_argument("--db", default="kpl_data.duckdb")
    parser.add_argument("--start-date", default="20260101")
    parser.add_argument("--end-date", default=date.today().strftime("%Y%m%d"))
    parser.add_argument("--datasets", default="stock_basic,daily,daily_basic,adj_factor,moneyflow,industry_flow")
    parser.add_argument("--batch-days", type=int, default=5)
    parser.add_argument("--batch-budget-seconds", type=float, default=600.0)
    parser.add_argument("--request-timeout", type=int, default=12)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--batch-limit", type=int, default=5000)
    parser.add_argument("--moneyflow-page-size", type=int, default=1000)
    parser.add_argument("--max-batches", type=int, default=0, help="0 means all remaining batches")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--report", default="reports/tushare_2026_ytd_latest.md")
    args = parser.parse_args()
    datasets = [item.strip() for item in args.datasets.split(",") if item.strip()]
    batch_days = max(1, int(args.batch_days))

    # Ensure the calendar once, then reuse it for the batch cursor.
    with TushareHistoryCollector(
        args.db,
        request_timeout=args.request_timeout,
        retries=args.retries,
        batch_limit=args.batch_limit,
        moneyflow_page_size=args.moneyflow_page_size,
        budget_seconds=min(args.batch_budget_seconds, 30.0),
    ) as bootstrap:
        dates = bootstrap.ensure_calendar(args.start_date, args.end_date)
    if not dates:
        dates = _success_dates(args.db, args.start_date, args.end_date)
    if not dates:
        print("No trading dates available in requested range")
        return 2

    batches = [dates[index:index + batch_days] for index in range(0, len(dates), batch_days)]
    if args.max_batches:
        batches = batches[: max(0, int(args.max_batches))]
    summaries = []
    for index, batch in enumerate(batches, 1):
        started = time.monotonic()
        with TushareHistoryCollector(
            args.db,
            request_timeout=args.request_timeout,
            retries=args.retries,
            batch_limit=args.batch_limit,
            moneyflow_page_size=args.moneyflow_page_size,
            budget_seconds=args.batch_budget_seconds,
        ) as collector:
            result = collector.run(
                batch[0], batch[-1], datasets=datasets,
                max_days=len(batch), force=args.force,
            )
        counts = {}
        for item in result["results"]:
            counts[item["status"]] = counts.get(item["status"], 0) + 1
        summaries.append({"batch": index, "start": batch[0], "end": batch[-1], "summary": counts})
        print(
            f"batch={index}/{len(batches)} dates={batch[0]}..{batch[-1]} "
            f"summary={counts} elapsed={time.monotonic() - started:.1f}s",
            flush=True,
        )

    final = {
        "start_date": args.start_date[:4] + "-" + args.start_date[4:6] + "-" + args.start_date[6:8],
        "end_date": args.end_date[:4] + "-" + args.end_date[4:6] + "-" + args.end_date[6:8],
        "dates": dates,
        "elapsed_seconds": 0,
        "batches": summaries,
    }
    report = render_report(args.db, final, args.report)
    print(f"ytd_backfill dates={len(dates)} batches={len(batches)} report={report}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
