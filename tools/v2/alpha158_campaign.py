"""Compute official Alpha158 on sealed adjusted shares/CNY; never enable model fitting."""
import argparse
from pathlib import Path
import sys

import numpy as np
import pandas as pd

sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from tools.v2.research_campaign import derive
from trade_system.v2.alpha158_research import compute
from trade_system.v2.domain import file_hash,canonical,identity
from trade_system.v2.gap_evidence import read_json,write_json
from trade_system.v2.research_receipts import sealed
from trade_system.v2.daily_session import seal


def run(source,receipts,output):
    source=Path(source);output=Path(output)
    members=sealed(source);receipt_members=sealed(receipts)
    report=read_json(source/'research-input.json')[0]
    if report!=derive(receipts) or report['volume_unit']!='reciprocal_adjusted_shares' or report['turnover_unit']!='CNY':
        raise ValueError('source campaign replay/units differ')
    if report['calendar']['SSE']!=report['calendar']['SZSE']:raise ValueError('joint sample requires aligned exchange calendars')
    metadata=read_json(source/'features.metadata.json')[0]
    if metadata['artifact_hashes']!={'features.parquet':file_hash(source/'features.parquet')}:
        raise ValueError('campaign feature bytes changed')
    frame=pd.read_parquet(source/'features.parquet');frame['datetime']=pd.to_datetime(frame.datetime)
    if frame.label_next_ret.notna().any():raise ValueError('campaign cannot self-authorize formal labels')
    output.mkdir(parents=True,exist_ok=False)
    paths=[Path(__file__),Path(__file__).parents[2]/'trade_system/v2/alpha158_research.py']
    hashes={p.name:file_hash(p) for p in paths}
    contract={'scope':'official_Alpha158_features_not_new_model_validation','source_manifest_id':identity(members),
        'receipt_manifest_id':identity(receipt_members),'source_sha256':hashes,
        'input_units':'adjusted_shares_CNY','warmup':61,'prefix_sessions':90,'fits_performed':0,'research_ready':False,'execution_ready':False}
    write_json(output/'input-contract.json',contract)
    calendar=report['calendar']['SSE']
    features,expressions=compute(frame,calendar,output/'provider-full',input_units='adjusted_shares_CNY')
    prefix_calendar=calendar[:90]
    prefix,_=compute(frame[frame.datetime<=pd.Timestamp(prefix_calendar[-1])],prefix_calendar,output/'provider-prefix',input_units='adjusted_shares_CNY')
    paired=prefix.merge(features,on=['instrument','datetime'],suffixes=('_prefix','_full'),validate='one_to_one')
    for field in expressions:
        if not np.allclose(paired[field+'_prefix'],paired[field+'_full'],equal_nan=True,rtol=1e-6,atol=1e-7):
            raise ValueError('Alpha158 prefix invariant failed: '+field)
    joined=features.merge(frame,on=['instrument','datetime'],validate='one_to_one')
    if joined.empty:raise ValueError('empty post-warmup Alpha158 sample')
    joined.to_parquet(output/'features.parquet',index=False)
    write_json(output/'expressions.json',expressions)
    if members!=sealed(source) or receipt_members!=sealed(receipts) or hashes!={p.name:file_hash(p) for p in paths}:
        raise ValueError('Alpha158 inputs changed')
    result={**contract,'rows':len(joined),'instruments':int(joined.instrument.nunique()),'official_expression_count':len(expressions),
        'prefix_rows_compared':len(paired),'prefix_invariance':True,'formal_label_rows':int(joined.label_next_ret.notna().sum()),
        'features_sha256':file_hash(output/'features.parquet'),
        'provider_artifact_hashes':{p.relative_to(output).as_posix():file_hash(p)
            for name in ('provider-full','provider-prefix') for p in sorted((output/name).rglob('*')) if p.is_file()}}
    write_json(output/'result.json',result)
    # Providers are nested; root manifest lists only root files, not a flat research receipt package.
    seal(output)
    return result


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--source',required=True);p.add_argument('--receipts',required=True);p.add_argument('--output',required=True)
    a=p.parse_args();print(canonical(run(a.source,a.receipts,a.output)))
