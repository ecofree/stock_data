"""Explicit offline original-evidence admission. Never imports reviews into a database."""
from pathlib import Path
import argparse
import json
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from trade_system.valuation_intake import admit_intake, plan_intake, plan_sha256  # noqa: E402


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('plan', 'admit'))
    parser.add_argument('--runtime-root', type=Path, required=True)
    parser.add_argument('--stage', type=Path)
    parser.add_argument('--manifest-sha256')
    parser.add_argument('--plan', type=Path)
    parser.add_argument('--plan-sha256')
    parser.add_argument('--output', type=Path, required=True,
                        help='New plan or receipt file; existing evidence is never overwritten.')
    args = parser.parse_args(argv)
    if args.output.exists():
        parser.error('new output path required')
    if args.action == 'plan':
        if not args.stage or not args.manifest_sha256 or args.plan or args.plan_sha256:
            parser.error('plan requires only --stage and exact reviewed --manifest-sha256')
        result = plan_intake(args.stage, args.manifest_sha256, args.runtime_root)
        raw = (json.dumps(result, sort_keys=True, separators=(',', ':'), ensure_ascii=True) + '\n').encode()
        status = {'status': 'planned_not_admitted', 'plan_sha256': plan_sha256(result)}
    else:
        if not args.plan or not args.plan_sha256 or args.stage or args.manifest_sha256:
            parser.error('admit requires only exact --plan and --plan-sha256')
        if args.plan.stat().st_size > 2_000_000:
            parser.error('bounded plan required')
        result = admit_intake(json.loads(args.plan.read_text('utf-8')), args.plan_sha256, args.runtime_root)
        raw = (json.dumps(result, sort_keys=True, indent=2) + '\n').encode()
        status = {'status': result['status'], 'manifest_sha256': result['manifest_sha256']}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open('xb') as stream:
        stream.write(raw)
    print(json.dumps({**status, 'output': str(args.output), 'business_database_writes': 0,
                      'financial_qualification_granted': False, 'network_requests': 0}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
