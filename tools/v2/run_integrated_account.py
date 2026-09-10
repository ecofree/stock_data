"""Run/verify synthetic parent-account scenarios; never restore historical/live accounts."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from trade_system.v2.integrated_run import golden_scenarios,run_scenarios,verify_scenarios
from trade_system.v2.gap_evidence import read_json


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command',required=True)
    run = commands.add_parser('run')
    run.add_argument('--output',type=Path,required=True)
    run.add_argument('--scenarios',type=Path)
    verify = commands.add_parser('verify')
    verify.add_argument('--folder',type=Path,required=True)
    args = parser.parse_args()
    result = (verify_scenarios(args.folder) if args.command == 'verify' else
              run_scenarios(read_json(args.scenarios)[0] if args.scenarios else golden_scenarios(),args.output))
    print(json.dumps({name:{k:r['final'][k] for k in ('cash_fen','fresh_equity_fen',
        'cost_reconciliation_residual_fen','account_reconciliation_residual_fen','execution_ready')}
        for name,r in result.items()},ensure_ascii=False))
