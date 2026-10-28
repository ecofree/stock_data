"""Independent clock counterexamples traverse the actual rise/fall merge entry."""
from datetime import datetime
from types import SimpleNamespace
import json

import duckdb
import pytest

from collectors import collect_daily, collect_market
from trade_system.data_store import DuckDBStore
from trade_system.market_contract import inherited_count_evidence, market_daily_status_sql
from trade_system.normalize import _create_market_daily, _create_market_state_inputs
from trade_system.readiness import relation_freshness

DAY='2026-10-12'
RECEIVED=datetime(2026,10,12,16)
NOW=datetime(2026,10,12,17)
COUNTS={'limit_up_count':10,'limit_down_count':2,'rise_count':3000,'fall_count':1000,'consecutive_count':3}


def stored_proof(clock='2026-10-12T16:00:00.000000'):
    # Explicit contract fixture, intentionally not built by the implementation.
    proof={field:{'value':value,'source':'kpl_daily','source_kind':'real','source_date':DAY,
                  'fetched_at':'2026-10-12T16:00:00.000000',
                  'freshness_basis':'source_relation_receipt_lower_bound','qualified':True}
           for field,value in COUNTS.items()}
    proof['consecutive_count']['fetched_at']=clock
    return proof


@pytest.mark.parametrize('clock',[
    DAY,'2026-10-12T16:00','20261012T160000','2026-10-12\t16:00:00',
    '2026-10-12T16:00:00.0000001','2026-10-12T16:00:00+08:00',None,
])
def test_inherited_noncanonical_clocks_cannot_be_reformatted_into_valid_receipts(clock):
    result=inherited_count_evidence(3,'consecutive_count',provenance=json.dumps(stored_proof(clock)),
        source_kind='real',source_date=DAY,fetched_at=RECEIVED,received_at=NOW)
    assert result['qualified'] is False


@pytest.mark.parametrize('clock',[
    '2026-10-12T16:00:00','2026-10-12 16:00:00.000001','2026-10-12T16:00:00.1',
])
def test_canonical_local_clock_forms_keep_actual_evidence(clock):
    result=inherited_count_evidence(3,'consecutive_count',provenance=json.dumps(stored_proof(clock)),
        source_kind='real',source_date=DAY,fetched_at=RECEIVED,received_at=NOW)
    assert result['qualified'] is True and result['value']==3


@pytest.mark.parametrize('receipt,row_clock,qualified',[
    ('2026-10-12T24:00:00','2026-10-12T16:00:00',False),
    ('2026-10-13T00:30:00','2026-10-12T24:00:00',False),
    ('2026-10-12T23:59:59','2026-10-12T23:59:59',True),
])
def test_python_and_sql_do_not_disagree_about_midnight_rollover(receipt,row_clock,qualified):
    # DuckDB accepts 24:00:00 as tomorrow, whereas Python rejects it. The
    # shared format contract must reject it before either parser can normalize.
    proof=stored_proof()
    for item in proof.values():item['fetched_at']=receipt
    raw=json.dumps(proof)
    as_of=datetime(2026,10,13,1)
    result=inherited_count_evidence(3,'consecutive_count',provenance=raw,source_kind='real',
        source_date=DAY,fetched_at=row_clock,received_at=as_of)
    assert result['qualified'] is qualified
    columns=['date',*COUNTS,'fetched_at','source_kind','count_provenance_json']
    expression=market_daily_status_sql(columns,as_of=as_of)
    # The row clock is deliberately VARCHAR, as it can be in retained history.
    with duckdb.connect(':memory:') as con:
        status=con.execute('SELECT '+expression+' FROM (SELECT '+
            ','.join('? AS '+name for name in columns)+')',
            [DAY,*COUNTS.values(),row_clock,'real',raw]).fetchone()[0]
    assert status==('available' if qualified else 'unverified')


@pytest.mark.parametrize('damage',[
    'date_only','minute_only','compact_date','tab_separator','extra_precision',
    'missing_row_clock','row_clock_before_day','refreshed_row_clock','valid',
])
def test_actual_merge_does_not_repair_unqualified_history_but_new_observations_can(monkeypatch,damage):
    class Clock(datetime):
        @classmethod
        def now(cls):return NOW
    monkeypatch.setattr(collect_market,'datetime',Clock)
    monkeypatch.setattr(collect_daily,'datetime',Clock)
    proof=stored_proof();row_clock=RECEIVED
    bad_clock={'date_only':DAY,'minute_only':'2026-10-12T16:00','compact_date':'20261012T160000',
        'tab_separator':'2026-10-12\t16:00:00','extra_precision':'2026-10-12T16:00:00.0000001'}
    if damage in bad_clock:proof['consecutive_count']['fetched_at']=bad_clock[damage]
    if damage=='missing_row_clock':row_clock=None
    if damage=='row_clock_before_day':row_clock=datetime(2026,10,11,16)
    if damage=='refreshed_row_clock':row_clock=datetime(2026,10,12,16,0,1)
    with duckdb.connect(':memory:') as con:
        con.execute('CREATE TABLE daily_summary(date DATE PRIMARY KEY,limit_up_count INTEGER,limit_down_count INTEGER,'
            'rise_count INTEGER,fall_count INTEGER,consecutive_count INTEGER,source_kind VARCHAR,fetched_at TIMESTAMP,'
            'count_provenance_json VARCHAR,raw_json VARCHAR)')
        con.execute('INSERT INTO daily_summary VALUES (?,?,?,?,?,?,?,?,?,?)',
            [DAY,*COUNTS.values(),'real',row_clock,json.dumps(proof),'original retained observation'])
        con.execute('CREATE TABLE market_mood(date DATE,rise_count INTEGER,fall_count INTEGER,source_kind VARCHAR,fetched_at TIMESTAMP)')
        con.execute('CREATE TABLE market_rise_fall(date DATE,limit_up_count INTEGER,limit_down_count INTEGER,'
            'broken_limit_up_count INTEGER,blown_limit_up_count INTEGER,blown_limit_up_rate DOUBLE,raw_field_5 INTEGER,'
            'raw_json VARCHAR,source_kind VARCHAR,fetched_at TIMESTAMP DEFAULT current_timestamp)')
        store=DuckDBStore(connection=con)
        store._init_meta()
        _create_market_daily(con);_create_market_state_inputs(con)
        def statuses():
            return {name:relation_freshness(con,name,DAY,now=NOW)['market_daily_status']
                    for name in ('daily_summary','v_market_daily','v_market_state_inputs')}
        assert all((state=='available')==(damage=='valid') for state in statuses().values())
        rise_fall=SimpleNamespace(get=lambda *a,**k:{'raw_data':[[12,3,2,8,20,3,DAY]]})
        for _ in range(2):
            assert collect_market.collect_market_rise_fall(rise_fall,store,DAY)==1
            assert all((state=='available')==(damage=='valid') for state in statuses().values())
            assert con.execute('SELECT consecutive_count FROM daily_summary').fetchone()==(3,)
        assert collect_daily.collect_daily(SimpleNamespace(get=lambda *a:dict(COUNTS)),store,DAY)==1
        assert set(statuses().values())=={'available'}
        assert con.execute('SELECT rise_count,fall_count,consecutive_count FROM daily_summary').fetchone()==(3000,1000,3)
