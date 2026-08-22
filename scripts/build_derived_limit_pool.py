"""Derive an approximate historical limit-up pool from local kline history.

Free data sources only keep ~10 sessions of true limit-up lists, but
``v_kline_daily`` holds years of full-market daily bars.  This script
derives a close-enough universe: a stock is "limit-up" when its daily
change crosses the board threshold implied by its code prefix

    30xxxx / 68xxxx  -> >= 19.7%   (ChiNext / STAR, 20cm)
    92/8/4 prefix    -> excluded   (BSE, noisy thresholds)
    everything else  -> >= 9.7%    (main board, 10cm)

and counts consecutive limit sessions per code as ``board_level``.

Known limits (documented, by design): cannot detect ST 5cm boards,
one-price vs sealed distinction, or intraday reopens.  Good enough for
premium/promotion statistics and strategy backtests; NOT a replacement for
the real pool where that exists.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import duckdb  # noqa: E402

from trade_system.logging_setup import configure  # noqa: E402

DERIVE_SQL = """
INSERT INTO derived_limit_up_daily
    (trade_date, stock_code, stock_name, close, pct_chg, board_level)
WITH k AS (
    SELECT CAST(trade_date AS DATE) AS d, stock_code, stock_name, close,
           change_pct,
           row_number() OVER (
               PARTITION BY trade_date, stock_code
               ORDER BY is_fallback ASC, fetched_at DESC
           ) AS _rn
    FROM v_kline_daily
    WHERE ktype='D' AND change_pct IS NOT NULL AND stock_code NOT LIKE '9%'
      AND stock_code NOT LIKE '8%' AND stock_code NOT LIKE '4%'
),
flagged AS (
    SELECT d, stock_code, stock_name, close, change_pct,
           CASE WHEN stock_code LIKE '30%' OR stock_code LIKE '68%'
                THEN 19.7 ELSE 9.7 END AS thr,
           CASE WHEN change_pct >= (CASE WHEN stock_code LIKE '30%'
                                              OR stock_code LIKE '68%'
                                         THEN 19.7 ELSE 9.7 END)
                THEN 1 ELSE 0 END AS is_zt
    FROM k WHERE _rn = 1
),
streaks AS (
    SELECT *,
           SUM(CASE WHEN is_zt = 0 THEN 0 ELSE 1 END) OVER (
               PARTITION BY stock_code ORDER BY d
           ) AS any_cnt,
           SUM(CASE WHEN is_zt = 0 THEN 1 ELSE 0 END) OVER (
               PARTITION BY stock_code ORDER BY d
           ) AS break_cnt
    FROM flagged
)
SELECT d, stock_code,
       max(stock_name) AS stock_name,
       max(close) AS close,
       max(change_pct) AS pct_chg,
       CAST(d - min(CASE WHEN is_zt=1 THEN d END)
            OVER (PARTITION BY stock_code, break_cnt ORDER BY d)
            AS INTEGER) + 1 AS _unused_board
FROM streaks
WHERE is_zt = 1
GROUP BY d, stock_code, break_cnt
"""


def rebuild(con: duckdb.DuckDBPyConnection, start: str | None = None) -> int:
    """Replace the whole derived table from kline history."""
    start_clause = "WHERE d >= ?" if start else ""
    binds = [start] if start else []
    con.execute("DELETE FROM derived_limit_up_daily")
    # board_level via consecutive-session streak: compute in SQL window then fix.
    con.execute(
        f"""
        INSERT INTO derived_limit_up_daily
            (trade_date, stock_code, stock_name, close, pct_chg, board_level)
        WITH k AS (
            SELECT CAST(trade_date AS DATE) AS d, stock_code,
                   close, change_pct
            FROM v_kline_daily
            WHERE ktype='D' AND change_pct IS NOT NULL
              AND stock_code NOT LIKE '9%' AND stock_code NOT LIKE '8%'
              AND stock_code NOT LIKE '4%'
            QUALIFY row_number() OVER (
                PARTITION BY trade_date, stock_code
                ORDER BY is_fallback ASC, fetched_at DESC
            ) = 1
        ),
        flagged AS (
            SELECT *, CASE WHEN stock_code LIKE '30%' OR stock_code LIKE '68%'
                           THEN 19.7 ELSE 9.7 END AS thr,
                   CASE WHEN change_pct >= (CASE WHEN stock_code LIKE '30%'
                                                      OR stock_code LIKE '68%'
                                                 THEN 19.7 ELSE 9.7 END)
                        THEN 1 ELSE 0 END AS is_zt
            FROM k
        ),
        marked AS (
            SELECT *,
                   SUM(CASE WHEN is_zt = 0 THEN 1 ELSE 0 END) OVER (
                       PARTITION BY stock_code ORDER BY d
                   ) AS grp
            FROM flagged
        )
        SELECT d, stock_code, NULL AS stock_name, close, change_pct,
               CAST(row_number() OVER (PARTITION BY stock_code, grp ORDER BY d) AS INTEGER)
        FROM marked WHERE is_zt = 1 {start_clause}
        """,
        binds,
    )
    return con.execute("SELECT count(*) FROM derived_limit_up_daily").fetchone()[0]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(PROJECT_ROOT / "kpl_data.duckdb"))
    parser.add_argument("--start", default=None,
                        help="Optionally restrict derivation to dates >= this.")
    args = parser.parse_args()

    configure()
    con = duckdb.connect(args.db)
    try:
        n = rebuild(con, args.start)
        bounds = con.execute(
            "SELECT min(trade_date), max(trade_date), "
            "count(DISTINCT trade_date) FROM derived_limit_up_daily"
        ).fetchone()
        print(f"derived_limit_up_daily: {n} rows, "
              f"{bounds[2]} sessions ({bounds[0]} .. {bounds[1]})")
        return 0
    finally:
        con.close()


if __name__ == "__main__":
    sys.exit(main())
