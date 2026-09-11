"""Bounded synthetic multi-account contention and repeated reopen audits, no broker I/O."""
import argparse
from collections import Counter
from copy import deepcopy
from datetime import timedelta
from pathlib import Path
import statistics
import sys
import time

sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from tools.v2.probe_bounded_hot import config
from tools.v2.run_event_replay import Clock,market
from trade_system.v2.domain import canonical,file_hash,identity
from trade_system.v2.gap_evidence import write_json
from trade_system.v2.paper_storage import open_paper,load_paper,_append
from trade_system.v2.storage import Store

CODES=('SZ.000002','SH.600000','SZ.000001')


def run(output,*,accounts=10,rounds=3):
    if type(accounts) is not int or not 2<=accounts<=10 or type(rounds) is not int or not 1<=rounds<=5:
        raise ValueError('bounded 2..10 accounts and 1..5 rounds required')
    output=Path(output);output.mkdir(parents=True,exist_ok=False)
    root=Path(__file__).resolve().parents[2]
    paths=['tools/v2/probe_multi_account.py','tools/v2/probe_bounded_hot.py','tools/v2/run_event_replay.py',
        'trade_system/v2/paper_ledger.py','trade_system/v2/paper_storage.py','trade_system/v2/storage.py','trade_system/v2/domain.py']
    hashes={p:file_hash(root/p) for p in paths}
    write_json(output/'started.json',{'source_files':hashes,'accounts':accounts,'rounds':rounds,
        'scope':'synthetic_private_append_single_writer_multi_account_not_operator_or_broker_acceptance'})
    clock=Clock();base=clock();serial=0;max_hot=0;max_cache=0;max_active=0;max_unknown=0;latencies=[];kinds=Counter();heads={};configs=[]
    with Store(output/'paper.duckdb',clock=clock) as store:
        for n in range(accounts):
            c=config();c['account_id']=f'fixture-complex-{n}'
            c['instruments']={code:deepcopy(next(iter(c['instruments'].values()))) for code in CODES}
            c['initial_lots']=[{**c['initial_lots'][0],'instrument':code} for code in CODES]
            configs.append(c);open_paper(store,c)
        def append(c,kind,payload):
            nonlocal serial,max_hot,max_cache,max_active,max_unknown
            serial+=1;clock.set((base+timedelta(milliseconds=100*serial)).isoformat())
            if kind=='market':payload={**payload,'source_event_at':clock().isoformat()}
            event={'event_id':f'complex-{serial}','at':clock().isoformat(),'kind':kind,'payload':payload}
            t=time.perf_counter();book=load_paper(store,c['account_id'],writer_session=True);_append(store,book,event)
            latencies.append(time.perf_counter()-t);kinds[kind]+=1
            max_hot=max(max_hot,len(canonical(book.state).encode()));max_cache=max(max_cache,len(store.paper_hot_cache))
            max_active=max(max_active,len(book.state['orders']))
            max_unknown=max(max_unknown,sum(o['status']=='unknown' for o in book.state['orders'].values()))
            if len(store.paper_hot_cache)>8 or len(book.state['orders'])>32:raise ValueError('hot bounds exceeded')
            return book
        for round_no in range(rounds):
            # Round-robin visits deliberately exceed the eight-account cache cap.
            for c in configs:
                for code in CODES:
                    for i in range(3):
                        order=f'{round_no}-{code}-{i}'
                        append(c,'submit',{'order_id':order,'instrument':code,'side':'sell','quantity':200,
                            'limit_price_fen':1000,'decision_ref':'synthetic-only'})
                for code in CODES:
                    append(c,'market',market(clock().isoformat(),instrument=code,capacity=40))
                    for i in range(3):
                        append(c,'unknown',{'order_id':f'{round_no}-{code}-{i}','evidence_id':'fixture-unknown'})
            for c in reversed(configs):
                for code in CODES:
                    for i in range(3):
                        p={'order_id':f'{round_no}-{code}-{i}','evidence_id':'fixture-resolution'}
                        append(c,'resolve_open',p);append(c,'cancel_request',p)
                    append(c,'market',market(clock().isoformat(),instrument=code,capacity=60))
                    for i in range(3):append(c,'cancel_ack',{'order_id':f'{round_no}-{code}-{i}','evidence_id':'fixture-cancel'})
                book=append(c,'cash_transfer',{'amount_fen':1,'evidence_id':'fixture-transfer'})
                if book.state['orders']:raise ValueError('terminal exposure retained in hot state')
                heads[c['account_id']]=identity(book.state)
            write_json(output/f'round-{round_no+1}.json',{'events':serial,'max_hot_bytes':max_hot,'max_cache_accounts':max_cache,'heads':heads})
    audits=[]
    for attempt in range(3):
        t=time.perf_counter()
        with Store(output/'paper.duckdb',clock=clock) as store:
            for c in configs:
                book=load_paper(store,c['account_id'],full_replay=True)
                if identity(book.state)!=heads[c['account_id']]:raise ValueError('cross-account/reopen mismatch')
        audits.append(time.perf_counter()-t)
    if hashes!={p:file_hash(root/p) for p in paths}:raise ValueError('probe source changed')
    result={'scope':'synthetic_multi_account_measurement_not_universal_cold_disk_SLA','source_files':hashes,
        'accounts':accounts,'instruments_per_account':3,'peak_active_orders_per_account':max_active,'peak_unknown_per_account':max_unknown,
        'durable_events':serial,'rounds':rounds,'event_kinds':dict(kinds),'max_hot_bytes':max_hot,'max_cache_accounts':max_cache,
        'median_append_ms':statistics.median(latencies)*1000,'p95_append_ms':sorted(latencies)[int(.95*(len(latencies)-1))]*1000,
        'reopen_full_audit_seconds':audits,'reopen_full_audit_equal':True,'source_unchanged':True,
        'database_sha256':file_hash(output/'paper.duckdb'),'execution_ready':False,'production_cutover':False}
    write_json(output/'result.json',result);return result


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',required=True);p.add_argument('--accounts',type=int,default=10);p.add_argument('--rounds',type=int,default=3)
    a=p.parse_args();print(canonical(run(a.output,accounts=a.accounts,rounds=a.rounds)))
