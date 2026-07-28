"""Fail-closed exchange-session gate for production collection phases."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import duckdb


@dataclass(frozen=True)
class TradingSessionStatus:
    trade_date: str
    state: str
    source: str
    reason: str

    @property
    def is_open(self) -> bool:
        return self.state == "open"


def _table_exists(con: duckdb.DuckDBPyConnection, table: str) -> bool:
    return bool(con.execute(
        "SELECT count(*) FROM information_schema.tables WHERE table_name=?",
        [table],
    ).fetchone()[0])


def trading_session_status(
    db_path: str | Path,
    trade_date: str,
) -> TradingSessionStatus:
    """Read the locally verified TuShare exchange calendar.

    Missing, malformed, or contradictory rows are ``unverified``.  The caller
    must not infer an open session from the weekday.
    """
    try:
        con = duckdb.connect(str(db_path), read_only=True)
    except Exception as exc:
        return TradingSessionStatus(
            trade_date, "unverified", "tushare_trade_cal",
            f"calendar database unavailable: {exc}",
        )
    try:
        if not _table_exists(con, "tushare_trade_cal"):
            return TradingSessionStatus(
                trade_date, "unverified", "tushare_trade_cal",
                "calendar table missing",
            )
        row = con.execute(
            """
            SELECT count(*) AS rows,
                   count(DISTINCT CAST(is_open AS BOOLEAN)) AS distinct_states,
                   bool_or(coalesce(CAST(is_open AS BOOLEAN), false)) AS is_open
            FROM tushare_trade_cal
            WHERE cal_date=CAST(? AS DATE)
            """,
            [trade_date],
        ).fetchone()
        rows = int(row[0] or 0)
        distinct_states = int(row[1] or 0)
        if rows == 0:
            return TradingSessionStatus(
                trade_date, "unverified", "tushare_trade_cal",
                "no calendar row for requested date",
            )
        if distinct_states != 1:
            return TradingSessionStatus(
                trade_date, "unverified", "tushare_trade_cal",
                f"contradictory or null calendar states across {rows} rows",
            )
        if bool(row[2]):
            return TradingSessionStatus(
                trade_date, "open", "tushare_trade_cal",
                f"verified open from {rows} row(s)",
            )
        return TradingSessionStatus(
            trade_date, "closed", "tushare_trade_cal",
            f"verified closed from {rows} row(s)",
        )
    except Exception as exc:
        return TradingSessionStatus(
            trade_date, "unverified", "tushare_trade_cal",
            f"calendar query failed: {exc}",
        )
    finally:
        con.close()


def ensure_trading_session_status(
    db_path: str | Path,
    trade_date: str,
) -> TradingSessionStatus:
    """Fetch one missing calendar date once, then return a fail-closed status."""
    status = trading_session_status(db_path, trade_date)
    if status.state != "unverified":
        return status
    try:
        from trade_system.tushare_history import TushareHistoryCollector

        with TushareHistoryCollector(db_path) as collector:
            collector.ensure_calendar(trade_date, trade_date)
    except Exception as exc:
        return TradingSessionStatus(
            trade_date, "unverified", "tushare_trade_cal",
            f"{status.reason}; refresh failed: {exc}",
        )
    return trading_session_status(db_path, trade_date)
