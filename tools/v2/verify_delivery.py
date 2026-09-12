"""Run a source-bound local regression and minimal installed-core probe.

Never accepts historical test XML as proof of current code. Output is an
immutable new evidence directory, not a production readiness certificate.
"""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
from datetime import datetime, timezone
import xml.etree.ElementTree as ET
import tomllib
import zipfile

ROOT=Path(__file__).resolve().parents[2]


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream,'sha256').hexdigest()


def source_files():
    files=subprocess.check_output(['git','ls-files','-z','--cached','--others','--exclude-standard'],cwd=ROOT).decode().split('\0')
    return {p:digest(ROOT/p) for p in sorted(set(files)) if p and (ROOT/p).is_file()
            and Path(p).suffix in ('.py','.ps1','.bat','.cmd','.toml','.sql','.lock','.in','.yml','.yaml')
            and not p.startswith(('docs/','reports/','tmp/','backups/','.workbuddy/'))}


def capture(output,installed_python,wheel,*,minimal_runtime=False):
    output.mkdir(parents=True,exist_ok=False)
    start=datetime.now(timezone.utc).isoformat()
    before=source_files()
    report={'scope':'local_code_and_installed_core_not_business_or_production_acceptance',
            'started_at':start,'base_commit':subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
            'branch':subprocess.check_output(['git','branch','--show-current'],cwd=ROOT,text=True).strip(),
            'worktree_is_dirty':bool(subprocess.check_output(['git','status','--porcelain','--untracked-files=no'],cwd=ROOT,text=True).strip()),
            'source_files':before,'source_sha256':hashlib.sha256(json.dumps(before,sort_keys=True).encode()).hexdigest(),
            'execution_ready':False,'production_cutover':False,'business_acceptance':'not_performed',
            'real_account_acceptance':'pending_user_has_no_export','checks':{}}
    with zipfile.ZipFile(wheel) as archive:
        expected={m.replace('.','/')+'.py' for m in tomllib.loads((ROOT/'pyproject.toml').read_text())['tool']['stock_data']['release']['modules']}
        actual={name for name in archive.namelist() if name.endswith('.py')}
        if actual!=expected or any(hashlib.sha256(archive.read(name)).hexdigest()!=before.get(name) for name in expected):
            raise ValueError('wheel module closure/content does not match current source; rebuild required')
    (output/'started.json').write_text(json.dumps(report,sort_keys=True),encoding='utf-8')
    commands={
        'lint':[sys.executable,'-m','ruff','check','--select','E9,F63,F7,F82,F401,F841','.'],
        'migrations':[sys.executable,'scripts/lint_migrations.py'],
        'writer_boundary':[sys.executable,'tools/v2/closure_readiness.py','--writer-gate'],
        'tests':[sys.executable,'-m','pytest','-o','addopts=','-q','-p','no:cacheprovider','--junitxml='+str(output/'tests.xml')],
        'installed_dependencies':[str(installed_python),'-m','pip','check'],
        'installed_core':[str(installed_python),'-I',str(ROOT/'tools/v2/probe_installed.py'),'--wheel',str(wheel)]+(['--minimal-runtime'] if minimal_runtime else [])}
    success=True
    for name,command in commands.items():
        try:
            result=subprocess.run(command,cwd=ROOT,text=True,encoding='utf-8',errors='replace',capture_output=True,timeout=900 if name=='tests' else 120)
            (output/(name+'.log')).write_text(result.stdout+'\n'+result.stderr,encoding='utf-8')
            report['checks'][name]={'returncode':result.returncode,'log_sha256':digest(output/(name+'.log'))}
            success=success and result.returncode==0
            print(json.dumps({'check':name,'returncode':result.returncode}),flush=True)
        except subprocess.TimeoutExpired:
            report['checks'][name]={'status':'timeout'}
            success=False
    if (output/'tests.xml').exists():
        suites=ET.parse(output/'tests.xml').getroot().iter('testsuite')
        totals={k:0 for k in ('tests','errors','failures','skipped')}
        for suite in suites:
            for key in totals:
                totals[key]+=int(suite.attrib.get(key,0))
        report['tests']=totals
        success=success and totals['tests']>0 and not totals['errors'] and not totals['failures']
    after=source_files()
    report['source_unchanged_during_checks']=before==after
    success=success and before==after
    # Preserve the exact distribution probed, not only an ignored local path.
    shutil.copyfile(wheel,output/wheel.name)
    report['wheel']={'file':wheel.name,'sha256':digest(wheel)}
    report['minimal_runtime_required']=minimal_runtime
    report['completed_at']=datetime.now(timezone.utc).isoformat()
    report['local_checks_passed']=success
    report['artifacts']={p.name:digest(p) for p in output.iterdir() if p.is_file()}
    (output/'verification.json').write_text(json.dumps(report,ensure_ascii=True,indent=2,sort_keys=True),encoding='utf-8')
    print(json.dumps({k:report[k] for k in ('local_checks_passed','source_sha256','source_unchanged_during_checks','checks','execution_ready')}))
    return success


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--installed-python',type=Path,required=True)
    parser.add_argument('--wheel',type=Path,required=True)
    parser.add_argument('--minimal-runtime',action='store_true')
    args=parser.parse_args()
    raise SystemExit(0 if capture(args.output.resolve(),args.installed_python.resolve(),args.wheel.resolve(),minimal_runtime=args.minimal_runtime) else 1)
