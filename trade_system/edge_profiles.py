"""Edge analytics: hot-money (游资) seat profiles and auction pattern stats.

Derived purely from collected history:
- ``hot_money_profile``  : per broker seat, appearance count and the average
  3-session forward return after its net-buy days (from ``lhb_youzi_dongxiang``
  joined against deduplicated daily kline).
- ``auction_pattern_stats`` : same-day open->close outcome per bidding
  anomaly type (from ``auction_bidding_anomaly``).
"""
from __future__ import annotations

import duckdb

KLINE_DEDUP_CTE = """
SELECT CAST(trade_date AS DATE) AS d, stock_code, open, close FROM (
    SELECT CAST(trade_date AS DATE) AS trade_date, stock_code, open, close,
           row_number() OVER (
               PARTITION BY trade_date, stock_code
               ORDER BY is_fallback ASC, fetched_at DESC
           ) AS _rn
    FROM v_kline_daily
    WHERE ktype='D' AND open IS NOT NULL AND close IS NOT NULL
) WHERE _rn = 1
"""


def build_hot_money_profile(con: duckdb.DuckDBPyConnection,
                            min_appearances: int = 2,
                            forward_sessions: int = 3) -> list[dict]:
    """Aggregate per-seat stats; replaces the whole table on success."""
    rows = con.execute(
        f"""
        WITH k AS ({KLINE_DEDUP_CTE}),
        calendar AS (
            SELECT DISTINCT d FROM k
        ),
        entries AS (
            SELECT y.broker_name, y.date, y.stock_code,
                   SUM(y.buy_amount - COALESCE(y.sell_amount, 0)) AS net_buy
            FROM lhb_youzi_dongxiang y
            GROUP BY 1, 2, 3
            HAVING net_buy > 0
        ),
        exits AS (
            SELECT e.broker_name, e.date, e.stock_code, e.net_buy,
                   k0.close AS entry_close, kx.close AS exit_close
            FROM entries e
            JOIN k k0 ON k0.stock_code = e.stock_code AND k0.d = CAST(e.date AS DATE)
            JOIN (
                SELECT stock_code, d,
                       LEAD(d, {forward_sessions}) OVER (
                           PARTITION BY stock_code ORDER BY d
                       ) AS target_day
                FROM (SELECT DISTINCT stock_code, d FROM k)
            ) seq ON seq.stock_code = e.stock_code AND seq.d = CAST(e.date AS DATE)
            JOIN k kx ON kx.stock_code = e.stock_code AND kx.d = seq.target_day
        )
        SELECT broker_name,
               count(*) AS appearances,
               SUM(net_buy) AS total_buy_amount,
               AVG(CASE WHEN exit_close > entry_close THEN 1.0 ELSE 0.0 END) AS win_rate_3d,
               AVG((exit_close / entry_close - 1.0) * 100.0) AS avg_ret_3d,
               max(CAST(date AS DATE)) AS last_seen
        FROM exits
        GROUP BY broker_name
        HAVING count(*) >= ?1
        ORDER BY avg_ret_3d DESC NULLS LAST
        """,
        {"1": min_appearances},
    ).fetchall()
    profile = [
        {
            "broker_name": r[0],
            "appearances": int(r[1]),
            "total_buy_amount": float(r[2]) if r[2] is not None else None,
            "win_rate_3d": round(float(r[3]), 4) if r[3] is not None else None,
            "avg_ret_3d": round(float(r[4]), 4) if r[4] is not None else None,
            "last_seen": str(r[5]) if r[5] is not None else None,
        }
        for r in rows
    ]

    con.execute("BEGIN TRANSACTION")
    try:
        con.execute("DELETE FROM hot_money_profile")
        for p in profile:
            con.execute(
                """INSERT INTO hot_money_profile
                   (broker_name, appearances, total_buy_amount, win_rate_3d,
                    avg_ret_3d, last_seen)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                [p["broker_name"], p["appearances"], p["total_buy_amount"],
                 p["win_rate_3d"], p["avg_ret_3d"], p["last_seen"]],
            )
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    return profile


def build_auction_pattern_stats(con: duckdb.DuckDBPyConnection,
                                min_occurrences: int = 1) -> list[dict]:
    rows = con.execute(
        f"""
        WITH k AS ({KLINE_DEDUP_CTE}),
        outcomes AS (
            SELECT a.anomaly_type, a.date,
                   count(*) AS occurrences,
                   avg((k.close - k.open) / k.open * 100.0) AS day_avg_oc_pct,
                   avg(CASE WHEN k.close > k.open THEN 1.0 ELSE 0.0 END) AS day_win_rate
            FROM auction_bidding_anomaly a
            JOIN k ON k.stock_code = a.stock_code AND k.d = a.date
            GROUP BY 1, 2
        )
        SELECT anomaly_type, date, occurrences, day_win_rate, day_avg_oc_pct
        FROM outcomes
        ORDER BY date DESC, occurrences DESC
        """
    ).fetchall()
    stats = [
        {
            "anomaly_type": r[0],
            "trade_date": str(r[1]),
            "occurrences": int(r[2]),
            "day_win_rate": round(float(r[3]), 4) if r[3] is not None else None,
            "day_avg_oc_pct": round(float(r[4]), 4) if r[4] is not None else None,
        }
        for r in rows if int(r[2]) >= min_occurrences
    ]
    con.execute("BEGIN TRANSACTION")
    try:
        con.execute("DELETE FROM auction_pattern_stats")
        for s in stats:
            con.execute(
                """INSERT INTO auction_pattern_stats
                   (anomaly_type, trade_date, occurrences, day_win_rate,
                    day_avg_oc_pct) VALUES (?, ?, ?, ?, ?)""",
                [s["anomaly_type"], s["trade_date"], s["occurrences"],
                 s["day_win_rate"], s["day_avg_oc_pct"]],
            )
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    return stats
