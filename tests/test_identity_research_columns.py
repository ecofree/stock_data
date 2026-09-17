from copy import deepcopy
from datetime import date, timedelta

import duckdb
import pytest

from scripts.export_qlib_features import export_features
from tools.incidents import identity_research_columns as p
from trade_system.v2.domain import identity


def fixture(missing=(), blocked=(), windows=None):
    days = [(date(2024, 1, 2)+timedelta(days=i)).isoformat() for i in range(10)]
    windows = windows or [(days[0], days[-1])]
    rows = []
    for day in days:
        bounds = [w for w in windows if w[0] <= day <= w[1]]
        if day in missing or not bounds: continue
        row = {'entity_key': 'synthetic', 'original_price_code': '302132', 'date': day,
            'observation_window': bounds[0], 'effective_code': '300114.SZ',
            'candidate_eligible': day not in blocked,
            'candidate_money_cny': None if day in blocked else dict(net_mf_amount='-100',
                buy_lg_amount='50', sell_lg_amount='10', buy_elg_amount='60', sell_elg_amount='20'),
            'money_source': {'received_at': '2026-09-11T00:00:00+00:00'}}
        row['record_id'] = identity(row); rows.append(row)
    con = duckdb.connect(':memory:')
    con.execute('CREATE TABLE semantic_features(datetime DATE,instrument VARCHAR,label_next_ret DOUBLE,net_mf_amount DOUBLE)')
    con.executemany('INSERT INTO semantic_features VALUES (?, ?, NULL, NULL)', [(d, '302132') for d in days])
    con.execute('CREATE TABLE verified_calendar_overlay(cal_date DATE,is_open BOOLEAN)')
    con.executemany('INSERT INTO verified_calendar_overlay VALUES (?,true)', [(d,) for d in days])
    return con, {'rows': rows, 'database_sha256': 'synthetic'}, days


def test_columns_preserve_originals_and_require_five_calendar_rows():
    con, candidate, days = fixture(); original = deepcopy(candidate)
    with con:
        meta = p.apply(con, candidate, start=days[0], end=days[-1])
        assert con.execute('SELECT datetime,instrument,label_next_ret,net_mf_amount FROM candidate_features EXCEPT ALL SELECT * FROM semantic_features').fetchall() == []
        sums = con.execute('SELECT candidate_identity_moneyflow_5d_cny FROM candidate_features ORDER BY datetime').fetchall()
        assert sums == [(None,)]*4+[(-500.0,)]*6
        assert meta['output_status_counts'] == {'candidate_only': 10}
        assert not meta['labels_changed'] and not meta['research_ready']
        assert candidate == original


@pytest.mark.parametrize('mode', ['missing', 'blocked'])
def test_missing_or_blocked_session_is_not_skipped_by_rolling_window(mode):
    con, candidate, days = fixture(**{mode: ('2024-01-04',)})
    with con:
        p.apply(con, candidate, start=days[0], end=days[-1])
        sums = con.execute('SELECT candidate_identity_moneyflow_5d_cny FROM candidate_features ORDER BY datetime').fetchall()
        assert sums == [(None,)]*7+[(-500.0,)]*3


def test_observation_windows_reset_warmup():
    con, candidate, days = fixture(windows=[('2024-01-02', '2024-01-06'), ('2024-01-07', '2024-01-11')])
    with con:
        p.apply(con, candidate, start=days[0], end=days[-1])
        sums = con.execute('SELECT candidate_identity_moneyflow_5d_cny FROM candidate_features ORDER BY datetime').fetchall()
        assert sums == ([(None,)]*4+[(-500.0,)])*2


@pytest.mark.parametrize('mode', ['hash', 'duplicate', 'calendar', 'year', 'eligibility'])
def test_invalid_candidate_consumer_inputs_fail(mode):
    con, candidate, days = fixture()
    if mode == 'hash': candidate['rows'][0]['effective_code'] = 'wrong'
    if mode == 'duplicate': candidate['rows'].append(candidate['rows'][0])
    if mode == 'calendar': con.execute('DELETE FROM verified_calendar_overlay WHERE cal_date=?', [days[0]])
    if mode == 'year': days[0] = '2025-01-02'; days[-1] = '2025-01-11'
    if mode == 'eligibility':
        row = candidate['rows'][0]; row['candidate_eligible'] = False
        row['record_id'] = identity({k: v for k, v in row.items() if k != 'record_id'})
    with con, pytest.raises(ValueError): p.apply(con, candidate, start=days[0], end=days[-1])


def test_export_requires_all_candidate_dependencies_before_opening_db(tmp_path):
    with pytest.raises(ValueError, match='candidate requires'):
        export_features(tmp_path/'absent.db', tmp_path/'out.csv', identity_candidate=tmp_path/'candidate')


@pytest.mark.parametrize('complete', [False, True])
def test_current_export_never_loads_historical_candidate(tmp_path, monkeypatch, complete):
    import builtins
    original = builtins.__import__
    def guarded(name, *args, **kwargs):
        if name.startswith('tools.incidents'):
            pytest.fail('current export entered historical workflow')
        return original(name, *args, **kwargs)
    monkeypatch.setattr(builtins, '__import__', guarded)
    options = {'identity_candidate': 'old'}
    if complete:
        options.update(identity_price_layer='old', identity_receipts='old',
                       semantics='old', calendar_overlay='old', research_overlay='old')
    with pytest.raises(ValueError, match='explicit historical maintenance'):
        export_features(tmp_path/'absent.db', tmp_path/'out.csv', **options)
    assert not (tmp_path/'absent.db').exists() and not (tmp_path/'out.csv').exists()


@pytest.mark.parametrize('changed', [False, True])
def test_explicit_incident_export_retains_columns_and_rejects_changed_evidence(tmp_path, monkeypatch, changed):
    from pathlib import Path
    from tools.incidents import build_identity_candidate as build
    from scripts import export_qlib_features as current
    from trade_system.v2.gap_evidence import read_json
    con, candidate, days = fixture()
    with con:
        base = tmp_path/'source.parquet'
        con.execute("COPY semantic_features TO '"+str(base).replace("'", "''")+"' (FORMAT PARQUET)")
    db = tmp_path/'source.db'
    with duckdb.connect(str(db)):
        pass
    calls = []
    def verify(*args):
        calls.append(1)
        return dict(candidate, changed=True) if changed and len(calls)>1 else candidate
    monkeypatch.setattr(build, 'verify', verify)
    def baseline(db, output, **kwargs):
        folder = Path(output).parent/'parts'; folder.mkdir()
        (folder/'part.parquet').write_bytes(base.read_bytes())
        return {'outputs': {'parquet': str(folder)}, 'start_date': days[0], 'end_date': days[-1],
                'feature_columns': ['net_mf_amount'], 'label_column': 'label_next_ret'}
    monkeypatch.setattr(current, 'export_features', baseline)
    def calendar(con, folder):
        con.execute('CREATE TEMP TABLE verified_calendar_overlay(cal_date DATE,is_open BOOLEAN)')
        con.executemany('INSERT INTO verified_calendar_overlay VALUES (?,true)', [(d,) for d in days])
    monkeypatch.setattr(current, 'apply_calendar_overlay', calendar)
    output = tmp_path/'result'
    inputs = {k:tmp_path/k for k in ('candidate', 'layer', 'receipts', 'policy', 'calendar', 'research_overlay')}
    if changed:
        with pytest.raises(ValueError, match='evidence changed'):
            p.export(db, output, **inputs)
        assert not output.exists()
    else:
        result = p.export(db, output, **inputs)
        assert result['feature_columns'] == ['net_mf_amount']
        assert result['candidate_feature_columns'] == p.FEATURES
        with duckdb.connect(':memory:') as check:
            values = check.execute('SELECT net_mf_amount,label_next_ret,candidate_identity_moneyflow_5d_cny FROM read_parquet(?) ORDER BY datetime', [str(output/'features.parquet')]).fetchall()
        assert values == [(None,None,None)]*4 + [(None,None,-500.0)]*6
        assert read_json(output/'features.metadata.json')[0]['execution_ready'] is False
    assert len(calls) == 2
    assert not list(tmp_path.glob('identity-export-*'))
