"""Ingress counterexamples must survive normalization and freshness gates."""
from contextlib import closing
from datetime import datetime, timedelta
from types import SimpleNamespace
import json
import shutil

import pytest

from collectors.collect_daily import collect_daily
from collectors import collect_market
from trade_system.data_store import DuckDBStore
from trade_system.schema import init_schema
from trade_system.market_contract import MARKET_COUNT_FIELDS, count_evidence
from trade_system.normalize import _create_market_daily, _create_market_state_inputs
from trade_system.readiness import relation_freshness

DAY = '2026-10-12'
NOW = datetime(2026,10,12,16)
COUNTS = dict(zip(MARKET_COUNT_FIELDS, (10,2,3000,1500,5)))


@pytest.fixture(scope='module')
def market_template(tmp_path_factory):
    path=tmp_path_factory.mktemp('market-template')/'seed.duckdb'
    with closing(DuckDBStore(path)) as db:
        init_schema(db.conn)
        db.conn.execute('INSERT INTO daily_summary(date,limit_up_count,limit_down_count,rise_count,fall_count,consecutive_count,source_kind,fetched_at) VALUES (?,?,?,?,?,?,?,?)',
                        [DAY,*COUNTS.values(),'real',NOW-timedelta(seconds=30)])
        proof = {field: count_evidence(value, source='kpl_daily', source_kind='real', source_date=DAY,
                                     fetched_at=NOW-timedelta(seconds=30), received_at=NOW)
                 for field, value in COUNTS.items()}
        db.conn.execute('UPDATE daily_summary SET count_provenance_json=?', [json.dumps(proof)])
    return path


@pytest.fixture
def store(tmp_path, monkeypatch, market_template):
    class Clock(datetime):
        @classmethod
        def now(cls): return NOW
    monkeypatch.setattr(collect_market, 'datetime', Clock)
    path=tmp_path/'market.duckdb'
    shutil.copy2(market_template,path)
    with closing(DuckDBStore(path)) as db:
        yield db


@pytest.mark.parametrize('field', MARKET_COUNT_FIELDS)
@pytest.mark.parametrize('damage', ['missing',None,True,1.5,-1,'bad',float('inf'),2147483648])
def test_incomplete_daily_rejects_without_overwriting_correct_snapshot(store, field, damage):
    data=dict(COUNTS)
    if damage=='missing':data.pop(field)
    else:data[field]=damage
    before=store.fetchall('SELECT * FROM daily_summary')
    assert collect_daily(SimpleNamespace(get=lambda *a:data),store,DAY)==0
    assert store.fetchall('SELECT * FROM daily_summary')==before
    assert store.fetchall("SELECT status FROM _collect_log WHERE table_name='daily_summary'")[0][0].startswith('error:')


@pytest.mark.parametrize('extra', [{'date':'2026-10-11'}, {'trade_date':None}, {'涨停数':99}, {'source_kind':'fallback'}, {'is_fallback':True}])
def test_wrong_date_or_conflicting_alias_is_not_a_success(store, extra):
    before=store.fetchall('SELECT * FROM daily_summary')
    assert collect_daily(SimpleNamespace(get=lambda *a:{**COUNTS,**extra}),store,DAY)==0
    assert store.fetchall('SELECT * FROM daily_summary')==before


def test_explicit_zero_is_distinct_from_missing_and_valid_retry_is_idempotent(store):
    client=SimpleNamespace(get=lambda *a:{k:0 for k in COUNTS})
    for _ in range(2): assert collect_daily(client,store,DAY)==1
    assert store.fetchall('SELECT limit_up_count,limit_down_count,rise_count,fall_count,consecutive_count FROM daily_summary')==[(0,0,0,0,0)]
    _create_market_daily(store.conn)
    assert store.fetchall('SELECT market_daily_status FROM v_market_daily')==[('available',)]


def tick(store):
    client=SimpleNamespace(get=lambda *a:{'raw_data':[[12,3,2,10,20,3,DAY]]})
    assert collect_market.collect_market_rise_fall(client,store,DAY)==1
    _create_market_daily(store.conn)
    _create_market_state_inputs(store.conn)


@pytest.mark.parametrize('kind,age,ready', [('fallback',30,False),('real',31,False),('real',30,True),('real',29,True),('real',-1,False),(None,0,False)])
def test_composite_never_refreshes_older_or_unqualified_constituents(store,kind,age,ready):
    store.conn.execute('INSERT INTO market_mood(date,rise_count,fall_count,source_kind,fetched_at) VALUES (?,?,?,?,?)',
                        [DAY,1,2,kind,NOW-timedelta(seconds=age)])
    for _ in range(2):
        tick(store)
        for relation in ('daily_summary','v_market_daily','v_market_state_inputs'):
            result=relation_freshness(store.conn,relation,DAY,now=NOW,max_age_seconds=30)
            assert (result['status']=='ready') == ready, result
    raw,provenance=store.fetchall('SELECT raw_json,count_provenance_json FROM daily_summary')[0]
    assert len(raw)<8192 and len(provenance)<8192
    evidence=json.loads(provenance)
    assert evidence['consecutive_count']['fetched_at']==(NOW-timedelta(seconds=30)).isoformat(timespec='microseconds')
    assert evidence['rise_count']['source_kind']==kind


def test_transient_bad_mood_does_not_destroy_valid_consecutive_lineage(store):
    store.conn.execute('INSERT INTO market_mood(date,rise_count,fall_count,source_kind,fetched_at) VALUES (?,?,?,?,?)',[DAY,1,2,'fallback',NOW])
    tick(store)
    assert relation_freshness(store.conn,'v_market_daily',DAY,now=NOW)['status']!='ready'
    store.conn.execute("UPDATE market_mood SET source_kind='real'")
    tick(store)
    assert relation_freshness(store.conn,'v_market_daily',DAY,now=NOW,max_age_seconds=30)['status']=='ready'
    assert store.fetchall('SELECT fetched_at FROM daily_summary')==[(NOW-timedelta(seconds=30),)]


@pytest.mark.parametrize('receipt', ['{}','invalid',json.dumps({'consecutive_count':{'value':99}})])
def test_explicit_corrupt_lineage_cannot_borrow_row_real_marker(store,receipt):
    store.conn.execute('UPDATE daily_summary SET count_provenance_json=?',[receipt])
    tick(store)
    assert relation_freshness(store.conn,'v_market_daily',DAY,now=NOW)['status']!='ready'


@pytest.mark.parametrize('damage', ['missing', 'unknown_row_kind', 'denied_constituent', 'missing_basis', 'duplicate_keys'])
def test_read_merge_read_does_not_launder_unqualified_stored_evidence(store, damage):
    if damage == 'missing':
        store.conn.execute('UPDATE daily_summary SET count_provenance_json=NULL')
    elif damage == 'unknown_row_kind':
        store.conn.execute("UPDATE daily_summary SET source_kind='unknown'")
    elif damage == 'duplicate_keys':
        proof = store.fetchall('SELECT count_provenance_json FROM daily_summary')[0][0]
        # Last-key-wins JSON would erase the explicit denial during merge.
        proof = proof.replace('"qualified": true', '"qualified": false, "qualified": true')
        store.conn.execute('UPDATE daily_summary SET count_provenance_json=?', [proof])
    else:
        proof = json.loads(store.fetchall('SELECT count_provenance_json FROM daily_summary')[0][0])
        if damage == 'denied_constituent':
            proof['consecutive_count']['qualified'] = False
        else:
            del proof['consecutive_count']['freshness_basis']
        store.conn.execute('UPDATE daily_summary SET count_provenance_json=?', [json.dumps(proof)])
    _create_market_daily(store.conn)
    assert relation_freshness(store.conn, 'v_market_daily', DAY, now=NOW)['status'] != 'ready'
    for _ in range(2):
        tick(store)
        for relation in ('daily_summary', 'v_market_daily', 'v_market_state_inputs'):
            assert relation_freshness(store.conn, relation, DAY, now=NOW)['status'] != 'ready'
    assert store.fetchall('SELECT consecutive_count FROM daily_summary') == [(COUNTS['consecutive_count'],)]
    # New, explicitly observed source facts can replace the unverified version;
    # this does not resurrect or relabel the old source evidence.
    assert collect_daily(SimpleNamespace(get=lambda *a: COUNTS), store, DAY) == 1
    _create_market_daily(store.conn)
    assert store.fetchall('SELECT market_daily_status FROM v_market_daily') == [('available',)]


def test_partial_new_source_preserves_unknown_instead_of_zero_and_explains_page(store):
    store.conn.execute('UPDATE daily_summary SET consecutive_count=NULL')
    tick(store)
    assert store.fetchall('SELECT consecutive_count,source_kind FROM daily_summary')==[(None,'real_partial')]
    from trade_system.review_queries import _market_context_review
    from trade_system.review_web import _render_kpis
    context=_market_context_review(store.conn,DAY)
    assert '数据不完整' in _render_kpis({'market_context':context})


def test_explicit_provenance_migration_is_additive_and_collectors_do_not_apply_it(store):
    from pathlib import Path
    store.conn.execute('ALTER TABLE daily_summary DROP COLUMN count_provenance_json')
    before=store.fetchall('SELECT * FROM daily_summary')
    with pytest.raises(RuntimeError,match='count_provenance_json'):
        collect_market._require_daily_summary_schema(store)
    assert collect_daily(SimpleNamespace(get=lambda *a:COUNTS),store,DAY)==0
    assert store.fetchall('SELECT * FROM daily_summary')==before
    migration=Path(__file__).resolve().parents[1]/'migrations/0017_daily_count_provenance.sql'
    store.conn.execute(migration.read_text())
    store.conn.execute(migration.read_text())
    assert store.fetchall('SELECT * EXCLUDE(count_provenance_json) FROM daily_summary')==before
    assert store.fetchall('SELECT count_provenance_json FROM daily_summary')==[(None,)]


def test_provenance_migration_does_not_create_legacy_tables_in_independent_databases():
    from pathlib import Path
    import duckdb
    migration=Path(__file__).resolve().parents[1]/'migrations/0017_daily_count_provenance.sql'
    with duckdb.connect(':memory:') as con:
        con.execute(migration.read_text())
        assert con.execute('SHOW TABLES').fetchall()==[]
