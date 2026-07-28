from __future__ import annotations

import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from base import DuckDBStore
from config import DB_PATH, TODAY
from schema import init_schema
from scripts.collect_tushare_basic_data import infer_stock_codes
from trade_system.normalize import build_normalized_views
from trade_system.tushare_backfill import (
    build_tushare_gap_list,
    plan_tushare_backfill_tasks,
    run_pending_tushare_backfill_tasks,
    write_tushare_gap_report,
)


def _split_values(value: str) -> list[str]:
    return [item.strip() for item in str(value or "").replace(";", ",").split(",") if item.strip()]


def _infer_index_codes(db_path: str | Path, limit: int) -> list[str]:
    con = DuckDBStore(str(db_path)).conn
    try:
        rows = con.execute(
            "SELECT DISTINCT index_code FROM tushare_index_daily "
            "WHERE index_code IS NOT NULL AND index_code <> '' "
            "ORDER BY index_code LIMIT ?",
            [max(1, limit)],
        ).fetchall()
        return [str(row[0]) for row in rows]
    except Exception:
        return []
    finally:
        con.close()


def _compact_date(value: str) -> str:
    raw = "".join(ch for ch in str(value or "") if ch.isdigit())
    if len(raw) >= 8:
        return raw[:8]
    raise ValueError(f"invalid date: {value}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Plan and run recoverable TuShare incremental backfill.")
    parser.add_argument("--db", default=DB_PATH)
    parser.add_argument("--start-date", required=True)
    parser.add_argument("--end-date", default=TODAY)
    parser.add_argument("--stock-codes", default="")
    parser.add_argument("--index-codes", default="SH000001,SZ399001,SZ399006")
    parser.add_argument("--max-indexes", type=int, default=3)
    parser.add_argument("--max-stocks", type=int, default=10)
    parser.add_argument("--data-kinds", default="daily,daily_basic,adj_factor")
    parser.add_argument("--run-limit", type=int, default=10)
    parser.add_argument("--retry-errors", action="store_true")
    parser.add_argument("--plan-only", action="store_true")
    parser.add_argument("--sync-core", action="store_true")
    parser.add_argument("--report", default="reports/tushare_gap_latest.md")
    args = parser.parse_args()

    start_date = _compact_date(args.start_date)
    end_date = _compact_date(args.end_date)
    data_kinds = _split_values(args.data_kinds)

    store = DuckDBStore(args.db)
    try:
        init_schema(store.conn)
    finally:
        store.close()

    stock_codes = _split_values(args.stock_codes) or infer_stock_codes(args.db, max(1, args.max_stocks))
    stock_codes = stock_codes[: max(0, args.max_stocks)]
    index_codes = _split_values(args.index_codes) or _infer_index_codes(args.db, max(1, args.max_indexes))
    index_codes = index_codes[: max(0, args.max_indexes)]
    stock_kinds = [kind for kind in data_kinds if kind != "index_daily"]
    index_kinds = [kind for kind in data_kinds if kind == "index_daily"]
    if stock_kinds and not stock_codes:
        print("status=no_stock_codes")
        return 2
    if index_kinds and not index_codes:
        print("status=no_index_codes")
        return 2

    gaps = []
    if stock_kinds:
        gaps.extend(
            build_tushare_gap_list(
                args.db,
                stock_codes=stock_codes,
                start_date=start_date,
                end_date=end_date,
                data_kinds=stock_kinds,
            )
        )
    if index_kinds:
        gaps.extend(
            build_tushare_gap_list(
                args.db,
                stock_codes=index_codes,
                start_date=start_date,
                end_date=end_date,
                data_kinds=index_kinds,
            )
        )
    planned = plan_tushare_backfill_tasks(args.db, gaps)
    results = []
    if not args.plan_only:
        results = run_pending_tushare_backfill_tasks(
            args.db,
            limit=max(0, args.run_limit),
            retry_errors=args.retry_errors,
            sync_core=args.sync_core,
        )
        if args.sync_core:
            build_normalized_views(args.db)
    report_path = write_tushare_gap_report(args.db, args.report)

    missing = sum(int(row["missing_rows"]) for row in gaps)
    print(
        "status=ok "
        f"start_date={start_date} end_date={end_date} "
        f"stocks={len(stock_codes)} indexes={len(index_codes)} data_kinds={','.join(data_kinds)} "
        f"gaps={len(gaps)} missing_rows={missing} planned_new={len(planned)} "
        f"ran={len(results)} report={report_path}"
    )
    for row in results:
        print(
            f"task={row['task_id']} status={row['status']} "
            f"rows_inserted={row['rows_inserted']} error={row['last_error']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
