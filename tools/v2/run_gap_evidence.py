"""Audit immutable portfolio gaps, import response evidence, adjudicate research cases."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from trade_system.v2.gap_audit import adjudicate_audit, audit_portfolio, read_package
from trade_system.v2.gap_evidence import ingest_response


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    audit = commands.add_parser('audit')
    audit.add_argument('--portfolio', required=True, type=Path)
    audit.add_argument('--snapshot', required=True, type=Path)
    audit.add_argument('--output', required=True, type=Path)
    ingest = commands.add_parser('import-response')
    ingest.add_argument('--receipt', required=True, type=Path)
    ingest.add_argument('--response', required=True, type=Path)
    ingest.add_argument('--output', required=True, type=Path)
    judge = commands.add_parser('adjudicate')
    judge.add_argument('--audit', required=True, type=Path)
    judge.add_argument('--evidence', action='append', type=Path, default=[])
    judge.add_argument('--asof')
    judge.add_argument('--mode', choices=['historical_repair', 'system_replay'], default='historical_repair')
    judge.add_argument('--output', required=True, type=Path)
    verify = commands.add_parser('verify')
    verify.add_argument('--folder', required=True, type=Path)
    args = parser.parse_args()
    if args.command == 'audit':
        result = audit_portfolio(args.portfolio, args.snapshot, args.output)
    elif args.command == 'import-response':
        result = ingest_response(args.receipt, args.response, args.output)
    elif args.command == 'adjudicate':
        result = adjudicate_audit(args.audit, args.evidence, args.output, asof=args.asof, mode=args.mode)
    else:
        result = read_package(args.folder)
    print(json.dumps({k: result[k] for k in ('scope', 'evidence_id', 'classification_counts',
                    'requested_bar_identities', 'audited_potential_windows', 'execution_ready', 'portfolio_resumed') if k in result}, ensure_ascii=False))


if __name__ == '__main__':
    main()
