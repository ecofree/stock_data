"""Preregistered full-variant runs, dependency-bound inputs and evidence sealing."""
from pathlib import Path
import subprocess
import sys
import tempfile
import shutil

from .daily_case_run import legacy_snapshot
from .domain import identity,now_utc,utc
from .gap_evidence import read_json,write_json
from .portfolio_replay import POLICY,SCOPE,replay_variant
from .rolling_research import file_hash


def implementation():
    names = ('research_program.py','portfolio_replay.py','daily_account.py','daily_case_run.py',
        'integrated_account.py','entitlements.py','domain.py','paper_ledger.py','portfolio_diagnostic.py',
        'gap_audit.py','historical_adapter.py','rolling_research.py','gap_evidence.py')
    return {n:file_hash(Path(__file__).with_name(n)) for n in names}


def source_inputs(plan):
    if set(plan) != {'scope','policy','parent','legacy_wheel','legacy_wheel_sha256','data_python'} or plan['scope'] != SCOPE or plan['policy'] != POLICY:
        raise ValueError('strict registered full-variant research plan required')
    # Data verification can remain on the registered data environment, separate
    # from the lean service environment. No package installation or gate bypass.
    data_python = Path(plan['data_python']).resolve()
    if data_python != Path(sys.executable).resolve():
        script = ('import json,sys; sys.path.insert(0,sys.argv[1]); from trade_system.v2.research_program import source_inputs; '
                  'p=json.loads(input()); print(json.dumps(source_inputs(p)))')
        with tempfile.TemporaryDirectory(prefix='stock-data-reader-') as temporary:
            shutil.copytree(Path(__file__).resolve().parents[1],Path(temporary)/'trade_system',
                            ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
            proc = subprocess.run([str(data_python),'-I','-c',script,temporary],input=__import__('json').dumps(plan),
                capture_output=True,text=True,timeout=90,cwd=temporary)
        if proc.returncode:
            raise ValueError('registered data process failed: '+proc.stderr[-1000:])
        return __import__('json').loads(proc.stdout)
    payload = legacy_snapshot(plan)
    field_reg = read_json(Path(plan['parent'])/'registration.json')[0]
    adapter = Path(field_reg['plan']['adapter'])
    adapter_reg = read_json(adapter/'registration.json')[0]
    portfolio = Path(adapter_reg['plan']['portfolio'])
    payload['base_policy'] = read_json(portfolio/'registration.json')[0]['policy']
    payload['prior_actions'] = read_json(adapter/'action_candidates.json')[0]['rows']
    if legacy_snapshot(plan) != {k:payload[k] for k in ('fields','bundle','parent_sha256','adapter_sha256')}:
        raise ValueError('parent changed during supplemental input read')
    return payload


def register(plan, folder, *, clock=now_utc):
    folder = Path(folder).resolve()
    if folder.exists():
        raise FileExistsError('new research registration directory required')
    payload = source_inputs(plan)
    at = utc(clock()).isoformat()
    if utc(at) < utc(payload['fields']['repair_asof']):
        raise ValueError('registration precedes source knowledge')
    folder.mkdir(parents=True,exist_ok=False)
    write_json(folder/'inputs.json',payload)
    r = {'scope':SCOPE,'plan':plan,'registered_at':at,'input_id':identity(payload),
        'input_sha256':file_hash(folder/'inputs.json'),'implementation':implementation(),
        'variants':sorted(payload['bundle']['predictions']),
        'exposure_status':'previously_inspected_not_untouched','execution_ready':False}
    write_json(folder/'registration.json',{**r,'registration_id':identity(r)})
    return r


def load(folder, *, parents=True):
    folder = Path(folder)
    reg = read_json(folder/'registration.json')[0]
    rid = reg.pop('registration_id')
    if identity(reg) != rid or reg['implementation'] != implementation():
        raise ValueError('registered program or source changed')
    if file_hash(folder/'inputs.json') != reg['input_sha256']:
        raise ValueError('frozen inputs changed')
    data = read_json(folder/'inputs.json')[0]
    if identity(data) != reg['input_id'] or sorted(data['bundle']['predictions']) != reg['variants']:
        raise ValueError('full variant sample changed')
    if parents and source_inputs(reg['plan']) != data:
        raise ValueError('source parents changed')
    return reg,rid,data


def execute(folder):
    folder = Path(folder)
    reg,rid,p = load(folder)
    out = folder/'run'
    out.mkdir(exist_ok=False)
    write_json(out/'started.json',{'registration_id':rid})
    for name in reg['variants']:
        result = replay_variant(p['bundle'],p['base_policy'],reg['plan']['policy'],p['fields']['cases'],
            p['prior_actions'],name,repair_asof=reg['registered_at'])
        write_json(out/(name+'.json'),result)
    if load(folder)[1] != rid:
        raise ValueError('program changed during execution')
    manifest = {'registration_id':rid,'artifact_hashes':{x.name:file_hash(x) for x in out.iterdir()}}
    write_json(out/'completed.json',{**manifest,'manifest_id':identity(manifest)})
    return read_run(folder)


def read_run(folder, *, recompute=False):
    folder = Path(folder)
    reg,rid,p = load(folder)
    out = folder/'run'
    manifest = read_json(out/'completed.json')[0]
    mid = manifest.pop('manifest_id')
    if identity(manifest) != mid or rid != manifest['registration_id']:
        raise ValueError('program manifest changed')
    paths = list(out.iterdir())
    if any(x.is_symlink() or not x.is_file() for x in paths) or {x.name:file_hash(x) for x in paths if x.name != 'completed.json'} != manifest['artifact_hashes']:
        raise ValueError('program members changed')
    if set(manifest['artifact_hashes']) != {'started.json'} | {v+'.json' for v in reg['variants']}:
        raise ValueError('unexpected program member')
    results = {v:read_json(out/(v+'.json'))[0] for v in reg['variants']}
    for name,r in results.items():
        if r['full_input_id'] != identity(p['bundle']) or r['variant'] != name or r['execution_ready'] is not False:
            raise ValueError('result identity mismatch')
        if recompute and replay_variant(p['bundle'],p['base_policy'],reg['plan']['policy'],p['fields']['cases'],
                p['prior_actions'],name,repair_asof=reg['registered_at']) != r:
            raise ValueError('program replay not reproducible')
    return results
