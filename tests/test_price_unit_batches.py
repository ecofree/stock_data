from datetime import datetime

import duckdb
import pytest

from tools.v2 import probe_price_unit_batches as batch
from tools.v2 import normalize_price_units as units
from trade_system.v2.daily_session import CST, seal
from trade_system.v2.domain import file_hash
from trade_system.v2.gap_evidence import read_json, write_json
from scripts.export_qlib_features import assert_unique_research_keys, export_features


class Fixture:
    def query(self, request):
        day = request['start']
        if request['provider'] == 'hithink_native':
            return {'item': [{'date_ms': int(datetime.fromisoformat(day).replace(tzinfo=CST).timestamp()*1000),
                'open_price': 10, 'high_price': 12, 'low_price': 9, 'close_price': 11,
                'volume': 10000, 'turnover': 100000.11}]}
        return {'fields': batch.probe.API_FIELDS['daily'],
            'items': [[request['code'], day.replace('-', ''), 10, 12, 9, 11, 100, 100]]}


def fixtures(tmp_path, *, declared_native=False, calendar=False):
    db = tmp_path/'source.db'
    with duckdb.connect(str(db)) as con:
        if calendar:
            con.execute('CREATE TABLE tushare_trade_cal(exchange VARCHAR,cal_date DATE,is_open BOOLEAN)')
            con.execute("INSERT INTO tushare_trade_cal VALUES ('SSE','2025-01-02',TRUE)")
        con.execute('CREATE TABLE tushare_daily('+','.join(f'{f} '+('DOUBLE' if f in ('open', 'high', 'low', 'close', 'volume', 'turnover') else 'VARCHAR') for f in units.FIELDS)+')')
        for ts, volume, amount, stamp in [('000001.SZ', 100, 100, '2026-07-11'), ('SZ.000001', 1, 10.000011, '2026-08-11')]:
            values = [ts, '000001', '2025-01-02', 10, 12, 9, 11, volume, amount, 'hands', 'thousand_yuan', 'none', 'tushare', stamp]
            con.execute('INSERT INTO tushare_daily VALUES ('+','.join('?' for _ in values)+')', values)
    receipts = tmp_path/'receipts'
    batch.capture(db, receipts, client=Fixture())
    if declared_native:
        # Synthetic values only: exercise replay path, never report market acceptance.
        reg = read_json(receipts/'registration.json')[0]
        reg['origin'] = 'native_and_relay'
        replace(receipts/'registration.json', reg)
        reseal(receipts)
    return db, receipts


def replace(path, value):
    path.unlink()
    write_json(path, value)


def reseal(folder):
    (folder/'completed.json').unlink()
    seal(folder)


def test_plan_fixed_budget_and_native_first():
    requests = batch.plan()
    assert len(requests) == 14 and len(batch.SAMPLES) == 7
    assert all(r['provider'] == 'hithink_native' for r in requests[:7])
    assert len({r['code'] for r in requests}) == 4
    assert all(r['params']['adjust'] == 'none' for r in requests[:7])


def test_fixture_refused_but_receipts_replayable(tmp_path):
    db, receipts = fixtures(tmp_path)
    assert len(batch.replay(receipts)[3]) == 14
    with pytest.raises(ValueError, match='synthetic'): batch.payload(receipts, db)


def test_two_unit_regimes_preserved_no_duplicate_merge(tmp_path):
    db, receipts = fixtures(tmp_path, declared_native=True)
    before = file_hash(db)
    output = tmp_path/'analysis'
    result = batch.analyze(receipts, db, output)
    assert result['qualified_source_rows'] == 2 and result['scoped_security_days'] == 1
    report = read_json(output/'result.json')[0]
    assert [r['volume_scale'] for r in report['rows']] == [100, 10000]
    assert [r['amount_scale'] for r in report['rows']] == [1000, 10000]
    assert report['duplicate_groups'][0]['normalized_values_agree_within_tolerance']
    assert report['canonical_rows_published'] == 0 and not report['merge_authorized']
    assert report['inventory_2025']['excess_rows'] == 1
    assert batch.verify(receipts, db, output)['verified'] and file_hash(db) == before
    report['rows'][0]['volume_scale'] = 10000
    replace(output/'result.json', report)
    reseal(output)
    with pytest.raises(ValueError, match='replay'): batch.verify(receipts, db, output)


@pytest.mark.parametrize('change', ['database', 'time', 'code', 'budget', 'membership'])
def test_capture_binding_and_receipt_replay_fail_closed(tmp_path, change):
    db, receipts = fixtures(tmp_path, declared_native=True)
    if change == 'database':
        with duckdb.connect(str(db)) as con: con.execute('DELETE FROM tushare_daily')
    elif change == 'budget':
        path = receipts/'registration.json'
        reg = read_json(path)[0]
        reg['policy']['max_requests'] = 100
        replace(path, reg)
        reseal(receipts)
    elif change == 'membership':
        write_json(receipts/'extra.json', {})
        reseal(receipts)
    else:
        path = receipts/'receipt-07.json'
        receipt = read_json(path)[0]
        if change == 'time': receipt['received_at'] = '2099-01-01T00:00:00+00:00'
        else: receipt['data']['items'][0][0] = '300114.SZ'
        replace(path, receipt)
        reseal(receipts)
    with pytest.raises(ValueError): batch.payload(receipts, db)


def test_exchange_explicit_no_relaxation_of_original_default():
    original = dict(zip(units.FIELDS, ['SH.600276', '600276', '2025-01-02', '10', '12', '9', '11', '100', '100', 'hands', 'thousand_yuan', 'none', 'tushare', '2026-07-11']))
    evidence = [{'provider': p, 'request_code': '600276.SH', 'date': original['date'],
        'values': {'open': '10', 'high': '12', 'low': '9', 'close': '11', 'volume_shares': '10000', 'turnover_cny': '100000.11'}} for p in ('hithink_native', 'xiaodefa_relay')]
    assert units.qualify(original, evidence)['reason'] == 'stored_code_conflict'
    assert units.qualify(original, evidence, exchange='SH')['status'] == 'unit_unqualified'
    assert units.qualify(original, evidence, exchange='SH', amount_tolerance='0.50')['status'] == 'observed_row_unit_qualified'
    with pytest.raises(ValueError): units.qualify(original, evidence, exchange='BJ')
    with pytest.raises(ValueError): units.qualify(original, evidence, amount_tolerance='1')


def create_key_tables(con):
    for table in ('tushare_daily', 'tushare_daily_basic', 'tushare_moneyflow', 'tushare_adj_factor', 'verified_adjustment', 'verified_moneyflow'):
        con.execute(f'CREATE TABLE {table}(stock_code VARCHAR,date DATE,close DOUBLE)')
    con.execute('CREATE TABLE qlib_stock_flow_features_v2(stock_code VARCHAR,trade_date DATE,close DOUBLE)')


@pytest.mark.parametrize('table', ['tushare_daily', 'tushare_daily_basic', 'tushare_moneyflow', 'tushare_adj_factor', 'qlib_stock_flow_features_v2'])
def test_duplicate_keys_rejected_before_windows_in_all_join_sources(table):
    with duckdb.connect(':memory:') as con:
        create_key_tables(con)
        con.execute(f"INSERT INTO {table} VALUES ('000001','2025-01-02',10),('000001','2025-01-02',10)")
        with pytest.raises(ValueError, match='duplicate_research_source_key:'+table):
            assert_unique_research_keys(con, '2025-01-01', '2025-01-03', adjustment_relation='tushare_adj_factor', include_flow=True)
        assert_unique_research_keys(con, '2025-01-03', '2025-01-04', adjustment_relation='tushare_adj_factor', include_flow=True)


def test_duplicate_legacy_overlay_not_consumed_and_invalid_relation_refused():
    with duckdb.connect(':memory:') as con:
        create_key_tables(con)
        con.execute("INSERT INTO tushare_moneyflow VALUES ('000001','2025-01-02',10),('000001','2025-01-02',10)")
        assert_unique_research_keys(con, '2025-01-01', '2025-01-03', money_relation='verified_moneyflow', adjustment_relation='verified_adjustment')
        with pytest.raises(ValueError, match='unsupported'): assert_unique_research_keys(con, '2025-01-01', '2025-01-03', money_relation='evil')


def test_export_duplicate_guard_keeps_previous_pointer_even_outside_requested_day(tmp_path):
    db = tmp_path/'duplicate.db'
    with duckdb.connect(str(db)) as con:
        for table in ('tushare_daily', 'tushare_daily_basic', 'tushare_moneyflow'):
            con.execute(f'CREATE TABLE {table}(stock_code VARCHAR,date DATE,close DOUBLE)')
        con.execute("INSERT INTO tushare_daily VALUES ('000001','2025-01-02',10),('000001','2025-01-02',10),('000001','2025-01-03',10)")
    pointer = tmp_path/'features.current.json'
    write_json(pointer, {'previous': 'retained'})
    before = file_hash(pointer)
    with pytest.raises(ValueError, match='duplicate_research_source_key'):
        export_features(db, tmp_path/'features.csv', start_date='2025-01-03', end_date='2025-01-03')
    assert file_hash(pointer) == before and not (tmp_path/'features.versions').exists()


def test_canonical_native_authority_not_first_latest_or_rounding_variant(tmp_path):
    from tools.v2 import canonical_price_research as canon
    db, receipts = fixtures(tmp_path, declared_native=True)
    report = batch.payload(receipts, db)
    first = canon.resolve(report['rows'])
    assert first == canon.resolve(list(reversed(report['rows'])))
    assert len(first) == 1 and first[0]['source_variant_count'] == 2
    assert first[0]['values']['turnover_cny'] == '100000.11'
    assert len(first[0]['source_variants']) == 2 and not first[0]['identity_qualified']


@pytest.mark.parametrize('conflict', ['unqualified', 'native', 'variants', 'missing_native'])
def test_canonical_conflict_quarantines_whole_group(tmp_path, conflict):
    from tools.v2 import canonical_price_research as canon
    db, receipts = fixtures(tmp_path, declared_native=True)
    report = batch.payload(receipts, db)
    rows = report['rows']
    if conflict == 'unqualified': rows[0]['status'] = 'unit_unqualified'
    if conflict == 'native':
        from copy import deepcopy
        rows[0]['evidence'] = deepcopy(rows[0]['evidence'])
        rows[0]['evidence'][0]['values']['turnover_cny'] = '100001'
    if conflict == 'variants': rows[0]['turnover_cny'] = '100002'
    if conflict == 'missing_native':
        for row in rows: row['evidence'] = [e for e in row['evidence'] if e['provider']!='hithink_native']
    resolved = canon.resolve(rows)
    assert len(resolved) == 1 and resolved[0]['status'] == 'quarantined'
    assert resolved[0]['values'] is None and len(resolved[0]['source_variants']) == 2


def test_canonical_build_export_replay_tamper_and_raw_database_unchanged(tmp_path):
    from tools.v2 import canonical_price_research as canon
    db, receipts = fixtures(tmp_path, declared_native=True, calendar=True)
    before = file_hash(db)
    layer = tmp_path/'canonical'
    assert canon.build(receipts, db, layer)['canonical_rows'] == 1
    report = canon.verify(layer, receipts, db)
    meta = export_features(db, tmp_path/'v7.csv', canonical_prices=layer, price_receipts=receipts, output_format='both')
    assert meta['rows'] == 1 and meta['labeled_rows'] == 0 and meta['feature_columns'] == canon.FEATURES
    assert not meta['legacy_source_fallback'] and 'raw_price_target_ret' not in meta['feature_columns']
    with duckdb.connect(':memory:') as con:
        row = con.execute('SELECT volume,turnover,label_next_ret FROM read_parquet(?)', [meta['outputs']['parquet']]).fetchone()
        assert row == (10000.0,100000.11,None)
    assert file_hash(db) == before
    from trade_system.v2.alpha158_research import export as export_alpha158
    with pytest.raises(ValueError, match='adjusted session-aligned export required'):
        export_alpha158(meta['outputs']['parquet'], db, tmp_path/'not_trained', instruments=2)
    assert not (tmp_path/'not_trained').exists()
    report['records'][0]['values']['turnover_cny'] = '999'
    replace(layer/'canonical-prices.json', report)
    reseal(layer)
    with pytest.raises(ValueError, match='replay'): canon.verify(layer, receipts, db)


def test_canonical_failure_before_pointer_publication(tmp_path, monkeypatch):
    from tools.v2 import canonical_price_research as canon
    db, receipts = fixtures(tmp_path, declared_native=True, calendar=True)
    layer = tmp_path/'canonical'; canon.build(receipts, db, layer)
    pointer = tmp_path/'v7.current.json'; write_json(pointer, {'prior': 'retained'})
    before = file_hash(pointer)
    original = canon.diagnostic_rows
    def changed(con, report):
        result = original(con, report)
        write_json(layer/'unexpected.json', {})
        return result
    monkeypatch.setattr(canon, 'diagnostic_rows', changed)
    with pytest.raises(ValueError): export_features(db, tmp_path/'v7.csv', canonical_prices=layer, price_receipts=receipts)
    assert file_hash(pointer) == before


@pytest.mark.parametrize('extra', [{'research_overlay': 'bad'}, {'semantics': 'bad'}, {'calendar_overlay': 'bad'}, {'label_mode': 'legacy'}, {'price_receipts': None}])
def test_canonical_mode_cannot_silently_mix_old_protocols(tmp_path, extra):
    args = {'canonical_prices': 'layer', 'price_receipts': 'receipts', **extra}
    with pytest.raises(ValueError, match='paired layer/receipts'):
        export_features(tmp_path/'not_opened.db', tmp_path/'out.csv', **args)
    assert not (tmp_path/'not_opened.db').exists()


@pytest.mark.parametrize('condition', ['complete', 'missing_price', 'missing_calendar', 'duplicate_calendar', 'quarantined'])
def test_canonical_exact_session_proxy_never_becomes_training_label(condition):
    from tools.v2 import canonical_price_research as canon
    records = []
    for day in ('2025-01-02','2025-01-03','2025-01-06'):
        records.append({'stock_code': '000001', 'date': day, 'values': {'open': '10','high': '12','low': '9','close': '11','volume_shares': '10000','turnover_cny': '100000'},
            'record_id': day,'status': 'canonical_price_observation','source_variant_count': 2})
    if condition == 'missing_price': records.pop(1)
    if condition == 'quarantined': records[0].update(values=None,status='quarantined')
    with duckdb.connect(':memory:') as con:
        con.execute('CREATE TABLE tushare_trade_cal(exchange VARCHAR,cal_date DATE,is_open BOOLEAN)')
        con.execute("INSERT INTO tushare_trade_cal SELECT 'SSE',d,dayofweek(d) NOT IN (0,6) FROM generate_series(DATE '2025-01-02',DATE '2025-01-06',INTERVAL 1 DAY) t(d)")
        if condition == 'missing_calendar': con.execute("DELETE FROM tushare_trade_cal WHERE cal_date='2025-01-03'")
        if condition == 'duplicate_calendar': con.execute("INSERT INTO tushare_trade_cal VALUES ('SSE','2025-01-03',TRUE)")
        if condition in ('missing_calendar','duplicate_calendar'):
            with pytest.raises(ValueError, match='complete unique'): canon.diagnostic_rows(con, {'records': records})
        else:
            rows = canon.diagnostic_rows(con, {'records': records})
            assert rows[0]['raw_price_target_ret'] == (10.0 if condition=='complete' else None)
            assert all(r['label_next_ret'] is None and r['volume_z20'] is None for r in rows)
            assert len(rows) == len(records)
