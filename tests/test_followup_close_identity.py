"""Execute wrapper argument assembly, then its real helper and completion gate.

The only child is an isolated PowerShell wrapper with all external process
boundaries replaced. It cannot invoke collectors, backups, or production tasks.
"""
from pathlib import Path
import json
import os
import shutil
import subprocess
import sys

import duckdb
import pytest


@pytest.mark.skipif(shutil.which('powershell.exe') is None, reason='Windows wrapper host required')
@pytest.mark.parametrize('exit_code,calendar', [(0,'open'), (7,'open'), (0,'closed'), (0,'unverified')])
def test_wrapper_attempt_identity_reaches_helper_and_checker(tmp_path, monkeypatch, capsys, exit_code, calendar):
    from scripts import pipeline_notify, check_pipeline_heartbeat as checker
    from trade_system import heartbeat
    root = Path(__file__).resolve().parents[1]
    scripts = tmp_path / 'scripts'; scripts.mkdir()
    wrapper = scripts / 'run_stock_data_daily.ps1'
    wrapper.write_bytes((root / 'scripts/run_stock_data_daily.ps1').read_bytes())
    for name in ('run_integrated_daily.py','pipeline_notify.py'):
        (scripts/name).write_text('# process boundary replaced; never executed')
    db = tmp_path/'fixture.duckdb'; db.write_bytes(b'not a database; never opened')
    calls = tmp_path/'calls.jsonl'
    native = r'''
function Invoke-StockDataProcess {
 param($Executable,$Arguments,$WorkingDirectory,$DeadlineEpoch,$TimeoutSeconds,$ProgressPath)
 $step=if ($Arguments -contains '--dry-run') {'preflight'}
 elseif ($Arguments[0] -like '*pipeline_notify.py') {'notify'}
 elseif ($Arguments[0] -like '*run_integrated_daily.py') {'collection'}
 elseif (($Arguments -join ' ') -like '*trading_session_status*') {'calendar'}
 elseif (($Arguments -join ' ') -like '*observation_windows*') {'window'}
 elseif (($Arguments -join ' ') -like '*prepare_daily_backup*') {'prepare'}
 elseif (($Arguments -join ' ') -like '*finalize_daily_backup*') {'finalize'}
 elseif (($Arguments -join ' ') -like '*retain_qualified_backups*') {'retention'}
 else {throw 'Unexpected process boundary'}
 [IO.File]::AppendAllText('__CALLS__', (@{step=$step;arguments=@($Arguments)}|ConvertTo-Json -Compress)+[Environment]::NewLine, [Text.UTF8Encoding]::new($false))
 $body=switch ($step) {
  'calendar' {@{state='__CALENDAR__';reason='synthetic'}}
  'window' {@{deadline_at=[DateTimeOffset]::UtcNow.AddMinutes(10).ToString('o');start_latest_at=[DateTimeOffset]::UtcNow.AddMinutes(7).ToString('o')}}
  'prepare' {@{raw='fixture-raw'}}
  'finalize' {@{status='qualified';qualified_recovery_point=$true;archive='fixture-archive';cleanup_errors=@()}}
  default {@{status='fixture'}}
 }
 $exitCode=if ($step -eq 'collection') {__CODE__} else {0}
 [pscustomobject]@{ExitCode=$exitCode;Stdout=($body|ConvertTo-Json -Compress);Stderr=''}
}
'''.replace('__CALLS__',str(calls).replace("'","''")).replace('__CALENDAR__',calendar).replace('__CODE__',str(exit_code))
    (scripts/'native_process.ps1').write_text(native)
    monkeypatch.delenv('STOCKDATA_PHASE_DEADLINE_EPOCH', raising=False)
    command = ['powershell.exe','-NoProfile','-NonInteractive','-File',str(wrapper),'-Db',str(db),
               '-Python',sys.executable,'-TradeDate','2026-10-12','-CollectorContract','fixture',
               '-CollectorContractSha256','a'*64,'-ReportsDirectory',str(tmp_path/'reports')]
    result = subprocess.run(command, capture_output=True, timeout=30,
                            env={k:v for k,v in os.environ.items() if k.upper() != 'PSMODULEPATH'})
    assert (result.returncode == 0) == (not exit_code and calendar != 'unverified'), result.stderr
    records = [json.loads(line) for line in calls.read_text(encoding='utf-8').splitlines()]
    def argument(args, flag): return args[args.index(flag)+1]
    attempt = argument(records[0]['arguments'], '--run-id')
    assert argument(records[0]['arguments'],'--trade-date') == '2026-10-12'
    notices = [r['arguments'] for r in records if r['step']=='notify']
    if calendar == 'closed':
        assert not notices
        assert not any(r['step']=='collection' for r in records)
        return
    for args in notices:
        assert argument(args,'--run-id') == attempt
        assert argument(args,'--trade-date') == '2026-10-12'
    if calendar == 'open':
        collect = next(r['arguments'] for r in records if r['step']=='collection')
        prepare = next(r['arguments'] for r in records if r['step']=='prepare')
        assert argument(collect,'--run-id') == attempt
        assert prepare[4] == attempt+'_backup'
    monkeypatch.setattr(heartbeat,'STATE_DIR',tmp_path/'state')
    monkeypatch.setattr(pipeline_notify.notify,'send_text',lambda *a,**k: {})
    monkeypatch.delenv('KPL_NOTIFY_HEALTHCHECK_URL',raising=False)
    calendar_db=tmp_path/'calendar.duckdb'
    with duckdb.connect(str(calendar_db)) as con:
        con.execute('CREATE TABLE tushare_trade_cal(cal_date DATE,exchange VARCHAR,is_open BOOLEAN)')
        con.execute("INSERT INTO tushare_trade_cal VALUES ('2026-10-12','SSE',true),('2026-10-12','SZSE',true),('2026-10-11','SSE',false),('2026-10-11','SZSE',false)")
    for args in notices:
        monkeypatch.setattr(sys,'argv',args)
        assert pipeline_notify.main()==0
    expected = int(bool(exit_code or calendar=='unverified'))
    check = ['checker','--calendar-db',str(calendar_db),'--trade-date','2026-10-12','--run-id',attempt]
    monkeypatch.setattr(sys,'argv',check)
    assert checker.main() == expected
    payload=heartbeat.read('daily_close')
    assert payload['run_id']==attempt and payload['acceptance_verified'] is False
    assert payload['completion_scope']=='collection_only'
    if not expected:
        monkeypatch.setattr(sys,'argv',check[:-1]+[attempt+'_backup'])
        assert checker.main()==1
        assert 'WRONG_RUN' in capsys.readouterr().out
        heartbeat.write('daily_close',{'event':'success','run_id':attempt,'trade_date':'2026-10-09'})
        monkeypatch.setattr(sys,'argv',check)
        assert checker.main()==1
        assert 'WRONG_SESSION' in capsys.readouterr().out
    paused=tmp_path/'paused.json';paused.write_text('{"status":"paused"}')
    monkeypatch.setattr(sys,'argv',check+['--runtime-state',str(paused)])
    assert checker.main()==0
    assert 'PAUSED' in capsys.readouterr().out
    monkeypatch.setattr(sys,'argv',['checker','--calendar-db',str(calendar_db),'--trade-date','2026-10-11','--run-id',attempt])
    assert checker.main()==0
    assert 'SESSION_CLOSED' in capsys.readouterr().out
