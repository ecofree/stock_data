from copy import deepcopy
from datetime import date, timedelta

import duckdb
import pytest

from scripts.export_qlib_features import export_features
from tools.v2 import identity_research_columns as p
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
