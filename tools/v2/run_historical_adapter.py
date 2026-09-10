"""Read-only frozen historical staging and evidence coverage, no account replay."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from trade_system.v2.historical_adapter import build_adapter,verify_adapter
from trade_system.v2.gap_evidence import read_json


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command',required=True)
    build = commands.add_parser('build')
    build.add_argument('--plan',type=Path,required=True)
    build.add_argument('--output',type=Path,required=True)
    verify = commands.add_parser('verify')
    verify.add_argument('--folder',type=Path,required=True)
    args = parser.parse_args()
    result = (build_adapter(read_json(args.plan)[0],args.output) if args.command == 'build' else verify_adapter(args.folder))
    keys = ('variant_counts','potential_windows','requested_raw_identities','retained_raw_bars','missing_raw_identities',
            'repair_classification_counts','replay_classification_counts','complete_action_term_candidates',
            'emitted_parent_events','historical_diagnostic_ready','execution_ready')
    print(json.dumps({k:result[k] for k in keys},ensure_ascii=False))
