"""Run with python -I outside checkout: installed package and real JSONL entry point."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile

import trade_system
from trade_system.v2.account_admission import inspect_account
from trade_system.v2.research_workbench import render
from trade_system.reports.real_data_backfill import build_real_data_backfill_status
from trade_system.reports.operator_report import SIGNAL_TABLES


def main():
    module = Path(trade_system.__file__).resolve()
    if 'site-packages' not in module.parts:
        raise RuntimeError('probe must use installed package, not checkout')
    assert callable(build_real_data_backfill_status) and 'kline' in SIGNAL_TABLES
    assert inspect_account()['status']=='missing_real_account'
    assert 'connect-src' in render({'probe':'synthetic'})
    with tempfile.TemporaryDirectory(prefix='v2-release-probe-') as folder:
        command = [sys.executable,'-I','-m','trade_system.v2','--db',str(Path(folder)/'paper.duckdb')]
        proc = subprocess.run(command,input=json.dumps({'command':'status'})+'\n',
            text=True,capture_output=True,cwd=folder,timeout=40)
        if proc.returncode:
            raise RuntimeError(proc.stderr[-1000:])
        response = json.loads(proc.stdout.strip())
        assert response['ok'] and response['result']['execution_ready'] is False
        missing = subprocess.run([sys.executable,'-I','-m','trade_system.v2.research_workbench_cli','check-account'],
            text=True,capture_output=True,cwd=folder,timeout=20)
        assert missing.returncode==0 and json.loads(missing.stdout)['reconciled'] is None
        desk = subprocess.run([sys.executable,'-I','-m','trade_system.v2.operator_workflow_cli','--help'],
            text=True,capture_output=True,cwd=folder,timeout=20)
        assert desk.returncode==0 and 'confirm' in desk.stdout
    print(json.dumps({'installed_package':str(module),'jsonl_status':True,'account_missing_fails_closed':True,
        'execution_ready':False,'scope':'synthetic_installed_entrypoint_not_live_acceptance'}))


if __name__=='__main__':
    main()
