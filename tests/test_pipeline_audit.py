from datetime import datetime

import duckdb

from trade_system.pipeline_audit import reap_stale_pipeline_tasks, record_pipeline_task


def test_pipeline_task_audit_is_durable(tmp_path):
    db = tmp_path / "audit.duckdb"
    record_pipeline_task(
        db,
        run_id="run-1",
        trade_date="2026-07-16",
        phase="intraday",
        task_name="collect_market_context",
        status="degraded",
        return_code=2,
        started_at=datetime(2026, 7, 16, 10, 0),
        finished_at=datetime(2026, 7, 16, 10, 0, 2),
        duration_seconds=2,
        reason="kpl_503",
    )
    con = duckdb.connect(str(db), read_only=True)
    row = con.execute(
        "SELECT run_id,trade_date,phase,task_name,status,return_code,reason FROM pipeline_task_audit"
    ).fetchone()
    con.close()
    assert row == ("run-1", datetime(2026, 7, 16).date(), "intraday", "collect_market_context", "degraded", 2, "kpl_503")


def test_stale_running_tasks_are_reaped_but_recent_tasks_are_kept(tmp_path):
    db = tmp_path / "reap.duckdb"
    record_pipeline_task(
        db,
        run_id="recent",
        trade_date="2026-07-16",
        phase="close",
        task_name="recent_task",
        status="running",
        started_at=datetime.now(),
    )
    record_pipeline_task(
        db,
        run_id="old",
        trade_date="2026-07-16",
        phase="close",
        task_name="old_task",
        status="running",
        started_at=datetime(2026, 7, 16, 8, 0),
    )

    changed = reap_stale_pipeline_tasks(db, max_age_seconds=60)
    assert changed >= 1
    con = duckdb.connect(str(db), read_only=True)
    rows = con.execute(
        "SELECT run_id,status,return_code,reason FROM pipeline_task_audit ORDER BY run_id"
    ).fetchall()
    con.close()
    assert rows[0][0:3] == ("old", "aborted", -1)
    assert rows[0][3] == "stale_running_reaped_after_seconds"
    assert rows[1][0:3] == ("recent", "running", None)
