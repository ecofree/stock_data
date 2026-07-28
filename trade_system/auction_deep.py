"""Deep auction analysis adapter with explicit data-quality mode labels."""

from __future__ import annotations

from pathlib import Path

import duckdb

from trade_system.quality import table_columns, table_exists


def _tick_rows(con: duckdb.DuckDBPyConnection, trade_date: str) -> list[dict]:
    cols = set(table_columns(con, "auction_tick"))
    if "stock_code" not in cols or "date" not in cols:
        return []
    rows = con.execute(
        """
        SELECT stock_code, count(*) AS tick_count
        FROM auction_tick
        WHERE CAST(date AS VARCHAR)=?
        GROUP BY stock_code
        ORDER BY tick_count DESC, stock_code
        """,
        [trade_date],
    ).fetchall()
    return [
        {
            "trade_date": trade_date,
            "stock_code": stock_code,
            "tick_count": int(tick_count),
            "reliability": "high",
            "operator_note": "auction_tick rows are available for deep confirmation",
        }
        for stock_code, tick_count in rows
    ]


def analyze_auction_deep(db_path: str | Path, trade_date: str) -> dict:
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        if table_exists(con, "auction_tick"):
            tick_rows = _tick_rows(con, trade_date)
            if tick_rows:
                return {"mode": "tick", "rows": tick_rows}

        if not table_exists(con, "auction_bidding_anomaly"):
            return {"mode": "missing", "rows": []}

        rows = con.execute(
            """
            SELECT CAST(date AS VARCHAR) AS trade_date, stock_code, anomaly_type, anomaly_value
            FROM auction_bidding_anomaly
            WHERE CAST(date AS VARCHAR)=?
            ORDER BY anomaly_value DESC NULLS LAST, stock_code
            """,
            [trade_date],
        ).fetchall()
    finally:
        con.close()

    return {
        "mode": "degraded_anomaly" if rows else "missing",
        "rows": [
            {
                "trade_date": row_trade_date,
                "stock_code": stock_code,
                "anomaly_type": anomaly_type,
                "anomaly_value": float(anomaly_value or 0),
                "reliability": "medium",
                "operator_note": "auction_tick is empty; using auction_bidding_anomaly as degraded confirmation",
            }
            for row_trade_date, stock_code, anomaly_type, anomaly_value in rows
        ],
    }
