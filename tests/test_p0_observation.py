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
                "started_at": f"{trade_date}T"+{'auction':'08:50:00','intraday':'10:00:00'}.get(phase,'17:30:00'),
                "completed_at": f"{trade_date}T"+{'auction':'09:27:00','intraday':'10:03:00'}.get(phase,'18:00:00'),
                "steps": [{'name':'fixture_required_step','required':True,'status':'completed'}],
                "scope": "transitional_market_collection_only",
                "collector_contract_sha256": "a" * 64,
            }
        ),
        encoding="utf-8",
    )


def test_two_strict_sessions_unlock_configured_observation_window(tmp_path,monkeypatch):
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
        from trade_system.v2.daily_session import seal
        sampling={'session':trade_date,'registered_at':trade_date+'T08:00:00+08:00','codes':['000001']}
        sampling['sampling_id']=identity(sampling)
        folder=workspace/'sampling'/sampling['sampling_id'];folder.mkdir(parents=True)
        (folder/'sampling.json').write_text(canonical(sampling),encoding='utf-8');seal(folder)
        observation={'as_of':trade_date+'T10:00:00+08:00','sampling':sampling,
            'rows':[{'instrument':'000001','state':'current_observation_not_executable','price':10,
                     'source_event_time':trade_date+'T09:59:00','received_at':trade_date+'T09:59:01'}]}
        observation['snapshot_id']=identity(observation)
        evidence=publish(workspace/'observation-publication',trade_date,
            {'observation.json':canonical(observation).encode()},generation=int(trade_date[-2:]))
        data = {'market':market,'execution_ready':False,'observation_evidence':evidence}
        data['report_id'] = identity(data)
        publish(workspace/'publication',trade_date,
                {'desk.json':canonical(data).encode(),'index.html':render(data).encode()},
                generation=int(trade_date[-2:]))

    result = audit_five_day_observation(
        db, reports, "2026-07-24", required_days=2, workspace=workspace, collector_contract_sha256='a'*64
    )

    assert result["ready_for_p1"] is True
    assert result["consecutive_passes"] == 2
    from trade_system.p0_observation import phase_evidence_errors
    assert 'auction_outside_window' in phase_evidence_errors({'phase':'auction',
        'started_at':'2026-07-24T09:15:00','completed_at':'2026-07-24T18:00:00',
        'steps':[{'name':'core','required':True,'status':'degraded'}]},'2026-07-24')
    assert 'required_step_not_complete:core' in phase_evidence_errors({'phase':'intraday',
        'started_at':'2026-07-24T10:00:00','completed_at':'2026-07-24T10:03:00',
        'steps':[{'name':'core','required':True,'status':'degraded'}]},'2026-07-24')
    assert 'dashboard' not in result['daily'][0]['checks']
    # Historical verification binds the original sealed template, not today's.
    from trade_system.v2 import research_product_view as view
    from trade_system.p0_observation import _publications
    with monkeypatch.context() as patch:
        patch.setattr(view,'render',lambda data:'new renderer contract')
        history,error=_publications(workspace,{'2026-07-23','2026-07-24'})
    assert error is None and all(row['passed'] for row in history.values())
    # Even an old-generation staged directory is not evidence of publication.
    staged=workspace/'publication/runs/unpublished';staged.mkdir()
    (staged/'manifest.json').write_text('{invalid unrelated history')
    history,error=_publications(workspace,{'2026-07-23','2026-07-24'})
    assert error is None and len(history)==2
    historical=workspace/'publication/runs/2026-07-23/index.html'
    saved=historical.read_bytes();historical.write_bytes(b'changed')
    history,error=_publications(workspace,{'2026-07-23','2026-07-24'})
    assert error is None and not history['2026-07-23']['passed'] and history['2026-07-24']['passed']
    historical.write_bytes(saved)

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

    # Recovery is a new linked receipt, not a rewrite of a failed close or
    # permission to recover missed auction/intraday stages after the fact.
    import hashlib
    _manifest(reports,'2026-07-24','close','completed_with_degradation')
    close_bytes=close_path.read_bytes();close=json.loads(close_bytes)
    _manifest(reports,'2026-07-24','supplemental','completed_with_warnings')
    recovery_path=reports/'runs/2026-07-24-supplemental/run.json'
    recovery=json.loads(recovery_path.read_text())
    recovery.update(started_at='2026-07-24T18:10:00',completed_at='2026-07-24T18:20:00',
        recovery_of={'phase':'close','run_id':close['run_id'],'completed_at':close['completed_at'],
                     'manifest_sha256':hashlib.sha256(close_bytes).hexdigest(),
                     'scope':'same_day_close_recovery_not_auction_or_intraday_replay'})
    recovery_path.write_text(json.dumps(recovery))
    recovered=audit_five_day_observation(db,reports,'2026-07-24',required_days=2,
                                         workspace=workspace,collector_contract_sha256='a'*64)
    assert recovered['consecutive_passes']==2
    assert recovered['daily'][-1]['phases']['close']['recovered_by_supplemental']
    assert recovered['daily'][-1]['phases']['close']['original_status']=='completed_with_degradation'
    assert close_path.read_bytes()==close_bytes
    recovery['recovery_of']['manifest_sha256']='b'*64
    recovery_path.write_text(json.dumps(recovery))
    invalid=audit_five_day_observation(db,reports,'2026-07-24',required_days=2,
                                      workspace=workspace,collector_contract_sha256='a'*64)
    assert not invalid['daily'][-1]['checks']['close_run']
    recovery['recovery_of']['manifest_sha256']=hashlib.sha256(close_bytes).hexdigest()
    recovery_path.write_text(json.dumps(recovery))

    _manifest(reports, "2026-07-24", "auction", "completed_with_degradation")
    result = audit_five_day_observation(
        db, reports, "2026-07-24", required_days=2, workspace=workspace, collector_contract_sha256='a'*64
    )
    assert result["ready_for_p1"] is False
    assert result["consecutive_passes"] == 0
