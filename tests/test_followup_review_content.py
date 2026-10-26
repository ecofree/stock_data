"""Real query output fixtures also used by isolated browser content acceptance."""
import duckdb
import pytest

from trade_system.review_queries import _sector_trail_review

DAY='2026-10-12'


def trail_fixture(case):
    with duckdb.connect(':memory:') as con:
        con.execute('CREATE TABLE v_limit_pool(trade_date DATE,stock_code VARCHAR,stock_name VARCHAR,board_level INTEGER,limit_up_time VARCHAR)')
        con.execute("INSERT INTO v_limit_pool VALUES (?,'000001','样本甲',1,'09:35:00')",[DAY])
        con.execute('CREATE TABLE v_default_concept_stock_history(trade_date DATE,concept_code VARCHAR,concept_name VARCHAR,stock_code VARCHAR,stock_name VARCHAR)')
        con.execute("INSERT INTO v_default_concept_stock_history VALUES (?,'THS-A','样本概念','000001','样本甲')",[DAY])
        con.execute('CREATE TABLE v_default_concept_daily(trade_date DATE,concept_code VARCHAR)')
        con.execute("INSERT INTO v_default_concept_daily VALUES (?,'THS-A')",[DAY])
        con.execute('CREATE TABLE v_kline_daily(trade_date DATE,stock_code VARCHAR,close DOUBLE,change_pct DOUBLE,turnover DOUBLE)')
        con.execute("INSERT INTO v_kline_daily VALUES (?,'000001',10,2,1000)",[DAY])
        if case=='foreign_day':
            con.execute('CREATE TABLE tushare_daily(date DATE,stock_code VARCHAR,open DOUBLE,high DOUBLE,low DOUBLE,close DOUBLE,change_pct DOUBLE,volume DOUBLE,turnover DOUBLE,fetched_at TIMESTAMP)')
            con.execute("INSERT INTO tushare_daily VALUES (?,'000002',10,10,10,10,3,100,1000,current_timestamp)",[DAY])
        if case=='partial_members':
            con.execute("INSERT INTO v_default_concept_stock_history VALUES (?,'THS-A','样本概念','000003','缺行情乙')",[DAY])
        con.execute('CREATE TABLE tushare_daily_basic(date DATE,stock_code VARCHAR,turnover_rate DOUBLE)')
        con.execute('CREATE TABLE multi_source_sector_flow(source_date DATE,sector_code VARCHAR,change_pct DOUBLE,main_net DOUBLE,provider VARCHAR,raw_json VARCHAR,fetched_at TIMESTAMP,is_stale BOOLEAN,sector_type VARCHAR)')
        con.execute('CREATE TABLE multi_source_stock_flow(source_date DATE,stock_code VARCHAR,change_pct DOUBLE,main_net DOUBLE,provider VARCHAR,fetched_at TIMESTAMP,is_stale BOOLEAN)')
        if case=='empty': con.execute('DELETE FROM v_limit_pool')
        intercepted=[]
        class Cursor:
            def execute(self, sql, params=None):
                # Fail or empty only the real optional member-panel query.
                # The preceding joins, detail fields, names and dates stay real.
                if 'max(stock_name) AS stock_name' in sql and 'GROUP BY 1, 2' in sql and 'FROM v_default_concept_stock_history' in sql and 'WITH' not in sql:
                    intercepted.append(True)
                    if case=='query_failure': raise duckdb.IOException('injected member read failure')
                    if case=='no_members': return con.execute('SELECT NULL WHERE false')
                return con.execute(sql, params) if params is not None else con.execute(sql)
        data=_sector_trail_review(Cursor(),DAY)
        if case!='empty': assert intercepted
        return data


@pytest.mark.parametrize('case,valid,total', [('members',1,1),('foreign_day',0,1),('partial_members',1,2)])
def test_member_quote_coverage_is_for_this_concept(case,valid,total):
    data=trail_fixture(case)
    codes=data['sectors'][0]['members']['codes']
    assert len(codes)==total
    assert sum(data['stock_pct'].get(DAY,{}).get(code) is not None for code in codes)==valid


@pytest.mark.parametrize('case',['members','no_members','query_failure','empty'])
def test_real_trail_query_preserves_visible_detail_contract(case):
    data=trail_fixture(case)
    if case=='empty':
        assert data['sectors']==[]
        return
    sector=data['sectors'][0]
    stock=sector['daily'][DAY]['stocks'][0]
    assert (stock['stock_code'],stock['stock_name'],stock['pct_chg'],stock['board_level'],stock['limit_up_time'])==('000001','样本甲',2,1,'09:35:00')
    assert ('members' in sector)==(case=='members')
