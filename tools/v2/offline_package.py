"""Standard-library bootstrap for a pinned offline observation recovery package.

No provider calls, scheduler writes, activation, or accounts. Requires a separately
recorded manifest digest; hashes prove binding, not publisher authenticity.
"""
from pathlib import Path, PurePosixPath
import argparse
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tools.v2.offline_contract import BUILD_SCRIPTS, validate_closure
from tools.v2.offline_process import run_stage

SCHEMA = 'stockdata_observation_offline_package_v2'
CANDIDATE_SCHEMA = 'stockdata_observation_offline_package_v3'
MAX_BYTES = 20 * 1024**3


def safe(path):
    path = Path(path).absolute()
    for p in (path, *path.parents):
        if p.is_symlink() or (p.exists() and getattr(p.lstat(), 'st_file_attributes', 0) & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0)):
            raise ValueError('linked package path refused')
    return path.resolve()


def member(root, name):
    if not isinstance(name, str) or not name or '\\' in name or ':' in name:
        raise ValueError('portable relative package path required')
    parts = name.split('/')
    if PurePosixPath(name).is_absolute() or any(p in ('', '.', '..') or p.endswith((' ', '.')) or Path(p).is_reserved() for p in parts):
        raise ValueError('unsafe package member')
    path = safe(root / name)
    if not path.is_relative_to(root):
        raise ValueError('package path escape')
    return path


def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(8*1024*1024), b''):
            h.update(block)
    return h.hexdigest()


def inventory(root):
    root = safe(root)
    result, size = {}, 0
    for p in sorted(root.rglob('*')):
        safe(p)
        if not p.is_file():
            continue
        name = p.relative_to(root).as_posix()
        if name == 'package.json':
            continue
        if (p.name.startswith('.env') and p.name != '.env.example') or p.suffix.lower() in ('.pem', '.pfx', '.key'):
            raise ValueError('credential material is not package input')
        size += p.stat().st_size
        if len(result) >= 100000 or size > MAX_BYTES:
            raise ValueError('offline package budget exceeded')
        result[name] = {'sha256': digest(p), 'bytes': p.stat().st_size}
    return result


def write_new(path, data):
    with path.open('x', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def candidate_materials(root, files):
    from tools.v2.offline_home import read, validate_targets
    required = ('RUN-HOME.ps1','COLLECT-ACCEPTANCE.ps1','HOME-STEPS.txt','RECOVERY-TARGETS.json',
                'source-export.json','rehearsal/bundles/initial/raw_facts/offline-bindings.json')
    if any(name not in files for name in required):
        raise ValueError('incomplete candidate home execution materials')
    validate_targets(read(root/'RECOVERY-TARGETS.json'))
    exported = read(root/'source-export.json')
    if exported.get('schema') != 'offline_source_export_v1' or not exported.get('files'):
        raise ValueError('explicit final source export required')
    actual = {name.removeprefix('code/'):record['sha256'] for name,record in files.items() if name.startswith('code/')}
    if actual != exported['files']:
        raise ValueError('candidate source export membership or bytes differ')
    prefix = 'rehearsal/bundles/initial/'
    if {name.removeprefix(prefix+'code/'):record['sha256'] for name,record in files.items()
            if name.startswith(prefix+'code/')} != actual:
        raise ValueError('bundle and entrypoint source exports differ')
    binding = read(root/(prefix+'raw_facts/offline-bindings.json'))
    if binding.get('schema') != 'offline_database_bindings_v1' or binding.get('execution_ready') is not False:
        raise ValueError('candidate database role contract required')
    roles = ('raw_archive_database','migrated_market_database','migration_receipt','synthetic_observation_receipt')
    for role in roles:
        record = binding[role]
        name = prefix+record['path']
        member(root,name)
        if name not in files or files[name]['sha256'] != record['sha256']:
            raise ValueError('candidate role identity differs')
    raw, market = (binding[name] for name in roles[:2])
    if raw['path'] == market['path'] or raw['sha256'] == market['sha256']:
        raise ValueError('distinct candidate database roles required')
    receipt = read(root/(prefix+binding['migration_receipt']['path']))
    qualification_sha = hashlib.sha256(json.dumps(receipt.get('qualification'),ensure_ascii=False,
        sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()
    if (receipt.get('schema')!='market_migration_rehearsal_v1' or receipt.get('status')!='passed'
            or receipt.get('execution_ready') is not False or receipt.get('production_cutover') is not False
            or receipt.get('source',{}).get('sha256')!=raw['sha256']
            or receipt.get('candidate',{}).get('sha256')!=market['sha256']
            or qualification_sha!=binding['qualification_sha256']):
        raise ValueError('candidate migration/qualification evidence differs')
    if not receipt.get('source_code_sql_sha256'):
        raise ValueError('candidate migration source closure required')
    for name,sha in receipt['source_code_sql_sha256'].items():
        if exported['files'].get(name)!=sha:
            raise ValueError('candidate migration source closure differs')
    return binding


def seal_package(root, *, base_sha, jsonpath_name, jsonpath_sha, catalog, candidate=False):
    root = safe(root)
    files = inventory(root)
    closure = validate_closure(root, files, base_sha=base_sha, jsonpath_name=jsonpath_name,
                               jsonpath_sha=jsonpath_sha, catalog=catalog, member=member, candidate=candidate)
    if candidate:
        candidate_materials(root, files)
    value = {'schema': CANDIDATE_SCHEMA if candidate else SCHEMA, 'scope': 'market_research_observation_v1', 'files': files,
             'base_sha256': base_sha, 'jsonpath_name': jsonpath_name, 'jsonpath_sha256': jsonpath_sha,
             'catalog': catalog, 'real_accounts': 'not_applicable_current_scope',
             'offsite_accepted': False, 'execution_ready': False, 'material_closure': closure}
    write_new(root / 'package.json', value)
    return digest(root / 'package.json')


def verify_package(root, expected_sha):
    root = safe(root)
    if not isinstance(expected_sha, str) or len(expected_sha) != 64 or digest(root / 'package.json') != expected_sha:
        raise ValueError('separately recorded package manifest SHA required')
    if (root / 'package.json').stat().st_size > 16000000:
        raise ValueError('manifest budget exceeded')
    value = json.loads((root / 'package.json').read_text(encoding='utf-8'))
    if value.get('schema') not in (SCHEMA,CANDIDATE_SCHEMA) or value.get('scope') != 'market_research_observation_v1' or value.get('execution_ready') is not False or value.get('offsite_accepted') is not False or value.get('real_accounts') != 'not_applicable_current_scope':
        raise ValueError('wrong offline scope')
    for name in value['files']:
        member(root, name)
    if inventory(root) != value['files']:
        raise ValueError('offline package membership or bytes differ')
    closure = validate_closure(root, value['files'], base_sha=value['base_sha256'],
                               jsonpath_name=value['jsonpath_name'], jsonpath_sha=value['jsonpath_sha256'],
                               catalog=value['catalog'], member=member, candidate=value['schema']==CANDIDATE_SCHEMA)
    if closure != value.get('material_closure'):
        raise ValueError('offline material closure differs')
    if value['schema']==CANDIDATE_SCHEMA:
        candidate_materials(root,value['files'])
    return value


def restore_package(root, expected_sha, destination, *, intake_started_at=None):
    from datetime import datetime, timezone
    technical_start = datetime.now(timezone.utc).isoformat()
    started = time.monotonic()
    root, target = safe(root), safe(destination)
    value = verify_package(root, expected_sha)
    if target.exists() or target.is_relative_to(root) or root.is_relative_to(target):
        raise ValueError('new separate offline output required')
    size = sum(v['bytes'] for v in value['files'].values())
    candidate = value['schema']==CANDIDATE_SCHEMA
    platform_evidence = None
    if candidate:
        from tools.v2.offline_home import platform_preflight, filesystem_probe, read, instant
        if intake_started_at and instant(intake_started_at)>instant(technical_start):
            raise ValueError('intake start cannot be after technical recovery')
        binding = candidate_materials(root,value['files'])
        raw_bytes = value['files']['rehearsal/bundles/initial/'+binding['raw_archive_database']['path']]['bytes']
        spill = min(16*1024**3,max(256*1024**2,raw_bytes*4))
        platform_evidence = platform_preflight(target,size,verification_temp_bytes=spill)
    if shutil.disk_usage(target.parent).free < size * 2.5 + 5*1024**3:
        raise ValueError('insufficient offline recovery capacity')
    target.mkdir()
    logs = target / 'evidence'; logs.mkdir()
    if candidate:
        platform_evidence['filesystem_probe'] = filesystem_probe(target)
        write_new(logs/'platform.json',platform_evidence)
    builder = target / 'environment-builder'
    for name in value['files']:
        if name.startswith('code/') and (name.endswith('.lock') or name.removeprefix('code/') in BUILD_SCRIPTS):
            dest = builder / name.removeprefix('code/')
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(member(root, name), dest)
    env = {k:v for k,v in os.environ.items() if not k.startswith(('STOCKDATA_', 'KPL_', 'HITHINK_', 'TUSHARE_', 'XIAODEFA_', 'DEEPSEEK_', 'PYTHON', 'PIP_', 'UV_'))}
    temporary = target / 'temp'; temporary.mkdir()
    env.update(PYTHONDONTWRITEBYTECODE='1', PYTHONUTF8='1', PIP_NO_INDEX='1', PIP_DISABLE_PIP_VERSION_CHECK='1',
               TEMP=str(temporary), TMP=str(temporary), UV_OFFLINE='1')

    def run(name, args, timeout=900):
        return run_stage(name, args, directory=logs, env=env, cwd=target,
                         timeout=timeout, runner=subprocess.run)

    run('environment-rebuild', [root/'bootstrap/python.exe', '-I', '-B', builder/'tools/v2/prepare_development_runtime.py',
        '--base-archive',root/'environment/base.zip','--base-sha256',value['base_sha256'],
        '--wheelhouse',root/'environment/wheels','--jsonpath-wheel',root/'environment/wheels'/value['jsonpath_name'],
        '--jsonpath-sha256',value['jsonpath_sha256'],'--output',target/'environment-evidence'])
    python = builder / '.venv/python.exe'
    rehearsal = target / 'rehearsal'
    shutil.copytree(root / 'rehearsal', rehearsal)
    # Rebind only this fresh copy. Original archived configuration remains evidence.
    code = root/'code'
    run('recovery-imports', [python, '-I', '-B', '-c',
        "import sys;sys.path.insert(0,sys.argv[1]);"
        "from tools.v2 import isolated_capabilities,verify_offline_recovery;"
        "print('OFFLINE_RECOVERY_IMPORTS_PASS')", code])
    run('business-restore', [python,'-I','-B',code/'tools/v2/isolated_capabilities.py','--root',rehearsal,
                           'restore','--bundle','bundles/initial','--out','restores/offline'])
    check_command = [python,'-I','-B',code/'tools/v2/verify_offline_recovery.py',
                            '--root',rehearsal,'--restored','restores/offline','--catalog',value['catalog'],
                            '--out',logs/'business-verification.json']
    if candidate:
        check_command += ['--bindings','raw_facts/offline-bindings.json']
    run('capacity-recheck', check_command)
    verify_package(root, expected_sha)
    result = {'schema':'offline_package_local_recovery_v1','package_sha256':expected_sha,
              'destination':str(target),'python':str(python),'elapsed_seconds':time.monotonic()-started,
              'environment_rebuilt_offline':True,'recovery_imports_verified':True,
              'production_activated':False,'execution_ready':False,
              'offsite_accepted':False,'rpo_seconds':None,'rto_target_seconds':None,
              'real_accounts':'not_applicable_current_scope'}
    if candidate:
        from tools.v2.offline_home import ARTIFACTS
        artifacts = {name:digest(member(target,name)) for name in ARTIFACTS}
        result.update(technical_started_at=technical_start,
            technical_finished_at=datetime.now(timezone.utc).isoformat(),
            elapsed_seconds=time.monotonic()-started,
            intake_started_at=intake_started_at, platform=platform_evidence,
            targets=read(root/'RECOVERY-TARGETS.json'),
            evidence_artifacts=artifacts,
            rto_target_seconds=14400, rpo_target={'unit':'trading_session','maximum':1,'seconds_equivalent':None},
            human_confirmation='pending',independent_machine_status='pending_external_verification')
    write_new(logs/'completed.json',result)
    return result


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--package',required=True,type=Path)
    p.add_argument('--manifest-sha256',required=True)
    p.add_argument('--restore-to',type=Path)
    p.add_argument('--intake-started-at')
    args=p.parse_args()
    if args.restore_to:
        value=restore_package(args.package,args.manifest_sha256,args.restore_to,intake_started_at=args.intake_started_at)
    else:
        value=verify_package(args.package,args.manifest_sha256)
    print(json.dumps({k:v for k,v in value.items() if k!='files'},ensure_ascii=False))


if __name__=='__main__':main()
