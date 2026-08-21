"""Shared DuckDB query helpers used across trade_system modules."""
from __future__ import annotations

from typing import Any

import duckdb


def fetch_dicts(
    con: duckdb.DuckDBPyConnection,
    sql: str,
    params: list[Any] | None = None,
) -> list[dict]:
    """Run ``sql`` and return rows as plain dicts keyed by column name."""
    cur = con.execute(sql, params or [])
    columns = [desc[0] for desc in cur.description]
    return [dict(zip(columns, row)) for row in cur.fetchall()]
