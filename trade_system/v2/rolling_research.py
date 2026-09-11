"""Frozen, bounded QLib walk-forward diagnostics. No order or champion writes."""
from datetime import datetime
import importlib.metadata
import json
import os
from pathlib import Path
import re
import time

import duckdb
import numpy as np
import pandas as pd

from .domain import canonical, identity, utc, file_hash
from .ml_protocol import rolling_partitions


RESERVED = {'datetime','instrument','label_next_ret','label_date','label_end_time','label_available_time','label_status'}


def dump(path, value):
    # Artifacts are generated into new, never reused run directories.
    with Path(path).open('x',encoding='utf-8') as stream:
        stream.write(canonical(value))


def validate_plan(plan):
    if plan.get('scope')!='historical_research_only' or plan.get('exposure_status')!='previously_inspected_not_untouched':
        raise ValueError('explicit historical exposure disclosure required; no untouched claim supported')
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,70}',plan['experiment_id']):
        raise ValueError('safe unique experiment identity required')
    if not plan.get('label_definition') or not plan.get('availability_assumption'):
        raise ValueError('label and availability contract required')
    utc(plan['evaluation_asof'])
    for key,upper in [('max_rows',100000),('train_observations',5000),('valid_observations',500),
                      ('test_observations',500),('excluded_tail_observations',500),('max_folds',12),
                      ('num_boost_round',100),('num_threads',4),('top_k',1000)]:
        if type(plan[key]) is not int or not 1<=plan[key]<=upper:
            raise ValueError('explicit bounded research budget required: '+key)
    if type(plan['seed']) is not int or not 0<=plan['seed']<=2**31-1:
        raise ValueError('explicit nonnegative seed required')
    variants = plan['variants']
    if not 1<=len(variants)<=4:
        raise ValueError('one to four predeclared variants required')
    for name,features in variants.items():
        if not re.fullmatch(r'[a-z][a-z0-9_]{0,30}',name) or name=='constant':
            raise ValueError('safe unique variant name required')
        if not features or len(features)!=len(set(features)) or set(features)&RESERVED:
            raise ValueError('unique non-label feature allowlist required')
        if any(not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]*',f) for f in features):
            raise ValueError('explicit feature identifiers required')
    if plan['comparison_baseline'] not in variants:
        raise ValueError('explicit paired feature baseline required')
    roles = plan.get('feature_roles', {})
    declared_features = {f for fs in variants.values() for f in fs}
    if not isinstance(roles,dict) or not set(roles)<=declared_features or any(r not in {'required','optional_event','structural_constant'} for r in roles.values()):
        raise ValueError('invalid predeclared feature role policy')
    requirements=plan.get('required_export_contract',{})
    if not isinstance(requirements,dict) or not set(requirements)<= {'label_version','calendar_evidence','adjustment_available','flow_feature_versions'}:
        raise ValueError('unsupported export contract requirement')
    costs = plan['round_trip_cost_bps']
    if not costs or costs!=sorted(set(costs)) or any(type(c) is not int or not 0<=c<=1000 for c in costs):
        raise ValueError('sorted explicit proxy cost scenarios required')


def load_frame(path, features, max_rows):
    """Sample on identity BEFORE label filtering; preserve the same sample for all variants."""
    path = Path(path).resolve()
    if path.is_dir() and path.suffix=='.parquet':
        relation = 'read_parquet(?)'; source = str(path/'*.parquet')
    elif path.suffix=='.parquet':
        relation = 'read_parquet(?)'; source = str(path)
    elif path.suffix=='.csv':
        relation = "read_csv(?, types={'instrument':'VARCHAR'})"; source = str(path)
    else:
        raise ValueError('explicit CSV or Parquet artifact required')
    columns = ','.join('"'+f+'"' for f in [*features,'label_next_ret','label_end_time','label_available_time'])
    with duckdb.connect(':memory:') as con:
        if con.execute(f'''SELECT count(*) FROM {relation}
            WHERE CAST(datetime AS TIMESTAMP)<>CAST(CAST(datetime AS DATE) AS TIMESTAMP)''',[source]).fetchone()[0]:
            raise ValueError('daily experiment rejects intraday identities; a separate timestamp-preserving protocol is required')
        con.execute(f'''CREATE TABLE features AS SELECT CAST(datetime AS DATE) AS datetime,
                    CAST(instrument AS VARCHAR) AS instrument,{columns} FROM {relation}''',[source])
        if con.execute('SELECT count(*) FROM features WHERE datetime IS NULL OR instrument IS NULL OR instrument=\'\'').fetchone()[0]:
            raise ValueError('feature identity missing')
        if con.execute('SELECT 1 FROM features GROUP BY datetime,instrument HAVING count(*)>1 LIMIT 1').fetchone():
            raise ValueError('duplicate feature identity')
        n_days = con.execute('SELECT count(DISTINCT datetime) FROM features').fetchone()[0]
        if not n_days or max_rows<n_days:
            raise ValueError('sampling budget must cover every observation date')
        per_day = max_rows//n_days
        frame = con.execute('''SELECT * EXCLUDE(sample_rank) FROM (
            SELECT *,row_number() OVER(PARTITION BY datetime ORDER BY sha256(CAST(datetime AS VARCHAR)||':'||instrument),instrument) AS sample_rank
            FROM features) WHERE sample_rank<=? ORDER BY datetime,instrument''',[per_day]).fetchdf()
    frame['datetime'] = pd.to_datetime(frame['datetime']).dt.strftime('%Y-%m-%d')
    # Legacy naive label times have an explicit Shanghai assumption in the plan.
    for col in ('label_end_time','label_available_time'):
        series = pd.to_datetime(frame[col],errors='coerce')
        if series.dt.tz is None:
            series = series.dt.tz_localize('Asia/Shanghai')
        frame[col] = series.dt.tz_convert('UTC')
    for feature in features+['label_next_ret']:
        frame[feature] = pd.to_numeric(frame[feature],errors='coerce').replace([np.inf,-np.inf],np.nan)
    dated = pd.to_datetime(frame['datetime']).dt.tz_localize('Asia/Shanghai').dt.tz_convert('UTC')
    valid_time = frame['label_end_time'].notna() & frame['label_available_time'].notna()
    if ((frame.loc[valid_time,'label_end_time']<=dated[valid_time]) |
        (frame.loc[valid_time,'label_available_time']<frame.loc[valid_time,'label_end_time'])).any():
        raise ValueError('label end/availability chronology invalid')
    return frame


def fold_frames(frame, fold, evaluation_asof, tail_start):
    valid_start = utc(fold['valid_start']+'T00:00:00+08:00')
    test_start = utc(fold['test_start']+'T00:00:00+08:00')
    eval_end = min(utc(evaluation_asof),utc(tail_start+'T00:00:00+08:00'))
    labeled = frame['label_next_ret'].notna() & frame['label_end_time'].notna() & frame['label_available_time'].notna()
    train = frame[(frame.datetime<=fold['train_end']) & labeled & (frame.label_end_time<valid_start) & (frame.label_available_time<valid_start)]
    valid = frame[frame.datetime.between(fold['valid_start'],fold['valid_end']) & labeled & (frame.label_end_time<test_start) & (frame.label_available_time<test_start)]
    test_all = frame[frame.datetime.between(fold['test_start'],fold['test_end'])]
    test = test_all[labeled.loc[test_all.index] & (test_all.label_end_time<eval_end) & (test_all.label_available_time<eval_end)]
    if train.empty or valid.empty or test.empty:
        raise ValueError('empty fold after label availability purge')
    return {'train':train.copy(),'valid':valid.copy(),'test':test.copy()}, {'test_sampled':len(test_all),'test_evaluable':len(test),
        'train_retained':len(train),'valid_retained':len(valid),'train_label_available_max':train.label_available_time.max().isoformat(),
        'valid_label_available_max':valid.label_available_time.max().isoformat(),'evaluation_label_cutoff':eval_end.isoformat()}


class FoldDataset:
    """QLib-compatible, already purged dataset; no test labels exposed during fit."""
    def __init__(self, frames, features, *, fitting=True, roles=None):
        roles = roles or {}
        self.segments = {'train':None,'valid':None} if fitting else {'test':None}
        columns = ['datetime','instrument',*features]+(['label_next_ret'] if fitting else [])
        self.frames = {key:frames[key][columns].copy() for key in self.segments}
        train = frames['train'][features]
        self.all_missing_features = list(train.columns[train.isna().all()])
        self.constant_features = list(train.columns[(train.nunique(dropna=True)==1)])
        invalid = set(self.all_missing_features+self.constant_features)
        self.dropped_features = sorted(f for f in invalid if roles.get(f) in {'optional_event','structural_constant'})
        if invalid-set(self.dropped_features):
            raise ValueError('invalid training features: '+canonical({
                'all_missing':self.all_missing_features,'constant':self.constant_features}))
        self.features = [f for f in features if f not in self.dropped_features]
        if not self.features:
            raise ValueError('no usable training features remain')
        self.medians = train[self.features].median()

    def prepare(self, segment, col_set='feature', data_key=None):
        if segment not in self.segments:
            raise ValueError('segment unavailable to this fit/prediction adapter')
        frame = self.frames[segment].set_index(['datetime','instrument']).sort_index()
        x = frame[self.features].replace([np.inf,-np.inf],np.nan).fillna(self.medians)
        if col_set=='feature':
            return x
        if segment=='test':
            raise ValueError('test labels unavailable to model adapter')
        y = frame[['label_next_ret']]
        return y if col_set=='label' else pd.concat({'feature':x,'label':y},axis=1)


def fit_qlib(frames, features, plan, output):
    from qlib.contrib.model.gbdt import LGBModel
    from qlib.workflow import R
    dataset = FoldDataset(frames,features,roles=plan.get('feature_roles'))
    model = LGBModel(loss='mse',num_boost_round=plan['num_boost_round'],early_stopping_rounds=5,
        learning_rate=.05,num_leaves=15,feature_fraction=1.0,verbosity=-1,num_threads=plan['num_threads'],
        seed=plan['seed'],deterministic=True,force_col_wise=True)
    with R.start(experiment_name=plan['experiment_id'],recorder_name=output.parent.name+'-'+output.name):
        model.fit(dataset,verbose_eval=0)
    prediction = model.predict(FoldDataset(frames,dataset.features,fitting=False),segment='test')
    model.model.save_model(str(output/'model.txt'))
    return prediction,{'medians':dataset.medians.to_dict(),'all_missing_features':dataset.all_missing_features,
                       'dropped_features':dataset.dropped_features,'used_features':dataset.features,
                       'best_iteration':model.model.best_iteration,'model_class':'qlib.contrib.model.gbdt.LGBModel'}


def daily_metrics(prediction, test, top_k):
    y = test.set_index(['datetime','instrument'])['label_next_ret'].sort_index()
    if not prediction.index.equals(y.index) or not np.isfinite(prediction.to_numpy()).all():
        raise ValueError('predictions must exactly cover frozen test identities with finite values')
    rows = []
    for day,actual in y.groupby(level='datetime'):
        pred = prediction.loc[actual.index]
        score = pd.DataFrame({'prediction':pred,'label':actual}).reset_index()
        score = score.sort_values(['prediction','instrument'],ascending=[False,True],kind='stable')
        correlation = pred.rank().corr(actual.rank()) if pred.nunique()>1 and actual.nunique()>1 else None
        rows.append({'date':day,'samples':len(actual),'mse':float(((pred-actual)**2).mean()),
                     'rank_ic':float(correlation) if correlation is not None and np.isfinite(correlation) else None,
                     'top_k_effective':min(top_k,len(score)),'top_k_label_mean_pct':float(score.head(top_k).label.mean())})
    return rows


def summarize(daily, costs):
    count = sum(r['samples'] for r in daily)
    values = [r['rank_ic'] for r in daily if r['rank_ic'] is not None]
    mean_label = float(np.mean([r['top_k_label_mean_pct'] for r in daily]))
    return {'samples':count,'days':len(daily),'mse':sum(r['mse']*r['samples'] for r in daily)/count,
            'mean_daily_rank_ic':float(np.mean(values)) if values else None,'rank_ic_days':len(values),
            'top_k_mean_label_pct':mean_label,
            'cost_sensitivity_label_proxy_pct':{str(c):mean_label-c/100 for c in costs},
            'portfolio_return':None,'scope':'label_and_equal_weight_top_k_opportunity_proxy_not_portfolio_backtest'}


def paired_comparison(daily, baseline, seed):
    """Paired day-level moving blocks; overlapping targets are not independent rows."""
    if [r['date'] for r in daily]!=[r['date'] for r in baseline]:
        raise ValueError('paired comparison must use identical dates')
    delta = np.array([a['mse']-b['mse'] for a,b in zip(daily,baseline)],dtype=float)
    rng = np.random.default_rng(seed)
    block = min(5,len(delta))
    means = []
    for _ in range(500):
        starts = rng.integers(0,len(delta)-block+1,size=int(np.ceil(len(delta)/block)))
        sample = np.concatenate([delta[s:s+block] for s in starts])[:len(delta)]
        means.append(sample.mean())
    return {'mean_daily_mse_difference':float(delta.mean()),'negative_favors_variant':True,
            'moving_block_bootstrap_95pct':[float(v) for v in np.quantile(means,[.025,.975])],
            'block_observations':block,'resamples':500,
            'caution':'descriptive_fixed_5_day_blocks_not_multiple_testing_corrected_or_causal_source_contribution'}


def source_fingerprint():
    root = Path(__file__).resolve().parents[2]
    names = ['trade_system/v2/rolling_research.py','trade_system/v2/ml_protocol.py',
             'trade_system/v2/domain.py']
    return {name:file_hash(root/name) for name in names}


def runtime_versions():
    versions = {}
    for package in ('pandas','numpy','duckdb','pyqlib','lightgbm'):
        try:
            versions[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            versions[package] = None
    return versions


def register_experiment(root, feature_path, plan, *, registered_at=None):
    validate_plan(plan)
    path = Path(feature_path).resolve()
    files = sorted(path.glob('*.parquet')) if path.is_dir() else [path]
    if not files or any(not p.is_file() for p in files):
        raise ValueError('immutable feature artifacts required')
    metadata = path.with_suffix('.metadata.json')
    if not metadata.is_file():
        raise ValueError('feature metadata required')
    meta = json.loads(metadata.read_text(encoding='utf-8'))
    if any(meta.get(k)!=v for k,v in plan.get('required_export_contract',{}).items()):
        raise ValueError('export does not satisfy frozen source/label version requirements')
    hashes = {str(p):file_hash(p) for p in files}
    declared = {str((metadata.parent/relative).resolve()):digest for relative,digest in meta.get('artifact_hashes',{}).items()}
    if any(declared.get(p)!=digest for p,digest in hashes.items()):
        raise ValueError('export manifest does not certify selected feature bytes')
    features = set(f for fs in plan['variants'].values() for f in fs)
    if not features<=set(meta['feature_columns']) or plan['label_definition']!=meta['label_definition'] or plan['availability_assumption']!=meta['availability_assumption']:
        raise ValueError('registered feature/label/availability contract differs from export metadata')
    record = {'plan':plan,'feature_path':str(path),'artifact_hashes':hashes,
        'metadata_path':str(metadata),'metadata_sha256':file_hash(metadata),'source_hashes':source_fingerprint(),
        'registered_at':utc(registered_at or datetime.now().astimezone()).isoformat(),
        'runtime_versions':runtime_versions(),
        'fixed_model_parameters':{'class':'qlib.contrib.model.gbdt.LGBModel','loss':'mse','learning_rate':.05,
            'num_leaves':15,'feature_fraction':1.0,'early_stopping_rounds':5,'deterministic':True,'force_col_wise':True},
        'registration_scope':'frozen_before_this_run_not_before_historical_outcomes_were_seen'}
    record['registration_id'] = identity(record)
    folder = Path(root).resolve()/plan['experiment_id']
    folder.mkdir(parents=True,exist_ok=False)
    dump(folder/'registration.json',record)
    return folder


def read_registration(folder):
    folder = Path(folder).resolve()
    record = json.loads((folder/'registration.json').read_text(encoding='utf-8'))
    key = record.pop('registration_id')
    if identity(record)!=key:
        raise ValueError('experiment registration checksum mismatch')
    record['registration_id'] = key
    validate_plan(record['plan'])
    if record['source_hashes']!=source_fingerprint():
        raise ValueError('research implementation changed since registration')
    for path,digest in record['artifact_hashes'].items():
        if file_hash(path)!=digest:
            raise ValueError('registered feature artifact changed')
    path = Path(record['feature_path'])
    actual_files = sorted(str(p) for p in path.glob('*.parquet')) if path.is_dir() else [str(path)]
    if actual_files!=sorted(record['artifact_hashes']):
        raise ValueError('feature artifact membership changed')
    if file_hash(record['metadata_path'])!=record['metadata_sha256']:
        raise ValueError('registered metadata changed')
    if runtime_versions()!=record['runtime_versions']:
        raise ValueError('registered data runtime changed')
    return record


def run_experiment(folder, *, fit=None):
    folder = Path(folder).resolve()
    record = read_registration(folder); plan = record['plan']
    out = folder/'run'; out.mkdir(exist_ok=False)
    dump(out/'started.json',{'registration_id':record['registration_id'],'started_at':datetime.now().astimezone().isoformat()})
    started = time.monotonic()
    try:
        features = sorted({f for fs in plan['variants'].values() for f in fs})
        frame = load_frame(record['feature_path'],features,plan['max_rows'])
        dates = sorted(frame.datetime.unique())
        if dates[-1]>pd.Timestamp(utc(plan['evaluation_asof'])).tz_convert('Asia/Shanghai').strftime('%Y-%m-%d'):
            raise ValueError('feature observations exceed declared evaluation asof')
        partition = rolling_partitions(dates,train_observations=plan['train_observations'],valid_observations=plan['valid_observations'],
            test_observations=plan['test_observations'],final_holdout_observations=plan['excluded_tail_observations'])
        if len(partition['folds'])>plan['max_folds']:
            raise ValueError('fold budget exceeded; no silent fold truncation')
        dump(out/'partitions.json',partition)
        if fit is None:
            import qlib
            os.environ['MLFLOW_ALLOW_FILE_STORE']='true'
            qlib.init(provider_uri=str(out/'unused_local_provider'),region='cn',
                      exp_manager={'class':'MLflowExpManager','module_path':'qlib.workflow.expm',
                                   'kwargs':{'uri':(out/'mlruns').as_uri(),'default_exp_name':plan['experiment_id']}})
            fit = fit_qlib
            runtime = {p:importlib.metadata.version(p) for p in ('pyqlib','lightgbm')}
        else:
            runtime = {'model_backend':'injected_test_backend_not_qlib'}
        all_daily = {name:[] for name in [*plan['variants'],'constant']}; reports = []
        for index,fold in enumerate(partition['folds']):
            frames,coverage = fold_frames(frame,fold,plan['evaluation_asof'],partition['final_holdout'][0])
            # Validate every paired group before spending this fold's fit budget.
            for columns in plan['variants'].values():
                FoldDataset(frames,columns,roles=plan.get('feature_roles'))
            target = frames['test'].set_index(['datetime','instrument']).sort_index()
            ids = [[str(d),str(i)] for d,i in target.index]
            folder_fold = out/f'fold-{index:02d}'; folder_fold.mkdir()
            dump(folder_fold/'test_ids.json',ids)
            row = {'fold':index,'partition':fold,'coverage':coverage,'test_identity_sha256':identity(ids),'variants':{}}
            for name,columns in plan['variants'].items():
                variant_dir = folder_fold/name; variant_dir.mkdir()
                prediction,details = fit(frames,columns,plan,variant_dir)
                daily = daily_metrics(prediction,frames['test'],plan['top_k']); all_daily[name].extend(daily)
                prediction.rename('prediction').to_csv(variant_dir/'predictions.csv')
                dump(variant_dir/'preprocessing.json',details)
                row['variants'][name] = summarize(daily,plan['round_trip_cost_bps'])
            constant = pd.Series(float(frames['train'].label_next_ret.mean()),index=target.index)
            daily = daily_metrics(constant,frames['test'],plan['top_k']); all_daily['constant'].extend(daily)
            row['variants']['constant'] = summarize(daily,plan['round_trip_cost_bps'])
            dump(folder_fold/'metrics.json',row); reports.append(row)
        if any(len({r['date'] for r in ds})!=len(ds) for ds in all_daily.values()):
            raise ValueError('overlapping out-of-sample dates cannot be pooled')
        aggregate = {name:summarize(ds,plan['round_trip_cost_bps']) for name,ds in all_daily.items()}
        baseline = all_daily[plan['comparison_baseline']]
        result = {'registration_id':record['registration_id'],'scope':'historical_research_only','execution_ready':False,
            'signal_impact':'disabled','champion_changed':False,'runtime':runtime,'folds':reports,'aggregate':aggregate,
            'paired_comparisons':{name:paired_comparison(ds,baseline,plan['seed']) for name,ds in all_daily.items() if name!=plan['comparison_baseline']},
            'excluded_tail':partition['final_holdout'],'excluded_tail_scored':False,
            'exposure_status':plan['exposure_status'],'sampled_rows':len(frame),'input_identity_sha256':identity(frame[['datetime','instrument']].values.tolist()),
            'cost_contract':'fixed round-trip bps subtracted from top-k label means; not fees/settlement/turnover portfolio accounting',
            'top_k_contract':'rank within sampled evaluable cross-section; ties use instrument order; constant baseline is for MSE, not stock selection',
            'elapsed_seconds':round(time.monotonic()-started,3)}
        dump(out/'daily_metrics.json',all_daily); dump(out/'results.json',result)
        with (out/'review.md').open('x',encoding='utf-8') as stream:
            stream.write(research_markdown(result))
        hashes = {str(p.relative_to(out)):file_hash(p) for p in out.rglob('*') if p.is_file() and 'mlruns' not in p.parts}
        completion = {'registration_id':record['registration_id'],'artifact_hashes':hashes,'execution_ready':False}
        dump(out/'completed.json',{**completion,'manifest_id':identity(completion)})
        return result
    except BaseException as exc:
        dump(out/'failed.json',{'error_type':type(exc).__name__,'message':str(exc),'registration_id':record['registration_id'],
                              'partial_outputs_are_not_success':True})
        raise


def read_result(folder):
    record = read_registration(folder)
    out = Path(folder).resolve()/'run'
    completion = json.loads((out/'completed.json').read_text(encoding='utf-8'))
    manifest_id = completion.pop('manifest_id')
    if identity(completion)!=manifest_id or completion['registration_id']!=record['registration_id']:
        raise ValueError('completed experiment manifest mismatch')
    actual = {str(p.relative_to(out)) for p in out.rglob('*') if p.is_file() and 'mlruns' not in p.parts and p.name!='completed.json'}
    if actual!=set(completion['artifact_hashes']):
        raise ValueError('research output membership changed')
    for relative,digest in completion['artifact_hashes'].items():
        path = (out/relative).resolve()
        if out not in path.parents or file_hash(path)!=digest:
            raise ValueError('research output checksum mismatch')
    return json.loads((out/'results.json').read_text(encoding='utf-8'))


def research_markdown(result):
    lines = ['# QLib 多折历史诊断','',
        '本报告是已检查过历史数据上的滚动 OOS 诊断，不是未触碰保留集、正式选股优势或组合回测。',
        f"折数：{len(result['folds'])}；抽样行数：{result['sampled_rows']}；排除尾段：{result['excluded_tail']}。",
        '尾段不评分，不进入模型训练/验证；其历史暴露状态不因此变为未观察。','',
        '| 模型 | 评分样本 | 日期数 | MSE | 日均 Rank IC | Top-K 标签均值 % |',
        '|---|---:|---:|---:|---:|---:|']
    for name,row in result['aggregate'].items():
        ic = '无定义' if row['mean_daily_rank_ic'] is None else f"{row['mean_daily_rank_ic']:.6f}"
        lines.append(f"| {name} | {row['samples']} | {row['days']} | {row['mse']:.6f} | {ic} | {row['top_k_mean_label_pct']:.6f} |")
    lines += ['','## 同样本配对比较','',
              'MSE 差为模型减去冻结基线，负值较好。区间采用固定 5 日移动块、500 次重采样，仅描述性；未校正多重比较。','']
    for name,row in result['paired_comparisons'].items():
        lines.append(f"- {name}: 日均 MSE 差 {row['mean_daily_mse_difference']:.6f}，描述区间 {row['moving_block_bootstrap_95pct']}")
    lines += ['','## 解释边界','',
        '- Top-K 仅是抽样且标签可评价的横截面，不是全市场选股；常数排序并列按证券代码处理，不解释为选股策略。',
        '- 成本场景只从标签收益均值扣去预登记基点，不含现金、仓位重叠、T+N、停牌/封板/冲击、税费/公司行为核算。portfolio_return 保持 null。',
        '- 资金组比较是当前组合特征的描述性消融，不是独立数据源/付费服务的因果贡献。',
        '- 缺少历史首次可知时间和退市池认证。相关标签可用时点为历史假设，不能授予实盘资格。',
        '- 没有更新 champion、信号权重或订单。负结果保留。','',f"实验登记：{result['registration_id']}"]
    return '\n'.join(lines)+'\n'
