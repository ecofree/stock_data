"""Bounded synthetic checkpoint comparison; not market latency acceptance."""
import argparse
import json
from pathlib import Path
import statistics
import sys
import time
import tracemalloc

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from tools.v2.run_event_replay import Clock,paper_config
from trade_system.v2.storage import Store
from trade_system.v2.paper_storage import open_paper,apply_paper_event,load_paper


def run(output,events):
    if not 64<=events<=256:
        raise ValueError('synthetic probe is bounded to 64..256 events')
    output.mkdir(parents=True,exist_ok=False)
    db=output/'synthetic.duckdb'
    with Store(db,clock=Clock()) as store:
        open_paper(store,paper_config())
        for i in range(events):
            apply_paper_event(store,'fixture-event-paper',{'event_id':str(i),'kind':'cash_transfer',
                'payload':{'amount_fen':1,'evidence_id':'synthetic_benchmark'}})
    measurements={}
    with Store(db,clock=Clock()) as store:
        reference=load_paper(store,'fixture-event-paper',full_replay=True).summary()
        for name,full in [('checkpoint_tail',False),('independent_full_replay',True)]:
            elapsed=[]
            tracemalloc.start()
            for _ in range(5):
                start=time.perf_counter()
                actual=load_paper(store,'fixture-event-paper',full_replay=full).summary()
                elapsed.append(time.perf_counter()-start)
                assert actual==reference
            _,peak=tracemalloc.get_traced_memory();tracemalloc.stop()
            measurements[name]={'samples_seconds':elapsed,'median_seconds':statistics.median(elapsed),
                'max_seconds':max(elapsed),'python_peak_bytes':peak}
        latest=store.con.execute('SELECT max(seq) FROM paper_checkpoint').fetchone()[0]
    report={'scope':'synthetic_cash_transfer_only_not_realtime_or_market_acceptance','events':events,
            'checkpoint_seq':latest,'tail_events':events-latest,'equal_results':True,'measurements':measurements,
            'limitations':['SQL prefix checksum still scans prior journal','checkpoint state retains history',
                           'native allocations are not included in tracemalloc','not a worst-case account load test'],
            'execution_ready':False}
    (output/'benchmark.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
    print(json.dumps(report))


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--events',type=int,default=128)
    args=parser.parse_args()
    run(args.output.resolve(),args.events)
