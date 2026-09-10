"""Run/verify an isolated synthetic corporate-entitlement scenario (no real account)."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from trade_system.v2.entitlement_run import golden_scenario, run_scenario, verify_scenario
from trade_system.v2.gap_evidence import read_json


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    demo = commands.add_parser('run')
    demo.add_argument('--output', type=Path, required=True)
    demo.add_argument('--scenario', type=Path)
    verify = commands.add_parser('verify')
    verify.add_argument('--folder', type=Path, required=True)
    args = parser.parse_args()
    result = (verify_scenario(args.folder) if args.command == 'verify' else
              run_scenario(read_json(args.scenario)[0] if args.scenario else golden_scenario(), args.output))
    print(json.dumps(result['final'], ensure_ascii=False))
