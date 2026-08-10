"""Durable task-level audit records for the integrated daily runner."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import duckdb

from base import connect_duckdb


def reap_stale_pipeline_tasks(
    db_path: str | Path,
    *,
    max_age_seconds: int = 6 * 60 * 60,
) -> int:
    """Close abandoned ``running`` audit rows without touching live work.

    A process can be terminated after it writes its start marker but before it
    records a terminal status.  Those rows must not make the next audit look
    perpetually active.  The threshold is deliberately conservative so a
    genuinely long-running collector is left alone; callers can override it
    in tests or for a known batch window.
    """
    cutoff = datetime.now() - timedelta(seconds=max_age_seconds)
    con = connect_duckdb(str(db_path))
    try:
        pending = con.execute(
            """
            SELECT count(*)
            FROM pipeline_task_audit
            WHERE status='running'
              AND started_at IS NOT NULL
              AND started_at < ?
            """,
            [cutoff],
        ).fetchone()[0]
        con.execute(
            """
            UPDATE pipeline_task_audit
            SET status='aborted',
                return_code=-1,
                finished_at=COALESCE(finished_at, current_timestamp),
                duration_seconds=COALESCE(
                    duration_seconds,
                    epoch(COALESCE(finished_at, current_timestamp) - started_at)
                ),
                reason='stale_running_reaped_after_seconds',
                updated_at=current_timestamp
            WHERE status='running'
              AND started_at IS NOT NULL
              AND started_at < ?
            """,
            [cutoff],
        )
        con.commit()
        return int(pending or 0)
    finally:
        con.close()


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
    # Run after the schema exists and outside the create transaction.  This
    # keeps recovery safe when called by every task wrapper while avoiding a
    # nested connection during table creation.
    reap_stale_pipeline_tasks(db_path)


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
