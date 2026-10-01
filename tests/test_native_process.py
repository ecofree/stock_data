"""Exercise Windows PowerShell 5.1, the actual registered-task host."""
import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


@pytest.mark.skipif(shutil.which('powershell.exe') is None,reason='Windows task host unavailable')
@pytest.mark.parametrize('exit_code',[0,7])
def test_stderr_does_not_replace_native_exit_code(tmp_path,exit_code):
    helper=Path(__file__).resolve().parents[1]/'scripts/native_process.ps1'
    command=(f". '{helper}'; $r=Invoke-StockDataProcess -Executable '{sys.executable}' "
             f"-Arguments @('-c','import sys; print(\"stdout\"); print(\"information on stderr\",file=sys.stderr); sys.exit({exit_code})') "
             f"-WorkingDirectory '{tmp_path}'; $r | ConvertTo-Json -Compress")
    result=subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-Command',command],
                          capture_output=True,text=True,timeout=20)
    assert result.returncode==0, result.stderr
    payload=json.loads(result.stdout)
    assert payload['ExitCode']==exit_code
    assert payload['Stdout'].strip()=='stdout'
    assert 'information on stderr' in payload['Stderr']


@pytest.mark.skipif(shutil.which('powershell.exe') is None,reason='Windows task host unavailable')
def test_native_deadline_keeps_child_alive_until_safe_exit_and_persists_state(tmp_path):
    helper=Path(__file__).resolve().parents[1]/'scripts/native_process.ps1'
    state=tmp_path/'process.json'
    command=(f". '{helper}'; $deadline=[DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()/1000.0+.15; "
             f"$r=Invoke-StockDataProcess -Executable '{sys.executable}' "
             "-Arguments @('-c','import time; time.sleep(.5); print(\"safe transaction completed\")') "
             f"-WorkingDirectory '{tmp_path}' -DeadlineEpoch $deadline -DrainGraceSeconds 0 "
             f"-PollMilliseconds 50 -ProgressPath '{state}'; $r | ConvertTo-Json -Compress")
    result=subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-Command',command],
                          capture_output=True,text=True,timeout=10)
    assert result.returncode==0,result.stderr
    payload=json.loads(result.stdout)
    assert payload['ExitCode']==-1 and payload['NativeExitCode']==0
    assert payload['DeadlineExceeded'] and payload['MaintenanceBlockedSeen'] and payload['DrainResolved']
    assert 'safe transaction completed' in payload['Stdout']
    progress=json.loads(state.read_text(encoding='utf-8'))
    assert progress['status']=='drained_after_deadline' and progress['child_exited']
    assert not progress['new_work_permitted'] and progress['exit_not_guaranteed']
    assert 'PROCESS_MAINTENANCE_BLOCKED' in result.stderr


@pytest.mark.skipif(shutil.which('powershell.exe') is None,reason='Windows task host unavailable')
def test_native_invalid_deadline_never_starts_child(tmp_path):
    helper=Path(__file__).resolve().parents[1]/'scripts/native_process.ps1'
    marker=tmp_path/'not-started'
    command=(f". '{helper}'; Invoke-StockDataProcess -Executable '{sys.executable}' "
             f"-Arguments @('-c','from pathlib import Path; Path(r\"{marker}\").touch()') "
             f"-WorkingDirectory '{tmp_path}' -DeadlineEpoch ([double]::NaN)")
    result=subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-Command',command],
                          capture_output=True,text=True,timeout=10)
    assert result.returncode!=0 and 'Finite cooperative' in result.stderr
    assert not marker.exists()
