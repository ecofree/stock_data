import json
from pathlib import Path

import duckdb

from trade_system.p0_observation import audit_five_day_observation
from trade_system.v2.domain import canonical, identity
from trade_system.v2.publisher import publish
from trade_system.v2.research_product_view import render


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
                "scope": "transitional_market_collection_only",
                "collector_contract_sha256": "a" * 64,
            }
        ),
        encoding="utf-8",
    )


def test_two_strict_sessions_unlock_configured_observation_window(tmp_path):
    db = tmp_path / "observation.duckdb"
    reports = tmp_path / "reports"
    workspace = tmp_path / 'workspace'
    con = duckdb.connect(str(db))
    con.execute("CREATE TABLE tushare_trade_cal(cal_date DATE, is_open BOOLEAN, exchange VARCHAR)")
    con.execute(
        "INSERT INTO tushare_trade_cal VALUES ('2026-07-23',true,'SSE'),('2026-07-24',true,'SSE'),"
        "('2026-07-23',true,'SZSE'),('2026-07-24',true,'SZSE')"
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
    con.execute(
        "CREATE TABLE ths_concept_snapshot_expectation("
        "trade_date DATE PRIMARY KEY,expected_concepts INTEGER,provider VARCHAR,"
        "catalog_hash VARCHAR,status VARCHAR DEFAULT 'success')"
    )
    con.execute(
        "INSERT INTO ths_concept_snapshot_expectation "
        "VALUES ('2026-07-23',374,'test_catalog','test','success')"
    )
    con.close()
    for trade_date in ("2026-07-23", "2026-07-24"):
        for phase in ("auction", "intraday", "close"):
            _manifest(reports, trade_date, phase)
        market = {'trade_date':trade_date,'as_of':trade_date+'T19:30:00+08:00',
                  'scope':'read_only_market_review_not_execution','execution_ready':False,
                  'stocks':[{'stock_code':'000001'}]}
        market['snapshot_id'] = identity(market)
        data = {'market':market,'execution_ready':False}
        data['report_id'] = identity(data)
        publish(workspace/'publication',trade_date,
                {'desk.json':canonical(data).encode(),'index.html':render(data).encode()},
                generation=int(trade_date[-2:]))

    result = audit_five_day_observation(
        db, reports, "2026-07-24", required_days=2, workspace=workspace, collector_contract_sha256='a'*64
    )

    assert result["ready_for_p1"] is True
    assert result["consecutive_passes"] == 2
    assert 'dashboard' not in result['daily'][0]['checks']

    wrong_version = audit_five_day_observation(db,reports,'2026-07-24',required_days=2,
                                               workspace=workspace,collector_contract_sha256='b'*64)
    assert not wrong_version['ready_for_p1']

    close_path = reports/'runs/2026-07-24-close/run.json'
    close = json.loads(close_path.read_text())
    close['completed_at'] = '2026-07-24T20:00:00+08:00'
    close_path.write_text(json.dumps(close))
    premature = audit_five_day_observation(db,reports,'2026-07-24',required_days=2,
                                           workspace=workspace,collector_contract_sha256='a'*64)
    assert premature['daily'][-1]['publication']['error'] == 'publication_precedes_close_completion'
    _manifest(reports,'2026-07-24','close')

    page = workspace/'publication/runs/2026-07-24/index.html'
    original = page.read_bytes()
    page.write_bytes(b'<html>tampered</html>')
    corrupt = audit_five_day_observation(db,reports,'2026-07-24',required_days=2,
                                        workspace=workspace,collector_contract_sha256='a'*64)
    assert not corrupt['ready_for_p1'] and not corrupt['daily'][-1]['checks']['publication']
    page.write_bytes(original)

    _manifest(reports, "2026-07-24", "auction", "completed_with_degradation")
    result = audit_five_day_observation(
        db, reports, "2026-07-24", required_days=2, workspace=workspace, collector_contract_sha256='a'*64
    )
    assert result["ready_for_p1"] is False
    assert result["consecutive_passes"] == 0
