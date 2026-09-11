"""Independent row-wise audit of semantic exclusions against the unmodified export."""
import argparse
import json
import math
from pathlib import Path
import sys

import duckdb

sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from tools.v2.probe_research_history import load_export
from trade_system.v2.domain import file_hash
from trade_system.v2.gap_evidence import write_json
from trade_system.v2.research_semantics import validate


def run(db,policy,baseline,current,output):
    output=Path(output);output.mkdir(parents=True,exist_ok=False)
    root=Path(__file__).resolve().parents[2]
    files=['scripts/export_qlib_features.py','trade_system/v2/research_semantics.py','tools/v2/probe_research_semantics.py']
    sources={p:file_hash(root/p) for p in files};before=file_hash(db)
    rules,proof=validate(policy);old,old_meta=load_export(baseline);new,meta=load_export(current)
    if proof['manifest_id']!=meta['research_semantics']['manifest_id']:
        raise ValueError('semantic binding differs')
    if old_meta['research_repair']['manifest_id']!=meta['research_repair']['manifest_id']:
        raise ValueError('base receipts differ')
    with duckdb.connect(str(db),read_only=True) as c:
        c.execute("SET memory_limit='512MB'");c.execute('SET threads=1')
        c.execute('CREATE TEMP TABLE old_export AS SELECT * FROM read_parquet(?)',[str(old/'*.parquet')])
        c.execute('CREATE TEMP TABLE new_export AS SELECT * FROM read_parquet(?)',[str(new/'*.parquet')])
        # Calendar order is reconstructed from the sealed receipt interval rather
        # than the production suspension SQL. No production apply() call here.
        # Metadata coverage includes all warmup and exact T+2 receipt sessions.
        days=[r['date'] for r in meta['research_repair']['coverage']]
        positions={d:i for i,d in enumerate(days)}
        volumes={(str(d),code):v for d,code,v in c.execute('SELECT date,stock_code,volume FROM tushare_daily WHERE date BETWEEN ? AND ?',[days[0],days[-1]]).fetchall()}
        row_mismatches=0;counts={};rejected=0;raw=0
        rows=c.execute('''SELECT n.datetime,n.instrument,n.label_next_ret,n.historical_price_target_ret,n.label_status,
            n.historical_instrument_hint,n.execution_target_ret,n.execution_qualified,o.label_next_ret
            FROM new_export n LEFT JOIN old_export o ON o.datetime=n.datetime AND o.instrument=n.instrument''').fetchall()
        def same(a,b): return (a is None and b is None) or (a is not None and b is not None and abs(a-b)<1e-10)
        for day,code,label,target,status,hint,execution,qualified,original in rows:
            day=str(day)[:10];i=positions[day];path=days[i:i+3]
            if len(path)!=3:raise ValueError('exact target tail required')
            conflict=any((code==a['new'] and day<a['effective']) or (code==a['old'] and path[-1]>=a['effective']) for a in rules['aliases'])
            expected_hint=next((a['old'] for a in rules['aliases'] if code==a['new'] and day<a['effective']),code)
            suspension=any(s['code']==code and any(s['start']<=d<s['resume'] for d in path) for s in rules['suspensions'])
            volume_missing=any(volumes.get((d,code)) is None or not math.isfinite(volumes[d,code]) or volumes[d,code]<=0 for d in path)
            expected=('identity_conflict_unverified_continuity' if conflict else 'documented_suspension_in_target_path'
                if suspension else 'missing_positive_volume_not_inferred_suspension' if volume_missing else
                'missing_session_price_not_zero' if original is None else 'historical_proxy_not_execution_certified')
            expected_label=None if conflict or suspension or volume_missing else original
            row_mismatches+=int(not same(target,original) or not same(label,expected_label) or status!=expected
                                or hint!=expected_hint or execution is not None or qualified is not False)
            counts[status]=counts.get(status,0)+1;raw+=int(original is not None);rejected+=int(original is not None and label is None)
        cohort=c.execute('''SELECT count(*) FROM ((SELECT datetime,instrument FROM old_export EXCEPT SELECT datetime,instrument FROM new_export)
            UNION ALL (SELECT datetime,instrument FROM new_export EXCEPT SELECT datetime,instrument FROM old_export))''').fetchone()[0]
        features=old_meta['feature_columns']
        changed=c.execute('SELECT count(*) FROM new_export n JOIN old_export o USING(datetime,instrument) WHERE '+
                          ' OR '.join(f'n.{f} IS DISTINCT FROM o.{f}' for f in features)).fetchone()[0]
    unchanged=(before==file_hash(db) and sources=={p:file_hash(root/p) for p in files}
               and proof==validate(policy)[1] and meta==load_export(current)[1] and old_meta==load_export(baseline)[1])
    result={'rows':len(rows),'original_price_targets':raw,'excluded_labels':rejected,'remaining_proxy_labels':raw-rejected,
        'row_mismatches':row_mismatches,'cohort_changes':cohort,'feature_changes':changed,'label_status_counts':counts,
        'inputs_unchanged':unchanged,'database_sha256':before,'source_files':sources,'semantics_manifest_id':proof['manifest_id'],
        'research_ready':False,'execution_ready':False,'scope':'retrospective_exclusion_check_not_execution_backtest'}
    write_json(output/'result.json',result)
    if (not unchanged or row_mismatches or cohort or changed or not rows
        or counts!=meta['research_semantics']['output_label_status_counts']):raise ValueError('semantic audit failed')
    return result


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    for name in ('db','policy','baseline','current','output'):p.add_argument('--'+name,required=True)
    a=p.parse_args();print(json.dumps(run(a.db,a.policy,a.baseline,a.current,a.output)))
