from copy import deepcopy
from datetime import date, timedelta
import json

import duckdb
import pytest

from trade_system.v2.domain import file_hash
from trade_system.v2.research_semantics import apply, validate


def policy(tmp_path):
    raw=tmp_path/'source.txt';raw.write_text('synthetic evidence, not a provider receipt')
    value={'schema':1,'scope':'retrospective_analyst_transcription_not_PIT_or_execution',
      'received_at':'2026-09-11T05:45:09+00:00','sources':{'test':{'path':'source.txt','sha256':file_hash(raw)}},
      'aliases':[{'old':'300114','new':'302132','effective':'2025-02-17','source':'test'}],
      'suspensions':[{'code':'000801','start':'2024-01-02','resume':'2024-01-05','source':'test'}]}
    path=tmp_path/'policy.json';path.write_text(json.dumps(value));return path,value


@pytest.mark.parametrize('bad',['source','placeholder','future','collision','overlap','unknown_field','missing_source'])
def test_semantic_contract_rejects_invalid_evidence(tmp_path,bad):
    path,value=policy(tmp_path)
    if bad=='source':(tmp_path/'source.txt').write_text('changed')
    if bad=='placeholder':value['suspensions'][0]['resume']='9999-12-31'
    if bad=='future':value['received_at']='2999-01-01T00:00:00Z'
    if bad=='collision':value['aliases'].append(deepcopy(value['aliases'][0]))
    if bad=='overlap':value['suspensions'].append(deepcopy(value['suspensions'][0]))
    if bad=='unknown_field':value['execution_ready']=True
    if bad=='missing_source':value['aliases'][0]['source']='absent'
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError):validate(path)


@pytest.mark.parametrize('bad_volume',[0,None,float('nan'),float('inf')])
def test_semantic_consumption_preserves_rows_raw_target_and_unknown_state(tmp_path,bad_volume):
    path,_=policy(tmp_path)
    db=tmp_path/'source.db'
    days=['2024-01-02','2024-01-03','2024-01-04','2024-01-05','2024-01-08','2024-01-09']
    with duckdb.connect(str(db)) as c:
        c.execute('CREATE TABLE tushare_daily(date DATE,stock_code VARCHAR,volume DOUBLE)')
        for code in ('000801','302132','600000','600001'):
            c.executemany('INSERT INTO tushare_daily VALUES (?,?,?)',[(d,code,bad_volume if code=='600001' and i==2 else 100) for i,d in enumerate(days)])
    before=file_hash(db)
    with duckdb.connect(str(db),read_only=True) as c:
        c.execute('CREATE TEMP TABLE verified_calendar_overlay(cal_date DATE,is_open BOOLEAN)')
        c.executemany('INSERT INTO verified_calendar_overlay VALUES (?,true)',[(d,) for d in days])
        c.execute("CREATE TEMP TABLE all_features AS SELECT date AS datetime,stock_code AS instrument,10.0 AS label_next_ret,'old' AS label_status FROM tushare_daily WHERE date<=DATE '2024-01-05'")
        meta=apply(c,path)
        assert not meta['research_ready'] and not meta['execution_ready']
        assert c.execute('SELECT count(*) FROM semantic_features').fetchone()[0]==16
        assert c.execute('SELECT count(execution_target_ret),count(*) FILTER(WHERE execution_qualified) FROM semantic_features').fetchone()==(0,0)
        assert c.execute("SELECT count(label_next_ret),min(historical_price_target_ret),min(historical_instrument_hint) FROM semantic_features WHERE instrument='302132'").fetchone()==(0,10,'300114')
        # End is exclusive: real resume day no longer inherits this suspension.
        assert c.execute("SELECT label_next_ret,suspended_today FROM semantic_features WHERE instrument='000801' AND datetime='2024-01-05'").fetchone()==(10,False)
        # A positive volume does not grant execution status, and a future zero
        # observation removes the label without deleting the observation row.
        assert c.execute("SELECT count(label_next_ret) FROM semantic_features WHERE instrument='600000'").fetchone()[0]==4
        assert c.execute("SELECT count(label_next_ret) FROM semantic_features WHERE instrument='600001'").fetchone()[0]==1
    assert before==file_hash(db)


def test_old_and_new_code_boundary_never_cross_join_money(tmp_path):
    path,_=policy(tmp_path)
    with duckdb.connect() as c:
        c.execute("CREATE TABLE verified_calendar_overlay AS SELECT d::DATE AS cal_date,true AS is_open FROM (VALUES ('2025-02-13'),('2025-02-14'),('2025-02-17'),('2025-02-18'),('2025-02-19')) t(d)")
        c.execute("CREATE TABLE tushare_daily AS SELECT cal_date AS date,code AS stock_code,100 AS volume FROM verified_calendar_overlay CROSS JOIN (VALUES ('300114'),('302132')) t(code)")
        c.execute("CREATE TEMP TABLE all_features AS SELECT date AS datetime,stock_code AS instrument,10 AS label_next_ret,'old' AS label_status FROM tushare_daily")
        apply(c,path)
        assert c.execute("SELECT identity_conflict FROM semantic_features WHERE instrument='300114' AND datetime='2025-02-13'").fetchone()[0]
        assert not c.execute("SELECT identity_conflict FROM semantic_features WHERE instrument='302132' AND datetime='2025-02-17'").fetchone()[0]
        assert c.execute("SELECT historical_instrument_hint FROM semantic_features WHERE instrument='300114' AND datetime='2025-02-17'").fetchone()[0]=='300114'


@pytest.mark.parametrize('code,start,resume',[
    ('000657','2023-12-26','2024-01-10'),
    ('603958','2024-01-02','2024-01-16'),
    ('002931','2024-01-30','2024-02-06'),
    ('600282','2024-01-12','2024-01-15'),
    ('600759','2024-01-12','2024-01-15'),
    ('603955','2024-01-15','2024-01-18'),
    ('600729','2024-02-02','2024-02-06'),
])
def test_supplemented_boundaries_exclude_target_path_but_not_resume(tmp_path,code,start,resume):
    # Synthetic calendar/evidence tests the consumer only, not these issuers.
    path,value=policy(tmp_path)
    value['aliases']=[]
    value['suspensions']=[{'code':code,'start':start,'resume':resume,'source':'test'}]
    path.write_text(json.dumps(value))
    first=date.fromisoformat(start)-timedelta(days=6)
    end=date.fromisoformat(resume)+timedelta(days=6)
    days=[first+timedelta(days=i) for i in range((end-first).days+1)
          if (first+timedelta(days=i)).weekday()<5]
    with duckdb.connect() as c:
        c.execute('CREATE TABLE verified_calendar_overlay(cal_date DATE,is_open BOOLEAN)')
        c.executemany('INSERT INTO verified_calendar_overlay VALUES (?,true)',[(d,) for d in days])
        c.execute('CREATE TABLE tushare_daily(date DATE,stock_code VARCHAR,volume DOUBLE)')
        c.executemany('INSERT INTO tushare_daily VALUES (?,?,100)',[(d,code) for d in days])
        c.execute("CREATE TEMP TABLE all_features AS SELECT date AS datetime,stock_code AS instrument,10.0 AS label_next_ret,'old' AS label_status FROM tushare_daily WHERE date<=?", [days[-3]])
        apply(c,path)
        rows=c.execute('SELECT datetime,label_next_ret,historical_price_target_ret,label_status,execution_target_ret,execution_qualified FROM semantic_features ORDER BY datetime').fetchall()
        assert len(rows)==len(days)-2
        for i,(day,label,raw,status,execution,qualified) in enumerate(rows):
            suspended=any(start<=d.isoformat()<resume for d in days[i:i+3])
            assert (label is None)==suspended
            assert (status=='documented_suspension_in_target_path')==suspended
            assert raw==10 and execution is None and qualified is False
        assert c.execute('SELECT label_next_ret,suspended_today FROM semantic_features WHERE datetime=?',[resume]).fetchone()==(10,False)


# Current identity/unit/export contracts retained after fixed incident runners exit.
from trade_system.v2 import research_semantics as price_semantics
from scripts.export_qlib_features import assert_unique_research_keys, export_features
from trade_system.v2.gap_evidence import write_json


def sample():
    original = dict(zip(price_semantics.OBSERVED_PRICE_FIELDS, ['SZ.302132', '302132', '2024-01-02',
        '10', '12', '9', '11', '1', '10', 'hands', 'thousand_yuan', 'none', 'tushare', '2026-09-11']))
    values = dict(open='10', high='12', low='9', close='11', volume_shares='10000', turnover_cny='100000')
    evidence = [{'provider': p, 'request_code': '302132.SZ', 'date': '2024-01-02', 'values': deepcopy(values)}
                for p in ('hithink_native', 'xiaodefa_relay')]
    return original, evidence

def test_exact_observed_scale_preserves_original_and_disagrees_with_labels():
    original, evidence = sample()
    row = price_semantics.qualify_observed_price(original, evidence)
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
    row = price_semantics.qualify_observed_price(original, evidence)
    assert row['reason'] == reason and row['volume_shares'] is None and row['turnover_cny'] is None

@pytest.mark.parametrize('field,value', [('request_code', '300114.SZ'), ('date', '2024-01-03')])
def test_no_implicit_alias_or_outside_evidence(field, value):
    original, evidence = sample()
    evidence[1][field] = value
    with pytest.raises(ValueError, match='binding'): price_semantics.qualify_observed_price(original, evidence)

def test_relay_rounding_retained_native_priority_and_ambiguous_scale_rejected(monkeypatch):
    original, evidence = sample()
    evidence[1]['values']['turnover_cny'] = '100000.50'
    assert price_semantics.qualify_observed_price(original, evidence)['turnover_cny'] == '100000'
    monkeypatch.setitem(price_semantics.OBSERVED_UNIT_POLICY, 'scales', [10000, 10000])
    assert price_semantics.qualify_observed_price(original, evidence)['status'] == 'unit_unqualified'

def test_exchange_explicit_no_relaxation_of_original_default():
    original = dict(zip(price_semantics.OBSERVED_PRICE_FIELDS, ['SH.600276', '600276', '2025-01-02', '10', '12', '9', '11', '100', '100', 'hands', 'thousand_yuan', 'none', 'tushare', '2026-07-11']))
    evidence = [{'provider': p, 'request_code': '600276.SH', 'date': original['date'],
        'values': {'open': '10', 'high': '12', 'low': '9', 'close': '11', 'volume_shares': '10000', 'turnover_cny': '100000.11'}} for p in ('hithink_native', 'xiaodefa_relay')]
    assert price_semantics.qualify_observed_price(original, evidence)['reason'] == 'stored_code_conflict'
    assert price_semantics.qualify_observed_price(original, evidence, exchange='SH')['status'] == 'unit_unqualified'
    assert price_semantics.qualify_observed_price(original, evidence, exchange='SH', amount_tolerance='0.50')['status'] == 'observed_row_unit_qualified'
    with pytest.raises(ValueError): price_semantics.qualify_observed_price(original, evidence, exchange='BJ')
    with pytest.raises(ValueError): price_semantics.qualify_observed_price(original, evidence, amount_tolerance='1')

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

def test_current_exporter_cannot_import_incident_workflows(tmp_path, monkeypatch):
    import builtins
    from scripts.export_qlib_features import export_features
    original = builtins.__import__
    def guarded(name, *args, **kwargs):
        if name.startswith('tools.incidents'):
            pytest.fail('current export entered a historical incident workflow')
        return original(name, *args, **kwargs)
    monkeypatch.setattr(builtins, '__import__', guarded)
    with pytest.raises(ValueError, match='retired'):
        export_features(tmp_path/'absent.db', tmp_path/'out.csv', canonical_prices='old', price_receipts='old')
    assert not (tmp_path/'absent.db').exists()

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


def _reconciled_rows():
    evidence = [{"provider": provider, "request_code": "000001.SZ", "date": "2025-01-02",
        "values": dict(open="10",high="12",low="9",close="11",volume_shares="10000",
                       turnover_cny="100000.11" if provider == "hithink_native" else "100000")}
        for provider in ("hithink_native","xiaodefa_relay")]
    rows = []
    for ts, volume, amount, stamp in [("000001.SZ",100,100,"2026-07-11"),("SZ.000001",1,10.000011,"2026-08-11")]:
        original = dict(zip(price_semantics.OBSERVED_PRICE_FIELDS,
            [ts,"000001","2025-01-02",10,12,9,11,volume,amount,"hands","thousand_yuan","none","tushare",stamp]))
        rows.append(price_semantics.qualify_observed_price(original,deepcopy(evidence),amount_tolerance="0.50"))
    return rows


def test_canonical_native_authority_not_first_latest_or_rounding_variant(tmp_path):
    report = {"rows": _reconciled_rows()}
    first = price_semantics.resolve_observed_prices(report['rows'])
    assert first == price_semantics.resolve_observed_prices(list(reversed(report['rows'])))
    assert len(first) == 1 and first[0]['source_variant_count'] == 2
    assert first[0]['values']['turnover_cny'] == '100000.11'
    assert len(first[0]['source_variants']) == 2 and not first[0]['identity_qualified']

@pytest.mark.parametrize('conflict', ['unqualified', 'native', 'variants', 'missing_native'])
def test_canonical_conflict_quarantines_whole_group(tmp_path, conflict):
    report = {"rows": _reconciled_rows()}
    rows = report['rows']
    if conflict == 'unqualified': rows[0]['status'] = 'unit_unqualified'
    if conflict == 'native':
        from copy import deepcopy
        rows[0]['evidence'] = deepcopy(rows[0]['evidence'])
        rows[0]['evidence'][0]['values']['turnover_cny'] = '100001'
    if conflict == 'variants': rows[0]['turnover_cny'] = '100002'
    if conflict == 'missing_native':
        for row in rows: row['evidence'] = [e for e in row['evidence'] if e['provider']!='hithink_native']
    resolved = price_semantics.resolve_observed_prices(rows)
    assert len(resolved) == 1 and resolved[0]['status'] == 'quarantined'
    assert resolved[0]['values'] is None and len(resolved[0]['source_variants']) == 2
