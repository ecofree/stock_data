"""Reproducible isolated historical-data and checkpoint measurements; no deployment."""
import argparse
import json
from pathlib import Path
import sys
import time

sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
import duckdb
from tools.v2.backup_verify import backup_verify,sha256
from tools.v2.run_event_replay import Clock,paper_config
from trade_system.flow_features import build_flow_features,STOCK_FEATURE_TABLE
from scripts.export_qlib_features import export_features
from trade_system.v2.domain import canonical
from trade_system.v2.storage import Store
from trade_system.v2.paper_storage import open_paper,apply_paper_event,load_paper


def run(source,output,events=512):
    output=Path(output).resolve()
    if output.exists() or not 128<=events<=2048:
        raise ValueError('new output directory and bounded 128..2048 events required')
    output.mkdir(parents=True)
    result={'scope':'isolated_historical_snapshot_and_synthetic_load_not_production_acceptance',
            'execution_ready':False,'production_cutover':False}
    if source:
        source=Path(source).resolve(strict=True)
        before=sha256(source)
        receipt=backup_verify(source,output/'source-copy')
        db=receipt['backup']
        result['source']={'path':str(source),'sha256':before,'restore_verified':receipt['restore_verified']}
        result['flow_rebuild']=build_flow_features(db)
        with duckdb.connect(str(db),read_only=True) as c:
            result['flow_quality_counts']=c.execute(f'SELECT quality_status,count(*) FROM {STOCK_FEATURE_TABLE} GROUP BY 1 ORDER BY 1').fetchall()
        result['export']=export_features(db,output/'features.parquet',output_format='parquet')
        result['source']['unchanged']=before==sha256(source)
    start=time.perf_counter()
    with Store(output/'paper.duckdb',clock=Clock()) as s:
        open_paper(s,paper_config())
        timings=[]
        for i in range(events):
            t=time.perf_counter()
            apply_paper_event(s,'fixture-event-paper',{'event_id':str(i),'kind':'cash_transfer',
                'payload':{'amount_fen':1,'evidence_id':'synthetic_load'}})
            if i%64==63: timings.append({'seq':i+1,'last_append_seconds':time.perf_counter()-t})
        rows=s.con.execute('SELECT seq,payload FROM paper_checkpoint ORDER BY seq').fetchall()
        t=time.perf_counter();book=load_paper(s,'fixture-event-paper');restore_time=time.perf_counter()-t
        result['checkpoint']={'events':events,'serialized_bytes_total':sum(len(b.encode()) for _,b in rows),
            'sizes':[{'seq':n,'bytes':len(b.encode()),'format':json.loads(b).get('format')} for n,b in rows],
            'full_replay_equal':book.summary()==load_paper(s,'fixture-event-paper',full_replay=True).summary(),
            'restore_seconds':restore_time,'append_samples':timings,
            'elapsed_seconds':time.perf_counter()-start,
            'remaining_limit':'full integrity verification and reconstructed account state still scale with history; not bounded hot-state acceptance'}
    (output/'result.json').write_text(canonical(result),encoding='utf-8')
    return result


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--source-snapshot');p.add_argument('--output',required=True);p.add_argument('--events',type=int,default=512)
    a=p.parse_args()
    r=run(a.source_snapshot,a.output,a.events)
    print(json.dumps({'output':a.output,'checkpoint':r['checkpoint'],'flow_quality_counts':r.get('flow_quality_counts')},ensure_ascii=True))
