from datetime import date
from pathlib import Path
import sys

import duckdb
import pytest

from scripts.run_integrated_daily import (
    _decode_process_bytes,
    _utf8_subprocess_env,
    command_plan,
    main,
)






def test_auction_plan_retains_blocked_diagnostics():
    steps = command_plan(
        "sample.duckdb",
        "2026-07-24",
        include_collection=True,
        phase="auction",
        collection_profile="priority",
    )
    by_name = {name: cmd for name, cmd, _ in steps}
    names = [name for name, _, _ in steps]

    assert 'generate_auction_stage_signals' not in by_name
    assert names.index('collect_realtime_limit_pool') < names.index('check_data_readiness')
    assert "generate_web_dashboard" not in by_name


def test_intraday_plan_collects_executable_quotes_before_signals():
    steps = command_plan(
        "sample.duckdb",
        "2026-07-30",
        include_collection=True,
        phase="intraday",
        collection_profile="priority",
        signal_limit=120,
    )
    names = [step[0] for step in steps]
    assert "collect_executable_quotes" in names
    assert "collect_l2_focus" in names
    assert names.index('collect_executable_quotes') < names.index('check_data_readiness')
    assert names.index('collect_l2_focus') < names.index('check_data_readiness')
    assert not {'generate_intraday_stage_signals','run_daily_operator_loop'} & set(names)
    by_name = {name: cmd for name, cmd, _ in steps}
    quote_cmd = by_name["collect_executable_quotes"]
    assert quote_cmd[quote_cmd.index("--limit") + 1] == "120"
    assert "--auto-boost-if-kpl-stale" in quote_cmd
    assert "--auto-boost-if-kpl-stale" in by_name["collect_l2_focus"]
    assert "generate_trading_terminal" not in by_name
    health = by_name['check_capital_flow_health']
    assert health[health.index('--stage') + 1] == 'intraday'
    assert 'reconcile_independent_stock_flow' not in by_name


def test_close_plan_reuses_intraday_l2_instead_of_fetching_after_hours():
    steps = command_plan(
        "sample.duckdb",
        "2026-07-31",
        include_collection=True,
        phase="close",
        collection_profile="priority",
    )
    names = [step[0] for step in steps]
    assert "collect_l2_focus" not in names
    assert "collect_executable_quotes" in names
    assert names.index("collect_ths_concepts_api") < names.index(
        "collect_intraday_sector_flow_full"
    )


def test_subprocess_text_protocol_is_utf8_and_never_injects_replacement_character(monkeypatch):
    import subprocess
    from scripts import run_integrated_daily as runner
    calls=[]
    class SlowWriter:
        pid=123
        returncode=0
        def communicate(self, timeout=None):
            calls.append(timeout)
            if len(calls) == 1:
                raise subprocess.TimeoutExpired('fixture',timeout)
            return b'committed',b''
        def kill(self):
            raise AssertionError('writer must drain without kill')
    monkeypatch.setattr(runner.subprocess,'Popen',lambda *a,**k:SlowWriter())
    drained=[]
    result=runner._run_writer(['fixture'],cwd='.',env={},timeout=.01,on_drain=drained.append)
    assert calls==[.01,5] and drained==[123] and result.returncode==-1
    assert result.stdout==b'committed'
    env = _utf8_subprocess_env()
    assert env["PYTHONUTF8"] == "1"
    assert env["PYTHONIOENCODING"] == "utf-8"
    text, broken = _decode_process_bytes("启动".encode("utf-8"), "stdout")
    assert (text, broken) == ("启动", False)
    text, broken = _decode_process_bytes("启动".encode("gbk"), "stdout")
    assert broken is True
    assert "�" not in text
    assert "stdout_encoding_error" in text


def test_stuck_writer_exposes_maintenance_before_safe_exit_and_keeps_guard(tmp_path, monkeypatch):
    import json
    import subprocess
    from scripts import run_integrated_daily as runner
    from trade_system import pipeline_runtime
    from trade_system.pipeline_runtime import PipelineLock, PipelineAlreadyRunning, RunManifest

    monkeypatch.setattr(pipeline_runtime, 'runtime_fingerprint', lambda: {'scope': 'fixture'})
    manifest = RunManifest(tmp_path/'reports', 'drain', '2026-10-01', 'close')
    db = tmp_path/'sample.duckdb'
    count = 0
    seen = []
    times = iter([0, 0, 10, 35, 40])
    monkeypatch.setattr(runner.time, 'monotonic', lambda: next(times, 40))

    class UninterruptibleWriter:
        pid = 321
        returncode = 0
        def communicate(self, timeout):
            nonlocal count
            count += 1
            # No poll, timeout, metadata mutation or callback may release the
            # owner's guard before this simulated transaction finally exits.
            with pytest.raises(PipelineAlreadyRunning):
                with PipelineLock(db, 'must-not-start'):
                    pass
            if count <= 3:
                raise subprocess.TimeoutExpired('fixture', timeout)
            return b'committed', b''
        def kill(self):
            raise AssertionError('no termination is authorized')
    monkeypatch.setattr(runner.subprocess, 'Popen', lambda *a, **k: UninterruptibleWriter())
    progress = runner._writer_progress(manifest, 'fixture', ['fixture'])
    def capture(state):
        progress(state)
        saved = json.loads(manifest.path.read_text())
        seen.append(saved['status'])
        assert not saved.get('completed_at')
        assert saved['writer_state']['guard_retained']
        assert not saved['writer_state']['new_work_permitted']
    with PipelineLock(db, 'owner'):
        result = runner._run_writer(['fixture'], cwd='.', env={}, timeout=.01,
            on_progress=capture, drain_grace_seconds=30, poll_seconds=.01)
        assert result.returncode == -1 and result.maintenance_seen
        assert manifest.data['maintenance_blocked_seen']
        assert seen == ['draining', 'draining', 'maintenance_blocked']
    assert Path(str(db)+'.pipeline.lock.guard').exists()
    with PipelineLock(db, 'after-safe-exit'):
        pass


def test_close_recovery_as_of_is_applied_to_all_freshness_gates():
    as_of = "2026-07-31T17:45:00"
    steps = command_plan(
        "sample.duckdb",
        "2026-07-31",
        include_collection=False,
        phase="close",
        as_of_time=as_of,
    )
    by_name = {name: command for name, command, _ in steps}
    for name in (
        "check_capital_flow_health",
        "check_data_readiness",
    ):
        command = by_name[name]
        assert command[command.index("--as-of") + 1] == as_of


def test_integrated_collection_exits_before_network_on_verified_holiday(
    tmp_path, monkeypatch, capsys
):
    db = tmp_path / "holiday.duckdb"
    con = duckdb.connect(str(db))
    con.execute(
        "CREATE TABLE tushare_trade_cal("
        "exchange VARCHAR, cal_date DATE, is_open BOOLEAN)"
    )
    con.execute(
        "INSERT INTO tushare_trade_cal VALUES ('SSE',?,false),('SZSE',?,false)",
        [date.today().isoformat(), date.today().isoformat()],
    )
    con.close()
    from tools.v2.backup_verify import backup_verify
    migration_root=tmp_path/'migration'
    verified=backup_verify(db,migration_root)
    db=verified['backup']
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_integrated_daily.py",
            "--migration-root",
            str(migration_root),
            "--reports-dir",
            str(migration_root/'reports'),
            "--db",
            str(db),
            "--trade-date",
            date.today().isoformat(),
            "--phase",
            "close",
        ],
    )
    assert main() == 0
    assert "MARKET_CLOSED" in capsys.readouterr().out


def test_manifest_upsert_step_transitions_running_to_completed(tmp_path):
    """A5: a step is recorded as 'running' before launch and updated in place to its
    final status, so a single manifest entry reflects progress (no duplicate rows) and
    a hung/killed step stays visible as 'running'."""
    import json
    from trade_system.pipeline_runtime import RunManifest

    m = RunManifest(str(tmp_path), "run-upsert-test", "2026-07-28", "intraday")
    m.upsert_step("step_a", "running", ["python", "a.py"], started_at="t0")
    data = json.loads(m.path.read_text(encoding="utf-8"))
    assert [s["status"] for s in data["steps"]] == ["running"]

    m.upsert_step("step_a", "completed", ["python", "a.py"], return_code=0, duration_seconds=1.5)
    data = json.loads(m.path.read_text(encoding="utf-8"))
    # Single entry transitioned to completed -- not a duplicate running + completed.
    assert len(data["steps"]) == 1
    assert data["steps"][0]["status"] == "completed"
    assert data["steps"][0]["return_code"] == 0
    assert data["steps"][0]["duration_seconds"] == 1.5


def test_manifest_finish_persists_informational_warnings(tmp_path, monkeypatch):
    import json
    from trade_system.pipeline_runtime import RunManifest

    m = RunManifest(str(tmp_path), "warning-test", "2026-07-28", "close")
    m.finish("completed", warnings=["operator_readiness_gate_not_passed"])
    data = json.loads(m.path.read_text(encoding="utf-8"))
    assert data["status"] == "completed"
    assert data["warnings"] == ["operator_readiness_gate_not_passed"]

    # Drive the actual runner and receipt state transitions. Only transport
    # children are replaced; data failures and cooldowns must remain failures.
    from types import SimpleNamespace
    from scripts import run_integrated_daily as runner
    from trade_system import collection_profiles, pipeline_runtime
    from tools.v2.backup_verify import backup_verify
    source=tmp_path/'source.duckdb'
    with duckdb.connect(str(source)) as con:
        con.execute('CREATE TABLE tushare_trade_cal(exchange VARCHAR,cal_date DATE,is_open BOOLEAN)')
        con.executemany('INSERT INTO tushare_trade_cal VALUES (?,?,true)',
                        [(x,date.today().isoformat()) for x in ('SSE','SZSE')])
    migration=tmp_path/'migration'
    verified=backup_verify(source,migration)
    fail_core=False;cooldown=False
    def child(command,**kwargs):
        code=2 if command[1]=='collectors/collect_market.py' or (
            fail_core and command[1]=='scripts/check_data_readiness.py') else 0
        return SimpleNamespace(returncode=code,stdout=b'',stderr=b'')
    monkeypatch.setattr(runner,'_run_writer',child)
    monkeypatch.setattr(pipeline_runtime,'runtime_fingerprint',lambda:{'scope':'test_fixture'})
    monkeypatch.setattr(collection_profiles,'task_due',lambda db,day,name,**k:
        (False,'retry cooldown age=0s ttl=150s status=error') if cooldown and name=='collect_intraday_stock_flow_market'
        else (False,'publication pending: fixture') if case=='pending' and name=='reconcile_independent_stock_flow'
        else (True,'fixture_due'))
    monkeypatch.setattr(collection_profiles, 'operational_readiness', lambda *a, **k:
        {'ready': True, 'scope': 'isolated_test_qualified_factual_inputs'})
    for case,expected in [('optional',0),('core',2),('cooldown',2),('pending',0)]:
        fail_core=case=='core';cooldown=case=='cooldown'
        monkeypatch.setattr(sys,'argv',['run_integrated_daily.py','--migration-root',str(migration),
            '--db',str(verified['backup']),'--reports-dir',str(migration/'reports'),
            '--phase','close' if case=='pending' else 'intraday','--run-id',case,'--trade-date',date.today().isoformat()])
        assert main()==expected
        receipt=json.loads((migration/'reports'/'runs'/case/'run.json').read_text())
        assert receipt['status']==('awaiting_publication' if case=='pending' else 'completed_with_warnings' if expected==0 else 'completed_with_degradation')
        assert receipt['pending']==(['reconcile_independent_stock_flow'] if case=='pending' else [])
        assert receipt['warnings']==['collect_market_context']
        if cooldown:
            assert next(x for x in receipt['steps'] if x['name']=='collect_intraday_stock_flow_market')['status']=='degraded'


@pytest.mark.parametrize('name,code,out,err,expected', [
    ('reconcile_independent_stock_flow', 1,
     'date=2026-09-29 status=incomparable primary=5571 reference=unqualified:0 overlap=0.00% corr=None sign=None\nout=report.md\n', '', True),
    ('reconcile_independent_stock_flow', 1, '', 'Traceback: bad database', False),
    ('reconcile_independent_stock_flow', 1, 'date=2026-09-28 status=incomparable primary=5571 reference=unqualified:0 overlap=0.00% corr=None sign=None', '', False),
    ('check_capital_flow_health', 2,
     'date=2026-09-29 source_ready=true data_certified_ready=true flow_certified_ready=false analysis_ready=false stock_ready=true sector_ready=true out=report.md', '', True),
    ('check_capital_flow_health', 2,
     'date=2026-09-29 data_certified_ready=false flow_certified_ready=false', '', False),
    ('check_capital_flow_health', -1, 'data_certified_ready=true flow_certified_ready=false', '', False),
    ('check_data_readiness', 2, 'data_certified_ready=false', '', False),
])
def test_only_explicit_financial_quality_results_are_separate_from_operation_failure(name, code, out, err, expected):
    from scripts.run_integrated_daily import _certification_gap
    assert _certification_gap(name, code, out, err, '2026-09-29') is expected


def test_runner_financial_gap_preserves_operational_completion_and_strict_failure(tmp_path, monkeypatch):
    import json
    from types import SimpleNamespace
    from scripts import run_integrated_daily as runner
    from trade_system import collection_profiles, pipeline_runtime
    from trade_system.p0_observation import phase_evidence_errors
    from tools.v2.backup_verify import backup_verify
    day = date.today().isoformat()
    source = tmp_path / 'source.duckdb'
    with duckdb.connect(str(source)) as con:
        con.execute('CREATE TABLE tushare_trade_cal(exchange VARCHAR,cal_date DATE,is_open BOOLEAN)')
        con.executemany('INSERT INTO tushare_trade_cal VALUES (?,?,true)', [(x, day) for x in ('SSE', 'SZSE')])
    migration = tmp_path / 'migration'
    verified = backup_verify(source, migration)
    commands = []
    def child(command, **kwargs):
        commands.append(command)
        if command[1] == 'scripts/reconcile_independent_stock_flow.py':
            return SimpleNamespace(returncode=1, stdout=(f'date={day} status=incomparable primary=5571 '
                'reference=unqualified:0 overlap=0.00% corr=None sign=None\n').encode(), stderr=b'')
        if command[1] == 'scripts/check_capital_flow_health.py':
            return SimpleNamespace(returncode=2, stdout=(f'date={day} source_ready=true '
                'data_certified_ready=true flow_certified_ready=false\n').encode(), stderr=b'')
        return SimpleNamespace(returncode=0, stdout=b'', stderr=b'')
    monkeypatch.setattr(runner, '_run_writer', child)
    monkeypatch.setattr(pipeline_runtime, 'runtime_fingerprint', lambda: {'scope': 'test_fixture'})
    monkeypatch.setattr(collection_profiles, 'task_due', lambda *a, **k: (True, 'fixture_due'))
    monkeypatch.setattr(collection_profiles, 'operational_readiness', lambda *a, **k:
        {'ready': True, 'scope': 'isolated_test_qualified_factual_inputs'})
    monkeypatch.setattr(sys, 'argv', ['runner', '--migration-root', str(migration), '--db', str(verified['backup']),
        '--reports-dir', str(migration / 'reports'), '--phase', 'close', '--run-id', 'financial-gap', '--trade-date', day])
    assert main() == 0
    receipt = json.loads((migration / 'reports/runs/financial-gap/run.json').read_text())
    assert receipt['status'] == 'completed_with_certification_gaps'
    assert receipt['operational_completion']['completed'] is True
    assert receipt['strict_financial_certification']['passed'] is False
    assert {'reconcile_independent_stock_flow', 'check_capital_flow_health', 'strict_financial_certification'} <= set(receipt['certification_gaps'])
    assert 'required_step_not_complete:reconcile_independent_stock_flow' in phase_evidence_errors(receipt, day)
    local = next(step for step in receipt['steps'] if step['name'] == 'prepare_valuation_session')
    assert local['status'] == 'completed' and local['local_valuation_evidence']['market_requests'] == 0
    assert local['local_valuation_evidence']['valuation_complete'] is False
    assert Path(local['local_valuation_evidence']['report_path']).is_file()
    assert not any('--valuation-session' in command for command in commands)
    with duckdb.connect(str(verified['backup']), read_only=True) as con:
        assert con.execute("SELECT count(*) FROM multi_source_observation WHERE data_type='valuation_daily_session'").fetchone() == (1,)


@pytest.mark.parametrize('operational_ready', [False, True])
def test_data_gap_only_separates_when_actual_supported_inputs_qualify(operational_ready):
    from scripts.run_integrated_daily import _certification_gap
    normal = 'trade_date=2026-09-29 stage=close data_certified_ready=false missing=kline out=report.md'
    assert _certification_gap('check_data_readiness', 2, normal, '', '2026-09-29',
        {'ready': operational_ready}) is operational_ready
    assert not _certification_gap('check_data_readiness', 2, normal, 'Traceback', '2026-09-29',
        {'ready': operational_ready})


def test_close_tushare_sync_uses_gapfill_lookback():
    """P0#2: close keeps a lookback query but processes only the newest open day.

    Older gaps belong to the explicit history phase; the close budget first
    guarantees today's close snapshot.
    """
    steps = command_plan("sample.duckdb", "2026-07-28", include_collection=True, phase="close")
    by_name = {name: cmd for name, cmd, _ in steps}
    cmd = by_name["sync_tushare_close"]
    assert cmd[cmd.index("--max-days") + 1] == "1"
    assert cmd[cmd.index("--end-date") + 1] == "20260728"
    # The start date precedes the trade date (a lookback window, not a single day).
    assert cmd[cmd.index("--start-date") + 1] < "20260728"


def test_close_readiness_gate_is_tightened_to_2h():
    """Audit P2 #1: the close readiness/capital-flow acceptance window is 7200s (2h),
    not the loose 21600s (6h), so a degraded stale intraday snapshot is fail-closed;
    the close-decision SIGNAL evidence window stays at 6h (spans the trading day)."""
    from trade_system.collection_profiles import CLOSE_READINESS_MAX_AGE_SECONDS, command_plan

    assert CLOSE_READINESS_MAX_AGE_SECONDS == 7200
    steps = command_plan("sample.duckdb", "2026-07-28", include_collection=True, phase="close")
    by_name = {name: cmd for name, cmd, _ in steps}
    for gate in ("check_data_readiness", "check_capital_flow_health"):
        cmd = by_name[gate]
        assert cmd[cmd.index("--max-age-seconds") + 1] == "7200"
    assert 'generate_close_stage_signals' not in by_name



@pytest.mark.parametrize("flag", ["--report-only", "--render-only", "--include-research"])
def test_retired_cli_flags_fail_before_database_access(tmp_path, monkeypatch, flag):
    db = tmp_path / "must-not-create.duckdb"
    monkeypatch.setattr(sys, "argv", ["run_integrated_daily.py", "--db", str(db), flag])
    with pytest.raises(SystemExit) as failure:
        main()
    assert failure.value.code == 2 and not db.exists()


def test_all_collection_phases_keep_data_and_retire_user_publications():
    retired = {"generate_web_dashboard", "generate_daily_review", "generate_daily_review_web",
               "generate_trading_terminal", "run_strategy_scan", "run_stage_backtest",
               "run_qlib_daily", "build_flow_features", "build_ai_review_snapshot",
               "generate_operator_reports", "create_operator_outcome_template"}
    for phase in ("auction", "intraday", "close", "supplemental", "history"):
        names = {n for n, _, _ in command_plan("sample.duckdb", "2026-09-16", include_collection=True, phase=phase)}
        assert not names & retired
    with pytest.raises(ValueError, match="research retired"):
        command_plan("sample.duckdb", "2026-09-16", include_research=True)


def test_supplemental_recovers_close_facts_under_shared_plan():
    from trade_system.collection_profiles import task_due, resolve_phase
    from trade_system.source_authority import validate_production_plan
    steps=command_plan('sample.duckdb','2026-09-17',include_collection=True,phase='supplemental')
    names=[n for n,_,_ in steps]
    assert names==['collect_market_context','sync_tushare_close','prepare_valuation_session','collect_ths_concepts_api',
                   'collect_hithink_limit_pool_daily','collect_realtime_limit_pool',
                   'collect_intraday_stock_flow_market','collect_intraday_sector_flow_full',
                   'derive_market_context','collect_lhb_daily','collect_auction_market_daily',
                   'collect_index_kline_daily','collect_xiaodefa_critical','build_normalized_views',
                   'reconcile_independent_stock_flow','audit_multisource_readiness',
                   'check_capital_flow_health','check_data_readiness']
    validate_production_plan('supplemental',names)
    with pytest.raises(ValueError):
        validate_production_plan('supplemental',[n for n in names if n!='collect_intraday_stock_flow_market'])
    assert resolve_phase('supplemental')=='supplemental'
    for name in names:
        assert task_due('must-not-open.duckdb','2026-09-17',name,phase='supplemental')[0]
    health=next(cmd for name,cmd,_ in steps if name=='check_capital_flow_health')
    assert health[health.index('--stage')+1]=='close'


def test_collection_handover_binds_sources_runtime_and_exact_targets(tmp_path, monkeypatch):
    import hashlib
    import json
    import subprocess
    from trade_system.migration_boundary import collection_contract, verify_collection_contract
    source=tmp_path/'source';source.mkdir()
    (source/'scripts').mkdir()
    (source/'scripts/run_integrated_daily.py').write_text('from collectors import retained')
    adapters=source/'collectors';adapters.mkdir()
    (adapters/'__init__.py').write_text('')
    (adapters/'retained.py').write_text('THRESHOLD = 1')
    research=source/'research';research.mkdir()
    (research/'experiment.py').write_text('VERSION = 1')
    cache=source/'trade_system/.stock_cache/limiter.json';cache.parent.mkdir(parents=True)
    cache.write_text('{"attempts":1}')
    thresholds=source/'config/phase_thresholds.json';thresholds.parent.mkdir()
    thresholds.write_text('{"threshold":1}')
    db=tmp_path/'market.duckdb'
    with duckdb.connect(str(db)) as con:
        con.execute('CREATE TABLE tushare_trade_cal(cal_date DATE)')
        con.execute('CREATE TABLE v_kline_daily(trade_date DATE)')
    def runtime(*args,**kwargs):
        return subprocess.CompletedProcess(args,0,stdout='[[3,12,4],[["fixture","1"]]]')
    monkeypatch.setattr(subprocess,'run',runtime)
    output=tmp_path/'data-reports'
    manifest=collection_contract(source,db,output,sys.executable)
    path=tmp_path/'contract.json';path.write_text(json.dumps(manifest),encoding='utf-8')
    digest=hashlib.sha256(path.read_bytes()).hexdigest()
    assert verify_collection_contract(path,digest,db,output)==manifest
    cache.write_text('{"attempts":2}')
    assert verify_collection_contract(path,digest,db,output)==manifest
    assert 'trade_system/.stock_cache/limiter.json' not in manifest['files']
    (research/'experiment.py').write_text('VERSION = 2')
    assert verify_collection_contract(path,digest,db,output)==manifest
    assert 'collectors/retained.py' in manifest['files']
    assert 'research/experiment.py' not in manifest['files']
    (adapters/'retained.py').write_text('THRESHOLD = 2')
    with pytest.raises(ValueError,match='source/runtime changed'):
        verify_collection_contract(path,digest,db,output)
    (adapters/'retained.py').write_text('THRESHOLD = 1')
    thresholds.write_text('{"threshold":2}')
    with pytest.raises(ValueError,match='source/runtime changed'):
        verify_collection_contract(path,digest,db,output)
    thresholds.write_text('{"threshold":1}')
    with pytest.raises(ValueError,match='target changed'):
        verify_collection_contract(path,digest,db,tmp_path/'different')
    with pytest.raises(ValueError,match='supplied hash'):
        verify_collection_contract(path,'0'*64,db,output)
    (source/'scripts/run_integrated_daily.py').write_text('# changed fixture')
    with pytest.raises(ValueError,match='source/runtime changed'):
        verify_collection_contract(path,digest,db,output)
    with duckdb.connect(str(db)) as con:
        con.execute('CREATE TABLE account_snapshot(id INT)')
    with pytest.raises(ValueError,match='V2/account'):
        collection_contract(source,db,output,sys.executable)
