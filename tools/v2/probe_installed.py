"""Run with python -I outside checkout: installed package and real JSONL entry point."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile

import trade_system
from importlib.util import find_spec


def paper_restart_probe(folder):
    """Installed operational core only; explicit synthetic ledger, no providers."""
    from trade_system.v2.service import Service
    from trade_system.v2.domain import utc
    from zoneinfo import ZoneInfo
    path=Path(folder)/'restart.duckdb'
    with Service(path) as service:
        at=utc(service.clock()).isoformat()
        day=utc(at).astimezone(ZoneInfo('Asia/Shanghai')).date().isoformat()
        config={'account_id':'installed-fixture','mode':'paper','trading_days':[day],
                'opened_at':at,'initial_cash_fen':100000,'initial_lots':[],
                'quote_ttl_seconds':30,'mark_ttl_seconds':60,'account_ttl_seconds':60,
                'fees':{'version':'synthetic','effective_from':day,'effective_to':day,
                        'commission_bps':'0','minimum_commission_fen':0,'transfer_bps':'0','sell_tax_bps':'0'},
                'instruments':{'SZ.000002':{'version':'synthetic','effective_from':day,'effective_to':day,
                    'buy_lot':100,'sell_lot':100,'t_plus_sessions':1,'allow_odd_sell_all':True}}}
        service.submit('paper_open',config=config).result(10)
        changed=service.submit('paper_event',account_id='installed-fixture',event={
            'event_id':'deposit','kind':'cash_transfer','payload':{'amount_fen':100,'evidence_id':'synthetic'}}).result(10)
        assert changed['cash_fen']==100100
    with Service(path) as service:
        assert service.submit('paper_status',account_id='installed-fixture').result(10)==changed
    from trade_system.v2.recovery import backup,restore
    receipt=backup(path,Path(folder)/'backup')
    restored=restore(Path(folder)/'backup',Path(folder)/'restored')
    assert restored['catalog_hash']==receipt['catalog_hash']
    assert 'installed-fixture' in restored['paper_replay_hashes']
    from trade_system.v2.daily_workflow import run
    pending=run(Path(folder)/'preclose',clock=lambda:utc('2026-09-11T09:00:00+08:00'))
    assert pending['native_requests']==0 and not pending['daily_close_complete']


def main():
    module = Path(trade_system.__file__).resolve()
    if 'site-packages' not in module.parts:
        raise RuntimeError('probe must use installed package, not checkout')
    if '--wheel' in sys.argv:
        import zipfile
        with zipfile.ZipFile(sys.argv[sys.argv.index('--wheel')+1]) as archive:
            for name in archive.namelist():
                if name.endswith('.py'):
                    assert (module.parent.parent/name).read_bytes()==archive.read(name), 'installed/wheel mismatch: '+name
    for forbidden in ('trade_system.paper_execution','trade_system.daily_loop','trade_system.v2.rolling_research'):
        assert find_spec(forbidden) is None, 'forbidden installed module: '+forbidden
    if '--minimal-runtime' in sys.argv:
        for research_dependency in ('numpy','pandas','qlib','akshare','pytest'):
            assert find_spec(research_dependency) is None, 'unexpected runtime dependency: '+research_dependency
    with tempfile.TemporaryDirectory(prefix='v2-release-probe-') as folder:
        command = [sys.executable,'-I','-m','trade_system.v2','--db',str(Path(folder)/'paper.duckdb')]
        proc = subprocess.run(command,input=json.dumps({'command':'status'})+'\n',
            text=True,capture_output=True,cwd=folder,timeout=40)
        if proc.returncode:
            raise RuntimeError(proc.stderr[-1000:])
        response = json.loads(proc.stdout.strip())
        assert response['ok'] and response['result']['execution_ready'] is False
        desk = subprocess.run([sys.executable,'-I','-m','trade_system.v2.operator_workflow_cli','--help'],
            text=True,capture_output=True,cwd=folder,timeout=20)
        assert desk.returncode==0 and 'confirm' in desk.stdout
        daily = subprocess.run([sys.executable,'-I','-m','trade_system.v2.daily_session_cli','--help'],
            text=True,capture_output=True,cwd=folder,timeout=20)
        assert daily.returncode==0 and 'capture' in daily.stdout and 'judge' in daily.stdout
        paper_restart_probe(folder)
    print(json.dumps({'installed_package':str(module),'jsonl_status':True,'forbidden_modules_absent':True,
        'paper_event_and_restart':True,'offline_backup_restore':True,'preclose_no_network':True,
        'minimal_runtime':'--minimal-runtime' in sys.argv,
        'execution_ready':False,'scope':'synthetic_installed_entrypoint_not_live_acceptance'}))


if __name__=='__main__':
    main()
