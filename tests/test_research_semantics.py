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
