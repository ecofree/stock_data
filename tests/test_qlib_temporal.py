import duckdb
import pandas as pd
import pytest

from scripts import export_qlib_features as exporter
from scripts.train_qlib_shadow import QlibFrameDataset, _load_features
from trade_system.ml.feature_artifacts import resolve_feature_path


def source(tmp_path, *, partial_adjustment=False):
    path = tmp_path / 'research.duckdb'
    dates = list(pd.bdate_range('2025-12-01', periods=100).strftime('%Y-%m-%d'))
    dates = dates[:35] + dates[50:]  # holiday/data gap longer than calendar padding
    with duckdb.connect(str(path)) as con:
        con.execute('CREATE TABLE tushare_daily(date DATE,stock_code VARCHAR,open DOUBLE,high DOUBLE,low DOUBLE,close DOUBLE,volume DOUBLE,turnover DOUBLE)')
        con.execute('CREATE TABLE tushare_daily_basic(date DATE,stock_code VARCHAR,turnover_rate DOUBLE,volume_ratio DOUBLE,pe DOUBLE,pb DOUBLE,total_mv DOUBLE,circ_mv DOUBLE)')
        con.execute('CREATE TABLE tushare_moneyflow(date DATE,stock_code VARCHAR,buy_lg_amount DOUBLE,sell_lg_amount DOUBLE,buy_elg_amount DOUBLE,sell_elg_amount DOUBLE,net_mf_amount DOUBLE)')
        con.executemany("INSERT INTO tushare_daily VALUES (?,'000001',?,?,?,?,?,1000)",
                        [(day, 10+i/10, 11+i/10, 9+i/10, 10+i/10, 100+i*i) for i, day in enumerate(dates)])
        con.executemany("INSERT INTO tushare_daily_basic VALUES (?,'000001',1,1,10,1,100,80)", [(d,) for d in dates])
        con.executemany("INSERT INTO tushare_moneyflow VALUES (?,'000001',10,5,20,10,15)", [(d,) for d in dates])
        if partial_adjustment:
            con.execute('CREATE TABLE tushare_adj_factor(date DATE,stock_code VARCHAR,adj_factor DOUBLE)')
            con.executemany("INSERT INTO tushare_adj_factor VALUES (?,'000001',2)", [(d,) for d in dates if d != dates[1]])
    return path, dates


def test_observation_warmup_and_batch_export_match_full_history(tmp_path):
    db, dates = source(tmp_path)
    full = exporter.export_features(db, tmp_path/'full.csv')
    partial = exporter.export_features(db, tmp_path/'partial.csv', start_date=dates[35], end_date=dates[65])
    full_frame = pd.read_csv(full['outputs']['csv'])
    partial_frame = pd.read_csv(partial['outputs']['csv'])
    expected = full_frame[full_frame.datetime.between(dates[35], dates[65])].reset_index(drop=True)
    pd.testing.assert_frame_equal(expected, partial_frame)
    assert pd.notna(partial_frame.iloc[0].volume_z20)


def test_missing_factor_never_becomes_one_and_codes_keep_leading_zero(tmp_path):
    db, dates = source(tmp_path, partial_adjustment=True)
    out = tmp_path / 'f.csv'
    meta = exporter.export_features(db, out)
    frame = pd.read_csv(resolve_feature_path(out))
    missing = frame[frame.datetime == dates[1]].iloc[0]
    assert pd.isna(missing.close) and pd.isna(missing.label_next_ret)
    loaded = _load_features(out, meta['feature_columns'], 0)
    assert set(loaded.instrument) == {'000001'}


def test_failed_export_preserves_old_complete_bundle(tmp_path, monkeypatch):
    db, dates = source(tmp_path)
    out = tmp_path / 'features.csv'
    exporter.export_features(db, out)
    old = resolve_feature_path(out)
    old_bytes = old.read_bytes()
    def fail(*args, **kwargs):
        raise OSError('injected export failure')
    monkeypatch.setattr(exporter, '_copy_batched', fail)
    with pytest.raises(OSError):
        exporter.export_features(db, out, start_date=dates[5])
    assert resolve_feature_path(out) == old
    assert old.read_bytes() == old_bytes


def test_csv_and_parquet_sample_the_same_cross_section(tmp_path):
    db, dates = source(tmp_path)
    out = tmp_path / 'formats.csv'
    meta = exporter.export_features(db, out, output_format='both')
    csv_frame = _load_features(out, meta['feature_columns'], 100)
    parquet_frame = _load_features(out.with_suffix('.parquet'), meta['feature_columns'], 100)
    pd.testing.assert_frame_equal(csv_frame, parquet_frame)


def test_label_boundaries_are_purged_and_holdout_is_separate():
    dates = ['2026-01-01','2026-01-02','2026-01-03','2026-01-04','2026-01-05','2026-01-06']
    frame = pd.DataFrame({'datetime': dates, 'instrument': ['000001']*6, 'f': range(6),
                          'label_next_ret': range(6), 'label_end_time': dates[2:]+['2026-01-07','2026-01-08'],
                          'label_available_time': dates[2:]+['2026-01-07','2026-01-08']})
    dataset = QlibFrameDataset(frame, ['f'], dates[1], dates[2], dates[3], dates[4], dates[5])
    assert dataset.prepare('train').empty  # both training labels cross validation start
    assert dataset.prepare('valid').empty  # both validation labels cross holdout start
    assert len(dataset.prepare('test')) == 2
    with pytest.raises(ValueError, match='unknown'):
        dataset.prepare('typo')


def test_label_columns_cannot_be_declared_as_features(tmp_path):
    with pytest.raises(ValueError, match='cannot include labels'):
        _load_features(tmp_path/'does-not-exist.csv', ['label_next_ret'], 0)
