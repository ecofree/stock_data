"""Observation-based walk-forward partitions and honest holdout diagnostics."""
from math import isfinite
import pandas as pd


def rolling_partitions(dates, *, train_observations, valid_observations, test_observations, final_holdout_observations):
    sizes = (train_observations, valid_observations, test_observations, final_holdout_observations)
    if dates != sorted(set(dates)) or any(type(n) is not int or n <= 0 for n in sizes):
        raise ValueError('ordered unique dates and positive observation counts required')
    stop = len(dates) - final_holdout_observations
    if stop < train_observations + valid_observations + test_observations:
        raise ValueError('not enough observations for rolling plus independent final holdout')
    folds = []
    cursor = train_observations
    while cursor + valid_observations + test_observations <= stop:
        folds.append({'train_end': dates[cursor-1], 'valid_start': dates[cursor],
                      'valid_end': dates[cursor+valid_observations-1],
                      'test_start': dates[cursor+valid_observations],
                      'test_end': dates[cursor+valid_observations+test_observations-1]})
        cursor += test_observations
    return {'folds': folds, 'final_holdout': [dates[stop], dates[-1]],
            'label_purge_required': True, 'scope': 'research_only'}


def holdout_diagnostics(predictions, labels, training_labels):
    paired = pd.concat([predictions.rename('prediction'), labels.rename('label')], axis=1).dropna()
    if paired.empty or training_labels.dropna().empty:
        raise ValueError('paired holdout and purged training labels required')
    baseline = float(training_labels.mean())
    mse = float(((paired.prediction-paired.label)**2).mean())
    baseline_mse = float(((baseline-paired.label)**2).mean())
    daily_ic = []
    for _, day in paired.groupby(level='datetime'):
        if day.prediction.nunique()>1 and day.label.nunique()>1:
            daily_ic.append(day.prediction.rank().corr(day.label.rank()))
    ic = float(pd.Series(daily_ic, dtype=float).mean())
    return {'scope':'label_prediction_only_not_portfolio_return', 'paired_samples':len(paired),
            'mse':mse, 'constant_train_mean_mse':baseline_mse,
            'relative_mse_improvement_pct':100*(baseline_mse-mse)/baseline_mse if baseline_mse else None,
            'mean_daily_rank_ic':ic if isfinite(ic) else None,
            'rank_ic_days':len(daily_ic), 'execution_ready':False}
