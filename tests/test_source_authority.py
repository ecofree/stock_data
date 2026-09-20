from __future__ import annotations

import pytest

from trade_system.source_authority import validate_production_plan


def test_close_plan_requires_official_and_realtime_limit_pool_edges():
    validate_production_plan(
        "close",
        [
            "collect_market_context", "sync_tushare_close",
            "collect_ths_concepts_api", "collect_hithink_limit_pool_daily",
            "collect_realtime_limit_pool", "collect_intraday_stock_flow_market",
            "collect_intraday_sector_flow_full",
        ],
    )


def test_close_plan_rejects_missing_canonical_producer():
    with pytest.raises(ValueError, match="limit_pool"):
        validate_production_plan(
            "close",
            [
                "collect_market_context", "sync_tushare_close",
                "collect_ths_concepts_api", "collect_intraday_stock_flow_market",
                "collect_intraday_sector_flow_full",
            ],
        )


@pytest.fixture()
def pool_db(tmp_path):
    import duckdb
    from pathlib import Path
    path = tmp_path/'pools.duckdb'
    with duckdb.connect(str(path)) as con:
        con.execute((Path(__file__).resolve().parents[1]/'migrations/0003_official_limit_pool_and_journal.sql').read_text())
        con.execute("CREATE TABLE history_fetch_checkpoint(dataset VARCHAR,trade_date DATE,page_no INTEGER,status VARCHAR,rows_written INTEGER,last_error VARCHAR,updated_at TIMESTAMP,PRIMARY KEY(dataset,trade_date,page_no))")
        con.execute("CREATE TABLE tushare_trade_cal(cal_date DATE,is_open BOOLEAN)")
        con.execute("INSERT INTO tushare_trade_cal VALUES ('2026-08-20',true),('2026-08-21',true)")
        con.execute("INSERT INTO market_journal VALUES ('2026-08-20','original human note','tag',now())")
    return path


def _pool_items():
    return [dict(ticker=f'{n:06}',continue_day_cnt=2,last_price=10,is_st=False) for n in range(1,6)]


def _receipt(day, rows):
    from trade_system.hithink_client import HiThinkClient
    import json, hashlib
    return {'path':'/api/a-share/special-data/limit-up-pool',
        'params':{'date_ms':HiThinkClient._date_ms(day)},'pages':1,'total':len(rows),
        'received_at':day+'T17:00:00+08:00',
        'items_sha256':hashlib.sha256(json.dumps(rows,sort_keys=True).encode()).hexdigest()}


def test_history_pool_uses_daily_writer_and_calendar_not_market_bar_count(pool_db,monkeypatch):
    import duckdb
    from scripts import backfill_limit_pool_history as history, collect_hithink_limit_pool_daily as daily
    assert history._write_snapshot is daily._write_snapshot and history._clean_items is daily._clean_items
    from trade_system.hithink_client import HiThinkClient, HiThinkError
    native=HiThinkClient(api_key='synthetic',min_interval=0)
    payload={'item':[], 'pagination':{'page':1,'pages':1,'total':0}}
    monkeypatch.setattr(native,'_get',lambda *a,**kw:payload)
    empty,receipt=native.limit_up_pool('2026-08-20',with_receipt=True)
    assert empty==[] and receipt['total']==0
    payload['pagination']['total']=1
    with pytest.raises(HiThinkError,match='total'):
        native.limit_up_pool('2026-08-20',with_receipt=True)
    payload['pagination'].pop('total')
    with pytest.raises(HiThinkError,match='total'):
        native.limit_up_pool('2026-08-20',with_receipt=True)

    with duckdb.connect(str(pool_db)) as con:
        assert history._missing_days(con,'2026-08-20','2026-08-21') == ['2026-08-20','2026-08-21']
        assert history._write_snapshot(con,'2026-08-20',history._clean_items(_pool_items()),receipt=_receipt('2026-08-20',_pool_items())) == 5
        assert history._missing_days(con,'2026-08-20','2026-08-21') == ['2026-08-21']
        assert con.execute("SELECT note FROM market_journal").fetchone()[0] == 'original human note'
        # Source-certified small and empty revisions replace obsolete rows.
        for rows in (_pool_items()[:1], []):
            history._write_snapshot(con,'2026-08-20',history._clean_items(rows),receipt=_receipt('2026-08-20',rows))
            assert con.execute("SELECT count(*) FROM official_limit_pool").fetchone()[0] == len(rows)
            assert history._missing_days(con,'2026-08-20','2026-08-21') == ['2026-08-21']

        con.execute("DELETE FROM tushare_trade_cal WHERE cal_date='2026-08-21'")
        with pytest.raises(ValueError, match='calendar'):
            history._missing_days(con,'2026-08-20','2026-08-21')


@pytest.mark.parametrize('fault',['duplicate','bad_code','missing_height','fractional_height','nonfinite_height','boolean_height','empty'])
def test_official_pool_invalid_batch_preserves_previous_rows(pool_db,fault):
    import duckdb
    from scripts import collect_hithink_limit_pool_daily as daily
    with duckdb.connect(str(pool_db)) as con:
        daily._write_snapshot(con,'2026-08-20',daily._clean_items(_pool_items()),receipt=_receipt('2026-08-20',_pool_items()))
        before=con.execute("SELECT * FROM official_limit_pool ORDER BY stock_code").fetchall()
        rows=_pool_items()
        if fault=='duplicate':rows.append(rows[0])
        elif fault=='bad_code':rows[0]['ticker']='bad'
        elif fault=='missing_height':rows[0]['continue_day_cnt']=None
        elif fault=='fractional_height':rows[0]['continue_day_cnt']=1.5
        elif fault=='nonfinite_height':rows[0]['continue_day_cnt']=float('inf')
        elif fault=='boolean_height':rows[0]['continue_day_cnt']=True
        else:rows=[]
        with pytest.raises((ValueError,RuntimeError)):
            daily._write_snapshot(con,'2026-08-20',daily._clean_items(rows),receipt=_receipt('2026-08-20',_pool_items()))
        assert con.execute("SELECT * FROM official_limit_pool ORDER BY stock_code").fetchall()==before


@pytest.mark.parametrize('mode,expected',[('complete',0),('empty',2),('permission',2),('budget',2)])
def test_history_pool_completion_does_not_hide_failed_or_unattempted_days(pool_db,monkeypatch,mode,expected):
    import duckdb,sys
    from scripts import backfill_limit_pool_history as history
    calls=[]
    class Provider:
        def __init__(self,**kwargs):pass
        def limit_up_pool(self,day,**kwargs):
            calls.append(day)
            if mode=='permission':raise history.HiThinkError('5003 unavailable')
            if mode=='empty':raise history.HiThinkError('missing native total')
            return _pool_items(), _receipt(day,_pool_items())
    monkeypatch.setattr(history,'HiThinkClient',Provider)
    monkeypatch.setattr(sys,'argv',['history','--db',str(pool_db),'--start','2026-08-20','--end','2026-08-21','--max-days','1' if mode=='budget' else '2'])
    assert history.main()==expected
    with duckdb.connect(str(pool_db),read_only=True) as con:
        assert con.execute("SELECT count(*) FROM official_limit_pool").fetchone()[0]==(10 if mode=='complete' else 5 if mode=='budget' else 0)
        assert con.execute("SELECT note FROM market_journal").fetchone()[0]=='original human note'
    assert len(calls)==(1 if mode in ('budget','permission') else 2)
    from collectors.xiaodefa import fetch_limit_pool
    class Relay:
        def query_all(self,*args,**kwargs):
            return [dict(ts_code='000001.SZ',trade_date='20260820',limit_times=3),
                    dict(ts_code='000002.SZ',trade_date='20260820',limit_times=None)]
    assert [r['board_level'] for r in fetch_limit_pool(Relay(),'2026-08-20')]==[3,None]
