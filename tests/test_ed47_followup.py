"""Counterexamples from the ed47 audit, not real-provider acceptance."""
from datetime import date, timedelta
import json
from pathlib import Path
import subprocess
import sys

import duckdb
import pandas as pd
import pytest

from scripts.export_qlib_features import _query
from trade_system.flow_contract import normalize_stock_flow_row
from trade_system.flow_features import build_flow_features, STOCK_FEATURE_TABLE
from trade_system.multi_source_store import MultiSourceStore
from trade_system.v2.domain import file_hash
from trade_system.v2.rolling_research import FoldDataset, load_frame


@pytest.mark.parametrize('name,args',[
    ('generate_signals.py',['--allow-partial']),
    ('generate_stage_signals.py',['--date','2026-09-10','--stage','close_decision','--allow-blocked'])])
def test_retired_cli_refuses_before_database_write(tmp_path,name,args):
    db=tmp_path/'unknown.duckdb'
    script=Path(__file__).resolve().parents[1]/'scripts'/name
    result=subprocess.run([sys.executable,str(script),'--db',str(db),*args],capture_output=True,cwd=tmp_path)
    assert result.returncode!=0 and b'legacy signal writes retired' in result.stderr
    assert not db.exists()


@pytest.mark.parametrize('entry', ['signals','stage','refresh'])
def test_direct_retired_function_preserves_existing_database(tmp_path,entry):
    from trade_system.signals import generate_signals
    from trade_system.stage_signals import generate_stage_signals,refresh_close_signals_if_needed
    db=tmp_path/'manual.duckdb'
    with duckdb.connect(str(db)) as con:
        con.execute("CREATE TABLE watchlist(note VARCHAR); INSERT INTO watchlist VALUES ('human')")
    before=file_hash(db)
    with pytest.raises(ValueError,match='retired'):
        if entry=='signals': generate_signals(db,require_ready=False)
        elif entry=='stage': generate_stage_signals(db,'2026-09-10','close_decision')
        else: refresh_close_signals_if_needed(db,'2026-09-10')
    assert file_hash(db)==before


def test_unit_zero_and_ths_semantics():
    f=normalize_stock_flow_row
    assert f({'large_net':0,'big_net':9},'unknown')['large_net']==0
    r=f({'source_api':'moneyflow','buy_lg_amount':0,'sell_lg_amount':5,'raw':{'buy_lg_amount':99}},'tushare')
    assert r['large_net']==-50000 and r['main_net'] is None
    assert f({'large_net':10000,'super_net':0,'amount_unit':'CNY'},'tushare')['main_net']==10000
    r=f({'source_api':'moneyflow_ths','buy_lg_amount':10,'net_amount':12},'tushare')
    assert r['large_net']==100000 and r['net_total']==120000 and r['flow_definition']=='ths_large_orders_net'
    assert f({'main_net':10},'unknown')['flow_unit'] is None
    assert f({'main_net':float('inf')},'unknown')['main_net'] is None


def test_raw_normalization_precedes_placeholder_filter(tmp_path):
    with MultiSourceStore(tmp_path/'flow.duckdb') as store:
        assert store._store_stock_flow('000001',[{'trade_date':'20260102','buy_lg_amount':10,
            'sell_lg_amount':3,'buy_elg_amount':5,'sell_elg_amount':1}],'tushare',False)==1
        assert store.con.execute('SELECT main_net,flow_unit FROM multi_source_stock_flow').fetchone()==(110000,'CNY')


@pytest.mark.parametrize('denominator_unit',[None,'CNY'])
def test_real_writer_contract_and_fieldwise_missing(tmp_path,denominator_unit):
    db=tmp_path/'flow.duckdb'
    days=[]; d=date(2026,1,2)
    while len(days)<22:
        if d.weekday()<5: days.append(d.isoformat())
        d+=timedelta(days=1)
    with MultiSourceStore(db) as store:
        for day in days:
            store._store_stock_flow('000001',[{'date':day,'main_net':100,'turnover':1000,
                'amount_unit':'CNY','turnover_unit':denominator_unit,'flow_definition':'declared_fixture'}],'hithink',False)
    build_flow_features(db)
    with duckdb.connect(str(db),read_only=True) as con:
        rows=con.execute(f'SELECT main_net_1d,main_net_5d,main_net_20d,main_net_ratio_1d,quality_status FROM {STOCK_FEATURE_TABLE} ORDER BY trade_date').fetchall()
    assert rows[0][0]==100 and rows[0][1] is None
    assert rows[4][1]==500 and rows[4][2] is None
    assert rows[-1][:3]==(100,500,2000)
    assert rows[-1][3]==(.1 if denominator_unit else None)
    assert rows[-1][4] in ('research_candidate_not_certified','research_partial_features_not_certified')


def test_label_missing_session_never_skips_to_next_observed_row():
    with duckdb.connect(':memory:') as c:
        c.execute('CREATE TABLE tushare_daily(date DATE,stock_code VARCHAR,open DOUBLE,high DOUBLE,low DOUBLE,close DOUBLE,volume DOUBLE,turnover DOUBLE)')
        c.execute('CREATE TABLE tushare_daily_basic(date DATE,stock_code VARCHAR,turnover_rate DOUBLE,volume_ratio DOUBLE,pe DOUBLE,pb DOUBLE,total_mv DOUBLE,circ_mv DOUBLE)')
        c.execute('CREATE TABLE tushare_moneyflow(date DATE,stock_code VARCHAR,buy_lg_amount DOUBLE,sell_lg_amount DOUBLE,buy_elg_amount DOUBLE,sell_elg_amount DOUBLE,net_mf_amount DOUBLE)')
        c.execute('CREATE TABLE tushare_trade_cal(cal_date DATE,is_open BOOLEAN)')
        for day in ['2026-01-02','2026-01-05','2026-01-06','2026-01-07']:
            c.execute('INSERT INTO tushare_trade_cal VALUES (?,true)',[day])
            if day!='2026-01-05':
                c.execute("INSERT INTO tushare_daily VALUES (?,'000001',10,12,9,11,100,1000)",[day])
        r=c.execute(_query('2026-01-02','2026-01-07',include_calendar=True)).fetchdf().iloc[0]
        assert pd.isna(r.label_next_ret) and str(r.label_end_time.date())=='2026-01-06'
        assert r.label_status=='missing_session_price_not_zero'


def test_optional_role_does_not_weaken_required_columns():
    frame=pd.DataFrame({'datetime':['2026-01-02','2026-01-05'],'instrument':['000001']*2,
                        'x':[1.,2.],'sparse':[0.,0.],'label_next_ret':[1.,2.]})
    frames={'train':frame,'valid':frame,'test':frame.assign(sparse=[100,200])}
    with pytest.raises(ValueError,match='constant'): FoldDataset(frames,['x','sparse'])
    ds=FoldDataset(frames,['x','sparse'],roles={'sparse':'optional_event'})
    assert ds.features==['x'] and ds.dropped_features==['sparse']


def test_intraday_is_not_silently_coerced(tmp_path):
    path=tmp_path/'f.csv'
    pd.DataFrame({'datetime':['2026-01-02T09:35:00'],'instrument':['000001'],'x':[1],
        'label_next_ret':[1],'label_end_time':['2026-01-06'],'label_available_time':['2026-01-06']}).to_csv(path,index=False)
    with pytest.raises(ValueError,match='intraday'): load_frame(path,['x'],10)


def test_daily_page_full_inert_payload_survives_compact_initial_js():
    from trade_system.review_web import _page_html
    from scripts.audit_daily_review_artifact import _embedded_json
    stocks=[{'stock_code':f'{i:06d}','stock_name':str(i),'board_level':1} for i in range(65)]
    ctx={'narrative':{},'concept_limit_up':{'groups':[{'concept_name':'a','limit_up_count':65,'limit_up_stocks':stocks},
        {'concept_name':'b','limit_up_count':1,'limit_up_stocks':stocks[:1]}]},
        'sector_trail':{'dates':['2026-09-10'],'sectors':[{'id':'THS1','name':'a','daily':{'2026-09-10':{'limit_up':65,'stocks':stocks}}}]}}
    text=_page_html(ctx,'2026-09-10','',{'dates':[]},[],[])
    data=_embedded_json(text)
    assert data['trail-data']['dates']==['2026-09-10'] and 'trail-details' in data
    assert len(data['review-concept-data'])==2 and len(data['review-concept-data'][0]['limit_up_stocks'])==65


@pytest.mark.parametrize('default_encoding',['cp1252','gbk','utf-8'])
def test_workflow_prices_are_wired_but_not_native_or_human_acceptance(tmp_path,monkeypatch,default_encoding):
    from trade_system.v2 import daily_workflow as w
    from tests.test_v2_daily_session import report,moment
    parent=tmp_path/'parent';parent.mkdir()
    monkeypatch.setattr(w.daily,'capture',lambda *a,**k:report('2026-09-11'))
    monkeypatch.setattr(w.daily,'verify',lambda p:report() if Path(p)==parent else report('2026-09-11'))
    prices={'SZ.000002':{'date':'2026-09-11','open':'10','high':'11','low':'9','close':'10.5','provider_change_pct':'5',
                          'source_status':'legacy_table_not_native_authenticated'}}
    monkeypatch.setattr(w.daily,'legacy_observations',lambda *a,**k:prices)
    result=w.run(tmp_path/'run',clock=lambda:moment('2026-09-11'),parent=parent,observation_db=tmp_path/'synthetic.duckdb')
    assert result['review_prices_observed']==1 and result['review_prices_missing']==0
    assert not result['human_loop_complete'] and not result['price_source_native_authenticated']
    review_path=tmp_path/'run/next_session_review.json'
    original_open=Path.open
    def locale_open(path,mode='r',buffering=-1,encoding=None,errors=None,newline=None):
        if path==review_path and 'b' not in mode and encoding in (None,'locale'):
            encoding=default_encoding
        return original_open(path,mode,buffering,encoding,errors,newline)
    monkeypatch.setattr(Path,'open',locale_open)
    if default_encoding=='cp1252':
        with pytest.raises(UnicodeDecodeError):review_path.read_text()
    review=json.loads(review_path.read_text(encoding='utf-8'))
    assert review['rows'][0]['actual_operator_return'] is None


def test_delta_checkpoints_preserve_replay_and_durable_idempotency(tmp_path):
    from tools.v2.run_event_replay import Clock,paper_config
    from trade_system.v2.storage import Store
    from trade_system.v2.paper_storage import open_paper,apply_paper_event,load_paper
    with Store(tmp_path/'paper.duckdb',clock=Clock()) as s:
        open_paper(s,paper_config())
        event={'event_id':'0','kind':'cash_transfer','payload':{'amount_fen':1,'evidence_id':'synthetic'}}
        for i in range(128): apply_paper_event(s,'fixture-event-paper',{**event,'event_id':str(i)})
        rows=s.con.execute('SELECT payload FROM paper_checkpoint ORDER BY seq').fetchall()
        a,b=map(lambda r:json.loads(r[0]),rows)
        assert 'seen' not in a and b['parent_seq']==64
        assert len(b['delta']['nav_history']['append'])==64
        original=load_paper(s,'fixture-event-paper').summary()
        assert apply_paper_event(s,'fixture-event-paper',event)==original
        assert load_paper(s,'fixture-event-paper',full_replay=True).summary()==original
        with pytest.raises(ValueError,match='idempotency'):
            apply_paper_event(s,'fixture-event-paper',{**event,'payload':{'amount_fen':2,'evidence_id':'synthetic'}})
        s.con.execute('DELETE FROM paper_checkpoint WHERE seq=64')
        with pytest.raises(ValueError,match='parent'):
            load_paper(s,'fixture-event-paper')
