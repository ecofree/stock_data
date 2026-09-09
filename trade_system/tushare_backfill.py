"""Recoverable TuShare backfill planning.

This module keeps TuShare supplementation incremental: identify missing rows,
create resumable tasks, and run only the pending work. It stores operational
state in DuckDB, not large raw API responses.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import duckdb

from base import DuckDBStore
from trade_system.schema import init_schema
from trade_system.tushare_relay import (
    TushareRelayClient,
    collect_tushare_adj_factor,
    collect_tushare_daily,
    collect_tushare_daily_basic,
    collect_tushare_index_daily,
    index_code_to_ts_code,
    sync_tushare_ohlc_to_core_tables,
    ts_code_to_index_code,
)
from trade_system.trading_calendar import open_session_dates


DATA_KIND_SPECS = {
    "daily": {"table": "tushare_daily", "code_col": "stock_code", "is_index": False},
    "daily_basic": {"table": "tushare_daily_basic", "code_col": "stock_code", "is_index": False},
    "adj_factor": {"table": "tushare_adj_factor", "code_col": "stock_code", "is_index": False},
    "index_daily": {"table": "tushare_index_daily", "code_col": "index_code", "is_index": True},
}


def _iso_date(value: str) -> str:
    raw = "".join(ch for ch in str(value or "") if ch.isdigit())
    if len(raw) >= 8:
        return f"{raw[:4]}-{raw[4:6]}-{raw[6:8]}"
    raise ValueError(f"invalid date: {value}")


def _compact_date(value: str) -> str:
    return _iso_date(value).replace("-", "")


def _date_range(start_date: str, end_date: str) -> list[str]:
    start = datetime.strptime(_iso_date(start_date), "%Y-%m-%d").date()
    end = datetime.strptime(_iso_date(end_date), "%Y-%m-%d").date()
    dates = []
    current = start
    while current <= end:
        if current.weekday() < 5:
            dates.append(current.isoformat())
        current += timedelta(days=1)
    return dates


def _open_dates(con: duckdb.DuckDBPyConnection, start_date: str, end_date: str) -> list[str]:
    start = _iso_date(start_date)
    end = _iso_date(end_date)
    return open_session_dates(con, start, end)


def _normalize_code(data_kind: str, code: str) -> str:
    if DATA_KIND_SPECS[data_kind]["is_index"]:
        return ts_code_to_index_code(index_code_to_ts_code(code))
    digits = "".join(ch for ch in str(code or "") if ch.isdigit())
    return digits.zfill(6)[-6:] if digits else ""


def _task_id(data_kind: str, code: str, start_date: str, end_date: str) -> str:
    return f"{data_kind}:{code}:{_iso_date(start_date)}:{_iso_date(end_date)}"


def build_tushare_gap_list(
    db_path: str | Path,
    *,
    stock_codes: list[str],
    start_date: str,
    end_date: str,
    data_kinds: list[str],
) -> list[dict[str, Any]]:
    """Record row-level coverage gaps for requested TuShare data kinds."""

    store = DuckDBStore(str(db_path))
    try:
        init_schema(store.conn)
        open_dates = _open_dates(store.conn, start_date, end_date)
        expected_rows = len(open_dates)
        gap_rows: list[dict[str, Any]] = []
        for data_kind in data_kinds:
            if data_kind not in DATA_KIND_SPECS:
                raise ValueError(f"unsupported TuShare data kind: {data_kind}")
            spec = DATA_KIND_SPECS[data_kind]
            source_table = spec["table"]
            code_col = spec["code_col"]
            for raw_code in stock_codes:
                code = _normalize_code(data_kind, raw_code)
                existing_rows = int(
                    store.conn.execute(
                        f"""
                        SELECT count(DISTINCT date)
                        FROM {source_table}
                        WHERE {code_col} = ?
                          AND date BETWEEN CAST(? AS DATE) AND CAST(? AS DATE)
                        """,
                        [code, _iso_date(start_date), _iso_date(end_date)],
                    ).fetchone()[0]
                )
                missing_rows = max(expected_rows - existing_rows, 0)
                status = "missing" if missing_rows else "complete"
                gap = {
                    "data_kind": data_kind,
                    "code": code,
                    "start_date": _iso_date(start_date),
                    "end_date": _iso_date(end_date),
                    "expected_rows": expected_rows,
                    "existing_rows": existing_rows,
                    "missing_rows": missing_rows,
                    "status": status,
                    "source_table": source_table,
                }
                gap_rows.append(gap)
        store.insert_rows(
            "tushare_gap_status",
            [
                (
                    row["data_kind"],
                    row["code"],
                    row["start_date"],
                    row["end_date"],
                    row["expected_rows"],
                    row["existing_rows"],
                    row["missing_rows"],
                    row["status"],
                    row["source_table"],
                )
                for row in gap_rows
            ],
            [
                "data_kind",
                "code",
                "start_date",
                "end_date",
                "expected_rows",
                "existing_rows",
                "missing_rows",
                "status",
                "source_table",
            ],
            replace_on=["data_kind", "code", "start_date", "end_date"],
        )
        return gap_rows
    finally:
        store.close()


def plan_tushare_backfill_tasks(db_path: str | Path, gap_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Create pending tasks for missing gaps without downgrading completed work."""

    store = DuckDBStore(str(db_path))
    try:
        init_schema(store.conn)
        planned: list[dict[str, Any]] = []
        for gap in gap_rows:
            if gap.get("status") != "missing" or int(gap.get("missing_rows") or 0) <= 0:
                continue
            task_id = _task_id(gap["data_kind"], gap["code"], gap["start_date"], gap["end_date"])
            existing = store.conn.execute(
                "SELECT status FROM tushare_backfill_task WHERE task_id = ?",
                [task_id],
            ).fetchone()
            if existing and existing[0] == "done":
                continue
            task = {
                "task_id": task_id,
                "data_kind": gap["data_kind"],
                "code": gap["code"],
                "start_date": gap["start_date"],
                "end_date": gap["end_date"],
                "status": "pending",
                "attempts": 0,
                "rows_inserted": 0,
                "last_error": "",
            }
            planned.append(task)
        store.insert_rows(
            "tushare_backfill_task",
            [
                (
                    task["task_id"],
                    task["data_kind"],
                    task["code"],
                    task["start_date"],
                    task["end_date"],
                    task["status"],
                    task["attempts"],
                    task["rows_inserted"],
                    task["last_error"],
                )
                for task in planned
            ],
            [
                "task_id",
                "data_kind",
                "code",
                "start_date",
                "end_date",
                "status",
                "attempts",
                "rows_inserted",
                "last_error",
            ],
            replace_on=["task_id"],
        )
        return planned
    finally:
        store.close()


def _collect_task(client: TushareRelayClient, store: DuckDBStore, task: dict[str, Any]) -> int:
    data_kind = task["data_kind"]
    code = task["code"]
    start_date = _compact_date(task["start_date"])
    end_date = _compact_date(task["end_date"])
    if data_kind == "daily":
        return collect_tushare_daily(client, store, [code], start_date, end_date)
    if data_kind == "daily_basic":
        return collect_tushare_daily_basic(client, store, [code], start_date, end_date)
    if data_kind == "adj_factor":
        return collect_tushare_adj_factor(client, store, [code], start_date, end_date)
    if data_kind == "index_daily":
        return collect_tushare_index_daily(client, store, [code], start_date, end_date)
    raise ValueError(f"unsupported TuShare data kind: {data_kind}")


def run_pending_tushare_backfill_tasks(
    db_path: str | Path,
    *,
    client: TushareRelayClient | None = None,
    limit: int = 10,
    retry_errors: bool = False,
    sync_core: bool = False,
) -> list[dict[str, Any]]:
    """Run a bounded batch of pending tasks and keep status resumable."""

    store = DuckDBStore(str(db_path))
    client = client or TushareRelayClient()
    sync_requests: list[dict[str, Any]] = []
    try:
        init_schema(store.conn)
        statuses = ["pending", "running"]
        if retry_errors:
            statuses.append("error")
        placeholders = ", ".join(["?"] * len(statuses))
        tasks = [
            {
                "task_id": row[0],
                "data_kind": row[1],
                "code": row[2],
                "start_date": str(row[3])[:10],
                "end_date": str(row[4])[:10],
            }
            for row in store.conn.execute(
                f"""
                SELECT task_id, data_kind, code, start_date, end_date
                FROM tushare_backfill_task
                WHERE status IN ({placeholders})
                ORDER BY created_at, task_id
                LIMIT ?
                """,
                [*statuses, int(limit)],
            ).fetchall()
        ]
        results: list[dict[str, Any]] = []
        for task in tasks:
            store.conn.execute(
                """
                UPDATE tushare_backfill_task
                SET status='running',
                    attempts=coalesce(attempts, 0) + 1,
                    updated_at=current_timestamp
                WHERE task_id=?
                """,
                [task["task_id"]],
            )
            try:
                rows_inserted = _collect_task(client, store, task)
                status = "done" if rows_inserted else "empty"
                last_error = ""
            except Exception as exc:
                rows_inserted = 0
                status = "error"
                last_error = f"{type(exc).__name__}: {exc}"[:500]
            store.conn.execute(
                """
                UPDATE tushare_backfill_task
                SET status=?,
                    rows_inserted=?,
                    last_error=?,
                    updated_at=current_timestamp
                WHERE task_id=?
                """,
                [status, rows_inserted, last_error, task["task_id"]],
            )
            result = dict(task)
            result.update({"status": status, "rows_inserted": rows_inserted, "last_error": last_error})
            results.append(result)
            if sync_core and status == "done" and task["data_kind"] in {"daily", "index_daily"}:
                sync_requests.append(task)
    finally:
        store.close()
    for task in sync_requests:
        if task["data_kind"] == "daily":
            sync_tushare_ohlc_to_core_tables(
                db_path,
                stock_codes=[task["code"]],
                start_date=task["start_date"],
                end_date=task["end_date"],
            )
        elif task["data_kind"] == "index_daily":
            sync_tushare_ohlc_to_core_tables(
                db_path,
                index_codes=[task["code"]],
                start_date=task["start_date"],
                end_date=task["end_date"],
            )
    return results


def write_tushare_gap_report(db_path: str | Path, out_path: str | Path) -> Path:
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        gap_summary = con.execute(
            """
            SELECT data_kind, status, count(*) AS task_count, sum(missing_rows) AS missing_rows
            FROM tushare_gap_status
            GROUP BY data_kind, status
            ORDER BY data_kind, status
            """
        ).fetchall()
        task_summary = con.execute(
            """
            SELECT data_kind, status, count(*) AS task_count, sum(rows_inserted) AS rows_inserted
            FROM tushare_backfill_task
            GROUP BY data_kind, status
            ORDER BY data_kind, status
            """
        ).fetchall()
        latest_tasks = con.execute(
            """
            SELECT data_kind, code, start_date, end_date, status, attempts, rows_inserted
            FROM tushare_backfill_task
            ORDER BY updated_at DESC, task_id
            LIMIT 30
            """
        ).fetchall()
    finally:
        con.close()
    lines = [
        "# TuShare 缺口与补数任务",
        "",
        "## 缺口概览",
        "",
        "| 数据类型 | 状态 | 任务数 | 缺失行 |",
        "|---|---:|---:|---:|",
    ]
    lines.extend(f"| {kind} | {status} | {count} | {missing or 0} |" for kind, status, count, missing in gap_summary)
    lines.extend(
        [
            "",
            "## 补数任务概览",
            "",
            "| 数据类型 | 状态 | 任务数 | 写入行 |",
            "|---|---:|---:|---:|",
        ]
    )
    lines.extend(f"| {kind} | {status} | {count} | {rows or 0} |" for kind, status, count, rows in task_summary)
    lines.extend(
        [
            "",
            "## 最近任务",
            "",
            "| 数据类型 | 代码 | 开始 | 结束 | 状态 | 尝试 | 写入行 |",
            "|---|---|---:|---:|---:|---:|---:|",
        ]
    )
    lines.extend(
        f"| {kind} | {code} | {start} | {end} | {status} | {attempts or 0} | {rows or 0} |"
        for kind, code, start, end, status, attempts, rows in latest_tasks
    )
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out
