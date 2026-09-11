"""Independent arithmetic checks against raw responses and official-provider binary bytes."""
import argparse
from datetime import datetime
from decimal import Decimal
from pathlib import Path
import sys

import numpy as np
import pandas as pd

sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from trade_system.v2.daily_session import CST
from trade_system.v2.domain import canonical,file_hash,identity
from trade_system.v2.gap_evidence import read_json,write_json
from trade_system.v2.research_receipts import sealed


def run(receipts,source,alpha,output):
    receipts=Path(receipts);source=Path(source);alpha=Path(alpha);output=Path(output)
    if output.exists():raise ValueError('new independent verification output required')
    raw_seal=sealed(receipts);source_seal=sealed(source)
    registration=read_json(receipts/'registration.json')[0]
    if registration['origin']!='native_and_relay':raise ValueError('real receipts required')
    bykey={};calendars={}
    for i,r in enumerate(registration['requests']):
        if read_json(receipts/f'status-{i:02d}.json')[0]['status']!='observed':raise ValueError('complete observed arithmetic sample required')
        body=read_json(receipts/f'receipt-{i:02d}.json')[0]['data']
        if r['kind']=='calendar':calendars[r['params']['exchange']]=sorted(x[1] for x in body['items'] if x[2]==1)
        if r['kind']!='security_history':continue
        if r['provider']=='hithink_native':
            for row in body['item']:
                day=datetime.fromtimestamp(row['date_ms']/1000,tz=CST).strftime('%Y%m%d')
                bykey.setdefault((r['code'].split('.')[0],day),{})['native']=row
        else:
            for values in body['items']:
                row=dict(zip(body['fields'],values));bykey.setdefault((row['ts_code'].split('.')[0],row['trade_date']),{})[r['api']]=row
    if calendars['SSE']!=calendars['SZSE']:raise ValueError('calendar mismatch')
    frame=pd.read_parquet(source/'features.parquet');checked=[];proxy_rows=0
    for row in frame.to_dict('records'):
        code=row['instrument'];day=str(row['datetime']).replace('-','')[:8];raw=bykey[(code,day)]
        native=raw['native'];factor=Decimal(str(raw['adj_factor']['adj_factor']))
        expected={k:float(Decimal(str(native[k+'_price']))*factor) for k in ('open','high','low','close')}
        expected.update(volume=float(Decimal(str(native['volume']))/factor),turnover=float(native['turnover']),net_mf_amount=float(Decimal(str(raw['moneyflow']['net_mf_amount']))*10000))
        for k,v in expected.items():
            if not np.isclose(row[k],v,rtol=1e-12,atol=1e-10):raise ValueError('raw arithmetic differs: '+k)
        relay=raw['daily']
        if abs(Decimal(str(relay['vol']))*100-Decimal(str(native['volume'])))>Decimal('.000001') or abs(Decimal(str(relay['amount']))*1000-Decimal(str(native['turnover'])))>Decimal('.5'):raise ValueError('raw unit corroboration differs')
        if pd.notna(row['label_next_ret']) or row['label_date'] is not None:raise ValueError('formal labels unexpectedly enabled')
        pos=calendars['SSE'].index(day)
        if pos+2<len(calendars['SSE']):
            entry,exit_=[bykey[(code,calendars['SSE'][pos+j])] for j in (1,2)]
            expected_proxy=(Decimal(str(exit_['native']['close_price']))*Decimal(str(exit_['adj_factor']['adj_factor']))/(Decimal(str(entry['native']['open_price']))*Decimal(str(entry['adj_factor']['adj_factor'])))-1)*100
            if not np.isclose(row['adjusted_price_target_ret'],float(expected_proxy),rtol=1e-10,atol=1e-10):raise ValueError('adjusted proxy differs')
            proxy_rows+=1
        elif pd.notna(row['adjusted_price_target_ret']):raise ValueError('unmatured proxy present')
        checked.append(row)
    result=read_json(alpha/'result.json')[0]
    if result['source_manifest_id']!=identity(source_seal) or result['receipt_manifest_id']!=identity(raw_seal) or result['features_sha256']!=file_hash(alpha/'features.parquet'):raise ValueError('Alpha158 source/artifact mismatch')
    provider_files={p.relative_to(alpha).as_posix():file_hash(p) for name in ('provider-full','provider-prefix') for p in sorted((alpha/name).rglob('*')) if p.is_file()}
    if provider_files!=result['provider_artifact_hashes']:raise ValueError('provider bytes changed')
    for code,rows in frame.groupby('instrument'):
        rows=rows.sort_values('datetime')
        for field in ('open','high','low','close','volume','vwap'):
            values=(rows.turnover/rows.volume if field=='vwap' else rows[field]).to_numpy(dtype='<f4')
            actual=np.fromfile(alpha/'provider-full/features'/code/(field+'.day.bin'),dtype='<f4')
            if actual[0]!=0 or not np.array_equal(actual[1:],values,equal_nan=True):raise ValueError('provider unit conversion differs: '+field)
    if raw_seal!=sealed(receipts) or source_seal!=sealed(source):raise ValueError('inputs changed')
    output.mkdir(parents=True,exist_ok=False)
    report={'scope':'independent_arithmetic_not_PIT_model_or_execution_acceptance','raw_receipt_manifest_id':identity(raw_seal),'source_manifest_id':identity(source_seal),
        'alpha_result_sha256':file_hash(alpha/'result.json'),'verification_source_sha256':file_hash(Path(__file__)),
        'rows_checked':len(checked),'adjusted_proxy_rows_checked':proxy_rows,'binary_fields_checked':6,'instruments_checked':int(frame.instrument.nunique()),
        'arithmetic_equal':True,'provider_bytes_equal':True,'fits_performed':0,'execution_ready':False}
    write_json(output/'result.json',report);return report


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    for name in ('receipts','source','alpha','output'):p.add_argument('--'+name,required=True)
    a=p.parse_args();print(canonical(run(a.receipts,a.source,a.alpha,a.output)))
