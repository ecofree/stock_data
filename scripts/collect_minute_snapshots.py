"""Store 1-minute intraday bars for the stocks that matter (replay axis).

Universe defaults to the day's official limit-up pool — a few dozen names
instead of the whole market, keeping the table at ~30k rows/day.  Data via
easy-tdx (TDX protocol, free); servers only keep recent sessions, so this
must run daily to accumulate history.
"""
from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import duckdb  # noqa: E402

from trade_system.logging_setup import configure, get_logger  # noqa: E402

logger = get_logger("minute_snapshots")

UPSERT_SQL = """
INSERT INTO intraday_minute_bars (trade_date, stock_code, bar_time, price, volume)
VALUES (?, ?, ?, ?, ?)
ON CONFLICT (trade_date, stock_code, bar_time) DO UPDATE SET
    price=excluded.price, volume=excluded.volume,
    fetched_at=now()
"""


def _market_of(code: str):
    from easy_tdx import Market

    if code.startswith(("6", "9")):
        return Market.SH
    if code.startswith(("0", "2", "3")):
        return Market.SZ
    return None


def _universe(con: duckdb.DuckDBPyConnection, day: str, source: str,
              max_stocks: int) -> list[str]:
    if source == "limit-pool":
        # Use v_limit_pool (unified view) which falls back to derived pool
        # when official backfill hasn't run yet for today.
        rows = con.execute(
            """SELECT DISTINCT stock_code FROM v_limit_pool
               WHERE CAST(trade_date AS DATE) = ?""",
            [day],
        ).fetchall()
    elif source == "watchlist":
        rows = con.execute(
            """SELECT DISTINCT stock_code FROM watchlist
               WHERE CAST(trade_date AS VARCHAR) >= (
                   SELECT max(CAST(trade_date AS VARCHAR)) FROM watchlist
                   WHERE CAST(trade_date AS VARCHAR) <= ?)""",
            [day],
        ).fetchall()
    else:
        rows = [(c,) for c in source.split(",") if c.strip()]
    codes = [str(r[0]) for r in rows if str(r[0]).isdigit()]
    return [c for c in codes if _market_of(c)][:max_stocks]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(PROJECT_ROOT / "kpl_data.duckdb"))
    parser.add_argument("--date", default=str(date.today()).replace("-", ""))
    parser.add_argument("--source", default="limit-pool",
                        help="limit-pool | watchlist | comma-separated codes")
    parser.add_argument("--max-stocks", type=int, default=150)
    args = parser.parse_args()

    configure()
    ymd = args.date.replace("-", "")
    day = f"{ymd[:4]}-{ymd[4:6]}-{ymd[6:]}"

    from easy_tdx import TdxClient

    con = duckdb.connect(args.db)
    try:
        codes = _universe(con, day, args.source, args.max_stocks)
        print(f"universe: {len(codes)} stocks for {day}")
        stored = failed = 0
        with TdxClient.from_best_host() as client:
            for code in codes:
                market = _market_of(code)
                try:
                    df = client.get_history_minute_time_data(market, code, int(ymd))
                    if df is None or df.empty:
                        continue
                    con.execute("BEGIN TRANSACTION")
                    try:
                        for _, row in df.iterrows():
                            hhmm = str(row["datetime"])[11:16]
                            con.execute(UPSERT_SQL, [
                                day, code, hhmm,
                                float(row["price"]), int(row["vol"] or 0),
                            ])
                        con.execute("COMMIT")
                        stored += 1
                    except Exception:
                        con.execute("ROLLBACK")
                        raise
                except Exception as exc:
                    failed += 1
                    logger.debug("minute fetch failed %s: %s", code, exc)
        total = con.execute(
            "SELECT count(*) FROM intraday_minute_bars WHERE trade_date = ?",
            [day],
        ).fetchone()[0]
        print(f"stored={stored} failed={failed}; bars for {day}: {total}")
        return 0 if stored else 1
    finally:
        con.close()


if __name__ == "__main__":
    sys.exit(main())
