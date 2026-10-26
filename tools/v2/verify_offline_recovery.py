"""Verify a restored observation workspace and historical raw-table catalog."""
import argparse
import json
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from trade_system.v2 import recovery_bundle, capacity_acceptance, offline_report
from trade_system.v2.acceptance_scope import SCOPE, for_workspace
from trade_system.v2.publisher import read_current
from trade_system.v2.isolated_workspace import read_json, child


def verify(root, restored, catalog, output):
    root=Path(root).resolve();output=Path(output).resolve()
    if output.exists() or not output.parent.is_relative_to(root.parent):
        raise ValueError('new evidence output inside recovery destination required')
    proof=recovery_bundle.verified_restore(root,restored)
    workspace=root/restored/'workspace'
    if for_workspace(workspace)!=SCOPE:raise ValueError('observation workspace scope required')
    capacity=capacity_acceptance.verify_table(root,catalog,restored)
    inventory=json.loads((workspace/'capacity-inventory.json').read_text(encoding='utf-8'))
    manifest=read_json(child(root,catalog+'/completed.json'))
    matches=[name for name,record in proof['files'].items() if name.startswith('databases/') and record['sha256']==manifest['source_sha256']]
    if len(matches)!=1:raise ValueError('one explicit recovery database binding required')
    rebound={'acceptance_scope':SCOPE,'market_database':str(child(root,restored+'/'+matches[0])),
             'read_only':True,'activation_allowed':False}
    if capacity_acceptance.inventory(root,restored+'/'+matches[0])!=inventory:
        raise ValueError('restored product inventory differs')
    current,files=read_current(workspace/'publication')
    data=json.loads(files['desk.json'])
    if data.get('acceptance_scope')!=SCOPE or data.get('execution_ready') is not False:
        raise ValueError('restored publication scope differs')
    evidence={'acceptance_scope':SCOPE,'recovery':proof,'capacity':capacity,'inventory':inventory,
              'publication_run':current['run_id'],'execution_ready':False,'offsite_accepted':False,
              'real_accounts':'not_applicable_current_scope','rebound_readonly_configuration':rebound}
    page=offline_report.render(evidence)
    for name,content in ((output.name,json.dumps(evidence,ensure_ascii=False,indent=2)),
                         ('index.html',page),('workbench.html',files['index.html'].decode('utf-8'))):
        with (output.parent/name).open('x',encoding='utf-8') as f:f.write(content)
    return {'verified':True,'rows':capacity['rows'],'batches':capacity['batches'],'execution_ready':False,'offsite_accepted':False}


def main():
    p=argparse.ArgumentParser(description=__doc__)
    for name in ('root','restored','catalog','out'):p.add_argument('--'+name,required=True)
    a=p.parse_args();print(json.dumps(verify(a.root,a.restored,a.catalog,a.out)))


if __name__=='__main__':main()
