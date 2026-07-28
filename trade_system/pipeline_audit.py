"""Durable task-level audit records for the integrated daily runner."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import duckdb

from base import connect_duckdb


def ensure_pipeline_task_audit(db_path: str | Path) -> None:
    con = connect_duckdb(str(db_path))
    try:
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS pipeline_task_audit (
                run_id VARCHAR,
                trade_date DATE,
                phase VARCHAR,
                task_name VARCHAR,
                status VARCHAR,
                return_code INTEGER,
                started_at TIMESTAMP,
                finished_at TIMESTAMP,
                duration_seconds DOUBLE,
                reason VARCHAR,
                updated_at TIMESTAMP DEFAULT current_timestamp,
                PRIMARY KEY (run_id, task_name)
            )
            """
        )
        con.commit()
    finally:
        con.close()


def record_pipeline_task(
    db_path: str | Path,
    *,
    run_id: str,
    trade_date: str,
    phase: str,
    task_name: str,
    status: str,
    return_code: int | None = None,
    started_at: datetime | None = None,
    finished_at: datetime | None = None,
    duration_seconds: float | None = None,
    reason: str | None = None,
) -> None:
    ensure_pipeline_task_audit(db_path)
    con = connect_duckdb(str(db_path))
    try:
        con.execute(
            """
            INSERT INTO pipeline_task_audit
            (run_id,trade_date,phase,task_name,status,return_code,started_at,
             finished_at,duration_seconds,reason,updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,current_timestamp)
            ON CONFLICT (run_id,task_name) DO UPDATE SET
                status=excluded.status,
                return_code=excluded.return_code,
                started_at=excluded.started_at,
                finished_at=excluded.finished_at,
                duration_seconds=excluded.duration_seconds,
                reason=excluded.reason,
                updated_at=excluded.updated_at
            """,
            [run_id, trade_date, phase, task_name, status, return_code,
             started_at, finished_at, duration_seconds, (reason or "")[:1000]],
        )
        con.commit()
    finally:
        con.close()
