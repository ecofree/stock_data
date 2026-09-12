"""Historical campaign CLI; all capture/parse/derive behavior lives in the application."""
import argparse
from pathlib import Path
import sys
import duckdb
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from trade_system.v2.research_campaign import *  # noqa: F403
from trade_system.v2.research_campaign import PRICES

def export(folder,output):
    output=Path(output)
    if output.exists():raise ValueError('new campaign output required')
    before=sealed(folder);result=derive(folder);output.mkdir(parents=True,exist_ok=False)
    write_json(output/'research-input.json',result)
    with duckdb.connect(':memory:') as con:
        con.execute("CREATE TABLE features AS SELECT value->>'datetime' AS datetime,value->>'instrument' AS instrument,"+
            ','.join(f"CAST(value->>'{k}' AS DOUBLE) AS {k}" for k in ('open','high','low','close','volume','turnover','adj_factor','net_mf_amount','adjusted_price_target_ret','label_next_ret'))+
            ",value->>'adjusted_price_target_date' AS adjusted_price_target_date,value->>'label_date' AS label_date FROM json_each(?)",[json.dumps(result['rows'])])
        con.execute("COPY (SELECT * FROM features ORDER BY datetime,instrument) TO '"+str(output/'features.parquet').replace("'","''")+"' (FORMAT PARQUET)")
    write_json(output/'features.metadata.json',{'label_version':'native_campaign_v8_adjusted_proxy_not_training_labels','feature_columns':['open','high','low','close','volume','turnover','net_mf_amount'],
        'label_column':'label_next_ret','volume_unit':result['volume_unit'],'turnover_unit':'CNY','adjustment_available':True,
        'artifact_hashes':{'features.parquet':file_hash(output/'features.parquet')},'receipt_manifest_id':result['receipt_manifest_id'],
        'coverage':result['coverage'],'research_ready':False,'execution_ready':False})
    if before!=sealed(folder):raise ValueError('campaign changed during export')
    seal(output);return result['coverage']


def reconcile(folder,db,output):
    from tools.v2.normalize_price_units import qualify,FIELDS as RAW_FIELDS
    from tools.v2.canonical_price_research import resolve
    reg,members,data,statuses=replay(folder)
    if reg['origin']!='native_and_relay':raise ValueError('native campaign required')
    before=file_hash(db);evidence={}
    for i,r in enumerate(plan()):
        if i not in data or r['kind']!='security_history' or r['api'] not in (PRICES,'daily'):continue
        receipt=read_json(Path(folder)/f'receipt-{i:02d}.json')[0]
        for day,values in data[i].items():
            evidence.setdefault((r['code'].split('.')[0],day),[]).append({'provider':r['provider'],'date':day,'request_code':r['code'],'values':values,
                'receipt_file':f'receipt-{i:02d}.json','receipt_sha256':members[f'receipt-{i:02d}.json'],'received_at':receipt['received_at']})
    rows=[]
    with duckdb.connect(str(db),read_only=True) as con:
        con.execute('SET threads=2')
        for code in CODES:
            ticker,exchange=code.split('.')
            values=con.execute('SELECT '+','.join(RAW_FIELDS)+" FROM tushare_daily WHERE stock_code=? AND date BETWEEN '2025-01-01' AND '2025-06-30' ORDER BY date,ts_code",[ticker]).fetchall()
            if len(values)>400:raise ValueError('bounded scoped source rows required')
            for record in values:
                original={k:None if v is None else str(v) for k,v in zip(RAW_FIELDS,record)}
                rows.append(qualify(original,evidence.get((ticker,original['date']),[]),exchange=exchange,amount_tolerance='.50'))
    resolved=resolve(rows)
    if before!=file_hash(db) or members!=sealed(folder):raise ValueError('reconciliation inputs changed')
    output=Path(output);output.mkdir(parents=True,exist_ok=False)
    result={'scope':'campaign_observed_source_variants_not_database_repair','database_sha256':before,'receipt_manifest_id':identity(members),
        'source_rows':len(rows),'source_units_qualified':sum(r['status']=='observed_row_unit_qualified' for r in rows),
        'canonical_rows':sum(r['status']=='canonical_price_observation' for r in resolved),
        'reconciled_duplicate_groups':sum(r['status']=='canonical_price_observation' and r['source_variant_count']>1 for r in resolved),
        'quarantined_groups':sum(r['status']=='quarantined' for r in resolved),'records':resolved,
        'raw_records_deleted':0,'source_unchanged':True,'research_ready':False,'execution_ready':False}
    write_json(output/'result.json',result);seal(output)
    return {k:v for k,v in result.items() if k!='records'}


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('action',choices=['capture','export','reconcile']);p.add_argument('--output',required=True);p.add_argument('--receipts');p.add_argument('--db')
    p.add_argument('--config',help='Frozen capture ranges and request budget')
    a=p.parse_args()
    if a.action=='capture':capture(a.output,config=read_json(a.config)[0] if a.config else None)
    elif a.action=='reconcile':print(canonical(reconcile(a.receipts,a.db,a.output)))
    else:print(canonical(export(a.receipts,a.output)))
