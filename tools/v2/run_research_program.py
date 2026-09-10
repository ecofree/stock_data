"""Full-variant historical research register/run/verify, never orders."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from trade_system.v2.research_program import register,execute,read_run
from trade_system.v2.gap_evidence import read_json

if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('command',choices=['register','run','verify'])
    p.add_argument('--folder',type=Path,required=True)
    p.add_argument('--plan',type=Path)
    p.add_argument('--recompute',action='store_true')
    a = p.parse_args()
    if a.command == 'register':
        r = register(read_json(a.plan)[0],a.folder)
        print(json.dumps({k:r[k] for k in ('registered_at','variants','input_id')}))
    else:
        r = execute(a.folder) if a.command == 'run' else read_run(a.folder,recompute=a.recompute)
        print(json.dumps({k:{'complete':v['complete_requested_period'],'fills':len(v['fills']),
            'return':v['portfolio_return'],'gaps':len(v['data_requests']),'fatal':v['fatal']} for k,v in r.items()}))
