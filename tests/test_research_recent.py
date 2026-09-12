from copy import deepcopy

import numpy as np
import pandas as pd
import pytest

from trade_system.v2 import research_campaign as campaign
from trade_system.v2 import research_recent as recent, research_dataset as ds
from trade_system.v2.domain import identity
from tests.test_research_delivery import prices, configuration


POLICY = {'train_sessions':60, 'valid_sessions':15, 'variant':'price_baseline'}


@pytest.mark.parametrize('counts,new,old,ready', [([6,8],40,40,True),([3,4],40,40,False),([6,8],39,40,False),([],40,40,False)])
def test_sparse_or_regressing_model_does_not_replace_current(counts,new,old,ready):
    result=recent.publication_readiness([{'samples':n} for n in counts],5,new,old)
    assert result['structurally_eligible'] is ready
    assert not result['can_replace_current_model']
    assert not result['execution_ready']


def training_frame():
    source, days = prices(181)
    frame = ds.features(source, days)
    frame['label_next_ret'] = 1.0
    frame['label_end_time'] = (frame.datetime + pd.Timedelta(days=4)).dt.tz_localize('Asia/Shanghai').dt.tz_convert('UTC')
    frame['label_available_time'] = frame.label_end_time
    frame['datetime'] = frame.datetime.dt.strftime('%Y-%m-%d')
    return frame, days


def test_final_refit_keeps_holdout_and_purges_label_boundary():
    frame, days = training_frame()
    frames, report = recent.final_frames(frame, days[-20], POLICY)
    assert not report['holdout_used_for_fit']
    assert frames['train'].datetime.max() < frames['valid'].datetime.min()
    assert frames['valid'].datetime.max() < days[-20]
    assert frames['train'].label_available_time.max() < pd.Timestamp(frames['valid'].datetime.min(), tz='Asia/Shanghai')
    assert frames['valid'].label_available_time.max() < pd.Timestamp(days[-20], tz='Asia/Shanghai')
    changed = frame.copy()
    changed.loc[changed.datetime >= days[-20], ds.BASE + ['label_next_ret']] = 999999
    again, _ = recent.final_frames(changed, days[-20], POLICY)
    for name in frames:
        pd.testing.assert_frame_equal(frames[name], again[name])


@pytest.mark.parametrize('change', ['short', 'empty', 'missing', 'policy'])
def test_refit_refuses_unusable_input_instead_of_filling(change):
    frame, days = training_frame()
    policy = deepcopy(POLICY)
    if change == 'short':
        frame = frame[frame.datetime >= days[-60]]
    elif change == 'empty':
        frame['label_next_ret'] = np.nan
    elif change == 'missing':
        frame[ds.BASE] = np.nan
    else:
        policy['variant'] = 'test_winner'
    with pytest.raises(ValueError):
        recent.final_frames(frame, days[-20], policy)


def receipt_fixture(monkeypatch):
    frame, days = prices(181)
    config = configuration()
    config.update(start=days[0], end=days[-1], sources={'receipts':{'path':'receipts','manifest_id':identity({'one':'hash'})}})
    capture = {'codes':['000001.SZ','600001.SH'], 'start':days[0], 'end':days[-1]}
    reg = {'origin':'native_and_relay', 'config':capture}
    observed = {'calendar':{'SSE':days, 'SZSE':days.copy()}, 'rows':frame.to_dict('records'), 'coverage':{'security_days':len(frame)}}
    monkeypatch.setattr(campaign, 'replay', lambda _: (reg, {'one':'hash'}, {}, []))
    monkeypatch.setattr(campaign, 'derive', lambda _, **kwargs: observed)
    from trade_system.v2 import research_receipts
    monkeypatch.setattr(research_receipts, 'sealed', lambda _: {'one':'hash'})
    return config, reg, observed


def test_raw_receipt_loader_preserves_adjusted_units_and_money(monkeypatch, tmp_path):
    config, _, observed = receipt_fixture(monkeypatch)
    frame, days, summary = recent.load_receipts(config, tmp_path)
    assert frame.iloc[10].net_mf_amount == observed['rows'][10]['net_mf_amount']
    assert frame.iloc[10].close == observed['rows'][10]['close']
    assert len(frame) == len(days)*2
    assert summary['receipt_manifest_id'] == config['sources']['receipts']['manifest_id']


def test_capacity_preflight_exposes_nonselective_pool_without_any_fit(monkeypatch, tmp_path):
    config, _, _ = receipt_fixture(monkeypatch)
    report = recent.capacity_preflight(config, tmp_path)
    assert report['provider_requests'] == report['fits'] == 0
    assert not report['execution_ready']
    for family in report['families'].values():
        assert family['test_days'] > 0
        assert family['selection_days'] == 0
        assert family['minimum_cross_section'] <= 2


@pytest.mark.parametrize('change', ['manifest','origin','universe','range','calendar','cohort'])
def test_receipt_bindings_fail_closed(monkeypatch, tmp_path, change):
    config, reg, observed = receipt_fixture(monkeypatch)
    if change == 'manifest': config['sources']['receipts']['manifest_id'] = 'changed'
    elif change == 'origin': reg['origin'] = 'synthetic_fixture'
    elif change == 'universe': reg['config']['codes'] = ['000002.SZ','600001.SH']
    elif change == 'range': reg['config']['end'] = '2026-09-11'
    elif change == 'calendar': observed['calendar']['SZSE'] = observed['calendar']['SZSE'][:-1]
    else: observed['rows'] = observed['rows'][:-1]
    with pytest.raises(ValueError): recent.load_receipts(config, tmp_path)


def test_recent_capture_stays_within_approved_request_budget():
    from pathlib import Path
    from trade_system.v2.gap_evidence import read_json
    config = read_json(Path(__file__).resolve().parents[1]/'config/research_recent_capture.json')[0]
    assert len(campaign.plan(config)) == 562
    assert config['max_requests'] == 600
    assert len(config['codes']) == 43


def test_recent_dataset_target_uses_exact_two_sessions_and_keeps_tail(tmp_path, monkeypatch):
    from trade_system.v2 import alpha158_research
    config, _, observed = receipt_fixture(monkeypatch)
    def alpha(frame, days, output, input_units):
        assert input_units == 'adjusted_shares_CNY'
        result = frame[['datetime','instrument']].copy()
        result['datetime'] = pd.to_datetime(result.datetime)
        result['alpha_test'] = 1.0
        return result, {'alpha_test':'synthetic_test_expression'}
    monkeypatch.setattr(alpha158_research, 'compute', alpha)
    meta = recent.build_dataset(config, tmp_path, tmp_path/'dataset')
    assert (tmp_path/'dataset/features.metadata.json').exists()
    assert not (tmp_path/'dataset/dataset.json').exists()
    rows = pd.read_parquet(tmp_path/'dataset/features.parquet')
    assert meta['rows'] == 242
    assert not meta['point_in_time_qualified']
    group = rows[rows.instrument == '000001'].sort_values('datetime')
    expected = (observed['rows'][62]['close']/observed['rows'][61]['open']-1)*100
    assert group.iloc[0].label_next_ret == pytest.approx(expected)
    assert group.tail(2).label_next_ret.isna().all()
    price_rows = pd.read_parquet(tmp_path/'dataset/price-refit.parquet')
    assert len(price_rows) == 322
    assert price_rows.groupby('instrument').tail(2).label_next_ret.isna().all()


def test_independent_price_target_does_not_inherit_alpha_window(tmp_path, monkeypatch):
    from trade_system.v2 import alpha158_research
    config, _, observed = receipt_fixture(monkeypatch)
    observed['rows'][100]['close'] = None
    def alpha(frame, days, output, input_units):
        result = frame[['datetime','instrument']].copy()
        result['datetime'] = pd.to_datetime(result.datetime)
        result['alpha_test'] = 1.0
        return result, {'alpha_test':'synthetic'}
    monkeypatch.setattr(alpha158_research, 'compute', alpha)
    recent.build_dataset(config, tmp_path, tmp_path/'dataset')
    common = pd.read_parquet(tmp_path/'dataset/features.parquet')
    price = pd.read_parquet(tmp_path/'dataset/price-refit.parquet')
    day = pd.Timestamp(observed['rows'][140]['datetime'])
    a = common[(common.instrument=='000001') & (common.datetime==day)].iloc[0]
    b = price[(price.instrument=='000001') & (price.datetime==day)].iloc[0]
    assert pd.isna(a.label_next_ret) and pd.notna(b.label_next_ret)


@pytest.mark.parametrize('invalid', [None, 'identity', 'overlap'])
def test_previous_model_comparison_uses_only_exact_new_test_ids(tmp_path, monkeypatch, invalid):
    import sys
    from types import SimpleNamespace
    from trade_system.v2.domain import file_hash
    from trade_system.v2.gap_evidence import write_json
    frame, days = training_frame()
    (tmp_path/'dataset').mkdir()
    frame.to_parquet(tmp_path/'dataset/features.parquet', index=False)
    folder = tmp_path/'experiment/run/fold-00'
    folder.mkdir(parents=True)
    ids = [[days[90], '000001'], [days[90], '600001']]
    write_json(folder/'test_ids.json', ids)
    write_json(folder.parent/'daily_metrics.json', {'price_baseline':[{'date':days[90], 'mse':0.5}]})
    write_json(tmp_path/'old-model.json', {'fixture':True})
    write_json(tmp_path/'old-prep.json', {'used_features':ds.BASE})
    previous = {'model_id':'old', 'train_end':'2024-01-01', 'validation_end':'2024-02-01',
        'model_path':str(tmp_path/'old-model.json'), 'model_sha256':file_hash(tmp_path/'old-model.json'),
        'preprocessing_path':str(tmp_path/'old-prep.json'), 'preprocessing_sha256':file_hash(tmp_path/'old-prep.json')}
    class Booster:
        def __init__(self, **kwargs): pass
        def predict(self, values, **kwargs):
            assert len(values) == 2
            return np.zeros(2)
    monkeypatch.setitem(sys.modules, 'lightgbm', SimpleNamespace(Booster=Booster))
    result = {'folds':[{'test_identity_sha256':identity(ids)}], 'aggregate':{'price_baseline':{'mse':0.5}}}
    if invalid == 'identity': result['folds'][0]['test_identity_sha256'] = 'changed'
    if invalid == 'overlap': previous['validation_end'] = days[90]
    args = (tmp_path, previous, {'max_rows':20000}, result, {'top_k':5,'round_trip_cost_bps':[0],'seed':42})
    if invalid:
        with pytest.raises(ValueError): recent.compare_previous(*args)
    else:
        comparison = recent.compare_previous(*args)
        assert comparison['old_frozen_model']['samples'] == 2
        assert comparison['test_identity_hashes'] == [identity(ids)]
        assert not comparison['final_refit_scored']
        assert len(comparison['prediction_files']) == 1
