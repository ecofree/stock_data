"""Read-side comparison counterexamples; all database writes are synthetic."""
from datetime import date, timedelta
import json

import duckdb
import pytest

from trade_system.normalize import _create_kline_daily
from trade_system.source_authority import policy, PROVIDER_ALIASES
from trade_system.v2 import market_workspace as market

DAY='2026-10-12'
AS_OF='2026-10-12T18:00:00+08:00'


def calendar(con, day=DAY, sessions=('2026-10-09', DAY)):
    con.execute('CREATE TABLE tushare_trade_cal(exchange VARCHAR,is_open INTEGER,cal_date DATE)')
    start=date.fromisoformat(day[:7]+'-01')
    # The fixture explicitly certifies both exchanges for the whole period.
    for offset in range((date.fromisoformat(day)-start).days+1):
        current=start+timedelta(days=offset)
        con.executemany('INSERT INTO tushare_trade_cal VALUES (?,?,?)',
                        [(exchange,int(str(current) in sessions),current) for exchange in ('SSE','SZSE')])


@pytest.fixture
def con():
    with duckdb.connect(':memory:') as connection:
        calendar(connection)
        connection.execute('CREATE TABLE v_kline_daily(trade_date DATE,stock_code VARCHAR,change_pct DOUBLE,'
            'provider VARCHAR,adjustment VARCHAR,volume_unit VARCHAR,amount_unit VARCHAR,fetched_at TIMESTAMP,close DOUBLE)')
        for day,pct in [('2026-10-09',-2),(DAY,2)]:
            for code in ('000001','000002'):
                connection.execute('INSERT INTO v_kline_daily VALUES (?,?,?,?,?,?,?,?,?)',
                    [day,code,pct,'xiaodefa','none','shares','yuan',day+' 16:00:00',10])
        connection.execute('CREATE TABLE v_default_concept_daily(trade_date DATE,concept_code VARCHAR,concept_name VARCHAR,stock_count INTEGER)')
        connection.execute('INSERT INTO v_default_concept_daily VALUES (?,?,?,?)',[DAY,'T','完整合成题材',2])
        connection.execute('CREATE TABLE v_default_concept_stock_history(trade_date DATE,concept_code VARCHAR,stock_code VARCHAR,stock_name VARCHAR)')
        connection.executemany('INSERT INTO v_default_concept_stock_history VALUES (?,?,?,?)',
            [(DAY,'T',code,'合成证券') for code in ('000001','000002')])
        yield connection


def project(con, as_of=AS_OF):
    return market.project(con,DAY,as_of,set())


@pytest.mark.parametrize('column,reason',[
    ('provider','unknown_provider'),('adjustment','unknown_adjustment'),
    ('volume_unit','unknown_volume_unit'),('amount_unit','unknown_amount_unit')])
@pytest.mark.parametrize('value',[None,'','  ','unknown','UNKNOWN','undeclared'])
def test_unknown_identity_is_preserved_but_not_counted_or_compared(con,column,reason,value):
    con.execute(f"UPDATE v_kline_daily SET {column}=? WHERE stock_code='000002'",[value])
    before=con.execute('SELECT * FROM v_kline_daily ORDER BY trade_date,stock_code').fetchall()
    result=project(con)
    assert result['breadth']['samples']==1
    assert result['price_qualification']['excluded_reasons']=={reason:1}
    assert result['stocks']['000002']['close']==10
    assert result['stocks']['000002']['price_status']=='unqualified'
    assert result['stocks']['000002']['price_reason']==reason
    assert result['matched_previous']['samples']==1
    assert result['matched_previous']['excluded_reasons']=={'current_'+reason:1}
    for period,expected in [('day',1),('week',1),('month',2),('quarter',2)]:
        summary=result['periods'][period]
        assert summary['security_session_samples']==expected
        assert summary['price_qualification']['qualified_rows']==expected
        assert summary['price_qualification']['excluded_rows']==expected
        assert summary['price_qualification']['observed_rows']==2*expected
        assert summary['price_qualification']['excluded_reasons']=={reason:expected}
    theme=result['periods']['week']['themes'][0]['rows'][0]
    assert theme['priced_members']==1 and theme['excluded_price_members']==1
    assert theme['price_exclusion_reasons']=={reason:1}
    assert theme['mean_change_pct']==2
    assert not result['execution_ready']
    assert con.execute('SELECT * FROM v_kline_daily ORDER BY trade_date,stock_code').fetchall()==before


@pytest.mark.parametrize('column,value',[('provider','native'),('adjustment','raw')])
def test_synthetic_old_labels_are_not_added_to_the_business_whitelist(con,column,value):
    con.execute(f'UPDATE v_kline_daily SET {column}=?',[value])
    result=project(con)
    assert result['matched_previous']['status']=='unavailable'
    assert result['breadth']['samples']==0
    assert result['periods']['month']['security_session_samples']==0
    assert len(result['stocks'])==2


@pytest.mark.parametrize('provider',policy('kline').provider_order+tuple(
    alias for alias,target in PROVIDER_ALIASES.items() if target in policy('kline').provider_order))
def test_declared_provider_and_alias_same_product_remain_comparable(con,provider):
    con.execute('UPDATE v_kline_daily SET provider=?',[provider])
    result=project(con)
    assert result['matched_previous']['status']=='matched'
    assert result['matched_previous']['samples']==2
    assert result['price_qualification']['excluded_rows']==0
    assert result['periods']['month']['security_session_samples']==4


@pytest.mark.parametrize('column,value',[('provider','baostock'),('adjustment','qfq'),
                                        ('volume_unit','hands'),('amount_unit','thousand_yuan')])
def test_known_but_changed_products_do_not_match_previous_session(con,column,value):
    con.execute(f'UPDATE v_kline_daily SET {column}=? WHERE trade_date=?',[value,DAY])
    result=project(con)
    assert result['matched_previous']['status']=='unavailable'
    assert result['matched_previous']['excluded_reasons']=={'price_contract_changed':2}
    assert result['breadth']['samples']==2  # known single-day observations remain useful
    assert result['periods']['month']['security_session_samples']==4


def test_unqualified_previous_product_does_not_borrow_current_identity(con):
    con.execute("UPDATE v_kline_daily SET adjustment='unknown' WHERE trade_date<'2026-10-12'")
    result=project(con)
    assert result['matched_previous']['status']=='unavailable'
    assert result['matched_previous']['excluded_reasons']=={'previous_unknown_adjustment':2}
    assert result['breadth']['samples']==2
    assert result['periods']['month']['security_session_samples']==2


@pytest.mark.parametrize('field,value,reason',[
    ('fetched_at','2026-10-11 23:59:00','invalid_source_clock'),
    ('close',float('inf'),'invalid_price'),('close',0,'invalid_price'),
    ('change_pct',float('nan'),'invalid_price')])
def test_invalid_clock_or_price_never_enters_qualified_breadth(con,field,value,reason):
    con.execute(f'UPDATE v_kline_daily SET {field}=? WHERE stock_code=? AND trade_date=?',[value,'000002',DAY])
    result=project(con)
    assert result['price_qualification']['excluded_reasons']=={reason:1}
    assert result['breadth']['samples']==1
    assert result['periods']['day']['security_session_samples']==1
    assert result['periods']['week']['themes'][0]['rows'][0]['priced_members']==1


def test_as_of_utc_is_converted_to_exchange_time_and_future_receipts_stay_excluded(con):
    assert project(con,'2026-10-12T10:00:00+00:00')['matched_previous']['samples']==2
    con.execute("UPDATE v_kline_daily SET fetched_at='2026-10-12 18:00:01' WHERE stock_code='000002' AND trade_date='2026-10-12'")
    result=project(con,'2026-10-12T10:00:00+00:00')
    assert result['breadth']['samples']==1
    assert result['stocks']['000002']['price_status']=='source_missing'
    assert result['periods']['day']['security_session_samples']==1


def test_week_crossing_quarter_keeps_the_same_qualification_denominator(con):
    con.execute("DELETE FROM tushare_trade_cal")
    for day,opened in [('2026-09-28',1),('2026-09-29',1),('2026-09-30',1),('2026-10-01',1)]:
        con.executemany('INSERT INTO tushare_trade_cal VALUES (?,?,?)',
                        [(exchange,opened,day) for exchange in ('SSE','SZSE')])
    con.execute('DELETE FROM v_kline_daily')
    for day in ('2026-09-28','2026-09-29','2026-09-30','2026-10-01'):
        for code,adjustment in [('000001','none'),('000002','unknown')]:
            con.execute('INSERT INTO v_kline_daily VALUES (?,?,?,?,?,?,?,?,?)',
                [day,code,1,'xiaodefa',adjustment,'shares','yuan',day+' 16:00:00',10])
    result=market.project(con,'2026-10-01','2026-10-01T10:00:00+00:00',set())
    for period,expected in [('day',1),('week',4),('month',1),('quarter',1)]:
        assert result['periods'][period]['security_session_samples']==expected
        assert result['periods'][period]['price_qualification']['qualified_rows']==expected
        assert result['periods'][period]['price_qualification']['excluded_rows']==expected


def test_duplicate_in_older_period_cannot_hide_in_a_different_qualification_group(con):
    con.execute("INSERT INTO v_kline_daily VALUES ('2026-10-01','000009',1,'xiaodefa','none','shares','yuan','2026-10-01 16:00:00',10)")
    con.execute("INSERT INTO v_kline_daily SELECT * REPLACE ('unknown' AS adjustment) FROM v_kline_daily WHERE stock_code='000009'")
    with pytest.raises(ValueError,match='duplicate canonical period identity'):
        project(con)


def test_raw_null_adjustment_survives_normalization_but_not_comparison():
    with duckdb.connect(':memory:') as con:
        calendar(con)
        con.execute('CREATE TABLE multi_source_kline(source_date DATE,asset_type VARCHAR,asset_code VARCHAR,provider VARCHAR,'
            'adjustment VARCHAR,volume_unit VARCHAR,amount_unit VARCHAR,fetched_at TIMESTAMP,open DOUBLE,high DOUBLE,low DOUBLE,'
            'close DOUBLE,volume DOUBLE,amount DOUBLE,change_pct DOUBLE,is_stale BOOLEAN)')
        con.executemany("INSERT INTO multi_source_kline VALUES (?,'stock','000001','xiaodefa',NULL,'shares','yuan',?,10,11,9,10,100,1000,?,false)",
            [('2026-10-09','2026-10-09 16:00:00',-2),(DAY,DAY+' 16:00:00',2)])
        _create_kline_daily(con)
        result=project(con)
        assert result['matched_previous']['status']=='unavailable'
        assert result['breadth']['samples']==0
        assert result['periods']['month']['security_session_samples']==0
        assert result['periods']['month']['price_qualification']['excluded_rows']==2
        assert result['stocks']['000001']['price_contract'][1]=='unknown'
        assert con.execute('SELECT count(*) FROM multi_source_kline WHERE adjustment IS NULL').fetchone()==(2,)


def test_current_publication_preserves_exclusion_contract_and_raw_observations(con,tmp_path):
    from trade_system.v2.acceptance_scope import SCOPE
    from trade_system.v2.publisher import read_current
    from trade_system.v2.research_product import publish_desk
    con.execute("UPDATE v_kline_daily SET adjustment='unknown' WHERE stock_code='000002'")
    (tmp_path/'workspace-config.json').write_text(json.dumps({'acceptance_scope':SCOPE}),encoding='utf-8')
    publish_desk(tmp_path,market=project(con))
    _,files=read_current(tmp_path/'publication')
    data=json.loads(files['desk.json'])
    assert data['market']['matched_previous']['samples']==1
    assert data['market']['price_qualification']['excluded_reasons']=={'unknown_adjustment':1}
    assert data['market']['stocks']['000002']['price_status']=='unqualified'
    assert data['market']['stocks']['000002']['close']==10
    assert data['acceptance_scope']==SCOPE and not data['execution_ready']
    assert b'unknown_adjustment' in files['index.html']
