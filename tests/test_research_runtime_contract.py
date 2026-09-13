"""Mandatory offline research lane: actual official expressions and QLib fit."""
import importlib.metadata

import numpy as np
import pandas as pd

from trade_system.v2 import alpha158_research, rolling_research


def test_official_alpha158_qlib_fit_and_frozen_reload(tmp_path, monkeypatch):
    import lightgbm as lgb
    import qlib

    # Match run_experiment's explicit local-only MLflow persistence policy.
    monkeypatch.setenv('MLFLOW_ALLOW_FILE_STORE', 'true')

    assert importlib.metadata.version('pyqlib') == '0.9.7'
    assert lgb.__version__ == '4.6.0'
    days = pd.bdate_range('2025-01-02', periods=90)
    frame = pd.DataFrame([
        {'datetime': day, 'instrument': code, 'open': 10 + i * .1 + shift,
         'close': 10.05 + i * .1 + shift, 'high': 11 + i * .1 + shift,
         'low': 9 + i * .1 + shift, 'volume': 1000 + i * 3,
         'turnover': (1000 + i * 3) * (10.02 + i * .1 + shift)}
        for code, shift in [('000001', 0), ('600001', 2)] for i, day in enumerate(days)])
    factors, expressions = alpha158_research.compute(
        frame, days, tmp_path / 'provider', input_units='adjusted_shares_CNY')
    assert len(expressions) == 158 and len(factors) == 60
    assert factors['ROC5'].notna().all()
    # This is a synthetic execution contract, never investment-effect evidence.
    factors['label_next_ret'] = np.arange(len(factors)) / 100
    factors = factors.sort_values(['datetime', 'instrument'])
    frames = {'train': factors.iloc[:30], 'valid': factors.iloc[30:44], 'test': factors.iloc[44:]}
    qlib.init(provider_uri=str(tmp_path / 'provider'), region='cn', kernels=1,
              exp_manager={'class': 'MLflowExpManager', 'module_path': 'qlib.workflow.expm',
                           'kwargs': {'uri': (tmp_path / 'mlruns').as_uri(), 'default_exp_name': 'contract'}})
    target = tmp_path / 'model'; target.mkdir()
    predicted, prep = rolling_research.fit_qlib(frames, ['ROC5'],
        {'num_boost_round': 3, 'num_threads': 1, 'seed': 42, 'experiment_id': 'contract'}, target)
    test = rolling_research.FoldDataset(frames, prep['used_features'], fitting=False)
    replay = lgb.Booster(model_file=str(target / 'model.txt')).predict(test.prepare('test'), num_threads=1)
    np.testing.assert_allclose(predicted.to_numpy(), replay, rtol=0, atol=0)
    assert len(predicted) == 16 and np.isfinite(replay).all()
