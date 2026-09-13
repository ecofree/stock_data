"""Receipt-backed recent research; fixed refit policy, no production qualification."""
from pathlib import Path

import numpy as np
import pandas as pd

from . import research_dataset as dataset, rolling_research as research
from .domain import file_hash, identity, now_utc
from .gap_evidence import read_json, write_json


def load_receipts(config, root):
    from . import research_campaign as campaign
    source = config['sources']['receipts']
    folder = (Path(root) / source['path']).resolve()
    replayed = campaign.replay(folder)
    reg, members, _, _ = replayed
    if identity(members) != source['manifest_id']:
        raise ValueError('frozen receipt manifest changed')
    capture = reg.get('config') or {}
    expected_codes = [c + ('.SH' if c.startswith('6') else '.SZ') for c in config['universe']]
    if (reg['origin'] != 'native_and_relay' or capture.get('codes') != expected_codes
            or capture.get('start') != config['start'] or capture.get('end') != config['end']):
        raise ValueError('receipt origin, range or frozen universe differs')
    observed = campaign.derive(folder, replayed=replayed)
    days = observed['calendar']['SSE']
    if days != observed['calendar']['SZSE']:
        raise ValueError('exchange calendars differ')
    summary = dataset.preflight(config, days)
    frame = pd.DataFrame(observed['rows'])
    if len(frame) != len(days) * len(expected_codes):
        raise ValueError('whole frozen cohort required')
    # derive already applies split factors and converts money to CNY exactly once.
    summary.update(observed['coverage'])
    summary.update(receipt_manifest_id=identity(members),
        calendar_source='dual_exchange_receipts', money_source='relay_receipt_CNY',
        availability='historical values received now; not original point-in-time evidence')
    return frame, days, summary


def build_dataset(config, root, output):
    from .alpha158_research import compute
    output = Path(output)
    if output.exists():
        raise ValueError('new recent dataset version required')
    frame, days, summary = load_receipts(config, root)
    output.mkdir(parents=True)
    calculated,expressions=dataset.assemble(frame,days,output,alpha_compute=compute)
    # The independently fitted price model must not inherit Alpha158's 61-day
    # dependency. This does not change any identity/label of the paired study.
    price_refit=dataset.target_labels(calculated.drop(columns=list(expressions)),days,family='price21')
    price_refit = price_refit[price_refit.datetime >= pd.Timestamp(days[20])].sort_values(['datetime', 'instrument'])
    price_refit.to_parquet(output/'price-refit.parquet', index=False)
    calculated = calculated[calculated.datetime >= pd.Timestamp(days[60])].sort_values(['datetime', 'instrument'])
    calculated.to_parquet(output/'features.parquet', index=False)
    from .research_receipts import sealed
    if summary['receipt_manifest_id'] != identity(sealed((Path(root)/config['sources']['receipts']['path']).resolve())):
        raise ValueError('receipts changed during build')
    return dataset.write_metadata(config,summary,len(calculated),expressions,output,
        artifacts=('features.parquet','price-refit.parquet'),recent_source=__file__)


def final_frames(frame, tail_start, policy):
    """The independent final holdout is unavailable even to the inference refit."""
    if policy != {'train_sessions':60, 'valid_sessions':15, 'variant':'price_baseline'}:
        raise ValueError('explicit fixed recent refit policy required')
    dates = sorted(d for d in frame.datetime.unique() if d < tail_start)
    if len(dates) < 75:
        raise ValueError('insufficient recent refit sessions')
    train_dates, valid_dates = dates[-75:-15], dates[-15:]
    valid_start = pd.Timestamp(valid_dates[0], tz='Asia/Shanghai').tz_convert('UTC')
    cutoff = pd.Timestamp(tail_start, tz='Asia/Shanghai').tz_convert('UTC')
    good = frame.label_next_ret.notna() & np.isfinite(frame[dataset.BASE]).all(axis=1)
    train = frame[frame.datetime.isin(train_dates) & good & (frame.label_available_time < valid_start) & (frame.label_end_time < valid_start)]
    valid = frame[frame.datetime.isin(valid_dates) & good & (frame.label_available_time < cutoff) & (frame.label_end_time < cutoff)]
    if train.empty or valid.empty:
        raise ValueError('empty recent refit after exact label purge')
    return {'train':train.copy(), 'valid':valid.copy(), 'test':valid.copy()}, {
        'train_start':train.datetime.min(), 'train_end':train.datetime.max(),
        'validation_start':valid.datetime.min(), 'validation_end':valid.datetime.max(),
        'train_rows':len(train), 'valid_rows':len(valid), 'holdout_start':tail_start,
        'holdout_used_for_fit':False, 'refit_test_segment':'validation only; not scored or reported as out of sample',
        'train_label_available_max':train.label_available_time.max().isoformat()}


def refit(run, config, result, plan):
    run = Path(run)
    frame = research.load_frame(run/'dataset/price-refit.parquet', dataset.BASE, config['max_rows'])
    frames, boundaries = final_frames(frame, result['excluded_tail'][0], config['final_refit'])
    folder = run/'recent-price-model'
    folder.mkdir()
    write_json(folder/'registration.json', {'policy':config['final_refit'], 'boundaries':boundaries,
        'features_sha256':file_hash(run/'dataset/price-refit.parquet'),
        'dataset_role':'independent_price21_not_Alpha158_common_cohort',
        'registered_at':now_utc().isoformat(), 'selection':'fixed_price_model_not_test_winner'})
    _, preprocessing = research.fit_qlib(frames, dataset.BASE, plan, folder)
    write_json(folder/'preprocessing.json', preprocessing)
    write_json(folder/'boundaries.json', boundaries)
    return folder, boundaries


def publication_readiness(daily, top_k, nonempty, previous_nonempty):
    """Structural selection check, not a test-performance optimisation rule."""
    counts = [row['samples'] for row in daily]
    reasons = []
    if not counts or min(counts) <= top_k:
        reasons.append('test_cross_section_not_larger_than_top_k')
    if nonempty < max(1, previous_nonempty):
        reasons.append('current_coverage_regresses')
    structural = not reasons
    if structural:
        reasons.append('model_promotion_requires_separate_review')
    return {'can_replace_current_model':False, 'structurally_eligible':structural, 'reasons':reasons,
        'test_days':len(counts), 'minimum_test_cross_section':min(counts) if counts else 0,
        'maximum_test_cross_section':max(counts) if counts else 0,
        'required_minimum_cross_section':top_k+1,
        'current_nonempty':nonempty, 'previous_nonempty':previous_nonempty,
        'scope':'structural_gate_added_after_initial_recent_run_not_proof_of_alpha',
        'execution_ready':False}


def capacity_preflight(config, root):
    """Read-only cohort/label capacity, before Alpha computation or model fitting."""
    from .ml_protocol import rolling_partitions
    from .price_study import price_features
    if set(config['sources']) == {'receipts'}:
        frame, days, summary = load_receipts(config, root)
    else:
        frame, days, summary = dataset.load_history(config, root)
    computed = price_features({'rows':frame.to_dict('records'), 'calendar':{'SSE':days,'SZSE':days}}, days[60])
    splits = config['split']
    partition = rolling_partitions(days[60:-2], train_observations=splits['train'],
        valid_observations=splits['valid'], test_observations=splits['test'],
        final_holdout_observations=splits['holdout'])
    test_days = sorted({day for fold in partition['folds'] for day in days
        if fold['test_start'] <= day <= fold['test_end']})
    families = {}
    for name, column in [('price','price_eligible'), ('price_money','money_eligible'),
                         ('alpha158_window','alpha158_window_eligible')]:
        valid = computed[column] & computed.label_next_ret.notna()
        counts = computed[valid].groupby('datetime').size()
        daily = [{'date':day,'samples':int(counts.get(pd.Timestamp(day), 0))} for day in test_days]
        families[name] = {'days':daily,'selection_days':sum(r['samples'] > 5 for r in daily),
            'test_days':len(daily),'minimum_cross_section':min((r['samples'] for r in daily),default=0)}
    return {'configuration_id':identity(config), 'summary':summary, 'families':families,
        'partition':partition, 'source_sha256':file_hash(__file__),
        'scope':'capacity_only_not_factor_validity_or_model_effectiveness',
        'provider_requests':0, 'fits':0, 'execution_ready':False}


def compare_previous(run, previous, config, result, plan):
    """Compare the unchanged old model on precisely the new rolling test identities."""
    import lightgbm as lgb
    run = Path(run)
    for key in ('model', 'preprocessing'):
        if file_hash(previous[key+'_path']) != previous[key+'_sha256']:
            raise ValueError('previous frozen model changed')
    prep = read_json(previous['preprocessing_path'])[0]
    if prep['used_features'] != dataset.BASE:
        raise ValueError('previous model feature contract differs')
    frame = research.load_frame(run/'dataset/features.parquet', dataset.BASE, config['max_rows'])
    indexed = frame.set_index(['datetime', 'instrument']).sort_index()
    booster = lgb.Booster(model_file=previous['model_path'])
    daily = []
    fold_ids = []
    prediction_files = {}
    archive = run/'previous-model'
    archive.mkdir()
    for index, fold in enumerate(result['folds']):
        ids = read_json(run/'experiment/run'/f'fold-{index:02d}'/'test_ids.json')[0]
        if identity(ids) != fold['test_identity_sha256']:
            raise ValueError('paired test identity changed')
        test = indexed.loc[pd.MultiIndex.from_tuples([tuple(i) for i in ids], names=indexed.index.names)].reset_index()
        if (previous['validation_end'] >= test.datetime.min()
                or not np.isfinite(test[dataset.BASE]).all(axis=None)):
            raise ValueError('previous model overlaps or test features missing')
        values = booster.predict(test[dataset.BASE], num_threads=2)
        prediction = pd.Series(values, index=test.set_index(['datetime','instrument']).index)
        path = archive/f'fold-{index:02d}.csv'
        prediction.rename('prediction').to_csv(path)
        prediction_files[str(path.relative_to(run))] = file_hash(path)
        daily.extend(research.daily_metrics(prediction, test, plan['top_k']))
        fold_ids.append(fold['test_identity_sha256'])
    baseline = read_json(run/'experiment/run/daily_metrics.json')[0]['price_baseline']
    comparison = {'previous_model_id':previous['model_id'], 'previous_train_end':previous['train_end'],
        'test_identity_hashes':fold_ids, 'old_frozen_model':research.summarize(daily, plan['round_trip_cost_bps']),
        'old_daily_metrics':daily, 'prediction_files':prediction_files,
        'recent_rolling_price':result['aggregate']['price_baseline'],
        'paired_old_minus_recent':research.paired_comparison(daily, baseline, plan['seed']),
        'comparison_scope':'retrospective_same_sample_not_future_live_performance',
        'final_refit_scored':False, 'execution_ready':False}
    write_json(run/'previous-model-comparison.json', comparison)
    return comparison
