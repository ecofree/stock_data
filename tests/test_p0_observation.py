import json
from pathlib import Path

import duckdb

from trade_system.p0_observation import audit_five_day_observation


def _manifest(reports: Path, trade_date: str, phase: str, status: str = "completed"):
    run_dir = reports / "runs" / f"{trade_date}-{phase}"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "run.json").write_text(
        json.dumps(
            {
                "run_id": f"{trade_date}-{phase}",
                "trade_date": trade_date,
                "phase": phase,
                "status": status,
                "started_at": f"{trade_date}T09:00:00",
                "completed_at": f"{trade_date}T18:00:00",
                "steps": [],
            }
        ),
        encoding="utf-8",
    )
    if phase == "close":
        (run_dir / "daily_review_latest.md").write_text("review", encoding="utf-8")
        (run_dir / "trading_dashboard_latest.html").write_text("<html></html>", encoding="utf-8")


def test_two_strict_sessions_unlock_configured_observation_window(tmp_path):
    db = tmp_path / "observation.duckdb"
    reports = tmp_path / "reports"
    con = duckdb.connect(str(db))
    con.execute("CREATE TABLE tushare_trade_cal(cal_date DATE, is_open BOOLEAN)")
    con.execute(
        "INSERT INTO tushare_trade_cal VALUES ('2026-07-23',true),('2026-07-24',true)"
    )
    con.execute(
        "CREATE TABLE intraday_stock_flow_batch("
        "trade_date DATE,expected_rows INTEGER,fetched_rows INTEGER,"
        "coverage_pct DOUBLE,status VARCHAR,updated_at TIMESTAMP)"
    )
    con.execute(
        "CREATE TABLE intraday_sector_flow_batch("
        "trade_date DATE,expected_rows INTEGER,fetched_rows INTEGER,"
        "coverage_pct DOUBLE,status VARCHAR,updated_at TIMESTAMP)"
    )
    con.execute(
        "INSERT INTO intraday_stock_flow_batch VALUES "
        "('2026-07-23',5200,5200,100,'success','2026-07-23 15:01:00'),"
        "('2026-07-24',5200,5200,100,'success','2026-07-24 15:01:00')"
    )
    con.execute(
        "INSERT INTO intraday_sector_flow_batch VALUES "
        "('2026-07-23',870,870,100,'success_with_optional_gap','2026-07-23 15:02:00'),"
        "('2026-07-24',870,870,100,'success_with_optional_gap','2026-07-24 15:02:00')"
    )
    con.execute(
        "CREATE TABLE history_fetch_checkpoint("
        "dataset VARCHAR,trade_date DATE,page_no INTEGER,status VARCHAR,"
        "rows_written INTEGER,updated_at TIMESTAMP)"
    )
    for trade_date in ("2026-07-23", "2026-07-24"):
        for dataset in ("daily", "daily_basic", "adj_factor", "moneyflow", "industry_flow"):
            con.execute(
                "INSERT INTO history_fetch_checkpoint VALUES (?,CAST(? AS DATE),0,'success',10,current_timestamp)",
                [dataset, trade_date],
            )
    con.execute(
        "INSERT INTO history_fetch_checkpoint VALUES "
        "('ths_concept_snapshot','2026-07-23',0,'success',1000,current_timestamp)"
    )
    con.execute(
        "CREATE TABLE ths_concept_daily("
        "trade_date DATE,concept_code VARCHAR,stock_count INTEGER,"
        "raw_json VARCHAR,date_verified BOOLEAN)"
    )
    con.execute(
        "INSERT INTO ths_concept_daily "
        "SELECT '2026-07-23', 'THS-' || lpad(CAST(i AS VARCHAR),4,'0'), 2, "
        "'{\"fetched_date\":\"2026-07-23\"}', true "
        "FROM range(374) t(i)"
    )
    con.execute(
        "CREATE TABLE ths_concept_stock_history("
        "trade_date DATE,concept_code VARCHAR,stock_code VARCHAR,"
        "raw_json VARCHAR,date_verified BOOLEAN)"
    )
    con.execute(
        "INSERT INTO ths_concept_stock_history "
        "SELECT '2026-07-23', 'THS-' || lpad(CAST(i % 374 AS VARCHAR),4,'0'), "
        "'00' || lpad(CAST(i AS VARCHAR),4,'0'), "
        "'{\"fetched_date\":\"2026-07-23\"}', true FROM range(748) t(i)"
    )
    con.execute(
        "CREATE TABLE ths_concept_member_checkpoint("
        "trade_date DATE,concept_code VARCHAR,status VARCHAR)"
    )
    con.execute(
        "INSERT INTO ths_concept_member_checkpoint "
        "SELECT '2026-07-23', 'THS-' || lpad(CAST(i AS VARCHAR),4,'0'), 'success' "
        "FROM range(374) t(i)"
    )
    con.close()
    for trade_date in ("2026-07-23", "2026-07-24"):
        for phase in ("auction", "intraday", "close"):
            _manifest(reports, trade_date, phase)

    result = audit_five_day_observation(
        db, reports, "2026-07-24", required_days=2
    )

    assert result["ready_for_p1"] is True
    assert result["consecutive_passes"] == 2

    _manifest(reports, "2026-07-24", "auction", "completed_with_degradation")
    result = audit_five_day_observation(
        db, reports, "2026-07-24", required_days=2
    )
    assert result["ready_for_p1"] is False
    assert result["consecutive_passes"] == 0
