"""Independent arithmetic check against sealed receipts and read-only price rows."""
import argparse
import json
from pathlib import Path
import sys

import duckdb

sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from trade_system.v2.domain import canonical,file_hash,identity
from trade_system.v2.research_receipts import verify,sealed


def run(db,receipts,pointer,baseline,output):
    db=Path(db).resolve(strict=True);output=Path(output).resolve()
    if output.exists():raise ValueError('new evidence directory required')
    output.mkdir(parents=True)
    root=Path(__file__).resolve().parents[2]
    names=['scripts/export_qlib_features.py','trade_system/v2/research_receipts.py',
           'tools/v2/probe_research_repair.py','trade_system/v2/domain.py']
    sources={p:file_hash(root/p) for p in names};db_hash=file_hash(db)
    reg,data,receipt_meta=verify(receipts)
    receipt_hash=identity(sealed(receipts))
    def load(pointer):
        pointer=Path(pointer).resolve(strict=True);p=json.loads(pointer.read_text())
        data_path=pointer.parent/p['outputs']['parquet'];meta_path=data_path.with_suffix('.metadata.json')
        meta=json.loads(meta_path.read_text())
        if file_hash(meta_path)!=p['metadata_sha256']:raise ValueError('metadata changed')
        for name,expected in meta['artifact_hashes'].items():
            if file_hash(meta_path.parent/name)!=expected:raise ValueError('export changed')
        return data_path,meta
    path,meta=load(pointer);old,old_meta=load(baseline)
    if meta['research_repair']['manifest_id']!=receipt_hash:raise ValueError('export receipt binding changed')
    with duckdb.connect(str(db),read_only=True) as c:
        c.execute("SET memory_limit='512MB'");c.execute('SET threads=1')
        c.execute("CREATE TEMP TABLE factors AS SELECT value->>'stock_code' AS code,CAST(value->>'date' AS DATE) AS trade_day,CAST(value->>'adj_factor' AS DOUBLE) AS factor FROM json_each(?)",[canonical(data['adj_factor'])])
        c.execute("CREATE TEMP TABLE money AS SELECT value->>'stock_code' AS code,CAST(value->>'date' AS DATE) AS trade_day,CAST(value->>'net_mf_amount' AS DOUBLE) AS net,CAST(value->>'buy_lg_amount' AS DOUBLE)-CAST(value->>'sell_lg_amount' AS DOUBLE) AS large,CAST(value->>'buy_elg_amount' AS DOUBLE)-CAST(value->>'sell_elg_amount' AS DOUBLE) AS extra FROM json_each(?)",[canonical(data['moneyflow'])])
        c.execute('CREATE TEMP TABLE sessions(trade_day DATE,t1 DATE,t2 DATE)')
        c.executemany('INSERT INTO sessions VALUES (?,?,?)',[reg['days'][i:i+3] for i in range(len(reg['days'])-2)])
        c.execute('CREATE TEMP TABLE exported AS SELECT * FROM read_parquet(?)',[str(path/'*.parquet') if path.is_dir() else str(path)])
        c.execute('CREATE TEMP TABLE baseline AS SELECT * FROM read_parquet(?)',[str(old/'*.parquet') if old.is_dir() else str(old)])
        # Formula is written independently from exporter CTEs and joins exact sessions.
        result=c.execute('''SELECT count(*),count(e.label_next_ret),
            max(abs(e.label_next_ret-((d2.close*a2.factor)/(d1.open*a1.factor)-1)*100)),
            count(*) FILTER(WHERE CAST(e.label_end_time AS DATE)<>s.t2),
            count(*) FILTER(WHERE e.net_mf_amount IS DISTINCT FROM m.net OR e.large_net_mf IS DISTINCT FROM m.large OR e.extra_large_net_mf IS DISTINCT FROM m.extra),
            count(e.moneyflow_5d)
          FROM exported e JOIN sessions s ON e.datetime=s.trade_day
          JOIN tushare_daily d1 ON d1.stock_code=e.instrument AND CAST(d1.date AS DATE)=s.t1
          JOIN tushare_daily d2 ON d2.stock_code=e.instrument AND CAST(d2.date AS DATE)=s.t2
          JOIN factors a1 ON a1.code=e.instrument AND a1.trade_day=s.t1
          JOIN factors a2 ON a2.code=e.instrument AND a2.trade_day=s.t2
          LEFT JOIN money m ON m.code=e.instrument AND m.trade_day=s.trade_day''').fetchone()
        rowcount=c.execute('SELECT count(*) FROM exported').fetchone()[0]
        changed=c.execute('''SELECT count(*) FROM ((SELECT datetime,instrument FROM exported EXCEPT SELECT datetime,instrument FROM baseline)
                         UNION ALL (SELECT datetime,instrument FROM baseline EXCEPT SELECT datetime,instrument FROM exported))''').fetchone()[0]
        if result[0]!=rowcount or result[1]!=rowcount or result[2]>1e-9 or result[3] or result[4] or result[5] or changed:
            raise ValueError('independent arithmetic/cohort/warmup verification failed')
        ranges=c.execute('SELECT min(label_next_ret),max(label_next_ret),count(DISTINCT instrument) FROM exported').fetchone()
        missing=[{'instrument':r[0],'dates':[str(d) for d in r[1]],
                  'status':'no_moneyflow_receipt_not_zero_not_inferred_suspension'} for r in c.execute('''
            SELECT d.stock_code,list(CAST(d.date AS DATE) ORDER BY d.date) FROM tushare_daily d
            LEFT JOIN money m ON m.code=d.stock_code AND m.trade_day=CAST(d.date AS DATE)
            WHERE CAST(d.date AS DATE) IN (SELECT trade_day FROM sessions UNION SELECT t1 FROM sessions UNION SELECT t2 FROM sessions)
            AND m.code IS NULL GROUP BY d.stock_code ORDER BY d.stock_code''').fetchall()]
    unchanged=sources=={p:file_hash(root/p) for p in names} and db_hash==file_hash(db) and receipt_hash==identity(sealed(receipts))
    result={'scope':'historical_repair_arithmetic_not_model_or_prospective_acceptance','source_files':sources,
            'database_sha256':db_hash,'source_and_database_unchanged':unchanged,'receipt_manifest_id':receipt_hash,
            'rows':rowcount,'old_labeled_rows':old_meta['labeled_rows'],'new_labeled_rows':result[1],
            'max_label_error':result[2],'label_date_mismatch':result[3],'money_value_mismatch':result[4],
            'partial_five_day_windows_kept_null':result[5]==0,'cohort_changes':changed,'label_range_pct':list(ranges[:2]),
            'instruments':ranges[2],'coverage':meta['research_repair']['coverage'],
            'missing_moneyflow_cases':missing,
            'research_ready':False,'execution_ready':False}
    (output/'result.json').write_text(canonical(result),encoding='utf-8')
    if not unchanged:raise ValueError('evidence input changed')
    return result


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    for name in ('db','receipts','pointer','baseline','output'):p.add_argument('--'+name,required=True)
    a=p.parse_args();print(json.dumps(run(a.db,a.receipts,a.pointer,a.baseline,a.output)))
