"""Read-only access helpers for the canonical daily K-line authority.

The normalized ``v_kline_daily`` view is retained as a compatibility surface,
but it contains an all-history de-duplication/fallback plan. Production
decision paths should read the certified TuShare daily table directly when it
is available so a bounded date/code query cannot expand the full history.
"""

from __future__ import annotations

import duckdb

from trade_system.quality import table_columns, table_exists


def canonical_daily_kline_relation(con: duckdb.DuckDBPyConnection) -> str:
    """Return a trusted SQL relation exposing normalized daily-bar columns."""
    if not table_exists(con, "tushare_daily"):
        return "v_kline_daily"
    columns = set(table_columns(con, "tushare_daily"))
    required = {"date", "stock_code", "open", "high", "low", "close"}
    if not required <= columns:
        return "v_kline_daily"
    fetched_expr = "fetched_at" if "fetched_at" in columns else "NULL::TIMESTAMP"
    change_expr = "change_pct" if "change_pct" in columns else "NULL::DOUBLE"
    order_expr = (
        "fetched_at DESC NULLS LAST, rowid DESC"
        if "fetched_at" in columns
        else "rowid DESC"
    )
    return f"""(
        SELECT CAST(date AS VARCHAR) AS trade_date,
               stock_code, open, high, low, close,
               change_pct, 'D' AS ktype,
               'tushare_daily' AS source_table,
               false AS is_fallback, fetched_at
        FROM (
            SELECT date, stock_code, open, high, low, close,
                   {change_expr} AS change_pct,
                   {fetched_expr} AS fetched_at,
                   row_number() OVER (
                       PARTITION BY date, stock_code
                       ORDER BY {order_expr}
                   ) AS _rn
            FROM tushare_daily
            WHERE close IS NOT NULL
        ) AS latest
        WHERE _rn = 1
    )"""

