"""Read-only closure/cutover dossier. Never imports an account or changes a task."""
import argparse
import ast
import hashlib
import json
from pathlib import Path
import subprocess
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from trade_system.v2.domain import canonical,file_hash,identity,now_utc
from trade_system.v2.research_product import read_prediction,saved_projection
from tools.v2.verify_delivery import source_files


# Full module bodies reviewed at 5ecfadd (owners unchanged since b6e37fe).
# Constants never derive from audited source at runtime; changes need review.
_WRITER_CONTRACTS = {
    'trade_system/v2/market_migration_rehearsal.py': {
        'namespace':'767667023c32591e88c860bca2e7555cc2b6b31114e4ee3699b229f05e44bb6b',
        'functions': {
            '_sha256':'66933e756d68f1366509835ca15d79a6ecf254ffa084a5ff8b9de02b7bd21946',
            '_literal':'d69fa131e908f0bba728915b491b9e098ff227d306ac4ad4ad0417a74a664dbb',
            '_no_links':'b8b2db42db5818ab8173d10a651e03c2863666dca104e21afa6c1d30495aefb7',
            '_validate_paths':'3f43f704ec62b617f55267dc0808cd52ac4d995b9cfaf7fb8ae28e59accdee44',
            '_source_read_guard':'719b63d007866579d392462d4dcc3aae008416a7388915c8da194bf398211123',
            '_fingerprints':'96cd011767c8ba6f6947f7e67e1347beeeaec4d814d395ce6d97a18aea34ebc7',
            'rehearse':'1502cad0de3c626fac2f63dec56747c5ec18b4680cceaf01a089dd9522f48571'},
        'symbols':('duckdb','Path','os','stat','re','hashlib','shutil','contextmanager','str','getattr','__file__')},
    'trade_system/v2/offline_binding.py': {
        'namespace':'6322461530a02667cbc693c40bc7d6dc819e85626409efeb62f681a04a891f1f',
        'functions':{'verify_normalized_definitions':'6dbfa35f314345a079768b7da8ea8db60bd0fa9a0fd82ccbc4caa7c3ed9fdc7d'},
        'symbols':('duckdb','Path','safe_path','migration','normalized_view_names',
                   'build_normalized_views_on_connection','shutil','uuid','str')},
    'trade_system/v2/isolated_workspace.py': {
        'namespace':'d2432d9409a935446ce8192027563203c6e716b3087e95e1cdda27da16b3a76a',
        'functions':{'safe_path':'16b5de9c1522c9ce2f2df984dfd806053620d204b69933a5c3eb139730e74d19'},
        'symbols':('Path','stat','getattr')},
    'trade_system/db_utils.py': {
        'namespace':'75e01aaf0c5ef86043a9529e56549051f9d8997c4ed0bd869d0e099b1c80b04f',
        'functions':{'_source_distribution_root':'76f300dffbba0510721ebc044d6928b505b5fed4b1703bdc411f5dd8c8397aa7',
                     'primary_checkout':'c8c8ebd1fefbbeede929676ed899ada11c87f11e8671f73039a49b83d0a17ed7'},
        'symbols':('__file__','str','getattr')},
}
_CLASSIFIED_WRITERS = ('run_local_operational_snapshot_writer',
    'isolated_historical_migration_writer','isolated_normalization_metadata_writer')


def _contract_form(value):
    if isinstance(value,ast.AST):
        # Empty type_params was added in 3.12; location attributes are never
        # fields. Nonempty new syntax remains part of the contract.
        return [type(value).__name__,sorted([[key,_contract_form(item)]
            for key,item in ast.iter_fields(value) if not (key=='type_params' and not item)],key=lambda pair:pair[0])]
    if isinstance(value,list):return [_contract_form(item) for item in value]
    return value


def _contract_digest(node):
    return hashlib.sha256(canonical(_contract_form(node)).encode()).hexdigest()


def _namespace_digest(tree):
    # Every helper body is included: a local alias can mutate a shared safety
    # dependency even when the exact writer and original guard remain intact.
    return hashlib.sha256(canonical([_contract_form(node) for node in tree.body]).encode()).hexdigest()


def _root_name(node):
    while isinstance(node,(ast.Attribute,ast.Subscript)):
        node=node.value
    return node.id if isinstance(node,ast.Name) else None


def _guard_contract_matches(tree,relative,functions):
    contract=_WRITER_CONTRACTS[relative]
    # Fixed full modules include imports, bindings, defaults and all bodies;
    # individual guard/call contracts below remain independently explicit.
    try:
        if _namespace_digest(tree)!=contract['namespace']:return False
        for name in functions:
            matches=[node for node in tree.body if isinstance(node,(ast.FunctionDef,ast.AsyncFunctionDef)) and node.name==name]
            if len(matches)!=1 or not isinstance(matches[0],ast.FunctionDef):return False
            if _contract_digest(matches[0])!=contract['functions'][name]:return False
    except (TypeError,ValueError):
        return False  # Unknown AST literals cannot receive an implicit exemption.
    protected=set(contract['symbols'])|set(contract['functions'])
    for node in ast.walk(tree):
        if isinstance(node,(ast.Global,ast.Nonlocal)) and protected.intersection(node.names):return False
        if isinstance(node,(ast.Attribute,ast.Subscript)) and isinstance(node.ctx,(ast.Store,ast.Del)):
            if _root_name(node) in protected:return False
        if isinstance(node,ast.Call) and isinstance(node.func,ast.Name) and node.func.id in ('setattr','delattr'):
            if node.args and _root_name(node.args[0]) in protected:return False
    return True


def classified_isolated_writer_calls(relative,tree,root):
    """Two fixed reviewed call contracts, never a module writer exemption."""
    migration='trade_system/v2/market_migration_rehearsal.py'
    metadata='trade_system/v2/offline_binding.py'
    if relative==migration:
        function='rehearse';call='duckdb.connect(str(candidate), config=config)'
        kind='isolated_historical_migration_writer';contract_id='historical_migration_rehearsal_v1'
        dependencies={migration:tuple(_WRITER_CONTRACTS[migration]['functions']),
                      'trade_system/db_utils.py':('_source_distribution_root','primary_checkout')}
    elif relative==metadata:
        function='verify_normalized_definitions';call='duckdb.connect(str(database),config=configuration)'
        kind='isolated_normalization_metadata_writer';contract_id='normalized_metadata_derivation_v1'
        dependencies={metadata:(function,),'trade_system/v2/isolated_workspace.py':('safe_path',),migration:('_literal',)}
    else:return {}
    for name,functions in dependencies.items():
        try:
            dependency=tree if name==relative else ast.parse((Path(root)/name).read_text('utf-8-sig'))
        except (OSError,ValueError,SyntaxError):return {}
        if not _guard_contract_matches(dependency,name,functions):return {}
    owner=next(node for node in tree.body if isinstance(node,ast.FunctionDef) and node.name==function)
    expected=ast.dump(ast.parse(call).body[0].value)
    matches=[node for node in ast.walk(owner) if isinstance(node,ast.Call) and ast.dump(node)==expected]
    if len(matches)!=1:return {}
    return {matches[0]:{'kind':kind,'function':function,'contract_id':contract_id,
        'contract_sha256':_WRITER_CONTRACTS[relative]['functions'][function]}}


def classified_snapshot_writer_calls(relative, tree):
    """Classify one reviewed run-local writer, not every writer in its module.

    This deliberately recognizes the current AST contract instead of attempting
    general Python data-flow or claiming an OS sandbox. Any changed construction
    remains unclassified until it is reviewed.
    """
    if relative != 'trade_system/pipeline_runtime.py':
        return set()
    functions=[node for node in tree.body
        if isinstance(node,(ast.FunctionDef,ast.AsyncFunctionDef))
        and node.name=='freeze_operational_inputs']
    if len(functions)!=1 or not isinstance(functions[0],ast.FunctionDef):
        return set()
    function=functions[0]
    if 'run_dir' not in {arg.arg for arg in function.args.posonlyargs+function.args.args}:
        return set()
    patterns=[
        'folder = Path(run_dir).resolve(strict=True)',
        "partial = folder / ('operational-inputs.' + uuid.uuid4().hex + '.partial.duckdb')",
        'output = duckdb.connect(str(partial))',
    ]
    matched=[]
    for text in patterns:
        expected=ast.dump(ast.parse(text).body[0])
        matches=[(index,node) for index,node in enumerate(function.body)
            if ast.dump(node)==expected]
        if len(matches)!=1:
            return set()
        matched.append(matches[0])
    if not matched[0][0] < matched[1][0] < matched[2][0]:
        return set()
    from collections import Counter
    protected={'folder','partial','run_dir'}
    bindings=Counter(node.id for node in ast.walk(function)
        if isinstance(node,ast.Name) and isinstance(node.ctx,(ast.Store,ast.Del))
        and node.id in protected)
    if bindings!=Counter({'folder':1,'partial':1}):
        return set()
    # Some binding forms use strings rather than Name(Store), e.g. except/import.
    arguments=set(function.args.posonlyargs+function.args.args+function.args.kwonlyargs)
    for node in ast.walk(function):
        names=[]
        if isinstance(node,(ast.FunctionDef,ast.AsyncFunctionDef,ast.ClassDef,ast.ExceptHandler)):
            names=[node.name]
        elif isinstance(node,ast.alias):names=[node.asname or node.name.split('.')[0]]
        elif isinstance(node,ast.arg) and node not in arguments:names=[node.arg]
        elif isinstance(node,(ast.Global,ast.Nonlocal)):names=node.names
        elif isinstance(node,(ast.MatchAs,ast.MatchStar)):names=[node.name]
        elif isinstance(node,ast.MatchMapping):names=[node.rest]
        if protected.intersection(names):
            return set()
    return {matched[2][1].value}


def inventory(root):
    paths=subprocess.check_output(['git','ls-files','-z','--cached','--others','--exclude-standard'],cwd=root).decode().split('\0')
    sites=[];files=0
    for name in sorted(set(paths)):
        path=root/name
        if not name.endswith('.py') or name.startswith(('reports/','tmp/','data/','build/')) or not path.is_file():continue
        tree=ast.parse(path.read_text(encoding='utf-8-sig'));files+=1
        modules={'duckdb'};connects=set()
        snapshot_calls=classified_snapshot_writer_calls(name,tree)
        isolated_calls=classified_isolated_writer_calls(name,tree,root)
        for node in ast.walk(tree):
            if isinstance(node,ast.Import):modules.update(a.asname or a.name for a in node.names if a.name=='duckdb')
            if isinstance(node,ast.ImportFrom) and node.module=='duckdb':connects.update(a.asname or a.name for a in node.names if a.name=='connect')
        for node in ast.walk(tree):
            if not isinstance(node,ast.Call):continue
            f=node.func
            direct=(isinstance(f,ast.Name) and f.id in connects) or (isinstance(f,ast.Attribute) and f.attr=='connect' and isinstance(f.value,ast.Name) and f.value.id in modules)
            if direct:
                readonly=any(k.arg=='read_only' and isinstance(k.value,ast.Constant) and k.value.value is True for k in node.keywords)
                memory=bool(node.args and isinstance(node.args[0],ast.Constant) and node.args[0].value==':memory:')
                kind=('readonly' if readonly else 'memory' if memory else
                    'run_local_operational_snapshot_writer' if node in snapshot_calls else
                    isolated_calls[node]['kind'] if node in isolated_calls else 'potential_disk_writer')
                site={'file':name,'line':node.lineno,'kind':kind,'test':name.startswith('tests/'),'sha256':file_hash(path)}
                if node in snapshot_calls:site['function']='freeze_operational_inputs'
                elif node in isolated_calls:site.update(isolated_calls[node])
                sites.append(site)
            elif isinstance(f,ast.Attribute) and f.attr in ('execute','executemany','sql') and node.args:
                statement=node.args[0]
                if not isinstance(statement,ast.Constant):
                    sites.append({'file':name,'line':node.lineno,'kind':'dynamic_sql_requires_connection_boundary','test':name.startswith('tests/'),'sha256':file_hash(path)})
    return {'python_files':files,'sites':sites,'scope':'includes_tools_tests_and_dynamic_sql_not_whole_program_or_OS_proof',
        'installed_legacy_disk_write_policy':'deny','all_authority_closed':False}


def writer_gate(root):
    from collections import Counter
    result=inventory(Path(root))
    actual=Counter(s['file'] for s in result['sites'] if not s['test'] and s['kind']=='potential_disk_writer')
    allowed={'trade_system/db_utils.py':1,'trade_system/v2/storage.py':1,'tools/v2/verify_consolidated.py':2}
    if any(name not in allowed or count>allowed[name] for name,count in actual.items()):
        raise ValueError('new unclassified direct writer: '+canonical(dict(actual)))
    return {'direct_writer_gate_passed':True,'non_test_sites':dict(actual),
        'classified_writer_sites':[s for s in result['sites'] if not s['test']
            and s['kind'] in _CLASSIFIED_WRITERS],
        'limitations':'static_direct_imports_only_not_dynamic_python_or_OS_sandbox'}


def build(root,workspace,output,tasks_path):
    root=Path(root).resolve();workspace=Path(workspace).resolve();output=Path(output).resolve()
    output.mkdir(parents=True,exist_ok=False)
    sources=source_files();fingerprint=identity(sources)
    tasks=json.loads(Path(tasks_path).read_text(encoding='utf-8-sig'))
    if not isinstance(tasks,list) or any(not t.get('Name') for t in tasks):raise ValueError('exact task snapshot required')
    inv=inventory(root);(output/'writer-inventory.json').write_text(canonical(inv),encoding='utf-8')
    p=read_prediction(workspace);view=saved_projection(workspace)
    queue=[{'prediction_id':r['prediction_id'],'prediction_date':r['prediction_date'],'status':r['status'],
        'remaining_requirement':'actual_exact_T_plus_1_open_and_T_plus_2_close_with_receipt_times'} for r in view.get('reviews',[]) if r['status']!='mature']
    dirty=bool(subprocess.check_output(['git','status','--porcelain','--untracked-files=no'],cwd=root).strip())
    contract=(root/'.ai-project/contract.yaml').exists()
    reasons=['full_legacy_consumer_semantics_not_closed','OS_identity_separation_not_deployed',
             'specific_task_change_approval_and_writer_window_required']
    if dirty:reasons.append('dirty_source_not_release_revision')
    if not contract:reasons.append('project_acceptance_contract_missing')
    if any(t.get('LastResult')!=0 for t in tasks if t['Name']!='StockData-MonthlyCompact'):
        reasons.append('existing_production_task_failures_unresolved')
    report={'status':'PARTIAL','evaluated_at':now_utc().isoformat(),'base_commit':subprocess.check_output(['git','rev-parse','HEAD'],cwd=root,text=True).strip(),
        'source_fingerprint':fingerprint,'configuration_sha256':file_hash(root/'config/research_delivery.json'),
        'task_snapshot_sha256':file_hash(tasks_path),'tasks':tasks,'prediction_id':p['prediction_id'] if p else None,
        'account_gate':'BLOCKED_NO_USER_FILE','human_gate':'BLOCKED_NO_NAMED_APPROVAL','future_reviews':queue,
        'execution_blockers':['account_snapshot_not_supplied','named_human_judgements_missing','execution_readiness_not_accepted'],
        'account_not_required_for_read_only_reporting_cutover':True,
        'cutover_blockers':reasons,'production_cutover':False,'execution_ready':False,
        'task_change_proposal':{
            'apply_enabled':False,'new_reporting_task':'StockData-ResearchDaily',
            'candidate_action':['powershell.exe','-NoProfile','-File',str(root/'scripts/run_research_daily.ps1')],
            'candidate_working_directory':str(root),'timing':'after_verified_market_ingestion_not_yet_scheduled',
            'preserve':['StockData-MonthlyCompact remains Disabled','all original XML triggers principals and actions'],
            'do_not_repoint_existing_collection_tasks':'research distribution does not replace full-market ingestion',
            'rollback':'restore exact backed-up task XML and prior verified release pointer; do not roll back user notes or raw receipts'}}
    (output/'readiness.json').write_text(canonical(report),encoding='utf-8')
    return {'status':report['status'],'writer_sites':sum(s['kind'] in ('potential_disk_writer',*_CLASSIFIED_WRITERS) for s in inv['sites']),
        'dynamic_sql_sites':sum(s['kind'].startswith('dynamic') for s in inv['sites']),
        'pending_review_groups':len(queue),'cutover_blockers':reasons,'production_cutover':False}


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--workspace');p.add_argument('--output');p.add_argument('--tasks');p.add_argument('--writer-gate',action='store_true')
    a=p.parse_args();root=Path(__file__).resolve().parents[2]
    if a.writer_gate:print(json.dumps(writer_gate(root)))
    else:
        if not all([a.workspace,a.output,a.tasks]):p.error('workspace, output and tasks required')
        print(json.dumps(build(root,a.workspace,a.output,a.tasks)))
