"""Resume only a source-bound synthetic durability probe, preserving each measured batch."""
import argparse
from itertools import islice
from pathlib import Path
import statistics
import sys
import time

sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from tools.v2.probe_bounded_hot import config,events
from tools.v2.run_event_replay import Clock
from trade_system.v2.domain import canonical,file_hash,identity
from trade_system.v2.gap_evidence import read_json,write_json
from trade_system.v2.paper_storage import load_paper,_append
from trade_system.v2.storage import Store


def run(folder,output,*,batch_size=5000):
    folder=Path(folder).resolve(strict=True);output=Path(output).resolve()
    if output.exists() or output==folder or folder in output.parents:raise ValueError('separate new measurement output required')
    reg=read_json(folder/'started.json')[0]
    root=Path(__file__).resolve().parents[2]
    if reg['scope']!='synthetic_private_append_load_public_authority_tested_separately' or reg['events']!=100000 or reg['config_sha256']!=identity(config()):raise ValueError('only original registered synthetic 100k probe may resume')
    if any(file_hash(root/name)!=sha for name,sha in reg['source_files'].items()):raise ValueError('original probe source changed')
    if type(batch_size) is not int or not 1<=batch_size<=10000:raise ValueError('bounded 1..10000 event checkpoint batch required')
    output.mkdir(parents=True,exist_ok=False)
    clock=Clock();timings=[];samples=[];max_hot=0;started=time.monotonic()
    with Store(folder/'paper.duckdb',clock=clock) as store:
        account=config()['account_id'];seq=store.con.execute('SELECT last_seq FROM paper_account WHERE account_id=?',[account]).fetchone()[0]
        if not 0<seq<=reg['events']:raise ValueError('invalid resumable sequence')
        restored=load_paper(store,account,full_replay=True)
        expected=store.con.execute('SELECT last_hash FROM paper_account WHERE account_id=?',[account]).fetchone()[0]
        if identity(restored.state)!=expected:raise ValueError('pre-resume full audit mismatch')
        end=min(seq+batch_size,reg['events'])
        write_json(output/'started.json',{'start_seq':seq,'end_seq':end,'pre_resume_full_audit':True,'original_registration_sha256':file_hash(folder/'started.json'),'original_source_files':reg['source_files'],'resume_source_sha256':file_hash(Path(__file__)),'scope':'synthetic_per_event_durable_append_not_live_execution'})
        book=restored
        for i,event in enumerate(islice(events(reg['events']),seq,end),seq+1):
            clock.set(event['at']);t=time.monotonic();book=load_paper(store,account,writer_session=True);_append(store,book,event)
            timings.append(time.monotonic()-t);max_hot=max(max_hot,len(canonical(book.state).encode()))
            if (i-seq)%1000==0 or i==end:
                sample={'seq':i,'median_append_ms':statistics.median(timings)*1000,'p95_append_ms':sorted(timings)[int(.95*(len(timings)-1))]*1000,'hot_bytes':len(canonical(book.state).encode()),'batch_seconds':time.monotonic()-started}
                samples.append(sample);write_json(output/f'checkpoint-{i:06d}.json',sample);print(canonical(sample),flush=True);timings=[]
        final=identity(book.state)
    with Store(folder/'paper.duckdb',clock=clock) as store:
        check=load_paper(store,config()['account_id'],full_replay=True)
        if identity(check.state)!=final:raise ValueError('post-batch full audit mismatch')
    if any(file_hash(root/name)!=sha for name,sha in reg['source_files'].items()):raise ValueError('probe source changed during resumed batch')
    result={'start_seq':seq,'end_seq':end,'events_appended':end-seq,'target_events':reg['events'],'target_reached':end==reg['events'],
        'source_unchanged':True,'pre_and_post_full_replay_equal':True,'max_hot_bytes':max_hot,'samples':samples,
        'database_sha256':file_hash(folder/'paper.duckdb'),'initial_interrupted_segment_latency_not_available':True,
        'scope':'synthetic_durable_resumed_checkpoint_batch_not_cold_disk_SLA','execution_ready':False,'production_cutover':False}
    write_json(output/'result.json',result);return result


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--source',required=True);p.add_argument('--output',required=True);p.add_argument('--batch-size',type=int,default=5000)
    a=p.parse_args();print(canonical(run(a.source,a.output,batch_size=a.batch_size)))
