"""Explicit local paper-only handoff; no automatic execution of imported JSON."""
import argparse
import json
from pathlib import Path

from .domain import canonical
from .operator_workflow import ACK, render_desk
from .service import Service


def read_packet(path):
    path = Path(path)
    if path.stat().st_size > 64000:
        raise ValueError('paper packet exceeds 64 KB')
    return json.loads(path.read_text(encoding='utf-8'))


def write_new(path,data):
    with Path(path).open('x',encoding='utf-8') as stream:
        stream.write(canonical(data))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db',required=True,help='Existing isolated V2 paper database')
    sub=parser.add_subparsers(dest='command',required=True)
    export=sub.add_parser('export');export.add_argument('--decision-id',required=True);export.add_argument('--output',required=True)
    confirm=sub.add_parser('confirm');confirm.add_argument('--packet',required=True);confirm.add_argument('--quantity',required=True,type=int)
    confirm.add_argument('--operator',required=True);confirm.add_argument('--request-id',required=True)
    confirm.add_argument('--quote-manifest',required=True);confirm.add_argument('--acknowledge-paper',action='store_true',required=True)
    review=sub.add_parser('review');review.add_argument('--account-id',required=True);review.add_argument('--output',required=True)
    review.add_argument('--publish-root');review.add_argument('--generation',type=int)
    close=sub.add_parser('close-unsent',help='Cancel or expire a provably unsent paper confirmation')
    close.add_argument('--confirmation-request-id',required=True)
    close.add_argument('--operator',required=True);close.add_argument('--request-id',required=True)
    close.add_argument('--reason',choices=['cancelled','expired_not_sent'],required=True)
    reconcile=sub.add_parser('reconcile-paper-unsent',help='Full local paper-journal proof required; never real accounts')
    reconcile.add_argument('--confirmation-request-id',required=True)
    reconcile.add_argument('--operator',required=True);reconcile.add_argument('--request-id',required=True)
    args=parser.parse_args()
    if args.command=='review' and bool(args.publish_root)!=(args.generation is not None):
        parser.error('publish-root and generation are required together')
    if not Path(args.db).is_file():
        parser.error('existing V2 database required; no account is created by this command')
    # Fail before taking a writer handle if output cannot be a new artifact.
    if args.command in ('export','review') and Path(args.output).exists():
        parser.error('output must not exist')
    packet=read_packet(args.packet) if args.command=='confirm' else None
    with Service(args.db) as service:
        if args.command=='export':
            result=service.submit('paper_plan_export',decision_id=args.decision_id).result(35)
            write_new(args.output,result)
            response={'packet_id':result['packet_id'],'output':args.output,'execution_ready':False}
        elif args.command=='confirm':
            response=service.submit('paper_plan_confirm',packet=packet,quantity_requested=args.quantity,
                operator=args.operator,request_id=args.request_id,quote_manifest=args.quote_manifest,acknowledgement=ACK).result(35)
        elif args.command=='close-unsent':
            response=service.submit('close_unsent',confirmation_request_id=args.confirmation_request_id,
                                    operator=args.operator,request_id=args.request_id,reason=args.reason).result(35)
        elif args.command=='reconcile-paper-unsent':
            response=service.submit('reconcile_paper_unsent',confirmation_request_id=args.confirmation_request_id,
                                    operator=args.operator,request_id=args.request_id).result(35)
        else:
            result=service.submit('paper_desk_review',account_id=args.account_id).result(35)
            folder=Path(args.output);folder.mkdir(parents=True,exist_ok=False)
            write_new(folder/'review.json',result)
            with (folder/'index.html').open('x',encoding='utf-8') as stream:
                stream.write(render_desk(result))
            response={'report_id':result['report_id'],'output':str(folder),'execution_ready':False}
            if args.publish_root:
                from .publisher import publish
                response['publication']=publish(args.publish_root,result['report_id'],
                    {'review.json':(folder/'review.json').read_bytes(),'index.html':(folder/'index.html').read_bytes()},
                    generation=args.generation)
    print(json.dumps(response,ensure_ascii=True))
    return 0


if __name__=='__main__':
    raise SystemExit(main())
