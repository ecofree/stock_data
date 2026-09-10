"""Register, run and verify historical case hypotheses, never strategy orders."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from trade_system.v2.daily_case_run import register_cases,run_cases,verify_cases
from trade_system.v2.gap_evidence import read_json


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command',required=True)
    register = sub.add_parser('register')
    register.add_argument('--plan',type=Path,required=True)
    register.add_argument('--folder',type=Path,required=True)
    for name in ('run','verify'):
        sub.add_parser(name).add_argument('--folder',type=Path,required=True)
    args = parser.parse_args()
    if args.command == 'register':
        result = register_cases(read_json(args.plan)[0],args.folder)
        result = {k:result[k] for k in ('registered_at','input_id','knowledge_mode','execution_ready')}
    else:
        result = run_cases(args.folder) if args.command == 'run' else verify_cases(args.folder)
    print(json.dumps(result,ensure_ascii=False))
