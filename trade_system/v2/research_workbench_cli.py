"""Installed offline review and account-admission commands."""
import argparse
import json
from pathlib import Path

from .account_admission import inspect_account
from .gap_evidence import read_json
from .research_workbench import publish, import_note


def main():
    p=argparse.ArgumentParser(description=__doc__)
    sub=p.add_subparsers(dest='command',required=True)
    out=sub.add_parser('publish');out.add_argument('--program',required=True);out.add_argument('--rolling-results',required=True);out.add_argument('--output',required=True)
    note=sub.add_parser('import-note');note.add_argument('--note',required=True);note.add_argument('--report',required=True);note.add_argument('--archive',required=True)
    account=sub.add_parser('check-account');account.add_argument('--input')
    a=p.parse_args()
    if a.command=='publish':
        result=publish(a.program,a.rolling_results,a.output)
    elif a.command=='import-note':
        result=import_note(Path(a.note).read_bytes(),read_json(a.report)[0],a.archive)
    else:
        result=inspect_account(a.input)
    print(json.dumps(result,ensure_ascii=False))
    return 0


if __name__=='__main__':
    raise SystemExit(main())
