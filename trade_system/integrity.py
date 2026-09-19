"""Critical business-key repair and enforcement for production tables."""

from __future__ import annotations

from pathlib import Path


from trade_system.quality import dedupe_table, table_columns, table_exists


KTYPE_TABLES = ("kline", "index_kline", "advanced_kline_today", "advanced_gujia_kline")

DEDUPE_SPECS = (
    ('kline', ('date', 'stock_code', 'ktype'), 'fetched_at'),
    ('index_kline', ('date', 'index_code', 'ktype'), 'fetched_at'),
    ('advanced_kline_today', ('date', 'stock_code', 'ktype'), 'fetched_at'),
    ('advanced_gujia_kline', ('date', 'stock_code', 'ktype'), 'fetched_at'),
    ('ths_concept_stock_history', ('trade_date', 'concept_code', 'stock_code'), 'fetched_at'),
)

UNIQUE_INDEX_SPECS = (
    ('uq_kline_business', 'kline', ('date', 'stock_code', 'ktype')),
    ('uq_index_kline_business', 'index_kline', ('date', 'index_code', 'ktype')),
    ('uq_ths_concept_member_business', 'ths_concept_stock_history', ('trade_date', 'concept_code', 'stock_code')),
)

# These are non-unique operational indexes.  They are deliberately rebuilt on
# every integrity pass because DuckDB may retain a damaged index catalog entry
# after an interrupted DELETE/INSERT cycle.  A later CREATE INDEX IF NOT
# EXISTS would otherwise keep treating the broken index as healthy.
REBUILDABLE_INDEX_SPECS = (
    (
        "idx_intraday_sector_batch_date_updated",
        "intraday_sector_flow_batch",
        ("trade_date", "updated_at"),
    ),
)


def normalize_kline_periods(db_path: str | Path) -> dict[str, int]:
    from trade_system.db_utils import legacy_connect
    con = legacy_connect(str(db_path))
    changed: dict[str, int] = {}
    try:
        for table in KTYPE_TABLES:
            if not table_exists(con, table) or "ktype" not in table_columns(con, table):
                continue
            before = int(
                con.execute(
                    f"SELECT count(*) FROM \"{table}\" "
                    "WHERE ktype IS NOT NULL AND ktype != upper(trim(ktype))"
                ).fetchone()[0]
            )
            con.execute(
                f"UPDATE \"{table}\" SET ktype = upper(trim(ktype)) "
                "WHERE ktype IS NOT NULL AND ktype != upper(trim(ktype))"
            )
            changed[table] = before
    finally:
        con.close()
    return changed


def normalize_ths_member_codes(
    db_path: str | Path,
    *,
    dry_run: bool = False,
) -> dict[str, int | str]:
    """Normalize THS member codes before the common business-key dedupe.

    Historical snapshots mixed bare codes with ``.SZ/.SH/.BJ`` suffixes.  Rows
    that collapse onto the same normalized key are archived and removed in one
    transaction before the surviving codes are updated.  Current collectors
    already write bare codes, so subsequent close runs are a cheap no-op.
    """
    from trade_system.db_utils import legacy_connect
    con = legacy_connect(str(db_path), read_only=dry_run)
    try:
        if not table_exists(con, "ths_concept_stock_history"):
            return {
                "status": "missing_table",
                "suffixed_rows": 0,
                "duplicate_groups": 0,
                "removed_rows": 0,
                "normalized_rows": 0,
            }
        required = {"trade_date", "concept_code", "stock_code"}
        if not required <= set(table_columns(con, "ths_concept_stock_history")):
            return {
                "status": "missing_columns",
                "suffixed_rows": 0,
                "duplicate_groups": 0,
                "removed_rows": 0,
                "normalized_rows": 0,
            }

        suffix_sql = (
            "regexp_replace(trim(CAST(stock_code AS VARCHAR)), "
            "'(?i)[.][A-Z]+$', '')"
        )
        suffixed_rows = int(
            con.execute(
                "SELECT count(*) FROM ths_concept_stock_history "
                "WHERE regexp_matches(trim(CAST(stock_code AS VARCHAR)), "
                "'(?i)^[0-9]{6}[.][A-Z]+$')"
            ).fetchone()[0]
        )
        duplicate_groups = int(
            con.execute(
                "SELECT count(*) FROM ("
                "SELECT trade_date, concept_code, "
                f"{suffix_sql} AS normalized_code, count(*) AS n "
                "FROM ths_concept_stock_history "
                "GROUP BY 1,2,3 HAVING count(*) > 1)"
            ).fetchone()[0]
        )
        if dry_run or (suffixed_rows == 0 and duplicate_groups == 0):
            return {
                "status": "dry_run" if dry_run else "ok",
                "suffixed_rows": suffixed_rows,
                "duplicate_groups": duplicate_groups,
                "removed_rows": 0,
                "normalized_rows": 0,
            }

        columns = set(table_columns(con, "ths_concept_stock_history"))
        order_sql = (
            "fetched_at DESC NULLS LAST, rowid DESC"
            if "fetched_at" in columns
            else "rowid DESC"
        )
        con.execute("BEGIN")
        try:
            con.execute("DROP TABLE IF EXISTS _ths_member_normalize_rank")
            con.execute(
                "CREATE TEMP TABLE _ths_member_normalize_rank AS "
                "SELECT rowid AS source_rowid, "
                f"{suffix_sql} AS normalized_code, "
                "row_number() OVER ("
                "PARTITION BY trade_date, concept_code, "
                f"{suffix_sql} ORDER BY {order_sql}) AS rn "
                "FROM ths_concept_stock_history"
            )
            removed_rows = int(
                con.execute(
                    "SELECT count(*) FROM _ths_member_normalize_rank WHERE rn > 1"
                ).fetchone()[0]
            )
            if removed_rows:
                con.execute(
                    "CREATE TABLE IF NOT EXISTS "
                    "_dedupe_archive_ths_concept_stock_history AS "
                    "SELECT *, current_timestamp AS archived_at "
                    "FROM ths_concept_stock_history WHERE false"
                )
                con.execute(
                    "INSERT INTO _dedupe_archive_ths_concept_stock_history "
                    "SELECT h.*, current_timestamp AS archived_at "
                    "FROM ths_concept_stock_history h "
                    "JOIN _ths_member_normalize_rank r "
                    "ON h.rowid=r.source_rowid WHERE r.rn > 1"
                )
                con.execute(
                    "DELETE FROM ths_concept_stock_history WHERE rowid IN ("
                    "SELECT source_rowid FROM _ths_member_normalize_rank WHERE rn > 1)"
                )
            con.execute(
                "UPDATE ths_concept_stock_history "
                f"SET stock_code={suffix_sql} "
                "WHERE regexp_matches(trim(CAST(stock_code AS VARCHAR)), "
                "'(?i)^[0-9]{6}[.][A-Z]+$')"
            )
            if table_exists(con, "ths_concept_daily"):
                con.execute(
                    "UPDATE ths_concept_daily SET stock_count=("
                    "SELECT count(DISTINCT h.stock_code) "
                    "FROM ths_concept_stock_history h "
                    "WHERE h.trade_date=ths_concept_daily.trade_date "
                    "AND h.concept_code=ths_concept_daily.concept_code)"
                )
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise
        return {
            "status": "ok",
            "suffixed_rows": suffixed_rows,
            "duplicate_groups": duplicate_groups,
            "removed_rows": removed_rows,
            "normalized_rows": suffixed_rows - removed_rows,
        }
    finally:
        con.close()


def ensure_unique_indexes(db_path: str | Path) -> list[str]:
    from trade_system.db_utils import legacy_connect
    con = legacy_connect(str(db_path))
    created: list[str] = []
    try:
        for index_name, table, columns in UNIQUE_INDEX_SPECS:
            if not table_exists(con, table):
                continue
            existing = set(table_columns(con, table))
            if not set(columns) <= existing:
                continue
            column_sql = ", ".join(f'"{column}"' for column in columns)
            create_sql = f'CREATE UNIQUE INDEX IF NOT EXISTS "{index_name}" ON "{table}" ({column_sql})'
            con.execute(create_sql)
            created.append(index_name)
    finally:
        con.close()
    return created


def rebuild_operational_indexes(db_path: str | Path) -> list[str]:
    """Rebuild non-unique indexes whose writers replace same-day snapshots."""
    from trade_system.db_utils import legacy_connect
    con = legacy_connect(str(db_path))
    rebuilt: list[str] = []
    try:
        for index_name, table, columns in REBUILDABLE_INDEX_SPECS:
            if not table_exists(con, table):
                continue
            existing = set(table_columns(con, table))
            if not set(columns) <= existing:
                continue
            column_sql = ", ".join(f'"{column}"' for column in columns)
            con.execute(f'DROP INDEX IF EXISTS "{index_name}"')
            con.execute(f'CREATE INDEX "{index_name}" ON "{table}" ({column_sql})')
            rebuilt.append(index_name)
    finally:
        con.close()
    return rebuilt


def repair_critical_integrity(db_path: str | Path, dry_run: bool = False) -> dict:
    normalized = {} if dry_run else normalize_kline_periods(db_path)
    ths_member_codes = normalize_ths_member_codes(db_path, dry_run=dry_run)
    repairs = []
    for table, keys, order_column in DEDUPE_SPECS:
        result = dedupe_table(
            db_path,
            table,
            keys,
            order_column=order_column,
            dry_run=dry_run,
        )
        repairs.append(result)

    # Maintenance owns source normalization, never human plans or legacy signal DDL.
    indexes = []
    if not dry_run:
        indexes = ensure_unique_indexes(db_path)
        indexes.extend(rebuild_operational_indexes(db_path))
    return {
        "dry_run": dry_run,
        "normalized_ktype_rows": normalized,
        "ths_member_codes": ths_member_codes,
        "repairs": repairs,
        "unique_indexes": indexes,
    }
