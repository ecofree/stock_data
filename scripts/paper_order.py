"""Reject the retired entry point before importing a writer or opening a DB."""
import sys


def main():
    print('Retired: use stock-data-paper-desk with a V2 certificate; no legacy writes performed.', file=sys.stderr)
    return 2


if __name__ == '__main__':
    raise SystemExit(main())
