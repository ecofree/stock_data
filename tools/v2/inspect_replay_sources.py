"""Read-only frozen-copy capability inventory, not a live source certification."""
import argparse
import hashlib
import json
from pathlib import Path

import duckdb


def inspect(source, output):
    source,output = Path(source).resolve(),Path(output).resolve()
    if source.name!='kpl_data.duckdb' or 'backups' not in source.parts:
        raise ValueError('use a verified backup, never the live writer database')
    output.mkdir(parents=True,exist_ok=False)
    sha = hashlib.sha256()
    with source.open('rb') as handle:
        for block in iter(lambda:handle.read(1024*1024),b''):
            sha.update(block)
    data = {'source':str(source),'source_sha256':sha.hexdigest(),'mode':'historical_research',
            'execution_ready':False,'source_license':'not_certified','tables':{}}
    with duckdb.connect(str(source),read_only=True) as con:
        for table,col in [('auction_tick','date'),('multi_source_stock_flow','source_date'),('l2_stock_intraday','date')]:
            columns = [r[1] for r in con.execute('PRAGMA table_info('+table+')').fetchall()]
            count,latest = con.execute(f'SELECT count(*),max({col}) FROM {table}').fetchone()
            rows,stocks = con.execute(f'SELECT count(*),count(DISTINCT stock_code) FROM {table} WHERE {col}=?',[latest]).fetchone()
            info = {'total_rows':count,'latest_date':str(latest),'latest_rows':rows,'latest_stocks':stocks,'columns':columns}
            if table=='multi_source_stock_flow':
                fields = ('amount_unit','flow_definition','source_api','origin_provider','field_mapping_version')
                info['latest_nonempty_fields']={f:con.execute(f"SELECT count(*) FROM {table} WHERE {col}=? AND {f} IS NOT NULL AND cast({f} AS VARCHAR)<>''",[latest]).fetchone()[0] for f in fields}
                info['latest_timestamp_counts']=dict(zip(('event_time','collected_at','fetched_at'),con.execute(
                    f'SELECT count(source_event_time),count(collected_at),count(fetched_at) FROM {table} WHERE {col}=?',[latest]).fetchone()))
                info['latest_max_distinct_times_per_stock_provider']=con.execute(f'''SELECT coalesce(max(n),0) FROM
                    (SELECT count(DISTINCT source_event_time) AS n FROM {table} WHERE {col}=? GROUP BY stock_code,provider)''',[latest]).fetchone()[0]
                info['blockers']=['daily_source_date_is_not_intraday_cumulative_process',
                                  'provider_and_definition_must_bind_each_temporal_pair','original_receipt_time_not_certified']
            elif table=='auction_tick':
                info['blockers']=['no_explicit_finality_field','no_source_api_or_origin_field','no_observable_bid_ask_capacity']
            else:
                info['blockers']=['no_observable_bid_ask_capacity','no_explicit_phase_or_halt_field','no_versioned_price_limit_evidence']
            data['tables'][table]=info
    data['verdict']='historical_features_available_but_live_event_and_fill_acceptance_blocked'
    (output/'capabilities.json').write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')
    return {k:data[k] for k in ('source_sha256','verdict','execution_ready','tables')}


if __name__=='__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args = parser.parse_args()
    print(json.dumps(inspect(args.source,args.output),ensure_ascii=False))
