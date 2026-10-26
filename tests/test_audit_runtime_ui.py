import json
import shutil
import subprocess
from pathlib import Path

import duckdb
import pytest

from scripts import check_pipeline_heartbeat as checker
from trade_system import heartbeat
from trade_system.review_web import _page_html, _script_json


def test_failure_and_start_never_certify_completion(tmp_path,monkeypatch,capsys):
    monkeypatch.setattr(heartbeat,'STATE_DIR',tmp_path/'state')
    monkeypatch.setattr('sys.argv',['check','--name','fixture'])
    assert checker.main()==1
    heartbeat.write('fixture',{'event':'success','run_id':'old','trade_date':'2026-10-12'})
    prior=heartbeat.read('fixture')['last_success']
    assert checker.main()==0
    for event in ('start','failure'):
        heartbeat.write('fixture',{'event':event,'run_id':'new'})
        assert heartbeat.read('fixture')['last_success']==prior
        assert heartbeat.age_seconds('fixture')<10
        assert checker.main()==1
    assert 'FAILED' in capsys.readouterr().out
    paused=tmp_path/'paused.json'
    paused.write_text(json.dumps({'status':'paused'}))
    monkeypatch.setattr('sys.argv',['check','--name','fixture','--runtime-state',str(paused)])
    assert checker.main()==0
    assert 'PAUSED' in capsys.readouterr().out


def test_completion_calendar_and_staleness(tmp_path,monkeypatch,capsys):
    monkeypatch.setattr(heartbeat,'STATE_DIR',tmp_path/'state')
    db=tmp_path/'calendar.duckdb'
    with duckdb.connect(str(db)) as con:
        con.execute('CREATE TABLE tushare_trade_cal(cal_date DATE,exchange VARCHAR,is_open BOOLEAN)')
        con.execute("INSERT INTO tushare_trade_cal VALUES ('2026-10-12','SSE',true),('2026-10-12','SZSE',true),('2026-10-11','SSE',false),('2026-10-11','SZSE',false)")
    argv=['check','--name','fixture','--calendar-db',str(db),'--trade-date']
    monkeypatch.setattr('sys.argv',argv+['2026-10-11'])
    assert checker.main()==0
    assert 'SESSION_CLOSED' in capsys.readouterr().out
    monkeypatch.setattr('sys.argv',argv+['2026-10-13'])
    assert checker.main()==1
    heartbeat.write('fixture',{'event':'success','trade_date':'2026-10-11'})
    monkeypatch.setattr('sys.argv',argv+['2026-10-12'])
    assert checker.main()==1
    assert 'WRONG_SESSION' in capsys.readouterr().out
    path=heartbeat.write('fixture',{'event':'success','trade_date':'2026-10-12'})
    assert checker.main()==0
    payload=json.loads(path.read_text())
    payload['last_success']['epoch']-=100000
    path.write_text(json.dumps(payload))
    assert heartbeat.completion_status('fixture')=='STALE_SUCCESS'


@pytest.mark.parametrize('options',[[],['-Execute'],['-Execute','-IncludeMainDb','-Rclone']])
def test_retired_offsite_entrypoint_never_creates_destination(tmp_path,options):
    shell=shutil.which('powershell') or shutil.which('pwsh')
    if not shell:
        pytest.skip('PowerShell unavailable')
    root=Path(__file__).resolve().parents[1]
    destination=tmp_path/'destination'
    result=subprocess.run([shell,'-NoProfile','-File',str(root/'scripts/offsite_backup.ps1'),
        '-Destination',str(destination),*options],capture_output=True,text=True,timeout=30)
    assert result.returncode==2
    assert 'RETIRED' in result.stderr
    assert not destination.exists()


def test_script_json_roundtrip_and_full_html_boundary():
    marker='</ScRiPt><script>window.__audit_marker=1</script><!--&\u2028\u2029'
    assert json.loads(_script_json({'text':marker},ensure_ascii=False))=={'text':marker}
    assert '<' not in _script_json(marker,ensure_ascii=False)
    ctx={'theme_mainline':[{'code':'fixture','name':marker,'score':1}],
         'concept_limit_up':{'groups':[{'name':marker,'stocks':[]} ]}}
    html=_page_html(ctx,'2026-10-12','',{'dates':[marker]},[{'name':marker}],[{'name':marker}])
    assert '<script>window.__audit_marker' not in html
    assert '\\u003c' in html
    assert 'fetch(' not in html
