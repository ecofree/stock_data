"""Intraday signal watcher: polls for new triggered signals during trading
hours and pushes them via configured channels.

Designed to run as a Windows scheduled task at 09:14, exiting after 15:10.
Poll interval defaults to 3 minutes.  Only NEW signals (not seen in the
previous poll) trigger a push, so operators aren't spammed.
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import duckdb  # noqa: E402

from trade_system.db_utils import fetch_dicts  # noqa: E402
from trade_system.logging_setup import configure, get_logger  # noqa: E402
from trade_system.notify import send_text  # noqa: E402

logger = get_logger("intraday_watcher")


def _is_trading_hours(now: datetime) -> bool:
    t = now.time()
    from datetime import time as _t
    return (
        (_t(9, 20) <= t < _t(11, 31))
        or (_t(12, 58) <= t < _t(15, 1))
    )


def fetch_new_signals(con, seen_keys: set) -> list[dict]:
    rows = fetch_dicts(con, """
        SELECT stage, stock_code, stock_name, score, reference_price,
               CAST(trade_date AS VARCHAR) AS td
        FROM stock_candidate_stage_signal
        WHERE signal_triggered = true
          AND is_actionable = true
          AND CAST(trade_date AS DATE) = current_date
    """)
    fresh = []
    for r in rows:
        key = f"{r['td']}:{r['stage']}:{r['stock_code']}"
        if key not in seen_keys:
            fresh.append(r)
            seen_keys.add(key)
    return fresh


def push_batch(rows: list[dict]) -> None:
    by_stage: dict[str, list[dict]] = {}
    for r in rows:
        by_stage.setdefault(r["stage"], []).append(r)
    lines = []
    for stage, items in sorted(by_stage.items()):
        names = "、".join(
            f"{it.get('stock_name') or it.get('stock_code')}({it.get('score') or 0:.0f})"
            for it in items[:6])
        more = f" 等{len(items)}只" if len(items) > 6 else ""
        lines.append(f"[{stage}] {names}{more}")
    send_text("[stock_data] 盘中信号", "\n".join(lines))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(PROJECT_ROOT / "kpl_data.duckdb"))
    parser.add_argument("--interval", type=int, default=180)
    parser.add_argument("--end-hour", type=int, default=15)
    parser.add_argument("--end-min", type=int, default=10)
    parser.add_argument("--once", action="store_true",
                        help="Single poll then exit (for cron/scheduled task).")
    args = parser.parse_args()

    configure()
    seen: set = set()
    try:
        while True:
            now = datetime.now()
            if not _is_trading_hours(now):
                logger.info("outside trading hours, sleeping")
                time.sleep(300)
                continue
            try:
                # Do not hold a DuckDB connection across the polling interval.
                # On Windows even a read-only connection can prevent the close
                # pipeline from opening the database for writes.
                con = duckdb.connect(args.db, read_only=True)
                try:
                    fresh = fetch_new_signals(con, seen)
                finally:
                    con.close()
                if fresh:
                    logger.info("%d new triggered signals", len(fresh))
                    push_batch(fresh)
            except Exception as exc:
                logger.warning("poll error: %s", exc)

            if args.once:
                break
            end_cutoff = now.replace(hour=args.end_hour, minute=args.end_min)
            if now >= end_cutoff:
                logger.info("past end time, exiting watcher")
                break
            time.sleep(max(30, args.interval))
    except KeyboardInterrupt:
        pass
    print("watcher done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
