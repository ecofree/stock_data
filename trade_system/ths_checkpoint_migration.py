"""Explicit recovery migration for the THS member checkpoint tables.

This operation used to run every time ``THSConceptHistoryCollector`` was
constructed.  It is intentionally kept outside the collector so normal close
and review runs never execute ``DROP TABLE``/index rebuilds implicitly.
"""

from __future__ import annotations

from typing import Any

import duckdb


def repair_ths_checkpoint_storage(
    con: duckdb.DuckDBPyConnection, *, dry_run: bool = False
) -> dict[str, Any]:
    """Repair the small THS checkpoint indexes as an explicit operation.

    The caller must hold the normal pipeline lock.  ``dry_run`` performs only
    metadata/count reads and never changes the connection.
    """
    exists = int(
        con.execute(
            "SELECT count(*) FROM information_schema.tables "
            "WHERE table_schema='main' AND table_name='ths_concept_member_checkpoint'"
        ).fetchone()[0]
    )
    if not exists:
        return {"status": "missing", "checkpoint_rows": 0, "member_rows": 0}

    checkpoint_rows = int(
        con.execute("SELECT count(*) FROM ths_concept_member_checkpoint").fetchone()[0]
    )
    member_exists = int(
        con.execute(
            "SELECT count(*) FROM information_schema.tables "
            "WHERE table_schema='main' AND table_name='ths_concept_stock_history'"
        ).fetchone()[0]
    )
    member_rows = (
        int(con.execute("SELECT count(*) FROM ths_concept_stock_history").fetchone()[0])
        if member_exists
        else 0
    )
    if dry_run:
        return {
            "status": "dry_run",
            "checkpoint_rows": checkpoint_rows,
            "member_rows": member_rows,
        }

    columns = (
        "trade_date, concept_code, concept_name, status, pages_expected, "
        "pages_fetched, member_rows, attempts, last_error, updated_at, "
        "provider, crawler_version, catalog_hash"
    )
    con.execute("DROP INDEX IF EXISTS idx_ths_member_date_status_updated")
    con.execute(
        "CREATE TABLE ths_concept_member_checkpoint_repair AS "
        f"SELECT {columns} FROM ths_concept_member_checkpoint"
    )
    con.execute("DROP TABLE ths_concept_member_checkpoint")
    con.execute(
        "ALTER TABLE ths_concept_member_checkpoint_repair "
        "RENAME TO ths_concept_member_checkpoint"
    )
    con.execute(
        "ALTER TABLE ths_concept_member_checkpoint "
        "ADD PRIMARY KEY (trade_date, concept_code)"
    )
    con.execute(
        "CREATE INDEX idx_ths_member_date_status_updated "
        "ON ths_concept_member_checkpoint(trade_date, status, updated_at)"
    )
    if member_exists:
        con.execute("DROP INDEX IF EXISTS uq_ths_concept_member_business")
        con.execute(
            "CREATE UNIQUE INDEX uq_ths_concept_member_business "
            "ON ths_concept_stock_history(trade_date, concept_code, stock_code)"
        )
    con.commit()
    return {
        "status": "repaired",
        "checkpoint_rows": checkpoint_rows,
        "member_rows": member_rows,
    }
