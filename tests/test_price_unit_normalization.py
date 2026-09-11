from copy import deepcopy
from datetime import datetime

import duckdb
import pytest

from tools.v2 import normalize_price_units as units
from tools.v2 import probe_identity_sources as probe
from trade_system.v2.daily_session import CST, seal
from trade_system.v2.domain import file_hash
from trade_system.v2.gap_evidence import read_json, write_json


def sample():
    original = dict(zip(units.FIELDS, ['SZ.302132', '302132', '2024-01-02',
        '10', '12', '9', '11', '1', '10', 'hands', 'thousand_yuan', 'none', 'tushare', '2026-09-11']))
    values = dict(open='10', high='12', low='9', close='11', volume_shares='10000', turnover_cny='100000')
    evidence = [{'provider': p, 'request_code': '302132.SZ', 'date': '2024-01-02', 'values': deepcopy(values)}
                for p in ('hithink_native', 'xiaodefa_relay')]
    return original, evidence


def test_exact_observed_scale_preserves_original_and_disagrees_with_labels():
    original, evidence = sample()
    row = units.qualify(original, evidence)
    assert row['status'] == 'observed_row_unit_qualified'
    assert row['volume_scale'] == row['amount_scale'] == 10000
    assert row['volume_shares'] == '10000' and row['turnover_cny'] == '100000'
    assert row['original'] == original and row['original']['volume_unit'] == 'hands'


@pytest.mark.parametrize('case,reason', [
    ('missing', 'native_and_relay_required'), ('price', 'source_price_conflict'),
    ('volume', 'source_unit_conflict'), ('amount', 'source_unit_conflict'),
    ('scale', 'scale_unknown_or_ambiguous'), ('zero', 'scale_unknown_or_ambiguous'),
    ('adjusted', 'unadjusted_source_required'), ('stored_code', 'stored_code_conflict')])
def test_unknown_or_conflict_never_falls_back_to_raw(case, reason):
    original, evidence = sample()
    if case == 'missing': evidence.pop()
    if case == 'price': evidence[1]['values']['close'] = '11.1'
    if case == 'volume': evidence[1]['values']['volume_shares'] = '9999'
    if case == 'amount': evidence[1]['values']['turnover_cny'] = '100000.51'
    if case == 'scale': original['volume'] = '3'
    if case == 'zero': original['volume'] = '0'
    if case == 'adjusted': original['adjustment'] = 'qfq'
    if case == 'stored_code': original['ts_code'] = '300114.SZ'
    row = units.qualify(original, evidence)
    assert row['reason'] == reason and row['volume_shares'] is None and row['turnover_cny'] is None


@pytest.mark.parametrize('field,value', [('request_code', '300114.SZ'), ('date', '2024-01-03')])
def test_no_implicit_alias_or_outside_evidence(field, value):
    original, evidence = sample()
    evidence[1][field] = value
    with pytest.raises(ValueError, match='binding'): units.qualify(original, evidence)


def test_relay_rounding_retained_native_priority_and_ambiguous_scale_rejected(monkeypatch):
    original, evidence = sample()
    evidence[1]['values']['turnover_cny'] = '100000.50'
    assert units.qualify(original, evidence)['turnover_cny'] == '100000'
    monkeypatch.setitem(units.POLICY, 'scales', [10000, 10000])
    assert units.qualify(original, evidence)['status'] == 'unit_unqualified'


def fixture_package(tmp_path):
    class Fixture:
        def query(self, r):
            day = r['start']
            if r['provider'] == 'hithink_native':
                return {'item': [{'date_ms': int(datetime.fromisoformat(day).replace(tzinfo=CST).timestamp()*1000),
                    'open_price': 10, 'high_price': 12, 'low_price': 9, 'close_price': 11,
                    'volume': 10000, 'turnover': 100000}]}
            values = [10, 12, 9, 11, 100, 100] if r['api'] == 'daily' else [2] if r['api'] == 'adj_factor' else [1, 2, 3, 4, 5]
            return {'fields': probe.API_FIELDS[r['api']], 'items': [[r['code'], day.replace('-', ''), *values]]}
    receipts = tmp_path/'receipts'
    probe.capture(receipts, Fixture())
    db = tmp_path/'source.duckdb'
    with duckdb.connect(str(db)) as con:
        con.execute('CREATE TABLE tushare_daily('+','.join(f'{f} VARCHAR' for f in units.FIELDS)+')')
        original, _ = sample()
        con.execute('INSERT INTO tushare_daily VALUES ('+','.join('?' for _ in units.FIELDS)+')', list(original.values()))
        original['date'] = '2024-01-03'
        con.execute('INSERT INTO tushare_daily VALUES ('+','.join('?' for _ in units.FIELDS)+')', list(original.values()))
    return receipts, db


def reseal(folder):
    (folder/'completed.json').unlink()
    seal(folder)


def replace_fixture(path, value):
    path.unlink()
    write_json(path, value)


def test_fixture_origin_rejected(tmp_path):
    receipts, db = fixture_package(tmp_path)
    with pytest.raises(ValueError, match='synthetic'): units.build(receipts, db, tmp_path/'layer')
    assert not (tmp_path/'layer').exists()


def test_real_path_replay_tamper_readonly_and_null_join(tmp_path):
    receipts, db = fixture_package(tmp_path)
    # Test-only declared origin exercises the real receipt replay path, not market evidence.
    reg = read_json(receipts/'registration.json')[0]
    reg['origin'] = 'native_and_relay'
    replace_fixture(receipts/'registration.json', reg)
    reseal(receipts)
    before = file_hash(db)
    layer = tmp_path/'layer'
    assert units.build(receipts, db, layer)['qualified_rows'] == 1
    result = units.verify(layer, receipts, db)
    assert result['unqualified_or_out_of_scope_rows'] == 1
    assert all(result[k] is False for k in ('research_ready', 'execution_ready', 'identity_qualified', 'point_in_time_qualified', 'global_units_qualified'))
    with duckdb.connect(str(db), read_only=True) as con:
        units.apply(con, layer, receipts, db)
        rows = con.execute('SELECT p.date,n.volume_shares FROM tushare_daily p LEFT JOIN verified_price_units n ON p.stock_code=n.stock_code AND p.date::DATE=n.date ORDER BY p.date').fetchall()
        assert rows == [('2024-01-02', 10000.0), ('2024-01-03', None)]
    assert file_hash(db) == before
    with duckdb.connect(':memory:') as con:
        with pytest.raises(ValueError, match='exact bound'): units.apply(con, layer, receipts, db)
    with pytest.raises(ValueError, match='new output'): units.build(receipts, db, layer)
    result['rows'][0]['volume_shares'] = '999'
    replace_fixture(layer/'normalized-prices.json', result)
    reseal(layer)
    with pytest.raises(ValueError, match='replay'): units.verify(layer, receipts, db)


def test_source_duplicate_and_receipt_tamper_rejected(tmp_path):
    receipts, db = fixture_package(tmp_path)
    reg = read_json(receipts/'registration.json')[0]
    reg['origin'] = 'native_and_relay'
    replace_fixture(receipts/'registration.json', reg)
    reseal(receipts)
    with duckdb.connect(str(db)) as con:
        con.execute("INSERT INTO tushare_daily SELECT * FROM tushare_daily WHERE date='2024-01-02'")
    with pytest.raises(ValueError, match='duplicate'): units.payload(receipts, db)
    replace_fixture(receipts/'status-00.json', {})
    with pytest.raises(ValueError, match='package changed'): units.payload(receipts, db)


@pytest.mark.parametrize('change', ['source', 'echo', 'time'])
def test_changed_source_or_resealed_invalid_receipt_cannot_reuse_layer(tmp_path, change):
    receipts, db = fixture_package(tmp_path)
    reg = read_json(receipts/'registration.json')[0]
    reg['origin'] = 'native_and_relay'
    replace_fixture(receipts/'registration.json', reg)
    reseal(receipts)
    layer = tmp_path/'layer'
    units.build(receipts, db, layer)
    if change == 'source':
        with duckdb.connect(str(db)) as con:
            con.execute("UPDATE tushare_daily SET volume='2' WHERE date='2024-01-02'")
    else:
        path = receipts/'receipt-07.json'
        receipt = read_json(path)[0]
        if change == 'echo': receipt['data']['items'][0][0] = '300114.SZ'
        else: receipt['received_at'] = '2099-01-01T00:00:00+00:00'
        replace_fixture(path, receipt)
        reseal(receipts)
    with pytest.raises(ValueError): units.verify(layer, receipts, db)
