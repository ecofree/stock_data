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
