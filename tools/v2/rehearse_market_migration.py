"""Run an explicit, isolated historical market migration rehearsal."""
from pathlib import Path
import argparse
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from trade_system.v2.market_migration_rehearsal import rehearse


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True, type=Path)
    parser.add_argument('--source-sha256', required=True)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        receipt = rehearse(args.source, args.output, source_sha256=args.source_sha256)
    except Exception as exc:
        print(f'Rehearsal rejected ({type(exc).__name__}); inspect the isolated failure receipt if created.', file=sys.stderr)
        return 1
    print(f"{receipt['status']}: {args.output/'migration-receipt.json'}")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
