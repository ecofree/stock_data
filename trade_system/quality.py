"""Data quality checks for trading workflows."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
import re
from typing import Iterable

import duckdb


DEFAULT_DUPLICATE_KEYS = {
    "market_mood": ["date"],
    "market_rise_fall": ["date"],
    "daily_summary": ["date"],
    "daily_sentiment": ["date"],
    "daily_new_high": ["date"],
    "sector_strength": ["date", "sector_code"],
    "sector_stocks": ["date", "sector_code", "stock_code"],
    "sector_ranking": ["date", "sector_code"],
    "sector_capital": ["date", "sector_code"],
    "ladder_market": ["date", "board_level", "stock_code"],
    "lhb_list": ["date", "stock_code", "reason"],
    "kline": ["date", "stock_code", "ktype"],
    "index_kline": ["date", "index_code", "ktype"],
    "auction_tick": ["date", "stock_code", "time"],
    "auction_bidding_anomaly": ["date", "stock_code", "anomaly_type"],
    "advanced_dadan_kline": ["date", "stock_code", "ktype"],
    "advanced_zjmm_min": ["date", "stock_code", "time"],
    "advanced_main_activity_kline": ["date", "stock_code", "ktype"],
    "advanced_main_monitor": ["date", "stock_code", "ranking"],
    "tushare_trade_cal": ["exchange", "cal_date"],
    "tushare_stock_basic": ["ts_code"],
    "tushare_daily": ["ts_code", "date"],
    "tushare_daily_basic": ["ts_code", "date"],
    "tushare_moneyflow": ["ts_code", "date"],
    "tushare_moneyflow_industry": ["trade_date", "ts_code"],
    "tushare_adj_factor": ["ts_code", "date"],
    "tushare_index_daily": ["ts_code", "date"],
    "tushare_gap_status": ["data_kind", "code", "start_date", "end_date"],
    "tushare_backfill_task": ["task_id"],
    "l2_realtime_index_list": ["date", "index_code"],
    "l2_stock_intraday": ["date", "stock_code", "time"],
    "l2_stock_bigorder": ["date", "stock_code", "time"],
    "l2_sector_intraday": ["date", "sector_code", "time"],
    "market_regime_snapshot": ["trade_date"],
    "sector_rotation_score": ["trade_date", "sector_code"],
    "stock_candidate_score": ["trade_date", "stock_code"],
    "stock_candidate_stage_signal": ["trade_date", "stage", "stock_code"],
    "alert_events": ["trade_date", "severity", "category", "message"],
    "watchlist": ["trade_date", "stock_code"],
    "trade_plan": ["trade_date", "stock_code"],
    "risk_snapshot": ["trade_date"],
    "portfolio_snapshot": ["trade_date", "snapshot_time", "stock_code"],
    "operator_trade_outcome": ["trade_date", "stock_code"],
    "kpl_concept_daily": ["trade_date", "concept_code"],
    "kpl_concept_stock_history": ["trade_date", "concept_code", "stock_code"],
    "ths_concept_daily": ["trade_date", "concept_code"],
    "ths_concept_stock_history": ["trade_date", "concept_code", "stock_code"],
    "multi_source_stock_flow": ["source_date", "stock_code", "provider"],
    "multi_source_sector_flow": ["source_date", "sector_code", "provider"],
    "multi_source_kline": ["source_date", "asset_type", "asset_code", "provider"],
}


NORMALIZED_DUPLICATE_SQL = {
    "kline": '"date", "stock_code", upper(coalesce(nullif(trim(cast("ktype" as varchar)), \'\'), \'UNKNOWN\'))',
    "index_kline": '"date", "index_code", upper(coalesce(nullif(trim(cast("ktype" as varchar)), \'\'), \'UNKNOWN\'))',
    # P1-1: THS member codes mix bare (000001) and suffixed (000001.SZ) formats;
    # compare on the de-suffixed code so the duplicate audit catches mixed-format dupes.
    "ths_concept_stock_history": '"trade_date", "concept_code", regexp_replace(CAST(stock_code AS VARCHAR), \'[.].*$\', \'\')',
}


def table_exists(con: duckdb.DuckDBPyConnection, table_name: str) -> bool:
    row = con.execute(
        "SELECT count(*) FROM information_schema.tables "
        "WHERE table_schema = 'main' AND table_name = ?",
        [table_name],
    ).fetchone()
    return bool(row and row[0])


def table_columns(con: duckdb.DuckDBPyConnection, table_name: str) -> list[str]:
    if not table_exists(con, table_name):
        return []
    return [row[1] for row in con.execute(f'PRAGMA table_info("{table_name}")').fetchall()]


def _find_duplicate_keys_con(
    con: duckdb.DuckDBPyConnection,
    table_name: str,
    key_columns: Iterable[str],
    key_sql_override: str | None = None,
) -> dict:
    keys = list(key_columns)
    if not table_exists(con, table_name):
        return {
            "table": table_name,
            "keys": keys,
            "rows": 0,
            "duplicate_groups": 0,
            "max_duplicate_count": 0,
            "status": "missing_table",
        }
    cols = table_columns(con, table_name)
    missing = [col for col in keys if col not in cols]
    if missing:
        return {
            "table": table_name,
            "keys": keys,
            "rows": con.execute(f'SELECT count(*) FROM "{table_name}"').fetchone()[0],
            "duplicate_groups": 0,
            "max_duplicate_count": 0,
            "status": f"missing_columns:{','.join(missing)}",
        }
    key_sql = key_sql_override or ", ".join(f'"{col}"' for col in keys)
    rows = con.execute(f'SELECT count(*) FROM "{table_name}"').fetchone()[0]
    duplicate_groups = con.execute(
        f'SELECT count(*) FROM ('
        f'SELECT {key_sql}, count(*) c FROM "{table_name}" '
        f'GROUP BY {key_sql} HAVING count(*) > 1)'
    ).fetchone()[0]
    max_duplicate_count = con.execute(
        f'SELECT coalesce(max(c), 0) FROM ('
        f'SELECT {key_sql}, count(*) c FROM "{table_name}" GROUP BY {key_sql})'
    ).fetchone()[0]
    return {
        "table": table_name,
        "keys": keys,
        "rows": rows,
        "duplicate_groups": duplicate_groups,
        "max_duplicate_count": max_duplicate_count,
        "status": "ok",
    }


def find_duplicate_keys(db_path: str | Path, table_name: str, key_columns: Iterable[str]) -> dict:
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        return _find_duplicate_keys_con(con, table_name, key_columns)
    finally:
        con.close()


def _quote_identifier(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _safe_archive_name(table_name: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_]", "_", table_name)
    return f"_dedupe_archive_{safe}"


def dedupe_table(
    db_path: str | Path,
    table_name: str,
    key_columns: Iterable[str],
    order_column: str | None = None,
    dry_run: bool = False,
) -> dict:
    """Remove duplicate business-key rows while archiving discarded rows.

    Keeps the latest row by fetched_at/updated_at when present, otherwise by rowid.
    """
    keys = list(key_columns)
    con = duckdb.connect(str(db_path))
    try:
        if not table_exists(con, table_name):
            return {"table": table_name, "status": "missing_table", "removed_rows": 0}
        cols = table_columns(con, table_name)
        missing = [key for key in keys if key not in cols]
        if missing:
            return {"table": table_name, "status": f"missing_columns:{','.join(missing)}", "removed_rows": 0}
        if order_column and order_column not in cols and order_column != "rowid":
            return {"table": table_name, "status": f"missing_order_column:{order_column}", "removed_rows": 0}

        selected_order = order_column
        if selected_order is None:
            selected_order = next((col for col in ("fetched_at", "updated_at") if col in cols), "rowid")

        table_sql = _quote_identifier(table_name)
        archive_name = _safe_archive_name(table_name)
        archive_sql = _quote_identifier(archive_name)
        key_sql = ", ".join(_quote_identifier(key) for key in keys)
        if selected_order == "rowid":
            order_sql = "rowid DESC"
        else:
            order_sql = f"{_quote_identifier(selected_order)} DESC NULLS LAST, rowid DESC"

        duplicate_before = _find_duplicate_keys_con(con, table_name, keys)
        rows_before = duplicate_before["rows"]
        con.execute("DROP TABLE IF EXISTS _dedupe_drop_rowids")
        con.execute(
            f"""
            CREATE TEMP TABLE _dedupe_drop_rowids AS
            SELECT _rowid
            FROM (
                SELECT
                    rowid AS _rowid,
                    row_number() OVER (PARTITION BY {key_sql} ORDER BY {order_sql}) AS _rn
                FROM {table_sql}
            )
            WHERE _rn > 1
            """
        )
        remove_count = con.execute("SELECT count(*) FROM _dedupe_drop_rowids").fetchone()[0]
        if remove_count and not dry_run:
            con.execute(
                f"CREATE TABLE IF NOT EXISTS {archive_sql} AS "
                f"SELECT *, current_timestamp AS archived_at FROM {table_sql} WHERE false"
            )
            con.execute(
                f"""
                INSERT INTO {archive_sql}
                SELECT t.*, current_timestamp AS archived_at
                FROM {table_sql} t
                JOIN _dedupe_drop_rowids d ON t.rowid = d._rowid
                """
            )
            con.execute(f"DELETE FROM {table_sql} WHERE rowid IN (SELECT _rowid FROM _dedupe_drop_rowids)")
        duplicate_after = _find_duplicate_keys_con(con, table_name, keys)
        return {
            "table": table_name,
            "status": "ok",
            "key_columns": keys,
            "order_column": selected_order,
            "rows_before": rows_before,
            "rows_after": duplicate_after["rows"],
            "duplicate_groups_before": duplicate_before["duplicate_groups"],
            "duplicate_groups_after": duplicate_after["duplicate_groups"],
            "removed_rows": remove_count if not dry_run else 0,
            "would_remove_rows": remove_count,
            "archive_table": archive_name if remove_count else None,
        }
    finally:
        con.close()


def collect_table_coverage(db_path: str | Path) -> list[dict]:
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        relations = [
            (row[0], row[1])
            for row in con.execute(
                "SELECT table_name, table_type FROM information_schema.tables "
                "WHERE table_schema = 'main' ORDER BY table_name"
            ).fetchall()
        ]
        coverage = []
        for table_name, table_type in relations:
            rows = con.execute(f'SELECT count(*) FROM "{table_name}"').fetchone()[0]
            cols = table_columns(con, table_name)
            item = {
                "table": table_name,
                "relation_type": table_type,
                "rows": rows,
                "date_column": None,
                "min_date": None,
                "max_date": None,
                "distinct_dates": None,
            }
            date_col = next((col for col in ("date", "trade_date", "fetched_at") if col in cols), None)
            if date_col:
                min_date, max_date, distinct_dates = con.execute(
                    f'SELECT min("{date_col}"), max("{date_col}"), count(distinct "{date_col}") '
                    f'FROM "{table_name}"'
                ).fetchone()
                item.update(
                    {
                        "date_column": date_col,
                        "min_date": min_date,
                        "max_date": max_date,
                        "distinct_dates": distinct_dates,
                    }
                )
            coverage.append(item)
        return coverage
    finally:
        con.close()


def run_quality_audit(db_path: str | Path, duplicate_keys: dict[str, list[str]] | None = None) -> dict:
    keys = duplicate_keys or DEFAULT_DUPLICATE_KEYS
    coverage = collect_table_coverage(db_path)
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        duplicates = [
            _find_duplicate_keys_con(
                con,
                table,
                cols,
                key_sql_override=NORMALIZED_DUPLICATE_SQL.get(table),
            )
            for table, cols in keys.items()
        ]
    finally:
        con.close()
    base_relations = [item for item in coverage if item["relation_type"] == "BASE TABLE"]
    views = [item for item in coverage if item["relation_type"] == "VIEW"]
    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "summary": {
            "table_count": len(base_relations),
            "view_count": len(views),
            "relation_count": len(coverage),
            "total_rows": sum(item["rows"] for item in base_relations),
            "view_rows": sum(item["rows"] for item in views),
            "duplicate_issue_count": sum(1 for item in duplicates if item["duplicate_groups"] > 0),
        },
        "coverage": coverage,
        "duplicates": duplicates,
    }


def render_quality_markdown(audit: dict) -> str:
    lines = [
        "# Data Quality Audit",
        "",
        f"- Generated at: {audit['generated_at']}",
        f"- Base table count: {audit['summary']['table_count']}",
        f"- View count: {audit['summary'].get('view_count', 0)}",
        f"- Physical table rows: {audit['summary']['total_rows']}",
        f"- Derived view rows: {audit['summary'].get('view_rows', 0)}",
        f"- Duplicate issue count: {audit['summary']['duplicate_issue_count']}",
        "",
        "## Duplicate Key Checks",
        "",
        "| Table | Rows | Duplicate Groups | Max Duplicate | Status |",
        "|---|---:|---:|---:|---|",
    ]
    for item in audit["duplicates"]:
        lines.append(
            f"| `{item['table']}` | {item['rows']} | {item['duplicate_groups']} | "
            f"{item['max_duplicate_count']} | {item['status']} |"
        )
    lines.extend(
        [
            "",
            "## Coverage",
            "",
            "| Relation | Type | Rows | Date Column | Min | Max | Distinct Dates |",
            "|---|---|---:|---|---|---|---:|",
        ]
    )
    for item in audit["coverage"]:
        lines.append(
            f"| `{item['table']}` | {item.get('relation_type', '')} | {item['rows']} | {item['date_column'] or ''} | "
            f"{item['min_date'] or ''} | {item['max_date'] or ''} | {item['distinct_dates'] or 0} |"
        )
    lines.append("")
    return "\n".join(lines)


def write_quality_report(audit: dict, out_path: str | Path) -> Path:
    path = Path(out_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_quality_markdown(audit), encoding="utf-8")
    return path
