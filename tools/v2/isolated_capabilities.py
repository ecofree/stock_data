"""Explicit isolated C1/C2/C3 rehearsal. No production defaults or transport."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from trade_system.v2 import recovery_bundle, cold_archive, feedback_ledger, capability_report
from trade_system.v2.isolated_workspace import initialize, workspace, child, read_json, write_json


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', required=True, help='explicit new isolated workspace; never a production directory')
    sub = parser.add_subparsers(dest='operation', required=True)
    sub.add_parser('init')
    capture = sub.add_parser('capture')
    capture.add_argument('--plan', required=True)
    capture.add_argument('--out', required=True)
    restore = sub.add_parser('restore')
    restore.add_argument('--bundle', required=True)
    restore.add_argument('--out', required=True)
    archive = sub.add_parser('archive')
    joint = sub.add_parser('joint-read')
    coverage = sub.add_parser('plan-coverage')
    for command in (archive, joint, coverage):
        command.add_argument('--source', required=True)
        command.add_argument('--start', required=True)
        command.add_argument('--end', required=True)
    archive.add_argument('--restore', required=True)
    archive.add_argument('--damaged-receipt-sha256')
    archive.add_argument('--repair-reason')
    joint.add_argument('--partitions', nargs='*', default=[])
    joint.add_argument('--out', required=True)
    joint.add_argument('--coverage', required=True)
    coverage.add_argument('--restore', required=True)
    coverage.add_argument('--partitions', nargs='*', default=[])
    coverage.add_argument('--out', required=True)
    importer = sub.add_parser('import-feedback')
    importer.add_argument('--statement', required=True)
    importer.add_argument('--original', required=True)
    sub.add_parser('inspect-feedback')
    confirm = sub.add_parser('confirm-feedback')
    confirm.add_argument('--receipt', required=True)
    confirm.add_argument('--operator', required=True)
    confirm.add_argument('--original-sha256', required=True)
    report = sub.add_parser('report')
    report.add_argument('--restore', required=True)
    report.add_argument('--partitions', nargs='*', default=[])
    report.add_argument('--out', required=True)
    report.add_argument('--coverage')
    report.add_argument('--source')
    args = parser.parse_args(argv)
    try:
        if args.operation == 'init':
            initialize(args.root)
            result = {'initialized': True}
        else:
            root = workspace(args.root)
            if args.operation == 'capture':
                result = recovery_bundle.capture(root, read_json(child(root, args.plan)), args.out)
            elif args.operation == 'restore':
                result = recovery_bundle.restore(root, args.bundle, args.out)
            elif args.operation == 'archive':
                result = cold_archive.archive(root, args.source, args.start, args.end, args.restore,
                                              damaged_receipt_sha256=args.damaged_receipt_sha256, repair_reason=args.repair_reason)
            elif args.operation == 'plan-coverage':
                result = cold_archive.plan_coverage(root, args.source, args.partitions, args.start, args.end, args.restore, args.out)
            elif args.operation == 'joint-read':
                result = cold_archive.joint_read(root, args.source, args.partitions, args.start, args.end, coverage=args.coverage)
                # Dates/decimal values are serialized as text alongside unchanged typed archive schemas.
                write_json(child(root, args.out), json.loads(json.dumps(result, default=str)))
                result = {'rows': len(result['rows']), 'output': args.out,
                          'coverage_verified': result['coverage_verified'], 'coverage_receipt_id': result['coverage_receipt_id']}
            elif args.operation == 'import-feedback':
                result = feedback_ledger.import_statement(root, args.statement, args.original)
            elif args.operation == 'inspect-feedback':
                result = feedback_ledger.inspect(root)
            elif args.operation == 'confirm-feedback':
                result = feedback_ledger.confirm(root, args.receipt, args.operator, args.original_sha256)
            else:
                result = capability_report.export(root, args.restore, args.partitions, args.out, coverage=args.coverage, source=args.source)
        print(json.dumps({k: v for k, v in result.items() if k not in ('files', 'plan', 'database_controls', 'records')}, ensure_ascii=False, default=str))
        return 0
    except Exception as exc:
        print(json.dumps({'ok': False, 'error_type': type(exc).__name__, 'execution_ready': False}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
