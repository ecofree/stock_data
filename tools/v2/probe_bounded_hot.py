"""Synthetic mixed-load durability probe. No actual decisions or exchange fills."""
import argparse
from datetime import timedelta
import json
from pathlib import Path
import statistics
import sys
import time

sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from tools.v2.run_event_replay import Clock, CODE, market, paper_config
from trade_system.v2.domain import canonical, identity, file_hash
from trade_system.v2.paper_storage import open_paper, load_paper, _append
from trade_system.v2.storage import Store


def config():
    c=paper_config()
    c.update(state_format='bounded_hot_v3',hot_limits={'orders':32,'lots':32,'instruments':4})
    c['initial_lots']=[{'instrument':CODE,'quantity':10000000,'cost_fen':10000000000,
                      'sellable_from':'2026-09-10','mark_price_fen':1000}]
    return c


def events(count):
    start=Clock()()
    for i in range(count):
        cycle,step=divmod(i,14)
        at=(start+timedelta(milliseconds=100*i)).isoformat()
        order='sell-'+str(cycle)
        other='reject-'+str(cycle)
        if step==0:
            kind,p='market',market(at)
        elif step==1:
            kind,p='submit',{'order_id':order,'instrument':CODE,'side':'sell','quantity':200,
                            'limit_price_fen':1000,'decision_ref':'synthetic-load-only'}
        elif step in (2,4):
            kind,p='market',market(at,capacity=40 if step==2 else 60)
        elif step in (3,5,6,7,8):
            kind={3:'cancel_request',5:'unknown',6:'resolve_open',7:'cancel_request',8:'cancel_ack'}[step]
            p={'order_id':order,'evidence_id':'fixture-resolution-'+str(i)}
        elif step==9:
            kind,p='submit',{'order_id':other,'instrument':CODE,'side':'buy','quantity':100,
                            'limit_price_fen':1000,'decision_ref':'synthetic-load-only'}
        elif step==10:
            kind,p='reject',{'order_id':other,'evidence_id':'fixture-reject-'+str(i)}
        elif step==11:
            kind,p='cash_dividend',{'instrument':CODE,'action_id':'dividend-'+str(cycle),
                'evidence_id':'fixture-entitlement','entitled_quantity':100,'cash_per_share_fen':1}
        elif step==12:
            kind,p='split',{'instrument':CODE,'action_id':'split-'+str(cycle),
                'evidence_id':'fixture-noop-split','numerator':1,'denominator':1,'post_action_mark_fen':1000}
        else:
            kind,p='cash_transfer',{'amount_fen':1,'evidence_id':'fixture-transfer'}
        yield {'event_id':'mixed-'+str(i),'at':at,'kind':kind,'payload':p}


def run(output, count):
    if not 128 <= count <= 100000:
        raise ValueError('bounded 128..100000 events required')
    output=Path(output).resolve(); output.mkdir(parents=True,exist_ok=False)
    root=Path(__file__).resolve().parents[2]
    source_paths=['trade_system/v2/paper_ledger.py','trade_system/v2/paper_storage.py',
                  'trade_system/v2/storage.py','trade_system/v2/domain.py','tools/v2/run_event_replay.py',
                  'tools/v2/probe_bounded_hot.py']
    before={p:file_hash(root/p) for p in source_paths}
    (output/'started.json').write_text(canonical({'source_files':before,'events':count,
        'config_sha256':identity(config()),'scope':'synthetic_private_append_load_public_authority_tested_separately'}),encoding='utf-8')
    clock=Clock(); samples=[]; latencies=[]; max_hot=0; started=time.perf_counter()
    with Store(output/'paper.duckdb',clock=clock) as s:
        open_paper(s,config())
        for i,event in enumerate(events(count),1):
            clock.set(event['at'])
            t=time.perf_counter()
            book=load_paper(s,config()['account_id'],writer_session=True)
            _append(s,book,event)
            latencies.append(time.perf_counter()-t)
            max_hot=max(max_hot,len(canonical(book.state).encode()))
            if i%1000==0 or i==count:
                try:
                    import psutil
                    rss=psutil.Process().memory_info().rss
                except ImportError:
                    rss=None
                sample={'seq':i,'median_append_ms':statistics.median(latencies)*1000,
                        'p95_append_ms':sorted(latencies)[int(.95*(len(latencies)-1))]*1000,
                        'hot_bytes':len(canonical(book.state).encode()),'rss_bytes':rss,
                        'elapsed_seconds':time.perf_counter()-started}
                samples.append(sample); latencies=[]
                print(json.dumps(sample),flush=True)
        final_hash=identity(book.state); final=book.summary()
        checkpoints=s.con.execute('SELECT count(*),min(length(payload)),max(length(payload)),sum(length(payload)) FROM paper_checkpoint').fetchone()
    t=time.perf_counter()
    with Store(output/'paper.duckdb',clock=clock) as s:
        restored=load_paper(s,config()['account_id'],full_replay=True)
        assert identity(restored.state)==final_hash and restored.summary()==final
        replay_seconds=time.perf_counter()-t
    unchanged=before=={p:file_hash(root/p) for p in source_paths}
    if not unchanged:
        raise ValueError('probe source changed during run; measurement not current-version evidence')
    result={'scope':'synthetic_mixed_load_not_real_execution','source_files':before,'source_unchanged':unchanged,
        'events':count,'max_hot_bytes':max_hot,
        'samples':samples,'checkpoints':checkpoints,'streamed_full_replay_equal':True,
        'cold_audit_seconds':replay_seconds,'cold_audit_complexity':'O(history), pages <=256 rows',
        'execution_ready':False,'production_cutover':False}
    (output/'result.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
    return result


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output',required=True); p.add_argument('--events',type=int,default=10000)
    args=p.parse_args(); print(json.dumps(run(args.output,args.events)))
