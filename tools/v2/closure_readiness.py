"""Read-only closure/cutover dossier. Never imports an account or changes a task."""
import argparse
import ast
import json
from pathlib import Path
import subprocess
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from trade_system.v2.domain import canonical,file_hash,identity,now_utc
from trade_system.v2.research_product import read_prediction,saved_projection
from tools.v2.verify_delivery import source_files


def inventory(root):
    paths=subprocess.check_output(['git','ls-files','-z','--cached','--others','--exclude-standard'],cwd=root).decode().split('\0')
    sites=[];files=0
    for name in sorted(set(paths)):
        path=root/name
        if not name.endswith('.py') or name.startswith(('reports/','tmp/','data/','build/')) or not path.is_file():continue
        tree=ast.parse(path.read_text(encoding='utf-8-sig'));files+=1
        modules={'duckdb'};connects=set()
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
                sites.append({'file':name,'line':node.lineno,'kind':'readonly' if readonly else 'memory' if memory else 'potential_disk_writer','test':name.startswith('tests/'),'sha256':file_hash(path)})
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
    return {'status':report['status'],'writer_sites':sum(s['kind']=='potential_disk_writer' for s in inv['sites']),
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
