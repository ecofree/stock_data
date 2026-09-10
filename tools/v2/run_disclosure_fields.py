"""Archive and verify bounded manual field transcriptions; no account writes."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from trade_system.v2.disclosure_fields import build_fields, verify_fields
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
    result = build_fields(read_json(args.plan)[0],args.output) if args.command == 'build' else verify_fields(args.folder)
    print(json.dumps({k:result[k] for k in ('case_count','potential_windows','new_documents',
        'document_kind_counts','closed_halt_candidates','mapped_fields_pending_review','cases_with_conflicts',
        'historically_available_new_documents','emitted_parent_events','execution_ready')},ensure_ascii=False))
