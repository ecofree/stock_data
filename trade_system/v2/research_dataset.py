"""Config-driven, retrospective exploratory data product, isolated from formal labels."""
import json
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

from .domain import file_hash, identity, now_utc
from .gap_evidence import write_json
from .ml_protocol import rolling_partitions

BASE = ['ret_1d','ret_5d','ret_20d','volatility_20d','intraday_range','volume_ratio_20d']
MONEY = ['money_ratio','money_ratio_5d']
LABEL = 'adjusted T+1 open to exact T+2 close percent; retrospective price target, not executable return'
AVAILABILITY = 'assumed Shanghai 16:00 on exact T+2; original historical receipt time unavailable'


def preflight(config, calendar):
    if config['schema'] != 1 or config['scope'] != 'retrospective_availability_selected_exploration':
        raise ValueError('explicit retrospective exploration scope required')
    codes=config['universe']
    if codes!=sorted(set(codes)) or not 2<=len(codes)<=64 or any(len(c)!=6 or not c.isdigit() for c in codes):
        raise ValueError('bounded frozen six-digit universe required')
    days=[str(d)[:10] for d in calendar if config['start']<=str(d)[:10]<=config['end']]
    if days!=sorted(set(days)) or config['warmup_sessions']!=61:
        raise ValueError('unique ordered calendar and explicit 61-session warmup required')
    split=config['split']; minimum=60+sum(split.values())+2
    if set(split)!={'train','valid','test','holdout'} or any(type(v) is not int or v<5 for v in split.values()):
        raise ValueError('explicit train/valid/test/holdout session budget required')
    if len(days)<minimum: raise ValueError(f'insufficient input sessions: available={len(days)}, required>={minimum}')
    if not len(codes)*len(days)<=config['max_rows']<=100000:
        raise ValueError('explicit whole-cohort row budget required; no sampling fallback')
    rolling_partitions(days[60:-2],train_observations=split['train'],valid_observations=split['valid'],
        test_observations=split['test'],final_holdout_observations=split['holdout'])
    return {'input_sessions':len(days),'minimum_sessions':minimum,'planned_output_sessions':len(days)-60,
        'universe_count':len(codes),'input_rows':len(days)*len(codes),'scope':config['scope']}


def features(frame, calendar):
    """Same formulas for historical training and actually received current observations."""
    if frame.duplicated(['instrument','datetime']).any(): raise ValueError('duplicate security-day')
    result=[]; days=pd.DatetimeIndex(calendar)
    for code, group in frame.groupby('instrument',sort=True):
        g=group.copy(); g['datetime']=pd.to_datetime(g.datetime); g=g.set_index('datetime').reindex(days)
        valid=np.isfinite(g[['open','high','low','close','volume','turnover']]).all(axis=1)
        valid &= (g[['open','high','low','close','volume']]>0).all(axis=1)&(g.turnover>0)
        valid &= (g.low<=g[['open','close']].min(axis=1))&(g.high>=g[['open','close']].max(axis=1))
        g.loc[~valid,['open','high','low','close','volume','turnover']]=np.nan
        for n in (1,5,20): g[f'ret_{n}d']=(g.close/g.close.shift(n)-1)*100
        g['volatility_20d']=g.ret_1d.rolling(20,min_periods=20).std()
        g['intraday_range']=(g.high-g.low)/g.close
        g['volume_ratio_20d']=g.volume/g.volume.rolling(20,min_periods=20).mean()
        g['money_ratio']=g.net_mf_amount/g.turnover
        g['money_ratio_5d']=g.net_mf_amount.rolling(5,min_periods=5).sum()/g.turnover.rolling(5,min_periods=5).sum()
        g['feature_eligible']=valid.rolling(61,min_periods=61).sum().eq(61)
        g['instrument']=str(code);g['datetime']=days
        result.append(g.reset_index(drop=True))
    if not result: raise ValueError('no feature input')
    return pd.concat(result,ignore_index=True).replace([np.inf,-np.inf],np.nan)


def load_history(config, root):
    root=Path(root); paths={name:(root/item['path']).resolve() for name,item in config['sources'].items()}
    if set(paths)!={'prices','database'}: raise ValueError('explicit canonical prices and frozen database required')
    for name,path in paths.items():
        if path.stat().st_size > (64_000_000 if name=='prices' else 8_000_000_000): raise ValueError('source size budget')
        if file_hash(path)!=config['sources'][name]['sha256']: raise ValueError('frozen source changed: '+name)
    price=json.loads(paths['prices'].read_text(encoding='utf-8'))
    if price['database_sha256']!=config['sources']['database']['sha256'] or not price['source_unchanged']:
        raise ValueError('canonical analysis database binding differs')
    rows=[]
    for r in price['records']:
        if r['stock_code'] not in config['universe'] or not config['start']<=r['date']<=config['end']: continue
        if r['record_id']!=identity({k:v for k,v in r.items() if k!='record_id'}): raise ValueError('canonical row changed')
        good=r['status']=='canonical_price_observation'; v=r['values'] if good else {}
        rows.append({'datetime':r['date'],'instrument':r['stock_code'],'canonical_record_id':r['record_id'],
            **{k:float(v[k]) if good else None for k in ('open','high','low','close')},
            'volume':float(v['volume_shares']) if good else None,'turnover':float(v['turnover_cny']) if good else None})
    with duckdb.connect(str(paths['database']),read_only=True) as con:
        cal=con.execute("SELECT CAST(cal_date AS VARCHAR),is_open FROM tushare_trade_cal WHERE exchange='SSE' AND cal_date BETWEEN ? AND ? ORDER BY 1",[config['start'],config['end']]).fetchall()
        expected=pd.date_range(config['start'],config['end']).strftime('%Y-%m-%d').tolist()
        if [r[0] for r in cal]!=expected: raise ValueError('full stored daily calendar required')
        days=[d for d,opened in cal if opened]; summary=preflight(config,days)
        params=[config['start'],config['end'],config['universe']]
        extra=con.execute('''SELECT CAST(a.date AS VARCHAR) datetime,a.stock_code instrument,a.adj_factor,m.net_mf_amount,
          CAST(a.fetched_at AS VARCHAR) factor_received_at,CAST(m.fetched_at AS VARCHAR) money_received_at
          FROM tushare_adj_factor a LEFT JOIN tushare_moneyflow m ON a.stock_code=m.stock_code AND a.date=m.date
          WHERE a.date BETWEEN ? AND ? AND a.stock_code IN (SELECT unnest(?))''',params).df()
    frame=pd.DataFrame(rows)
    if frame.duplicated(['instrument','datetime']).any() or extra.duplicated(['instrument','datetime']).any():
        raise ValueError('join duplicate would multiply source observations')
    frame=frame.merge(extra,on=['datetime','instrument'],how='left',validate='one_to_one')
    grid=pd.MultiIndex.from_product([config['universe'],days],names=['instrument','datetime'])
    frame=frame.set_index(['instrument','datetime']).reindex(grid).reset_index()
    valid_factor=np.isfinite(frame.adj_factor)&(frame.adj_factor>0)
    for k in ('open','high','low','close'): frame[k]=frame[k]*frame.adj_factor.where(valid_factor)
    frame['volume']=frame.volume/frame.adj_factor.where(valid_factor)
    frame['net_mf_amount']=frame.net_mf_amount*10000
    summary['money_source']='frozen_tushare_moneyflow_original_10000_CNY_not_original_PIT_receipt'
    summary['calendar_source']='stored_SSE_common_sessions_not_both_exchange_original_receipts'
    summary['source_missing_price_rows']=int(frame.close.isna().sum())
    summary['source_missing_money_rows']=int(frame.net_mf_amount.isna().sum())
    return frame,days,summary


def build(config, root, output):
    output=Path(output).resolve()
    if output.exists(): raise ValueError('new dataset version required')
    frame,days,summary=load_history(config,root)
    computed=features(frame,days); output.mkdir(parents=True)
    from .alpha158_research import compute
    alpha,expressions=compute(frame,days,output/'alpha158-provider',input_units='adjusted_shares_CNY')
    computed['datetime']=pd.to_datetime(computed.datetime)
    computed=computed.merge(alpha,on=['datetime','instrument'],how='left',validate='one_to_one')
    computed=computed.sort_values(['instrument','datetime'])
    by=computed.groupby('instrument',sort=False)
    target=(by.close.shift(-2)/by.open.shift(-1)-1)*100
    future_valid=by.feature_eligible.shift(-1).eq(True)&by.feature_eligible.shift(-2).eq(True)
    computed['label_next_ret']=target.where(computed.feature_eligible&future_valid)
    computed['label_date']=by.datetime.shift(-2)
    computed['label_end_time']=computed.label_date+pd.Timedelta(hours=16)
    computed['label_available_time']=computed.label_end_time
    computed['label_status']=np.where(computed.label_next_ret.notna(),'retrospective_price_target_not_execution','missing_exact_target')
    # Keep all cohort identities after the declared common warmup, including failed rows.
    computed=computed[computed.datetime>=pd.Timestamp(days[60])].sort_values(['datetime','instrument'])
    computed.to_parquet(output/'features.parquet',index=False)
    for item in config['sources'].values():
        if file_hash(Path(root)/item['path']) != item['sha256']:
            raise ValueError('source changed during dataset build')
    meta={'label_version':'exploratory_price_target_v1_not_formal_execution_labels','label_definition':LABEL,
        'availability_assumption':AVAILABILITY,'feature_columns':BASE+MONEY+list(expressions),
        'artifact_hashes':{'features.parquet':file_hash(output/'features.parquet')},
        'dataset_config':config,'dataset_id':identity(config),'summary':summary,'rows':len(computed),
        'historical_exploration_allowed':True,'point_in_time_qualified':False,'execution_ready':False,
        'source_sha256':file_hash(__file__),'label_policy':'new_exploration_artifact_only_original_labels_unchanged'}
    write_json(output/'features.metadata.json',meta)
    write_json(output/'dataset.json',meta)
    return meta


def experiment_plan(config, meta, name):
    s=config['split']
    alpha=[f for f in meta['feature_columns'] if f not in BASE+MONEY]
    return {'scope':'historical_research_only','exposure_status':'previously_inspected_not_untouched',
        'experiment_id':name,'label_definition':LABEL,'availability_assumption':AVAILABILITY,
        'evaluation_asof':now_utc().isoformat(),'max_rows':config['max_rows'],
        'train_observations':s['train'],'valid_observations':s['valid'],'test_observations':s['test'],
        'excluded_tail_observations':s['holdout'],'max_folds':12,'num_boost_round':40,'num_threads':2,
        'top_k':5,'seed':42,'variants':{'price_baseline':BASE,'price_money':BASE+MONEY,'price_alpha158':BASE+alpha},
        'feature_roles':{f:'structural_constant' for f in alpha},'comparison_baseline':'price_baseline',
        'round_trip_cost_bps':[0,10,30,50],
        'required_export_contract':{'label_version':meta['label_version']}}
