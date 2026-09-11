"""Official QLib 0.9.7 Alpha158 expressions on frozen historical export bytes.

No handwritten approximation, no replacement label, no production promotion.
The source export's adjusted volume is in hands and amount in thousand CNY;
units are explicitly checked against a read-only source DB before conversion.
"""
import importlib.metadata
import json
from pathlib import Path
import re

import duckdb
import numpy as np
import pandas as pd

from .domain import canonical, file_hash, identity


def expressions():
    if importlib.metadata.version('pyqlib')!='0.9.7':
        raise ValueError('frozen official QLib 0.9.7 required')
    from qlib.contrib.data.loader import Alpha158DL
    fields,names=Alpha158DL.get_feature_config()
    if len(fields)!=158 or len(set(names))!=158:
        raise ValueError('official Alpha158 contract changed')
    return fields,names


def compute(frame, calendar, output, *, input_units='hands_thousand_CNY'):
    """Explicit legacy hands/kCNY or adjusted shares/CNY; never infer units."""
    if input_units not in ('hands_thousand_CNY','adjusted_shares_CNY'):
        raise ValueError('explicit supported Alpha158 input units required')
    import qlib
    from qlib.data import D
    fields,names=expressions()
    output=Path(output).resolve(); output.mkdir(parents=True,exist_ok=False)
    calendar=pd.DatetimeIndex(calendar)
    if not calendar.is_unique or not calendar.is_monotonic_increasing or len(calendar)<62:
        raise ValueError('ordered unique daily calendar and >=62 sessions required')
    if frame.duplicated(['datetime','instrument']).any():
        raise ValueError('duplicate input identity')
    frame=frame.copy(); frame['datetime']=pd.to_datetime(frame['datetime'])
    if not frame.datetime.isin(calendar).all():
        raise ValueError('input outside frozen calendar')
    (output/'calendars').mkdir(); (output/'features').mkdir(); (output/'instruments').mkdir()
    (output/'calendars/day.txt').write_text('\n'.join(calendar.strftime('%Y-%m-%d'))+'\n',encoding='utf-8')
    codes=sorted(frame.instrument.unique())
    if not 1<=len(codes)<=64 or any(not re.fullmatch(r'\d{6}',str(code)) for code in codes):
        raise ValueError('bounded explicit six-digit instrument list required')
    masks=[]
    for code in codes:
        rows=frame[frame.instrument==code].set_index('datetime').reindex(calendar)
        values=rows[['open','high','low','close','volume','turnover']].astype(float)
        valid=np.isfinite(values).all(axis=1)&(values[['open','high','low','close','volume']]>0).all(axis=1)&(values.turnover>=0)
        valid &= (values.low<=values[['open','close']].min(axis=1))&(values.high>=values[['open','close']].max(axis=1))
        values.loc[~valid,:]=np.nan
        values['volume']=values.volume*(100 if input_units=='hands_thousand_CNY' else 1)
        values['vwap']=values.turnover*(1000 if input_units=='hands_thousand_CNY' else 1)/values.volume
        # Do not fill suspensions or absent adjustment factors. Readiness is
        # based solely on trailing inputs, never future labels or performance.
        eligible=valid.astype(int).rolling(61,min_periods=61).sum().eq(61)
        masks.extend((d,str(code)) for d in calendar[eligible])
        target=output/'features'/str(code);target.mkdir()
        for field in ('open','high','low','close','volume','vwap'):
            np.concatenate(([0],values[field].to_numpy())).astype('<f4').tofile(target/(field+'.day.bin'))
    (output/'instruments/all.txt').write_text(''.join(f'{c}\t{calendar[0]:%Y-%m-%d}\t{calendar[-1]:%Y-%m-%d}\n' for c in codes),encoding='utf-8')
    qlib.init(provider_uri=str(output),region='cn',kernels=1,expression_cache=None,dataset_cache=None,
              redis_port=-1,joblib_backend='threading')
    factors=D.features(codes,fields,start_time=calendar[0],end_time=calendar[-1],freq='day',disk_cache=0)
    factors.columns=names
    factors=factors.reset_index()
    eligible=pd.DataFrame(masks,columns=['datetime','instrument'])
    factors=factors.merge(eligible,on=['datetime','instrument'],how='inner',validate='one_to_one')
    return factors.replace([np.inf,-np.inf],np.nan),dict(zip(names,fields))


def export(source, source_db, output, *, instruments=32):
    if not 2<=instruments<=64:
        raise ValueError('bounded 2..64 research instruments')
    source=Path(source).resolve(strict=True); output=Path(output).resolve()
    metadata=source.with_suffix('.metadata.json')
    meta=json.loads(metadata.read_text(encoding='utf-8'))
    if not meta.get('adjustment_available') or meta.get('label_version')!='market_session_aligned_v2':
        raise ValueError('adjusted session-aligned export required')
    files=sorted(source.glob('*.parquet')) if source.is_dir() else [source]
    before={str(p):file_hash(p) for p in files}
    declared={str((metadata.parent/p).resolve()):v for p,v in meta['artifact_hashes'].items()}
    if any(declared.get(p)!=h for p,h in before.items()):
        raise ValueError('source export bytes changed')
    with duckdb.connect(str(Path(source_db).resolve()),read_only=True) as c:
        units=c.execute('SELECT DISTINCT volume_unit,amount_unit,adjustment FROM tushare_daily').fetchall()
        if units!=[('hands','thousand_yuan','none')]:
            raise ValueError('explicit source price units required; never infer from magnitude')
        calendar=[r[0] for r in c.execute('SELECT DISTINCT cal_date FROM tushare_trade_cal WHERE is_open ORDER BY cal_date').fetchall()]
    with duckdb.connect(':memory:') as c:
        path=str(source/'*.parquet') if source.is_dir() else str(source)
        c.from_parquet(path).create_view('src')
        codes=[r[0] for r in c.execute('SELECT DISTINCT instrument FROM src ORDER BY sha256(instrument),instrument LIMIT ?',[instruments]).fetchall()]
        frame=c.execute("SELECT * FROM src WHERE instrument IN (SELECT unnest(?)) AND datetime>=DATE '2025-01-02' ORDER BY datetime,instrument",[codes]).df()
    calendar=[d for d in calendar if pd.Timestamp(frame.datetime.min())<=pd.Timestamp(d)<=pd.Timestamp(frame.datetime.max())]
    output.mkdir(parents=True,exist_ok=False)
    contract={'scope':'historical_research_only_not_native_PIT_certification','source_files':before,
        'source_metadata_sha256':file_hash(metadata),'calendar_sha256':identity([str(d) for d in calendar]),
        'instruments':codes,'selection':'sha256(instrument), before label or performance inspection',
        'input_unit_evidence':'stored source unit labels; not original provider authentication',
        'volume_conversion':'export reciprocal-adjusted hands *100 -> adjusted shares',
        'vwap_conversion':'export turnover thousand CNY *1000 / adjusted shares',
        'warmup':'61 consecutive calendar sessions of valid adjusted OHLCV; absent rows not filled',
        'input_start':'2025-01-02, first stored adjustment-factor session; incomplete 2024 calendar excluded before any fit',
        'source_code_sha256':file_hash(Path(__file__)),'execution_ready':False}
    (output/'input_contract.json').write_text(canonical(contract),encoding='utf-8')
    features,expr=compute(frame,calendar,output/'qlib-provider')
    joined=features.merge(frame,on=['datetime','instrument'],validate='one_to_one')
    if joined.empty:
        raise ValueError('no rows after explicit Alpha158 warmup')
    target=output/'features.parquet';joined.to_parquet(target,index=False)
    derived={**meta,'artifact_hashes':{target.name:file_hash(target)},
             'feature_columns':list(expr)+meta['feature_columns'],
             'alpha158_contract':contract,'alpha158_expressions':expr,'source_export_metadata':str(metadata)}
    target.with_suffix('.metadata.json').write_text(canonical(derived),encoding='utf-8')
    if any(file_hash(Path(p))!=h for p,h in before.items()):
        raise ValueError('input changed during Alpha158 computation')
    result={'rows':len(joined),'instruments':len(codes),'alpha158_features':158,
            'start':str(joined.datetime.min()),'end':str(joined.datetime.max()),
            'features_sha256':file_hash(target),'execution_ready':False}
    (output/'completed.json').write_text(canonical(result),encoding='utf-8')
    return result


def main():
    import argparse
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--source',required=True);p.add_argument('--source-db',required=True)
    p.add_argument('--output',required=True);p.add_argument('--instruments',type=int,default=32)
    a=p.parse_args();print(json.dumps(export(a.source,a.source_db,a.output,instruments=a.instruments)))


if __name__=='__main__':
    main()
