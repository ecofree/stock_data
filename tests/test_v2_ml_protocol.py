import pandas as pd
from trade_system.v2.ml_protocol import rolling_partitions, holdout_diagnostics


def test_rolling_folds_never_use_final_holdout():
    dates = list(pd.bdate_range('2025-01-01', periods=100).strftime('%Y-%m-%d'))
    plan = rolling_partitions(dates, train_observations=30, valid_observations=10, test_observations=10, final_holdout_observations=20)
    assert all(f['train_end'] < f['valid_start'] <= f['valid_end'] < f['test_start'] <= f['test_end'] < plan['final_holdout'][0] for f in plan['folds'])
    assert plan['final_holdout'] == [dates[-20], dates[-1]]


def test_bad_model_improvement_is_negative_not_hidden():
    index = pd.MultiIndex.from_product([['2025-01-01'], ['A','B','C']], names=['datetime','instrument'])
    labels = pd.Series([-1,0,1], index=index)
    result = holdout_diagnostics(pd.Series([100,99,98], index=index), labels, pd.Series([-1,1]))
    assert result['relative_mse_improvement_pct'] < 0
    assert result['mean_daily_rank_ic'] == -1
    assert result['execution_ready'] is False
