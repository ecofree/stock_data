"""Bounded exact-case native provider collection, followed by read-only verification."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from trade_system.v2.native_gap_run import collect, verify_collection


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    run = commands.add_parser('collect')
    run.add_argument('--audit', type=Path, required=True)
    run.add_argument('--output', type=Path, required=True)
    run.add_argument('--resume-from', type=Path)
    run.add_argument('--historical-source', type=Path)
    verify = commands.add_parser('verify')
    verify.add_argument('--folder', type=Path, required=True)
    verify.add_argument('--historical-source', type=Path)
    args = parser.parse_args()
    result = collect(args.audit, args.output, resume_from=args.resume_from, historical_source=args.historical_source) if args.command == 'collect' else verify_collection(args.folder, historical_source=args.historical_source)
    print(json.dumps({k: result[k] for k in ('registration_id', 'transport_attempts', 'status_counts',
                                           'evidence_count', 'execution_ready', 'portfolio_resumed')}))


if __name__ == '__main__':
    main()
