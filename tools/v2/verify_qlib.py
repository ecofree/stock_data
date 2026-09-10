"""Actual QLib fit/predict smoke test on explicitly synthetic isolated data."""
import argparse
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import pandas as pd
from scripts.train_qlib_shadow import train_shadow
from scripts.predict_qlib_daily import predict_daily


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    out = parser.parse_args().output.resolve()
    out.mkdir(parents=True, exist_ok=False)
    os.chdir(out)
    dates = list(pd.bdate_range('2025-01-02', periods=65).strftime('%Y-%m-%d'))
    rows = []
    for i, day in enumerate(dates[:-2]):
        for j in range(12):
            rows.append({'datetime': day, 'instrument': f'{j+1:06d}', 'f': (i+j)%7,
                         'g': (i*3+j)%11, 'label_next_ret': ((i+j)%7-3)/10,
                         'label_end_time': dates[i+2], 'label_available_time': dates[i+2]+' 16:00:00'})
    features = out / 'synthetic.csv'
    pd.DataFrame(rows).to_csv(features, index=False)
    features.with_suffix('.metadata.json').write_text(json.dumps(
        {'feature_columns':['f','g'], 'label_mode':'t1_exec', 'scope':'synthetic_fixture_only'}), encoding='utf-8')
    db = out / 'fixture.duckdb'
    result = train_shadow(db, features, model_id='fixture-v1', num_boost_round=5)
    prediction = predict_daily(db, features, result['model_file'], model_id='fixture-v1', trade_date=dates[-3])
    assert result['test_rows'] > 0 and prediction['rows'] == 12
    assert result['signal_impact'] == prediction['signal_impact'] == 'disabled'
    report = {'scope':'synthetic_fixture_only', 'fit_predict_passed':True, 'train_rows':result['train_rows'],
              'valid_rows':result['valid_rows'], 'test_rows':result['test_rows'], 'prediction_rows':prediction['rows']}
    (out / 'verification.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps(report))


if __name__ == '__main__':
    main()
