"""Archive exact public announcement PDFs and manually reviewed candidate fields."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from trade_system.v2.case_supplement import collect_supplement, verify_supplement
from trade_system.v2.gap_evidence import read_json


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    run = commands.add_parser('collect')
    run.add_argument('--plan', type=Path, required=True)
    run.add_argument('--output', type=Path, required=True)
    verify = commands.add_parser('verify')
    verify.add_argument('--folder', type=Path, required=True)
    args = parser.parse_args()
    result = (collect_supplement(read_json(args.plan)[0], args.output) if args.command == 'collect'
              else verify_supplement(args.folder))
    print(json.dumps({k: result[k] for k in ('scope','public_pdf_requests','classification_counts','execution_ready')}, ensure_ascii=False))
