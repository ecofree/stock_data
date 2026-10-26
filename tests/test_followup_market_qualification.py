"""Counterexamples across raw -> canonical -> scores -> gates -> page."""
from datetime import datetime
from zoneinfo import ZoneInfo
import hashlib
import json

import duckdb
import pytest

from trade_system.normalize import _create_market_daily, _create_market_state_inputs, _create_limit_pool
from trade_system.readiness import relation_freshness, assess_trade_date_readiness
from trade_system.market_contract import MARKET_COUNT_FIELDS

DAY = '2026-10-12'
NOW = datetime(2026, 10, 12, 18)


def summary(con, kind='real', counts=(10, 2, 3000, 1000, 3)):
    con.execute('CREATE TABLE daily_summary(date DATE,limit_up_count DOUBLE,limit_down_count DOUBLE,'
                'rise_count DOUBLE,fall_count DOUBLE,consecutive_count DOUBLE,source_kind VARCHAR,fetched_at TIMESTAMP)')
    con.execute("INSERT INTO daily_summary VALUES (?,?,?,?,?,?,?,'2026-10-12 16:00:00')", [DAY, *counts, kind])
    con.execute('CREATE TABLE market_emotion_money(date DATE,cgl DOUBLE,yll DOUBLE,success_rate DOUBLE,fetched_at TIMESTAMP)')
    con.execute("INSERT INTO market_emotion_money VALUES (?,50,50,60,'2026-10-12 16:00:00')", [DAY])
    con.execute('CREATE TABLE market_rise_fall(date DATE,limit_up_count INTEGER,limit_down_count INTEGER,'
                'broken_limit_up_count INTEGER,blown_limit_up_count INTEGER,blown_limit_up_rate DOUBLE,'
                'raw_field_5 INTEGER,source_kind VARCHAR,updated_at TIMESTAMP)')
    con.execute("INSERT INTO market_rise_fall VALUES (?,10,2,0,0,0,NULL,'real','2026-10-12 16:00:00')", [DAY])


@pytest.mark.parametrize('kind,counts,status', [
    ('real', (10,2,3000,1000,3), 'available'),
    ('real', (0,0,0,0,0), 'available'),
    ('real_partial', (10,2,3000,1000,3), 'partial'),
    ('fallback', (10,2,3000,1000,3), 'estimated'),
    *[('real', tuple(None if i == index else 0 for i in range(len(MARKET_COUNT_FIELDS))), 'partial') for index in range(len(MARKET_COUNT_FIELDS))],
    *[('real', (bad,2,3000,1000,3), 'partial') for bad in (-1, 1.5, float('nan'), float('inf'))],
])
def test_qualification_reaches_every_candidate_and_scores(kind, counts, status):
    with duckdb.connect(':memory:') as con:
        summary(con, kind, counts)
        _create_market_daily(con)
        _create_market_state_inputs(con)
        assert con.execute('SELECT market_daily_status FROM v_market_daily').fetchone() == (status,)
        row = con.execute('SELECT market_daily_status,earning_effect_score,acute_drop_risk_score FROM v_market_state_inputs').fetchone()
        assert row[0] == status
        if status != 'available':
            assert row[1:] == (None,None)
        elif not any(counts):
            assert row[1:] == (None, 0)  # empty breadth has no defined ratio; known zero risk inputs do.
        else:
            assert row[1:] == (56, 1.7)
        for relation in ('daily_summary','v_market_daily','v_market_state_inputs'):
            gate = relation_freshness(con, relation, DAY, now=NOW)
            assert (gate['status'] == 'ready') == (status == 'available')
            assert gate['market_daily_status'] == status
        group = assess_trade_date_readiness(con, DAY, 'close', required_groups=('market_state',), now=NOW)['groups'][0]
        assert group['ready'] == (status == 'available')
        from trade_system.review_queries import _market_context_review
        from trade_system.review_web import _render_kpis
        context = _market_context_review(con, DAY)
        assert context['market_daily']['market_daily_status'] == status
        page = _render_kpis({'market_context': context})
        assert {'available':'五项计数完整','partial':'数据不完整','estimated':'估算数据'}[status] in page


@pytest.mark.parametrize('missing', ['emotion','risk'])
def test_score_requires_its_own_inputs_even_when_market_counts_are_complete(missing):
    with duckdb.connect(':memory:') as con:
        summary(con)
        con.execute('DELETE FROM ' + ('market_emotion_money' if missing=='emotion' else 'market_rise_fall'))
        _create_market_daily(con); _create_market_state_inputs(con)
        row = con.execute('SELECT earning_effect_score,acute_drop_risk_score,earning_effect_status,acute_drop_risk_status FROM v_market_state_inputs').fetchone()
        assert row == ((None,1.7,'partial','available') if missing=='emotion' else (56,None,'available','partial'))


def test_latest_partial_raw_snapshot_cannot_borrow_earlier_complete_counts():
    with duckdb.connect(':memory:') as con:
        summary(con)
        con.execute("INSERT INTO daily_summary SELECT * REPLACE (NULL AS rise_count,'2026-10-12 17:00:00'::TIMESTAMP AS fetched_at) FROM daily_summary")
        _create_market_daily(con); _create_market_state_inputs(con)
        assert not assess_trade_date_readiness(con, DAY, required_groups=('market_state',), now=NOW)['groups'][0]['ready']


def test_schema_without_counts_does_not_certify_market_state():
    with duckdb.connect(':memory:') as con:
        con.execute('CREATE TABLE daily_summary(date DATE)')
        con.execute('INSERT INTO daily_summary VALUES (?)',[DAY])
        assert relation_freshness(con,'daily_summary',DAY,now=NOW)['status']=='partial'


@pytest.mark.parametrize('case',['missing','partial','complete_empty','complete'])
def test_pool_qualification_cannot_be_laundered_through_scores_or_raw_fallback(case):
    with duckdb.connect(':memory:') as con:
        con.execute('CREATE TABLE tushare_daily(date DATE,stock_code VARCHAR,change_pct DOUBLE,fetched_at TIMESTAMP)')
        con.execute("INSERT INTO tushare_daily VALUES (?,'000001',10,'2026-10-12 16:00:00')",[DAY])
        con.execute('CREATE TABLE official_limit_pool(trade_date DATE,stock_code VARCHAR,source VARCHAR,continue_day_cnt INTEGER)')
        con.execute('CREATE TABLE history_fetch_checkpoint(dataset VARCHAR,trade_date DATE,page_no INTEGER,status VARCHAR,rows_written INTEGER,last_error VARCHAR,updated_at TIMESTAMP)')
        count=1 if case in ('partial','complete') else 0
        if count:
            con.execute("INSERT INTO official_limit_pool VALUES (?,'000001','hithink',2)",[DAY])
        if case.startswith('complete'):
            receipt={'path':'/api/a-share/special-data/limit-up-pool',
                'params':{'date_ms':int(datetime(2026,10,12,tzinfo=ZoneInfo('Asia/Shanghai')).timestamp()*1000)},
                'total':count,'pages':1,'items_sha256':hashlib.sha256(b'fixture' if count else b'[]').hexdigest()}
            con.execute("INSERT INTO history_fetch_checkpoint VALUES ('hithink_limit_pool',?,0,'success',?,?,'2026-10-12 17:00:00')",[DAY,count,json.dumps(receipt)])
        _create_market_daily(con);_create_market_state_inputs(con)
        status='estimated' if case.startswith('complete') else 'partial'
        assert con.execute('SELECT limit_up_count,market_daily_status FROM v_market_daily').fetchone()==(count if status=='estimated' else None,status)
        assert con.execute('SELECT earning_effect_score,acute_drop_risk_score,market_daily_status FROM v_market_state_inputs').fetchone()==(None,None,status)
        assert not assess_trade_date_readiness(con,DAY,required_groups=('market_state',),now=NOW)['groups'][0]['ready']


@pytest.mark.parametrize('value,expected', [('1',1),('3',3),('2147483647',2147483647),
    ('2147483648',2),('1e20',2),('NaN',2),('Infinity',2),('-Infinity',2),('1.5',2),('-1',2),('0',2),(None,2),('',2)])
def test_bad_native_height_cannot_break_other_sources(value, expected):
    with duckdb.connect(':memory:') as con:
        con.execute('CREATE TABLE l2_realtime_all_boards(date DATE,stock_code VARCHAR,stock_name VARCHAR,board_level INTEGER,limit_up_time VARCHAR,fetched_at TIMESTAMP)')
        con.execute("INSERT INTO l2_realtime_all_boards VALUES (?, '000001','fallback',2,'09:35','2026-10-12 16:00:00')",[DAY])
        con.execute('CREATE TABLE xdf_limit_pool(trade_date DATE,ts_code VARCHAR,name VARCHAR,limit_times VARCHAR,fetched_at VARCHAR)')
        con.execute("INSERT INTO xdf_limit_pool VALUES (?,'000001.SZ','native',?,'2026-10-12 17:00:00')",[DAY,value])
        _create_limit_pool(con)
        assert con.execute('SELECT board_level,source FROM v_limit_pool').fetchone() == (expected, 'kpl_l2' if expected == 2 else 'xiaodefa')
        assert con.execute('SELECT limit_times FROM xdf_limit_pool').fetchone() == (value,)
        con.execute("UPDATE xdf_limit_pool SET limit_times='3',fetched_at='bad clock'")
        assert con.execute('SELECT board_level,source FROM v_limit_pool').fetchone() == (2,'kpl_l2')
