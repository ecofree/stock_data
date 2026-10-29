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


def verify(root, restored, catalog, output, *, bindings=None):
    root=Path(root).resolve();output=Path(output).resolve()
    if output.exists() or not output.parent.is_relative_to(root.parent):
        raise ValueError('new evidence output inside recovery destination required')
    proof=recovery_bundle.verified_restore(root,restored)
    workspace=root/restored/'workspace'
    if for_workspace(workspace)!=SCOPE:raise ValueError('observation workspace scope required')
    capacity=capacity_acceptance.verify_table(root,catalog,restored)
    inventory_path=workspace/'capacity-inventory.json'
    inventory=json.loads(inventory_path.read_text(encoding='utf-8')) if inventory_path.exists() or not bindings else None
    manifest=read_json(child(root,catalog+'/completed.json'))
    matches=[name for name,record in proof['files'].items() if name.startswith('databases/') and record['sha256']==manifest['source_sha256']]
    if len(matches)!=1:raise ValueError('one explicit recovery database binding required')
    rebound={'acceptance_scope':SCOPE,'market_database':str(child(root,restored+'/'+matches[0])),
             'read_only':True,'activation_allowed':False}
    actual_inventory=capacity_acceptance.inventory(root,restored+'/'+matches[0])
    if inventory is not None and actual_inventory!=inventory:
        raise ValueError('restored product inventory differs')
    inventory=actual_inventory
    current,files=read_current(workspace/'publication')
    data=json.loads(files['desk.json'])
    if data.get('acceptance_scope')!=SCOPE or data.get('execution_ready') is not False:
        raise ValueError('restored publication scope differs')
    role_evidence={}
    if bindings:
        from trade_system.v2.offline_binding import verify_bindings, historical_projection
        from trade_system.v2.research_product_view import render
        from trade_system.v2.isolated_workspace import write_json
        binding=read_json(child(root,restored+'/'+bindings))
        if binding['raw_archive_database']['path']!=matches[0]:
            raise ValueError('capacity catalog must bind the explicit raw archive role')
        role_evidence=verify_bindings(child(root,restored),binding)
        rebound=role_evidence['rebound_readonly_configuration']
        config=output.parent/'rebound-readonly-configuration.json'
        write_json(config,rebound)
        historical=historical_projection(config)
        write_json(output.parent/'historical-market.json',historical)
        banner='<p class="warning">历史存量行情；不是当前行情，资格缺口保留。合成观察演示另页展示。</p>'
        with (output.parent/'historical-market.html').open('x',encoding='utf-8') as stream:
            stream.write(render(historical).replace('<main>','<main>'+banner,1))
    evidence={'acceptance_scope':SCOPE,'recovery':proof,'capacity':capacity,'inventory':inventory,
              'publication_run':current['run_id'],'execution_ready':False,'offsite_accepted':False,
              'real_accounts':'not_applicable_current_scope','rebound_readonly_configuration':rebound,
              'database_roles':role_evidence}
    page=offline_report.render(evidence)
    workbench=files['index.html'].decode('utf-8')
    if bindings:
        page=page.replace('<a href="workbench.html">','<a href="historical-market.html">查看迁移库生成的历史存量页</a> · <a href="workbench.html">')
        workbench=workbench.replace('<main>','<main><p class="warning">SYNTHETIC 合成观察闭环演示；价格、署名和判断不是真实行情或用户观点。</p>',1)
    for name,content in ((output.name,json.dumps(evidence,ensure_ascii=False,indent=2)),
                         ('index.html',page),('workbench.html',workbench)):
        with (output.parent/name).open('x',encoding='utf-8') as f:f.write(content)
    return {'verified':True,'rows':capacity['rows'],'batches':capacity['batches'],'execution_ready':False,'offsite_accepted':False}


def main():
    p=argparse.ArgumentParser(description=__doc__)
    for name in ('root','restored','catalog','out'):p.add_argument('--'+name,required=True)
    p.add_argument('--bindings')
    a=p.parse_args();print(json.dumps(verify(a.root,a.restored,a.catalog,a.out,bindings=a.bindings)))


if __name__=='__main__':main()
