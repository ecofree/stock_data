"""Backfill the official limit-up pool into ``official_limit_pool``.

Source: HiThink special-data ``limit-up-pool`` (history ≈ 2024-07 onward;
dates outside the authorized window raise code=5003 and terminate the run).

Idempotent: PRIMARY KEY(trade_date, stock_code) with ON CONFLICT upsert.
"""
from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))


from trade_system.hithink_client import HiThinkClient, HiThinkError  # noqa: E402
from trade_system.logging_setup import configure, get_logger  # noqa: E402

logger = get_logger("limit_history_backfill")

UPSERT_SQL = """
INSERT INTO official_limit_pool
    (trade_date, stock_code, stock_name, limit_up_time, continue_day_cnt,
     limit_up_reason, close, pct_chg, seal_money, max_seal_money, is_st,
     source)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'hithink')
ON CONFLICT (trade_date, stock_code) DO UPDATE SET
    stock_name=excluded.stock_name, limit_up_time=excluded.limit_up_time,
    continue_day_cnt=excluded.continue_day_cnt,
    limit_up_reason=excluded.limit_up_reason, close=excluded.close,
    pct_chg=excluded.pct_chg, seal_money=excluded.seal_money,
    max_seal_money=excluded.max_seal_money, fetched_at=now()
"""


def _missing_days(con, start: str, end: str) -> list[str]:
    rows = con.execute(
        """
        WITH cal AS (
            SELECT DISTINCT CAST(trade_date AS DATE) AS d FROM v_kline_daily
            WHERE ktype='D' AND CAST(trade_date AS DATE) BETWEEN ? AND ?
        ),
        have AS (
            SELECT trade_date AS d, count(*) AS n FROM official_limit_pool GROUP BY 1
        )
        SELECT cal.d FROM cal LEFT JOIN have USING (d)
        WHERE coalesce(have.n, 0) < 10 ORDER BY cal.d
        """,
        [start, end],
    ).fetchall()
    return [str(r[0]) for r in rows]


def _write_day(con, day: str, items: list[dict]) -> int:
    con.execute("BEGIN TRANSACTION")
    try:
        for r in items:
            ticker = str(r.get("ticker") or "")
            if not ticker.isdigit():
                continue
            con.execute(UPSERT_SQL, [
                day, ticker, r.get("name"), r.get("limit_up_time"),
                r.get("continue_day_cnt"), r.get("limit_up_reason"),
                r.get("last_price"), r.get("price_change_ratio_pct"),
                r.get("seal_money"), r.get("max_seal_money"),
                bool(r.get("is_st")),
            ])
        con.execute("COMMIT")
        return len(items)
    except Exception:
        con.execute("ROLLBACK")
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(PROJECT_ROOT / "kpl_data.duckdb"))
    parser.add_argument("--start", default="2024-07-01",
                        help="HiThink window starts ≈ 2024-07.")
    parser.add_argument("--end", default=str(date.today()))
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
                items = client.limit_up_pool(day)
            except HiThinkError as exc:
                if "5003" in str(exc):
                    print(f"{day}: outside authorized history window — stop.")
                    break
                logger.warning("%s failed: %s", day, exc)
                continue
            n = _write_day(con, day, items) if items else 0
            logger.debug("%s: wrote %d rows", day, n)
            done += 1
            if done % 20 == 0:
                print(f"  progress: {done}/{len(todo)} days")
        total = con.execute(
            "SELECT count(*), min(trade_date), max(trade_date) "
            "FROM official_limit_pool").fetchone()
        print(f"run done: days={done}; table {total[0]} rows "
              f"({total[1]} .. {total[2]})")
        return 0
    finally:
        con.close()


if __name__ == "__main__":
    sys.exit(main())
