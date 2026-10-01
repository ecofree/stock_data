import json
import hashlib
from datetime import datetime, timedelta
from pathlib import Path

import duckdb
import pytest

from trade_system.p0_observation import audit_five_day_observation
from trade_system.pipeline_runtime import all_manifests, default_observation_policy, observation_windows
from trade_system.v2.domain import canonical, identity
from trade_system.v2.publisher import publish
from trade_system.v2.research_product_view import render


POLICY = default_observation_policy()
CONTRACT_BYTES = json.dumps({'observation_windows': POLICY}, sort_keys=True).encode()
CONTRACT_SHA = hashlib.sha256(CONTRACT_BYTES).hexdigest()


def _contract(reports):
    path = reports / 'accepted-collector.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_bytes(CONTRACT_BYTES)
    return path


def _manifest(reports: Path, trade_date: str, phase: str, status: str = "completed"):
    _contract(reports)
    windows = (observation_windows(POLICY, trade_date, phase) if phase != 'supplemental'
               else [{'start_at': trade_date + 'T17:30:00+08:00'}])
    for index, window in enumerate(windows):
        run_id = f'{trade_date}-{phase}' + (f'-{index}' if index else '')
        run_dir = reports / 'runs' / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        started = datetime.fromisoformat(window['start_at'])
        ended = started + timedelta(seconds={'auction': 45, 'intraday': 180}.get(phase, 1800))
        (run_dir / 'run.json').write_text(json.dumps({
            'run_id': run_id, 'trade_date': trade_date, 'phase': phase, 'status': status,
            'started_at': started.isoformat(), 'completed_at': ended.isoformat(),
            'steps': [{'name': 'fixture_required_step', 'required': True, 'status': 'completed'}],
            'scope': 'transitional_market_collection_only',
            'collector_contract_sha256': CONTRACT_SHA,
        }), encoding='utf-8')


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
        "CREATE TABLE intraday_sector_flow_taxonomy("
        "trade_date DATE,taxonomy VARCHAR,expected_rows INTEGER,fetched_rows INTEGER,"
        "coverage_pct DOUBLE,status VARCHAR)"
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
        "INSERT INTO intraday_sector_flow_taxonomy VALUES "
        "('2026-07-23','em_industry',496,496,100,'success'),"
        "('2026-07-23','ths_concept',374,374,100,'success'),"
        "('2026-07-24','em_industry',496,496,100,'success'),"
        "('2026-07-24','ths_concept',374,374,100,'success')"
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
        from trade_system.v2 import publisher
        monkeypatch.setattr(publisher, 'now_utc', lambda day=trade_date: datetime.fromisoformat(day+'T19:30:00+08:00'))
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
        db, reports, "2026-07-24", required_days=2, workspace=workspace,
        collector_contract_sha256=CONTRACT_SHA, collector_contract_path=_contract(reports)
    )

    assert result["ready_for_p1"] is True
    assert result["consecutive_passes"] == 2
    assert result['daily'][-1]['phases']['auction']['required_window_count'] == 5
    assert result['daily'][-1]['phases']['intraday']['required_window_count'] == 49
    undeclared = audit_five_day_observation(db, reports, '2026-07-24', required_days=2,
        workspace=workspace, collector_contract_sha256=CONTRACT_SHA)
    assert not undeclared['ready_for_p1'] and undeclared['observation_window_contract_error']
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
                                               workspace=workspace,collector_contract_sha256='b'*64,
                                               collector_contract_path=_contract(reports))
    assert not wrong_version['ready_for_p1']

    close_path = reports/'runs/2026-07-24-close/run.json'
    close = json.loads(close_path.read_text())
    close['completed_at'] = '2026-07-24T20:00:00+08:00'
    close_path.write_text(json.dumps(close))
    premature = audit_five_day_observation(db,reports,'2026-07-24',required_days=2,
                                           workspace=workspace,collector_contract_sha256=CONTRACT_SHA,
                                           collector_contract_path=_contract(reports))
    assert premature['daily'][-1]['publication']['error'] == 'publication_precedes_close_completion'
    _manifest(reports,'2026-07-24','close')

    page = workspace/'publication/runs/2026-07-24/index.html'
    original = page.read_bytes()
    page.write_bytes(b'<html>tampered</html>')
    corrupt = audit_five_day_observation(db,reports,'2026-07-24',required_days=2,
                                        workspace=workspace,collector_contract_sha256=CONTRACT_SHA,
                                        collector_contract_path=_contract(reports))
    assert not corrupt['ready_for_p1'] and not corrupt['daily'][-1]['checks']['publication']
    page.write_bytes(original)

    # Recovery is a new linked receipt, not a rewrite of a failed close or
    # permission to recover missed auction/intraday stages after the fact.
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
                                         workspace=workspace,collector_contract_sha256=CONTRACT_SHA,
                                         collector_contract_path=_contract(reports))
    assert recovered['consecutive_passes']==2
    assert recovered['daily'][-1]['phases']['close']['recovered_by_supplemental']
    assert recovered['daily'][-1]['phases']['close']['original_status']=='completed_with_degradation'
    assert close_path.read_bytes()==close_bytes
    recovery['recovery_of']['manifest_sha256']='b'*64
    recovery_path.write_text(json.dumps(recovery))
    invalid=audit_five_day_observation(db,reports,'2026-07-24',required_days=2,
                                      workspace=workspace,collector_contract_sha256=CONTRACT_SHA,
                                      collector_contract_path=_contract(reports))
    assert not invalid['daily'][-1]['checks']['close_run']
    recovery['recovery_of']['manifest_sha256']=hashlib.sha256(close_bytes).hexdigest()
    recovery_path.write_text(json.dumps(recovery))

    _manifest(reports, "2026-07-24", "auction", "completed_with_degradation")
    result = audit_five_day_observation(
        db, reports, "2026-07-24", required_days=2, workspace=workspace,
        collector_contract_sha256=CONTRACT_SHA, collector_contract_path=_contract(reports)
    )
    assert result["ready_for_p1"] is False
    assert result["consecutive_passes"] == 0


def _phase_check(reports, phase='intraday', publication=None):
    from trade_system.p0_observation import audit_phase_windows
    return audit_phase_windows(all_manifests(reports), '2026-09-29', phase,
                               CONTRACT_SHA, POLICY, publication)


def test_missing_window_and_latest_green_do_not_mask_failed_required_slot(tmp_path):
    _manifest(tmp_path, '2026-09-29', 'intraday')
    path = tmp_path / 'runs/2026-09-29-intraday/run.json'
    first = json.loads(path.read_text())
    first['status'] = 'completed_with_degradation'
    path.write_text(json.dumps(first))
    check = _phase_check(tmp_path)
    assert not check['passed'] and check['qualified_window_count'] == 48
    assert not check['windows'][0]['passed'] and check['windows'][-1]['passed']
    path.unlink()
    check = _phase_check(tmp_path)
    assert not check['passed'] and not check['windows'][0]['attempts']


@pytest.mark.parametrize('late_end,wrong_sha,renewed_deadline,passed', [
    ('09:33:00', False, False, True), ('09:35:00', False, False, False),
    ('09:33:00', True, False, False), ('09:33:00', False, True, False)])
def test_same_window_recovery_preserves_failure_and_original_budget(tmp_path, late_end, wrong_sha, renewed_deadline, passed):
    _manifest(tmp_path, '2026-09-29', 'intraday')
    path = tmp_path / 'runs/2026-09-29-intraday/run.json'
    first = json.loads(path.read_text())
    first.update(status='failed', completed_at='2026-09-29T09:30:40+08:00')
    path.write_text(json.dumps(first))
    recovery = dict(first, run_id='bounded-recovery', status='completed',
                    started_at='2026-09-29T09:31:00+08:00', completed_at='2026-09-29T'+late_end+'+08:00')
    if wrong_sha:
        recovery['collector_contract_sha256'] = 'b'*64
    if renewed_deadline:
        recovery['deadline_epoch'] = datetime.fromisoformat('2026-09-29T09:35:00+08:00').timestamp()
    folder = tmp_path / 'runs/bounded-recovery'
    folder.mkdir()
    (folder / 'run.json').write_text(json.dumps(recovery))
    check = _phase_check(tmp_path)
    assert check['passed'] is passed
    assert len(check['windows'][0]['attempts']) == 2
    assert check['windows'][0]['attempts'][0]['status'] == 'failed'
    assert check['windows'][0]['recovered_in_window'] is passed


def test_success_before_later_failure_and_late_success_without_original_attempt_stay_red(tmp_path):
    _manifest(tmp_path, '2026-09-29', 'intraday')
    first = json.loads((tmp_path / 'runs/2026-09-29-intraday/run.json').read_text())
    late = dict(first, run_id='later-failure', status='failed',
                started_at='2026-09-29T09:33:10+08:00', completed_at='2026-09-29T09:33:20+08:00')
    folder = tmp_path / 'runs/later-failure'
    folder.mkdir()
    (folder / 'run.json').write_text(json.dumps(late))
    assert not _phase_check(tmp_path)['passed']
    original = tmp_path / 'runs/2026-09-29-intraday/run.json'
    original.unlink()
    late['status'] = 'completed'
    (folder / 'run.json').write_text(json.dumps(late))
    check = _phase_check(tmp_path)
    assert not check['passed']
    assert 'late_start_without_same_window_attempt' in check['windows'][0]['attempts'][0]['evidence_errors']


def test_preparation_and_outside_window_receipts_cannot_replace_auction_slots(tmp_path):
    _manifest(tmp_path, '2026-09-29', 'auction')
    path = tmp_path / 'runs/2026-09-29-auction/run.json'
    prepare = json.loads(path.read_text())
    prepare.update(started_at='2026-09-29T08:50:00+08:00', completed_at='2026-09-29T09:27:00+08:00',
                   scope='pre_session_reference_preparation_only')
    path.write_text(json.dumps(prepare))
    check = _phase_check(tmp_path, 'auction')
    assert not check['passed'] and check['qualified_window_count'] == 4
    assert check['unassigned_attempts'][0]['run_id'] == prepare['run_id']
    assert not check['windows'][0]['attempts']


@pytest.mark.parametrize('defect', ['parent_missing', 'parent_hash', 'wrong_sha', 'cross_day', 'early_publication', 'naive_publication'])
def test_supplemental_cannot_create_missing_close_or_use_unbound_early_publication(tmp_path, defect):
    _manifest(tmp_path, '2026-09-29', 'close', 'failed')
    path = tmp_path / 'runs/2026-09-29-close/run.json'
    raw = path.read_bytes()
    parent = json.loads(raw)
    recovery = dict(parent, phase='supplemental', run_id='recovery', status='completed',
        started_at='2026-09-29T20:00:00+08:00', completed_at='2026-09-29T20:10:00+08:00',
        recovery_of={'phase':'close', 'run_id':parent['run_id'], 'completed_at':parent['completed_at'],
                     'manifest_sha256':hashlib.sha256(raw).hexdigest(),
                     'scope':'same_day_close_recovery_not_auction_or_intraday_replay'})
    publication = {'passed':True, 'as_of':'2026-09-29T20:20:00+08:00',
                   'published_at':'2026-09-29T20:21:00+08:00'}
    if defect == 'parent_missing':
        path.unlink()
    elif defect == 'parent_hash':
        recovery['recovery_of']['manifest_sha256'] = 'b'*64
    elif defect == 'wrong_sha':
        recovery['collector_contract_sha256'] = 'b'*64
    elif defect == 'cross_day':
        recovery['completed_at'] = '2026-09-30T00:10:00+08:00'
    elif defect == 'early_publication':
        publication['published_at'] = '2026-09-29T19:31:00+08:00'
    else:
        publication['published_at'] = '2026-09-29T20:21:00'
    folder = tmp_path / 'runs/recovery'
    folder.mkdir()
    (folder / 'run.json').write_text(json.dumps(recovery))
    check = _phase_check(tmp_path, 'close', publication)
    assert not check['passed'] and not check['recovered_by_supplemental']


def test_window_attempts_sort_by_actual_timezone_and_unverified_time_stays_red(tmp_path):
    _manifest(tmp_path, '2026-09-29', 'intraday')
    original = tmp_path / 'runs/2026-09-29-intraday/run.json'
    first = json.loads(original.read_text())
    first.update(status='failed', completed_at='2026-09-29T09:30:40+08:00')
    original.write_text(json.dumps(first))
    recovery = dict(first, run_id='utc-recovery', status='completed',
                    started_at='2026-09-29T01:31:00+00:00', completed_at='2026-09-29T01:33:00+00:00')
    folder = tmp_path / 'runs/utc-recovery'
    folder.mkdir()
    path = folder / 'run.json'
    path.write_text(json.dumps(recovery))
    assert _phase_check(tmp_path)['passed']
    recovery['started_at'] = None
    path.write_text(json.dumps(recovery))
    assert 'unassigned_phase_time_unverified' in _phase_check(tmp_path)['evidence_errors']


def test_meaningful_unknown_required_taxonomy_cannot_be_hidden_by_green_aggregate(tmp_path):
    from trade_system.p0_observation import _sector_status
    con = duckdb.connect(str(tmp_path / 'unknown-taxonomy.duckdb'))
    try:
        con.execute('CREATE TABLE intraday_sector_flow_batch(trade_date DATE,expected_rows INTEGER,'
                    'fetched_rows INTEGER,coverage_pct DOUBLE,status VARCHAR,updated_at TIMESTAMP)')
        con.execute("INSERT INTO intraday_sector_flow_batch VALUES "
                    "('2026-09-29',496,496,100,'partial','2026-09-29 15:00:00')")
        missing = _sector_status(con, '2026-09-29')
        assert not missing['passed']
        assert missing['reason'] == 'required_taxonomy_evidence_missing'
        con.execute('CREATE TABLE intraday_sector_flow_taxonomy(trade_date DATE,taxonomy VARCHAR,'
                    'expected_rows INTEGER,fetched_rows INTEGER,coverage_pct DOUBLE,status VARCHAR)')
        con.execute("INSERT INTO intraday_sector_flow_taxonomy VALUES "
                    "('2026-09-29','em_industry',496,496,100,'success'),"
                    "('2026-09-29','ths_concept',0,0,100,'missing')")
        unknown = _sector_status(con, '2026-09-29')
        assert not unknown['passed'] and unknown['coverage_pct'] == 100
        assert unknown['taxonomies']['em_industry']['passed'] is True
        ths = unknown['taxonomies']['ths_concept']
        assert ths['passed'] is False and ths['denominator_known'] is False
        assert ths['expected_rows'] is None and ths['coverage_pct'] is None
        # Old/misleading batch statuses must not manufacture the missing scope.
        con.execute("UPDATE intraday_sector_flow_batch SET status='success_with_optional_gap'")
        assert not _sector_status(con, '2026-09-29')['passed']
        con.execute("UPDATE intraday_sector_flow_taxonomy SET expected_rows=390,fetched_rows=390,"
                    "coverage_pct=100,status='success' WHERE taxonomy='ths_concept'")
        assert _sector_status(con, '2026-09-29')['passed']
        con.execute("UPDATE intraday_sector_flow_batch SET status='partial'")
        assert not _sector_status(con, '2026-09-29')['passed']
        con.execute("UPDATE intraday_sector_flow_batch SET status='success'")
        con.execute("DELETE FROM intraday_sector_flow_taxonomy WHERE taxonomy='ths_concept'")
        assert not _sector_status(con, '2026-09-29')['passed']
    finally:
        con.close()


def test_partial_stock_batch_keeps_available_facts_but_cannot_qualify_a_complete_day(tmp_path):
    from trade_system.p0_observation import _batch_status, GOOD_STOCK_BATCH
    con = duckdb.connect(str(tmp_path / 'partial-stock.duckdb'))
    try:
        con.execute('CREATE TABLE intraday_stock_flow_batch(trade_date DATE,expected_rows INTEGER,'
                    'fetched_rows INTEGER,coverage_pct DOUBLE,status VARCHAR,updated_at TIMESTAMP)')
        con.execute("INSERT INTO intraday_stock_flow_batch VALUES "
                    "('2026-09-29',5200,5174,99.5,'partial','2026-09-29 15:00:00')")
        partial = _batch_status(con, 'intraday_stock_flow_batch', '2026-09-29', GOOD_STOCK_BATCH)
        assert not partial['passed'] and partial['fetched'] == 5174 and partial['coverage_pct'] == 99.5
        con.execute('UPDATE intraday_stock_flow_batch SET fetched_rows=5200,coverage_pct=100')
        assert not _batch_status(con, 'intraday_stock_flow_batch', '2026-09-29', GOOD_STOCK_BATCH)['passed']
        for qualified_status in ('success', 'success_with_unavailable'):
            con.execute('UPDATE intraday_stock_flow_batch SET status=?', [qualified_status])
            assert _batch_status(con, 'intraday_stock_flow_batch', '2026-09-29', GOOD_STOCK_BATCH)['passed']
    finally:
        con.close()
