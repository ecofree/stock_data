"""Independent price-window study with an explicit, append-only factor supplement."""
from copy import deepcopy
from pathlib import Path
import uuid
import re

import numpy as np
import pandas as pd

from . import research_dataset as ds, rolling_research as research, research_baselines, research_recent
from .domain import file_hash, identity, now_utc, number
from .gap_evidence import read_json, write_json


def supplement_rows(observed, raw_native, factors, request, lineage):
    """Only a missing factor may be supplied; existing conflicts remain exclusions."""
    result = deepcopy(observed)
    if not re.fullmatch(r'\d{6}\.(SZ|SH)', request['code']):
        raise ValueError('explicit supported security required')
    code, exchange = request['code'].split('.')
    expected = [d for d in observed['calendar']['SZSE' if exchange == 'SZ' else 'SSE']
                if request['start'] <= d <= request['end']]
    if not expected or request['api'] != 'adj_factor' or sorted(factors) != expected:
        raise ValueError('exact approved security and complete factor dates required')
    if any(not np.isfinite(float(r['adj_factor'])) or number(r['adj_factor']) <= 0 for r in factors.values()):
        raise ValueError('positive finite factors required')
    supplied = []
    for row in result['rows']:
        day = row['datetime']
        if row['instrument'] != code or day not in factors:
            continue
        if row['adj_factor'] is not None or 'daily_factor_missing' not in row['gaps']:
            raise ValueError('supplement cannot overwrite an existing factor')
        factor = number(factors[day]['adj_factor'])
        row['adj_factor'] = float(factor)
        row['gaps'].remove('daily_factor_missing')
        row['factor_supplement'] = deepcopy(lineage)
        # No reuse of donor prices: only the base batch's reconciled prices/volume.
        if not row['gaps']:
            native = raw_native[(request['code'], day)]
            for field in ('open','high','low','close'):
                row[field] = float(number(native[field]) * factor)
            row['volume'] = float(number(native['volume_shares']) / factor)
            row['turnover'] = float(number(native['turnover_cny']))
        supplied.append(day)
    if supplied != expected:
        raise ValueError('supplement target membership differs')
    return result, supplied


def recover(base, donor, *, request=None):
    from . import research_campaign as campaign
    base, donor = Path(base), Path(donor)
    reg, members, parsed, statuses = campaign.replay(base)
    donor_reg, donor_members, donor_parsed, donor_statuses = campaign.replay(donor)
    if reg['origin'] != 'native_and_relay' or donor_reg['origin'] != 'native_and_relay':
        raise ValueError('real sealed receipts required')
    matches = [i for i,r in enumerate(reg['requests']) if r.get('api') == 'adj_factor'
        and statuses[i]['status'] == 'failed' and (request is None or r == request)
        and any(r == other and donor_statuses[j]['status'] == 'observed'
                for j, other in enumerate(donor_reg['requests']))]
    if len(matches) != 1:
        raise ValueError('one unambiguous failed factor request required; select an exact request otherwise')
    selected = reg['requests'][matches[0]]
    donor_matches = [i for i,r in enumerate(donor_reg['requests']) if r == selected]
    if len(matches) != 1 or len(donor_matches) != 1:
        raise ValueError('exact single approved request required')
    i, j = matches[0], donor_matches[0]
    if reg['requests'][i] != donor_reg['requests'][j] or statuses[i]['status'] != 'failed' or donor_statuses[j]['status'] != 'observed':
        raise ValueError('failed base and successful identical donor request required')
    filename = f'receipt-{j:02d}.json'
    receipt = read_json(donor/filename)[0]
    lineage = {'base_folder':str(base.resolve()), 'base_manifest_id':identity(members), 'failed_index':i,
        'security':selected['code'], 'request':selected,
        'donor_folder':str(donor.resolve()), 'donor_manifest_id':identity(donor_members),
        'donor_file':filename, 'donor_sha256':donor_members[filename], 'received_at':receipt['received_at'],
        'supplemented_at':now_utc().isoformat(), 'not_a_new_provider_response':True}
    raw = {}
    for k,r in enumerate(reg['requests']):
        if r['kind'] == 'security_history' and r['provider'] == 'hithink_native' and k in parsed:
            for day, values in parsed[k].items(): raw[(r['code'], day)] = values
    original = campaign.derive(base)
    fixed, dates = supplement_rows(original, raw, donor_parsed[j], reg['requests'][i], lineage)
    lineage.update(dates=dates, supplied_factors=len(dates), original_rows_sha256=identity(original['rows']),
        supplemented_rows_sha256=identity(fixed['rows']), additional_provider_requests=0,
        remaining_price_gaps=sum(bool(r['gaps']) for r in fixed['rows']), execution_ready=False)
    return fixed, lineage


def price_features(observed, first_date):
    days = observed['calendar']['SSE']
    if days != observed['calendar']['SZSE']:
        raise ValueError('exchange calendars differ')
    frame = ds.features(pd.DataFrame(observed['rows']), days).sort_values(['instrument','datetime'])
    frame=ds.target_labels(frame,days,family='price21')
    # Keep the already frozen evaluation calendar, not extra backtested dates.
    return frame[frame.datetime>=pd.Timestamp(first_date)].sort_values(['datetime','instrument'])


def summarize_selection(model_daily, old_daily, rule_daily, top_k=5):
    if ([r['date'] for r in model_daily] != [r['date'] for r in old_daily]
            or [r['date'] for r in model_daily] != [r['date'] for r in rule_daily]):
        raise ValueError('all baselines must use identical dates')
    rows = []
    for model, old, rule in zip(model_daily, old_daily, rule_daily):
        if not model['samples'] == old['samples'] == rule['samples']:
            raise ValueError('paired sample count differs')
        eligible = model['samples'] > top_k
        rows.append({'date':model['date'], 'samples':model['samples'], 'selection_eligible':eligible,
            'model_top5':model['top_k_label_mean_pct'] if eligible else None,
            'old_top5':old['top_k_label_mean_pct'] if eligible else None,
            'momentum_top5':rule['momentum_top5_target_pct'] if eligible else None,
            'equal_weight':rule['equal_weight_target_pct'],
            'model_mse':model['mse'], 'old_mse':old['mse']})
    good = [r for r in rows if r['selection_eligible']]
    return {'days':rows, 'all_days':len(rows), 'selection_days':len(good),
        'insufficient_days':len(rows)-len(good), 'all_samples':sum(r['samples'] for r in rows),
        'selection_only_means':{key:float(np.mean([r[key] for r in good])) if good else None
            for key in ('model_top5','old_top5','momentum_top5','equal_weight')},
        'scope':'top5_means_only_on_identical_eligible_days_all_dates_retained_not_portfolio_return'}


def run(root, output, donor):
    from . import research_product as product
    output = Path(output).resolve()
    parent, candidate, parent_result = product.read_build(output, pointer_name='research-candidate.json')
    _, previous, _ = product.read_build(output)
    config = read_json(parent/'configuration.json')[0]
    base = Path(root)/config['sources']['receipts']['path']
    observed, recovery = recover(base, donor)
    if recovery['base_manifest_id'] != config['sources']['receipts']['manifest_id']:
        raise ValueError('parent build receipt binding differs')
    folder = output/'price-studies'/uuid.uuid4().hex
    (folder/'dataset').mkdir(parents=True)
    parent_reg = research.read_registration(parent/'experiment')
    original = pd.read_parquet(parent/'dataset/features.parquet', columns=['datetime','instrument'])
    frame = price_features(observed, original.datetime.min())
    if frame[['datetime','instrument']].values.tolist() != original[['datetime','instrument']].values.tolist():
        raise ValueError('fixed cohort identity changed before label filtering')
    write_json(folder/'recovery.json', recovery)
    write_json(folder/'registration.json', {'parent_build':str(parent),'parent_registration_id':parent_reg['registration_id'],
        'recovery_id':identity(recovery),'registered_at':now_utc().isoformat(),
        'source_sha256':file_hash(__file__),'no_promotion':True,'top_k':5})
    frame.to_parquet(folder/'dataset/features.parquet', index=False)
    meta = {'feature_columns':ds.BASE,'label_version':'exploratory_price_target_v1_not_formal_execution_labels',
        'label_definition':ds.LABEL,'availability_assumption':ds.AVAILABILITY,
        'artifact_hashes':{'features.parquet':file_hash(folder/'dataset/features.parquet')},
        'price_sessions':21,'parent_calendar_unchanged':True,'recovery_id':identity(recovery),
        'point_in_time_qualified':False,'execution_ready':False}
    write_json(folder/'dataset/features.metadata.json',meta)
    plan = dict(parent_reg['plan'], variants={'price_baseline':ds.BASE}, feature_roles={}, experiment_id='experiment', evaluation_asof=now_utc().isoformat())
    research.register_experiment(folder,folder/'dataset/features.parquet',plan)
    result = research.run_experiment(folder/'experiment')
    if ([f['partition'] for f in result['folds']] != [f['partition'] for f in parent_result['folds']]
            or result['excluded_tail'] != parent_result['excluded_tail']):
        raise ValueError('evaluation dates or holdout changed')
    rules = research_baselines.build(folder,result)
    old = research_recent.compare_previous(folder,previous,config,result,plan)
    daily = read_json(folder/'experiment/run/daily_metrics.json')[0]['price_baseline']
    selection = summarize_selection(daily,old['old_daily_metrics'],rules['daily'])
    current = product.predict(pd.DataFrame(observed['rows']),observed['calendar']['SSE'],candidate)
    nonempty = int(current.prediction.notna().sum())
    prior = product.read_prediction(output)
    readiness = research_recent.publication_readiness(daily,5,nonempty,prior['predictions'])
    value = {'study_id':folder.name,'scope':'independent_price21_not_Alpha158_common_cohort',
        'recovery':recovery,'selection':selection,'aggregate':result['aggregate'],
        'previous_model':old['old_frozen_model'],'paired_old_minus_recent':old['paired_old_minus_recent'],
        'readiness':readiness,'holdout':result['excluded_tail'],'holdout_scored':False,
        'candidate_inference':{'model_id':candidate['model_id'],'date':observed['calendar']['SSE'][-1],
            'nonempty':nonempty,'rows':[{'instrument':r['instrument'],'prediction':float(r['prediction']) if pd.notna(r['prediction']) else None} for r in current.to_dict('records')]},
        'active_model_changed':False,'execution_ready':False}
    from . import research_campaign as campaign
    if identity(campaign.sealed(base))!=recovery['base_manifest_id'] or identity(campaign.sealed(donor))!=recovery['donor_manifest_id']:
        raise ValueError('receipt batches changed during study')
    write_json(folder/'result.json',value)
    # Bind the evidence needed to reproduce the study, without volatile MLflow internals.
    hashes = {str(p.relative_to(folder)):file_hash(p) for p in folder.rglob('*') if p.is_file() and 'mlruns' not in p.parts}
    write_json(folder/'completed.json',{'artifact_hashes':hashes,'execution_ready':False})
    product.publish_state(output,'price-study-current.json',{'folder':str(folder),'sha256':file_hash(folder/'completed.json')})
    return {'folder':str(folder),'samples':selection['all_samples'],'days':selection['all_days'],
        'selection_days':selection['selection_days'],'recovered_factors':recovery['supplied_factors'],
        'candidate_nonempty':nonempty,'active_model_changed':False}


def read(output):
    pointer = Path(output)/'price-study-current.json'
    if not pointer.exists(): return None
    binding = read_json(pointer)[0]
    folder = Path(binding['folder']).resolve()
    if (Path(output).resolve() not in folder.parents or file_hash(folder/'completed.json') != binding['sha256']):
        raise ValueError('price study pointer changed')
    for relative, digest in read_json(folder/'completed.json')[0]['artifact_hashes'].items():
        path = (folder/relative).resolve()
        if folder not in path.parents or file_hash(path) != digest:
            raise ValueError('price study artifact changed')
    research.read_result(folder/'experiment')
    return read_json(folder/'result.json')[0]
