"""Shared DuckDB query helpers used across trade_system modules."""
from __future__ import annotations

from typing import Any

import duckdb


def latest_open_session(db_path, as_of=None):
    """Pure read-only calendar lookup, independent of collectors/configuration."""
    from datetime import date
    digits=''.join(ch for ch in str(as_of or date.today().isoformat()) if ch.isdigit())
    if len(digits)<8:
        return None
    upper=f'{digits[:4]}-{digits[4:6]}-{digits[6:8]}'
    try:
        with duckdb.connect(str(db_path),read_only=True) as con:
            columns={str(row[1]).lower() for row in con.execute("PRAGMA table_info('tushare_trade_cal')").fetchall()}
            exchange="AND exchange='SSE'" if 'exchange' in columns else ''
            result=con.execute(f'''SELECT max(CAST(cal_date AS DATE)) FROM tushare_trade_cal
                WHERE coalesce(CAST(is_open AS BOOLEAN),false) {exchange}
                AND CAST(cal_date AS DATE) BETWEEN DATE '1900-01-01' AND CAST(? AS DATE)''',[upper]).fetchone()[0]
            return str(result) if result is not None else None
    except duckdb.Error:
        return None


def refuse_v2_writes(db_path):
    """Symmetric generation check for approved transitional legacy writers.

    This is an accidental-misrouting guard, not an OS permission boundary.
    Direct DuckDB callers still require retirement inventory and migration.
    """
    from pathlib import Path
    path=Path(db_path)
    if path.is_file():
        with duckdb.connect(str(path),read_only=True) as con:
            if con.execute("SELECT count(*) FROM information_schema.tables WHERE table_name IN ('v2_schema','account_snapshot','reservation','paper_ledger_event')").fetchone()[0]:
                raise ValueError('legacy writer refuses V2/account database identity')


def legacy_connect(database=':memory:', *, read_only=False, **kwargs):
    """Transitional legacy connection; never hand a V2 writer to old code.

    The read-only preflight rejects a stopped V2 file without opening it for
    writes. Recheck the actual connection before returning it as defense in
    depth. This is not a sandbox for arbitrary third-party DuckDB programs.
    """
    if not read_only and str(database) != ':memory:':
        refuse_v2_writes(database)
    con = duckdb.connect(str(database), read_only=read_only, **kwargs)
    if not read_only:
        try:
            if con.execute("SELECT count(*) FROM information_schema.tables WHERE table_name IN ('v2_schema','account_snapshot','reservation','paper_ledger_event')").fetchone()[0]:
                raise ValueError('legacy writer refuses V2/account database identity')
        except BaseException:
            con.close()
            raise
    return con


def fetch_dicts(
    con: duckdb.DuckDBPyConnection,
    sql: str,
    params: list[Any] | None = None,
) -> list[dict]:
    """Run ``sql`` and return rows as plain dicts keyed by column name."""
    cur = con.execute(sql, params or [])
    columns = [desc[0] for desc in cur.description]
    return [dict(zip(columns, row)) for row in cur.fetchall()]
