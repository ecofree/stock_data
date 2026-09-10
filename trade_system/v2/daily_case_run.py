"""Version-bound historical case registration, execution and replay verification.

Old evidence is verified using the retained old wheel, not silently rebound to
new source hashes. Extraction is temporary, offline, bounded and path-checked.
"""
from pathlib import Path
import subprocess
import sys
import tempfile
import zipfile

from .daily_account import ASSUMPTIONS, SCOPE, diagnose_case
from .domain import identity, now_utc, utc
from .gap_evidence import read_json, write_json
from .rolling_research import file_hash


def implementation():
    return {name:file_hash(Path(__file__).with_name(name)) for name in
            ('daily_account.py','daily_case_run.py','integrated_account.py','entitlements.py',
             'domain.py','paper_ledger.py','portfolio_diagnostic.py','gap_evidence.py','rolling_research.py')}


def legacy_snapshot(plan):
    wheel = Path(plan['legacy_wheel']).resolve()
    if file_hash(wheel) != plan['legacy_wheel_sha256']:
        raise ValueError('retained old verification wheel changed')
    # Isolated old module files retain their real __file__ for source hash checks.
    with tempfile.TemporaryDirectory(prefix='stock-legacy-read-') as task_tmp:
        root = Path(task_tmp).resolve()
        with zipfile.ZipFile(wheel) as archive:
            members = [m for m in archive.infolist() if m.filename.startswith('trade_system/') and not m.is_dir()]
            if not members or len(members) > 3000 or sum(m.file_size for m in members) > 100000000:
                raise ValueError('bounded retained wheel required')
            names = set()
            for member in members:
                target = (root/member.filename).resolve()
                if root not in target.parents or member.filename in names or '\\' in member.filename:
                    raise ValueError('unsafe or duplicate wheel path')
                names.add(member.filename)
                target.parent.mkdir(parents=True,exist_ok=True)
                with target.open('xb') as stream:
                    stream.write(archive.read(member))
        script = '''import sys,json
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from trade_system.v2.disclosure_fields import verify_fields
from trade_system.v2.gap_evidence import read_json,write_json
from trade_system.v2.rolling_research import file_hash
p=Path(sys.argv[2]).resolve()
r=verify_fields(p)
reg=read_json(p/'registration.json')[0]
a=Path(reg['plan']['adapter'])
write_json(Path(sys.argv[3]),{'fields':r,'bundle':read_json(a/'frozen_inputs.json')[0],
 'parent_sha256':file_hash(p/'completed.json'),'adapter_sha256':file_hash(a/'completed.json')})
'''
        result = subprocess.run([sys.executable,'-I','-c',script,str(root),str(Path(plan['parent']).resolve()),str(root/'result.json')],
            cwd=root,capture_output=True,text=True,timeout=60)
        if result.returncode:
            raise ValueError('retained-version parent verification failed: '+result.stderr[-1200:])
        payload = read_json(root/'result.json')[0]
    if file_hash(wheel) != plan['legacy_wheel_sha256']:
        raise ValueError('retained wheel changed during verification')
    return payload


def validate_plan(plan):
    if set(plan) != {'scope','parent','legacy_wheel','legacy_wheel_sha256','case_ids','assumptions','exposure_status'}:
        raise ValueError('strict historical case registration required')
    if plan['scope'] != SCOPE or plan['assumptions'] != ASSUMPTIONS or plan['exposure_status'] != 'previously_inspected_not_untouched':
        raise ValueError('explicit case hypothesis and exposure required')
    cases = plan['case_ids']
    if not isinstance(cases,list) or not 1 <= len(cases) <= 11 or len(set(cases)) != len(cases):
        raise ValueError('bounded unique frozen case ids required')
    if any(not isinstance(c,str) or len(c) != 64 or any(x not in '0123456789abcdef' for x in c) for c in cases):
        raise ValueError('canonical case ids required')
    digest = plan['legacy_wheel_sha256']
    if not isinstance(digest,str) or len(digest) != 64 or any(x not in '0123456789abcdef' for x in digest):
        raise ValueError('explicit retained verifier identity required')


def register_cases(plan, folder, *, clock=now_utc):
    validate_plan(plan)
    folder = Path(folder).resolve()
    if folder.exists():
        raise FileExistsError('new registered case experiment required')
    payload = legacy_snapshot(plan)
    available = {c['case_id'] for c in payload['fields']['cases']}
    if not set(plan['case_ids']) <= available:
        raise ValueError('case scope not in frozen investigation')
    created = utc(clock()).isoformat()
    if utc(created) < utc(payload['fields']['repair_asof']):
        raise ValueError('registration predates known source')
    folder.mkdir(parents=True,exist_ok=False)
    write_json(folder/'inputs.json',payload)
    reg = {'plan':plan,'registered_at':created,'input_id':identity(payload),
           'input_sha256':file_hash(folder/'inputs.json'),'implementation':implementation(),
           'knowledge_mode':'historical_repair_not_system_replay','execution_ready':False}
    write_json(folder/'registration.json',{**reg,'registration_id':identity(reg)})
    return reg


def _load(folder):
    folder = Path(folder).resolve()
    reg,_ = read_json(folder/'registration.json')
    rid = reg.pop('registration_id')
    validate_plan(reg['plan'])
    if identity(reg) != rid or reg['implementation'] != implementation():
        raise ValueError('case registration/source changed')
    if file_hash(folder/'inputs.json') != reg['input_sha256']:
        raise ValueError('case frozen input bytes changed')
    payload,_ = read_json(folder/'inputs.json')
    if identity(payload) != reg['input_id'] or legacy_snapshot(reg['plan']) != payload:
        raise ValueError('case parent verification or source binding changed')
    if utc(reg['registered_at']) < utc(payload['fields']['repair_asof']):
        raise ValueError('case knowledge chronology changed')
    return reg,rid,payload


def evaluate_cases(reg, payload):
    lookup = {c['case_id']:c for c in payload['fields']['cases']}
    return {key:diagnose_case(payload['bundle'],lookup[key],repair_asof=reg['registered_at'],
                              assumptions=reg['plan']['assumptions']) for key in reg['plan']['case_ids']}


def review(results):
    lines = ['# 历史日线病例假设诊断','',
        '各案声明初始持有100股，不是策略选股持仓。公告到账、容量和税额都是登记假设，不证明真实执行。','',
        '| 证券 | 终点 | 病例完成 | 留存股数 | 股份应收 | 现金应收(分) | 病例假设收益 |',
        '|---|---|---|---:|---:|---:|---|']
    for r in results.values():
        f = r['final']
        lines.append('| '+' | '.join(map(str,[r['instrument'],r['end'],r['case_hypothesis_complete'],
            sum(f['holdings'].values()),sum(f['share_receivable_quantity'].values()),
            f['cash_receivable_fen'],r['hypothetical_case_return']]))+' |')
    lines += ['', '不同病例的时间窗口不同，不作收益排序或选股优势比较。全部组合收益为null，真实执行关闭。',
        '停牌终点未知不延长成全天停牌证据；复牌有日期但无原始价时保留资产，不能假造退出。']
    return '\n'.join(lines)+'\n'


def run_cases(folder):
    folder = Path(folder).resolve()
    reg,rid,payload = _load(folder)
    out = folder/'run'
    out.mkdir(exist_ok=False)
    write_json(out/'started.json',{'registration_id':rid})
    results = evaluate_cases(reg,payload)
    for key,value in results.items():
        write_json(out/(key+'.json'),value)
    summary = {'scope':SCOPE,'case_count':len(results),
        'completed_case_hypotheses':sum(r['case_hypothesis_complete'] for r in results.values()),
        'incomplete_cases':[k for k,r in results.items() if not r['case_hypothesis_complete']],
        'parent_variants_preserved':payload['fields']['variant_counts'],
        'parent_windows_preserved':payload['fields']['potential_windows'],
        'evaluated_strategy_variants':0,'portfolio_return':None,'selection_advantage':None,'execution_ready':False}
    write_json(out/'summary.json',summary)
    (out/'review.md').write_text(review(results),encoding='utf-8')
    if _load(folder)[1:] != (rid,payload):
        raise ValueError('case inputs changed during run; output not sealed')
    manifest = {'registration_id':rid,'artifact_hashes':{p.name:file_hash(p) for p in out.iterdir()}}
    write_json(out/'completed.json',{**manifest,'manifest_id':identity(manifest)})
    return summary


def verify_cases(folder):
    folder = Path(folder).resolve()
    reg,rid,payload = _load(folder)
    out = folder/'run'
    manifest,_ = read_json(out/'completed.json')
    mid = manifest.pop('manifest_id')
    if identity(manifest) != mid or manifest['registration_id'] != rid:
        raise ValueError('case run seal changed')
    paths = list(out.iterdir())
    if any(not p.is_file() or p.is_symlink() for p in paths) or {p.name:file_hash(p) for p in paths if p.name != 'completed.json'} != manifest['artifact_hashes']:
        raise ValueError('case run members changed')
    results = evaluate_cases(reg,payload)
    expected_names = {'started.json','summary.json','review.md'} | {k+'.json' for k in results}
    if set(manifest['artifact_hashes']) != expected_names:
        raise ValueError('unexpected case run member')
    for key,value in results.items():
        if read_json(out/(key+'.json'))[0] != value:
            raise ValueError('case calculation not reproducible')
    s = read_json(out/'summary.json')[0]
    expected = {'scope':SCOPE,'case_count':len(results),
        'completed_case_hypotheses':sum(r['case_hypothesis_complete'] for r in results.values()),
        'incomplete_cases':[k for k,r in results.items() if not r['case_hypothesis_complete']],
        'parent_variants_preserved':payload['fields']['variant_counts'],
        'parent_windows_preserved':payload['fields']['potential_windows'],
        'evaluated_strategy_variants':0,'portfolio_return':None,'selection_advantage':None,'execution_ready':False}
    if s != expected or (out/'review.md').read_text(encoding='utf-8') != review(results) or read_json(out/'started.json')[0] != {'registration_id':rid}:
        raise ValueError('case summary not reproducible')
    return s
