"""Fill the index reference table from already persisted index K-lines.

This is intentionally network-free.  It makes the P2 close run useful even
when the live index-list endpoint is temporarily empty or rate limited.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import duckdb

NAMES = {
    "SH000001": "上证指数",
    "SZ399001": "深证成指",
    "SZ399006": "创业板指",
    "SH000688": "科创50",
}


def repair(db: str) -> int:
    con = duckdb.connect(db)
    rows = con.execute(
        """
        WITH ranked AS (
            SELECT date, index_code, close, volume, turnover,
                   lag(close) OVER (PARTITION BY index_code ORDER BY date) AS prev_close
            FROM index_kline
            WHERE ktype = 'D'
        ), latest AS (
            SELECT *, row_number() OVER (PARTITION BY index_code ORDER BY date DESC) AS rn
            FROM ranked
        )
        SELECT date, index_code, close, volume, turnover, prev_close
        FROM latest WHERE rn = 1
        ORDER BY index_code
        """
    ).fetchall()
    inserted = 0
    for trade_date, code, close, volume, turnover, prev_close in rows:
        pct = None
        if prev_close not in (None, 0) and close is not None:
            pct = (float(close) / float(prev_close) - 1.0) * 100.0
        con.execute("DELETE FROM index_list WHERE date = ? AND index_code = ?", [trade_date, code])
        con.execute(
            """
            INSERT INTO index_list
              (date, index_code, index_name, price, change_pct, change_amt, turnover, volume)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [trade_date, code, NAMES.get(code, code), close, pct,
             (float(close) - float(prev_close)) if prev_close is not None and close is not None else None,
             turnover or 0, volume or 0],
        )
        inserted += 1
    con.close()
    return inserted


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="kpl_data.duckdb")
    args = parser.parse_args()
    print(f"index_list_repaired={repair(args.db)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
