"""Professional data backfill helpers for real auction, capital, index, and K-line sources."""

from __future__ import annotations

import csv
from pathlib import Path

import duckdb


TABLE_SPECS = {
    "kline": {
        "columns": [
            "date",
            "stock_code",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "turnover",
            "change_pct",
            "ktype",
        ],
        "replace_on": ["date", "stock_code", "ktype"],
        "ddl": """
            CREATE TABLE IF NOT EXISTS kline (
                date DATE,
                stock_code VARCHAR,
                open DOUBLE,
                high DOUBLE,
                low DOUBLE,
                close DOUBLE,
                volume BIGINT,
                turnover BIGINT,
                change_pct DOUBLE,
                ktype VARCHAR,
                fetched_at TIMESTAMP DEFAULT current_timestamp
            )
        """,
    },
    "index_kline": {
        "columns": [
            "date",
            "index_code",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "turnover",
            "change_pct",
            "ktype",
        ],
        "replace_on": ["date", "index_code", "ktype"],
        "ddl": """
            CREATE TABLE IF NOT EXISTS index_kline (
                date DATE,
                index_code VARCHAR,
                open DOUBLE,
                high DOUBLE,
                low DOUBLE,
                close DOUBLE,
                volume BIGINT,
                turnover BIGINT,
                change_pct DOUBLE,
                ktype VARCHAR,
                raw_json VARCHAR,
                fetched_at TIMESTAMP DEFAULT current_timestamp
            )
        """,
    },
    "sector_capital": {
        "columns": [
            "date",
            "sector_code",
            "main_net_inflow",
            "super_net_inflow",
            "big_net_inflow",
            "mid_net_inflow",
            "small_net_inflow",
        ],
        "replace_on": ["date", "sector_code"],
        "ddl": """
            CREATE TABLE IF NOT EXISTS sector_capital (
                date DATE,
                sector_code VARCHAR,
                main_net_inflow BIGINT,
                super_net_inflow BIGINT,
                big_net_inflow BIGINT,
                mid_net_inflow BIGINT,
                small_net_inflow BIGINT,
                fetched_at TIMESTAMP DEFAULT current_timestamp
            )
        """,
    },
    "auction_tick": {
        "columns": ["date", "stock_code", "time", "price", "volume"],
        "replace_on": ["date", "stock_code", "time"],
        "ddl": """
            CREATE TABLE IF NOT EXISTS auction_tick (
                date DATE,
                stock_code VARCHAR,
                time VARCHAR,
                price DOUBLE,
                volume BIGINT,
                fetched_at TIMESTAMP DEFAULT current_timestamp
            )
        """,
    },
    "auction_bidding_anomaly": {
        "columns": ["date", "stock_code", "anomaly_type", "anomaly_value"],
        "replace_on": ["date", "stock_code", "anomaly_type"],
        "ddl": """
            CREATE TABLE IF NOT EXISTS auction_bidding_anomaly (
                date DATE,
                stock_code VARCHAR,
                anomaly_type VARCHAR,
                anomaly_value DOUBLE,
                fetched_at TIMESTAMP DEFAULT current_timestamp
            )
        """,
    },
}


def _read_csv_rows(path: str | Path, columns: list[str]) -> list[tuple]:
    with Path(path).open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        rows = []
        for item in reader:
            rows.append(tuple(item.get(column, None) for column in columns))
    return rows


def _replace_insert(
    con: duckdb.DuckDBPyConnection,
    table_name: str,
    rows: list[tuple],
    columns: list[str],
    replace_on: list[str],
) -> int:
    if not rows:
        return 0
    replace_indexes = [columns.index(column) for column in replace_on]
    delete_sql = f'DELETE FROM "{table_name}" WHERE ' + " AND ".join(f"{column} = ?" for column in replace_on)
    insert_sql = (
        f'INSERT INTO "{table_name}" ({", ".join(columns)}) '
        f'VALUES ({", ".join(["?"] * len(columns))})'
    )
    count = 0
    for row in rows:
        con.execute(delete_sql, [row[index] for index in replace_indexes])
        con.execute(insert_sql, list(row))
        count += 1
    return count


def import_professional_csvs(db_path: str | Path, table_to_csv: dict[str, str | Path | None]) -> dict[str, int]:
    """Import operator-provided real data CSVs into canonical raw tables.

    The importer uses delete+insert keys per table, so repeated imports of the same
    file refresh rows without duplicating them.
    """
    con = duckdb.connect(str(db_path))
    try:
        result = {}
        for table_name, csv_path in table_to_csv.items():
            if not csv_path:
                continue
            if table_name not in TABLE_SPECS:
                raise ValueError(f"Unsupported professional backfill table: {table_name}")
            spec = TABLE_SPECS[table_name]
            con.execute(spec["ddl"])
            rows = _read_csv_rows(csv_path, spec["columns"])
            result[table_name] = _replace_insert(
                con,
                table_name,
                rows,
                spec["columns"],
                spec["replace_on"],
            )
        return result
    finally:
        con.close()
