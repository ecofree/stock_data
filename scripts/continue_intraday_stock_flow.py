"""Bounded background continuation for the current-session stock-flow pages."""

from __future__ import annotations

import argparse
from datetime import date
import json
from pathlib import Path
import time
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import duckdb

from config import DB_PATH, TODAY
from scripts.collect_intraday_stock_flow_market import collect_market_stock_flow, render_report


def _progress(db: str | Path, trade_date: str) -> tuple[int, int, int, int]:
    con = duckdb.connect(str(db), read_only=True)
    try:
        row = con.execute(
            "SELECT expected_rows,fetched_rows,expected_pages,fetched_pages "
            "FROM intraday_stock_flow_batch WHERE trade_date=?",
            [trade_date],
        ).fetchone()
        return tuple(int(value or 0) for value in (row or (0, 0, 0, 0)))
    finally:
        con.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Continue today's paginated intraday stock flow safely.")
    parser.add_argument("--db", default=DB_PATH)
    parser.add_argument("--date", default=TODAY)
    parser.add_argument("--max-attempts", type=int, default=24)
    parser.add_argument("--pause-seconds", type=float, default=1.2)
    parser.add_argument("--retry-wait", type=float, default=25.0)
    parser.add_argument("--log", default="reports/intraday_stock_flow_autorun.jsonl")
    args = parser.parse_args()
    if args.date != date.today().isoformat():
        print(json.dumps({"status": "historical_collection_blocked", "trade_date": args.date}, ensure_ascii=False))
        return 2
    log = Path(args.log)
    log.parent.mkdir(parents=True, exist_ok=True)
    for attempt in range(1, max(1, args.max_attempts) + 1):
        expected, fetched, expected_pages, fetched_pages = _progress(args.db, args.date)
        if expected and fetched >= int(expected * 0.95):
            print(json.dumps({"status": "success", "attempt": attempt, "expected": expected, "fetched": fetched}))
            return 0
        try:
            result = collect_market_stock_flow(
                args.db, args.date, page_size=100, pause_seconds=args.pause_seconds,
                resume=True, max_pages=expected_pages or 60,
            )
        except Exception as exc:  # pragma: no cover - defensive worker guard
            result = {"status": "error", "error": str(exc)[:500]}
        after = _progress(args.db, args.date)
        entry = {"attempt": attempt, "before": [expected, fetched, expected_pages, fetched_pages],
                 "after": list(after), "result": result}
        if isinstance(result, dict) and result.get("trade_date"):
            Path("reports/intraday_stock_flow_latest.md").write_text(
                render_report(result), encoding="utf-8",
            )
        with log.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
        if after[0] and after[1] >= int(after[0] * 0.95):
            print(json.dumps({"status": "success", "attempt": attempt, "expected": after[0], "fetched": after[1]}))
            return 0
        # A failed page is expected under upstream throttling.  Wait before
        # the next bounded attempt instead of spinning or launching parallel
        # requests.
        time.sleep(max(5.0, args.retry_wait if after[1] <= fetched else min(args.retry_wait, 10.0)))
    final = _progress(args.db, args.date)
    print(json.dumps({"status": "partial", "attempts": args.max_attempts,
                      "expected": final[0], "fetched": final[1], "pages": final[3]}))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
