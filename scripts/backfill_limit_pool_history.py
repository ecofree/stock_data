"""Backfill the official limit-up pool into ``official_limit_pool``.

Source: HiThink special-data ``limit-up-pool`` (history ≈ 2024-07 onward;
dates outside the authorized window raise code=5003 and terminate the run).

Idempotent: PRIMARY KEY(trade_date, stock_code) with ON CONFLICT upsert.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))


from trade_system.hithink_client import HiThinkClient, HiThinkError  # noqa: E402
from trade_system.logging_setup import configure, get_logger  # noqa: E402
from trade_system.trading_calendar import open_session_dates  # noqa: E402
from scripts.collect_hithink_limit_pool_daily import MIN_ROWS, _clean_items, _write_snapshot  # noqa: E402

logger = get_logger("limit_history_backfill")

def _missing_days(con, start: str, end: str) -> list[str]:
    sessions = open_session_dates(con, start, end, strict=True)
    counts = dict(con.execute(
        "SELECT CAST(trade_date AS VARCHAR), count(*) FROM official_limit_pool "
        "WHERE trade_date BETWEEN ? AND ? GROUP BY trade_date", [start, end]).fetchall())
    # Existing row count is only a retry floor, not certification of coverage.
    return [day for day in sessions if counts.get(day, 0) < MIN_ROWS]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(PROJECT_ROOT / "kpl_data.duckdb"))
    parser.add_argument("--start", required=True, help="explicit first calendar date")
    parser.add_argument("--end", required=True, help="explicit last calendar date")
    parser.add_argument("--max-days", type=int, default=300)
    parser.add_argument("--min-interval", type=float, default=0.35)
    args = parser.parse_args()

    configure()
    client = HiThinkClient(min_interval=args.min_interval)
    from trade_system.db_utils import legacy_connect
    con = legacy_connect(args.db)
    try:
        pending = _missing_days(con, args.start, args.end)
        todo = pending[: max(0, args.max_days)]
        print(f"missing days: {len(pending)}; fetching {len(todo)} this run"
              + (f" ({todo[0]} .. {todo[-1]})" if todo else ""))
        done = 0
        for day in todo:
            try:
                items = _clean_items(client.limit_up_pool(day))
                n = _write_snapshot(con, day, items)
            except (HiThinkError, RuntimeError, ValueError) as exc:
                if "5003" in str(exc):
                    print(f"{day}: outside authorized history window — stop.")
                    break
                logger.warning("%s failed: %s", day, exc)
                continue
            logger.debug("%s: wrote %d rows", day, n)
            done += 1
            if done % 20 == 0:
                print(f"  progress: {done}/{len(todo)} days")
        total = con.execute(
            "SELECT count(*), min(trade_date), max(trade_date) "
            "FROM official_limit_pool").fetchone()
        print(f"run done: days={done}; table {total[0]} rows "
              f"({total[1]} .. {total[2]})")
        return 0 if done == len(pending) else 2
    finally:
        con.close()


if __name__ == "__main__":
    sys.exit(main())
