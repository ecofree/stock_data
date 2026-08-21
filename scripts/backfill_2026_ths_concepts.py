"""采集同花顺概念/所属个股快照，并记录 2026 历史缺口。

同花顺热榜接口目前没有历史日期参数，本脚本不会循环把同一份快照写入
每个历史交易日；它只保存真实抓取日，并在报告中列出缺失日期。
"""

from __future__ import annotations

import argparse
from datetime import date
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config import DB_PATH  # noqa: E402
from trade_system.ths_history import THSConceptHistoryCollector, render_report  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Collect THS concept/member snapshot without backdating it.")
    parser.add_argument("--db", default=DB_PATH)
    parser.add_argument("--start-date", default="20260101")
    parser.add_argument("--end-date", default=date.today().strftime("%Y%m%d"))
    parser.add_argument("--period", choices=("hour", "day", "week"), default="day")
    parser.add_argument("--mode", choices=("hot", "full"), default="full",
                        help="full reads the THS concept catalogue and member pages; hot reads only the top-100 hot list.")
    parser.add_argument("--max-member-pages", type=int, default=0,
                        help="Pages per THS concept board; 0 discovers and fetches every advertised page, positive values are bounded partial mode.")
    parser.add_argument("--max-concepts", type=int, default=0,
                        help="Maximum unfinished boards per invocation; 0 processes all unfinished boards (weekly job can resume checkpoints).")
    parser.add_argument("--member-source", choices=("web", "tushare"), default="web",
                        help="web crawls the THS concept pages (project default); tushare is explicit opt-in.")
    parser.add_argument("--snapshot-date", default="",
                        help="Resume a specific stored snapshot date (YYYYMMDD). Use only for an interrupted/partial checkpoint; it is never marked as date-verified unless it is today.")
    parser.add_argument("--retry-stale", action="store_true",
                        help="Retry success_stale boards too; default recovery skips cached boards and focuses on missing/error checkpoints.")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--report", default="reports/ths_2026_concepts_latest.md")
    args = parser.parse_args()

    with THSConceptHistoryCollector(args.db, period=args.period, mode=args.mode,
                                    max_member_pages=args.max_member_pages,
                                    member_source=args.member_source,
                                    max_concepts=args.max_concepts,
                                    retry_stale=args.retry_stale) as collector:
        # The collector intentionally stores only today's fetched snapshot;
        # the requested range is used to enumerate historical missing dates.
        collector.period = args.period
        result = collector.run(args.start_date, args.end_date, force=args.force,
                               snapshot_date=args.snapshot_date or None)
    report = render_report(args.db, result, args.report)
    snapshot = result["snapshot"]
    print(
        f"ths_concepts status={snapshot['status']} date={snapshot['trade_date']} "
        f"concept_rows={snapshot.get('concept_rows', 0)} member_rows={snapshot.get('member_rows', 0)} "
        f"missing_historical_dates={len(result['missing_historical_dates'])} report={report}"
    )
    return 0 if snapshot["status"] in {"success", "skipped"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
