"""Synthetic bulk-built recovery corpus, NOT per-event durability/throughput proof."""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import time

sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from tools.v2.probe_bounded_hot import config,events
from tools.v2.run_event_replay import Clock
from trade_system.v2.domain import canonical,file_hash,identity
from trade_system.v2.paper_ledger import PaperBook
from trade_system.v2.paper_storage import open_paper,load_paper,_event_identity,_projection
from trade_system.v2.storage import Store


def insert_rows(store,table,types,rows):
    if rows:
        columns=','.join(f"json_extract_string(value,'$[{i}]')::{t}" for i,t in enumerate(types))
        store.con.execute(f'INSERT INTO {table} SELECT {columns} FROM json_each(?)',[canonical(rows)])


def run(output,count=100000):
    if type(count) is not int or not 128<=count<=100000:
        raise ValueError('128..100000 fixture events required')
    output=Path(output).resolve();output.mkdir(parents=True,exist_ok=False)
    root=Path(__file__).resolve().parents[2]
    names=['trade_system/v2/paper_storage.py','trade_system/v2/paper_ledger.py','trade_system/v2/storage.py',
           'trade_system/v2/domain.py','tools/v2/probe_bounded_hot.py','tools/v2/run_event_replay.py',
           'tools/v2/probe_recovery_scale.py']
    sources={p:file_hash(root/p) for p in names}
    scope='synthetic_bulk_built_recovery_corpus_not_per_event_durable_append_benchmark'
    (output/'started.json').write_text(canonical({'scope':scope,'events':count,'source_files':sources}),encoding='utf-8')
    clock=Clock();c=config();account=c['account_id'];book=PaperBook(c)
    chain=identity({'format':'bounded_hot_v3','config':identity(c)})
    journal=[];history=[];indices=[];checkpoints=[];samples=[];max_hot=0
    started=time.monotonic()
    import psutil
    process=psutil.Process()
    with Store(output/'paper.duckdb',clock=clock) as s:
        s.con.execute("SET memory_limit='64MB'");s.con.execute('SET threads=1')
        open_paper(s,c)
        archive_dir=s.path.parent/(s.path.name+'.raw')
        def archive(body):
            # Intentional fixture-only construction: no fsync/atomic append claim.
            digest=hashlib.sha256(body.encode()).hexdigest()
            p=archive_dir/digest
            if not p.exists():p.write_bytes(body.encode())
            return digest
        def flush():
            with s.transaction():
                insert_rows(s,'paper_ledger_event',['VARCHAR','VARCHAR','BIGINT','TIMESTAMPTZ','JSON','VARCHAR','VARCHAR','VARCHAR'],journal)
                insert_rows(s,'paper_history',['VARCHAR','BIGINT','JSON','VARCHAR'],history)
                insert_rows(s,'paper_identity',['VARCHAR','VARCHAR','VARCHAR','BIGINT'],indices)
                insert_rows(s,'paper_checkpoint',['VARCHAR','BIGINT','VARCHAR','VARCHAR','VARCHAR','JSON','VARCHAR'],checkpoints)
            journal.clear();history.clear();indices.clear();checkpoints.clear()
        for seq,event in enumerate(events(count),1):
            clock.set(event['at']);prior=identity(book.state)
            book.seen={};book.identity_used=lambda kind,key:False
            book.apply(event);state_hash=identity(book.state)
            body=canonical(event);raw=archive(body)
            journal.append([account,event['event_id'],seq,event['at'],body,prior,state_hash,raw])
            cold=canonical(book.cold_batch);history.append([account,seq,cold,archive(cold)])
            item=_event_identity(event)
            if item:indices.append([account,*item,seq])
            chain=identity([chain,seq,raw,prior,state_hash])
            if seq%64==0:
                cp=canonical({'format':'bounded_hot_v3','state':book.state})
                checkpoints.append([account,seq,identity(c),state_hash,chain,cp,archive(cp)])
            max_hot=max(max_hot,len(canonical(book.state).encode()))
            if seq%256==0:flush()
            if seq%10000==0 or seq==count:
                sample={'seq':seq,'build_seconds':time.monotonic()-started,'rss_bytes':process.memory_info().rss}
                samples.append(sample);print(json.dumps(sample),flush=True)
        flush()
        with s.transaction():
            s.con.execute('UPDATE paper_account SET last_seq=?,last_hash=? WHERE account_id=?',[count,state_hash,account])
            _projection(s,book)
    db_hash=file_hash(output/'paper.duckdb');t=time.monotonic()
    with Store(output/'paper.duckdb',clock=clock) as s:
        s.con.execute("SET memory_limit='64MB'");s.con.execute('SET threads=1')
        s.command_deadline=time.monotonic()+180
        restored=load_paper(s,account,full_replay=True)
        equal=identity(restored.state)==state_hash and restored.summary()==book.summary()
        s.command_deadline=None
    result={'scope':scope,'source_files':sources,'events':count,'accounts':1,'max_hot_bytes':max_hot,
            'build_samples':samples,'full_replay_seconds':time.monotonic()-t,'replay_equal':equal,
            'duckdb_buffer_budget':'64MB_not_total_process_memory_cap','rss_after_replay_bytes':process.memory_info().rss,
            'os_file_cache':'not_flushed_no_physical_cold_disk_claim','database_sha256':db_hash,
            'database_unchanged_after_replay':db_hash==file_hash(output/'paper.duckdb'),
            'source_unchanged':sources=={p:file_hash(root/p) for p in names},'execution_ready':False}
    (output/'result.json').write_text(canonical(result),encoding='utf-8')
    if not equal or not result['source_unchanged'] or not result['database_unchanged_after_replay']:
        raise ValueError('recovery evidence failed')
    return result


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output',required=True);p.add_argument('--events',type=int,default=100000)
    a=p.parse_args();print(json.dumps(run(a.output,a.events)))
