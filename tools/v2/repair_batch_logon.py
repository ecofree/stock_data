"""One-account additive batch-logon repair. No account/task enabling or ACL edits."""
import argparse
import json
import os
from pathlib import Path
import socket
from datetime import datetime, timezone

ACCOUNT = 'StockDataResearch'
EXPECTED_SID = 'S-1-5-21-3027070730-734606845-1610825463-1006'
RIGHT = 'SeBatchLogonRight'
DENY = 'SeDenyBatchLogonRight'


def no_entries(call, *args):
    try:
        return call(*args) or ()
    except Exception as exc:
        # pywintypes.error is NOT an OSError. Only native no-entry statuses
        # are empty policy; access denied/unknown errors must remain failures.
        if getattr(exc,'winerror',None) not in (2, 259):
            raise
        return ()


def policy_state(api, handle, sid):
    return {'allow': sorted(api.ConvertSidToStringSid(s) for s in no_entries(api.LsaEnumerateAccountsWithUserRight, handle, RIGHT)),
            'deny': sorted(api.ConvertSidToStringSid(s) for s in no_entries(api.LsaEnumerateAccountsWithUserRight, handle, DENY)),
            'account_rights': sorted(no_entries(api.LsaEnumerateAccountRights, handle, sid))}


def add_only_batch(api, handle, sid, before):
    if api.ConvertSidToStringSid(sid) != EXPECTED_SID:
        raise ValueError('account SID changed; no repair permitted')
    if before['deny']:
        raise ValueError('deny policy differs from reviewed empty policy; manual policy review required')
    if RIGHT not in before['account_rights']:
        api.LsaAddAccountRights(handle, sid, (RIGHT,))
    after=policy_state(api,handle,sid)
    if (set(after['allow']) != set(before['allow']) | {EXPECTED_SID}
            or after['deny'] != before['deny']
            or set(after['account_rights']) != set(before['account_rights']) | {RIGHT}):
        raise RuntimeError('policy changed concurrently or verification failed; inspect retained receipt')
    return after


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply',action='store_true')
    parser.add_argument('--receipt',type=Path)
    args=parser.parse_args()
    import win32security as security
    import win32net
    user=win32net.NetUserGetInfo(None,ACCOUNT,1)
    if not user['flags'] & 2:
        raise ValueError('research account must remain disabled during this isolated repair')
    sid,_,_=security.LookupAccountName(None,socket.gethostname()+'\\'+ACCOUNT)
    if security.ConvertSidToStringSid(sid)!=EXPECTED_SID:
        raise ValueError('account SID differs from administrator-reviewed evidence')
    access=security.POLICY_LOOKUP_NAMES | security.POLICY_VIEW_LOCAL_INFORMATION
    if args.apply:access |= security.POLICY_CREATE_ACCOUNT
    handle=security.LsaOpenPolicy(None,access)
    try:
        before=policy_state(security,handle,sid)
        if not args.apply:
            print(json.dumps({'changes':0,'account':ACCOUNT,'sid':EXPECTED_SID,'state':before}))
            return
        if not args.receipt:
            parser.error('--receipt is required for an additive repair')
        if before['deny']:
            raise ValueError('deny policy present; no repair performed')
        record={'approval_scope':'existing_disabled_account_batch_logon_only',
                'account':ACCOUNT,'sid':EXPECTED_SID,'started_at':datetime.now(timezone.utc).isoformat(),
                'before':before,'account_enabled':False,'task_enabled':False,
                'status':'prepared_not_confirmed'}
        # Record and fsync prior state BEFORE the sole OS policy change. Never
        # overwrite a receipt or infer permission from a previous receipt.
        with args.receipt.open('x',encoding='utf-8') as stream:
            json.dump(record,stream,ensure_ascii=False,indent=2);stream.flush();os.fsync(stream.fileno())
        after=add_only_batch(security,handle,sid,before)
        if not win32net.NetUserGetInfo(None,ACCOUNT,1)['flags'] & 2:
            raise RuntimeError('account state changed concurrently; inspect, do not start task')
        record.update(after=after,status='batch_logon_granted_account_and_task_not_started',
                      changes=int(RIGHT not in before['account_rights']))
        completed=args.receipt.with_suffix('.complete.json')
        with completed.open('x',encoding='utf-8') as stream:
            json.dump(record,stream,ensure_ascii=False,indent=2);stream.flush();os.fsync(stream.fileno())
        print('BATCH_RIGHT_COMPLETE: '+str(completed))
    finally:
        handle.Close()


if __name__=='__main__':
    main()
