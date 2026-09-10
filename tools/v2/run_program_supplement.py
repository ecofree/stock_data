"""Frozen local-cache supplement; no network, production writes or orders."""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from trade_system.v2.program_supplement import register, execute, verify


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('command',choices=['register','run','verify'])
    p.add_argument('--folder',required=True)
    p.add_argument('--parent')
    p.add_argument('--snapshot')
    p.add_argument('--recompute',action='store_true')
    a = p.parse_args()
    if a.command == 'register':
        if not a.parent or not a.snapshot:
            p.error('register requires parent and snapshot')
        result = register(a.parent,a.snapshot,a.folder)
    else:
        rows = execute(a.folder) if a.command == 'run' else verify(a.folder,recompute=a.recompute)
        result = {v:{'complete':r['complete_requested_period'],'fills':len(r['fills']),
            'return':r['portfolio_return'],'gaps':len(r['data_requests'])} for v,r in rows.items()}
    print(json.dumps(result))


if __name__ == '__main__':
    main()
