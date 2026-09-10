"""Freeze verified OOS/raw inputs, then run a separate immutable portfolio diagnostic."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from trade_system.v2.portfolio_experiment import freeze_inputs, read_portfolio_result, run_portfolio


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    freeze = commands.add_parser('freeze')
    freeze.add_argument('--experiment', required=True, type=Path)
    freeze.add_argument('--snapshot', required=True, type=Path)
    freeze.add_argument('--policy', required=True, type=Path)
    freeze.add_argument('--output', required=True, type=Path)
    for name in ('run', 'verify'):
        sub = commands.add_parser(name)
        sub.add_argument('--folder', required=True, type=Path)
    args = parser.parse_args()
    if args.command == 'freeze':
        result = freeze_inputs(args.experiment, args.snapshot, json.loads(args.policy.read_text(encoding='utf-8')), args.output)
        print(json.dumps({k: result[k] for k in ('registration_id', 'snapshot_sha256', 'available_bars', 'requested_bar_identities')}))
    else:
        result = (run_portfolio if args.command == 'run' else read_portfolio_result)(args.folder)
        print(json.dumps({'registration_id': result['registration_id'],
            'common_full_period_comparison_available': result['common_full_period_comparison_available'],
            'accounts': {name: {**{k: row[k] for k in ('period_complete', 'hypothetical_return', 'blocker', 'fees_fen')},
                                'fills': len(row['fills']), 'open_positions': len(row['open_positions'])}
                         for name, row in result['accounts'].items()}}, ensure_ascii=False))


if __name__ == '__main__':
    main()
