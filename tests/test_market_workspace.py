"""Synthetic market contracts; never real decision evidence."""
import duckdb
import pytest
from trade_system.v2 import market_workspace as market, research_product as product
from trade_system.v2.gap_evidence import write_json, read_json


@pytest.fixture
def con():
    c=duckdb.connect(':memory:')
    c.execute('CREATE TABLE tushare_trade_cal(exchange VARCHAR,is_open INTEGER,cal_date DATE)')
    c.execute("INSERT INTO tushare_trade_cal VALUES ('SSE',1,'2026-09-10'),('SSE',1,'2026-09-11')")
    c.execute('CREATE TABLE v_kline_daily(trade_date VARCHAR,stock_code VARCHAR,change_pct DOUBLE,provider VARCHAR,adjustment VARCHAR,volume_unit VARCHAR,amount_unit VARCHAR,fetched_at TIMESTAMP,close DOUBLE)')
    c.execute("INSERT INTO v_kline_daily VALUES ('2026-09-10','000001',-1,'native','raw','shares','yuan','2026-09-10 16:00',10),('2026-09-11','000001',2,'native','raw','shares','yuan','2026-09-11 16:00',10)")
    c.execute('CREATE TABLE v_default_concept_daily(trade_date DATE,concept_code VARCHAR,concept_name VARCHAR,stock_count INTEGER)')
    c.execute("INSERT INTO v_default_concept_daily VALUES ('2026-09-11','T','题材',2)")
    c.execute('CREATE TABLE v_default_concept_stock_history(trade_date DATE,concept_code VARCHAR,stock_code VARCHAR,stock_name VARCHAR)')
    c.execute("INSERT INTO v_default_concept_stock_history VALUES ('2026-09-11','T','000001','甲'),('2026-09-11','T','000002','乙')")
    yield c
    c.close()


def project(c):return market.project(c,'2026-09-11','2026-09-12T12:00:00+08:00',{'000001'})


def test_full_members_and_fixed_scope_previous_day(con):
    r=project(con)
    assert r['themes'][0]['member_codes']==['000001','000002']
    assert r['themes'][0]['research_covered']==1
    assert r['stocks']['000002']['change_pct'] is None
    assert r['matched_previous']['previous']['fall']==1
    assert r['matched_previous']['current']['rise']==1
    assert r['account_state']=='unknown' and not r['execution_ready']


def test_changed_provider_not_compared_and_partial_members_not_published(con):
    con.execute("UPDATE v_kline_daily SET provider='other' WHERE trade_date='2026-09-10'")
    con.execute('UPDATE v_default_concept_daily SET stock_count=3')
    r=project(con)
    assert r['matched_previous']['status']=='unavailable'
    assert r['themes']==[] and r['membership_status']=='partial'


def test_normalized_turnover_is_not_converted_twice_for_old_view_labels(con):
    con.execute('ALTER TABLE v_kline_daily ADD COLUMN turnover BIGINT')
    con.execute("UPDATE v_kline_daily SET turnover=12345000,amount_unit='thousand_yuan'")
    result=project(con)['turnover']
    assert result['cny']==12345000 and result['legacy_unit_label_mismatch']


def test_missing_price_date_and_duplicate_identity_refused(con):
    with pytest.raises(ValueError,match='exact market session'):
        market.project(con,'2026-09-12','2026-09-12T17:00:00',set())
    con.execute('INSERT INTO v_kline_daily SELECT * FROM v_kline_daily LIMIT 1')
    with pytest.raises(ValueError,match='duplicate'):project(con)


def test_market_failure_prevents_half_publication(tmp_path,monkeypatch):
    write_json(tmp_path/'prediction-current.json',{'old':'pointer'})
    old=(tmp_path/'prediction-current.json').read_bytes()
    def fail(*args,**kwargs):raise ValueError('synthetic render failed')
    monkeypatch.setattr(product,'_publish_desk',fail)
    with pytest.raises(ValueError,match='render failed'):
        product.publish_state(tmp_path,'prediction-current.json',{'new':'pointer'},market={'trade_date':'2026-09-11'})
    assert (tmp_path/'prediction-current.json').read_bytes()==old


def test_configured_market_uses_its_own_calendar_not_prediction_date(tmp_path,monkeypatch):
    write_json(tmp_path/'workspace-config.json',{'market_database':'synthetic.duckdb','read_only':True})
    calls=[]
    monkeypatch.setattr(market,'latest_snapshot',lambda *args:calls.append(args) or {'trade_date':'2026-09-14'})
    assert product.configured_market(tmp_path,{'date':'2026-09-11','rows':[{'instrument':'000001'}]})['trade_date']=='2026-09-14'
    assert calls[0][0]=='synthetic.duckdb' and calls[0][2]==['000001']
    assert read_json(tmp_path/'workspace-config.json')[0]['read_only']


def test_one_metadata_authority_and_conflicting_legacy_rejected(tmp_path):
    folder=tmp_path/'dataset';folder.mkdir()
    write_json(folder/'features.metadata.json',{'version':2})
    assert product.dataset_metadata(tmp_path)=={'version':2}
    write_json(folder/'dataset.json',{'version':1})
    with pytest.raises(ValueError,match='conflicting'):product.dataset_metadata(tmp_path)


def test_managed_service_stops_without_killing_processes(tmp_path):
    import socket
    import threading
    import time
    from trade_system.v2.research_product_server import serve,stop
    with socket.socket() as reservation:
        reservation.bind(('127.0.0.1',0));port=reservation.getsockname()[1]
    thread=threading.Thread(target=serve,args=(tmp_path,tmp_path,port),daemon=True)
    thread.start()
    for _ in range(100):
        if (tmp_path/f'service-{port}.json').exists():break
        time.sleep(.02)
    assert stop(tmp_path,port)['stop_requested']
    thread.join(timeout=5)
    assert not thread.is_alive() and not (tmp_path/f'service-{port}.json').exists()


def test_release_extraction_refuses_escape(tmp_path):
    import zipfile
    from tools.v2.rehearse_research_release import stage
    archive=tmp_path/'bad.zip'
    with zipfile.ZipFile(archive,'w') as bundle:bundle.writestr('../escaped.txt','not allowed')
    with pytest.raises(ValueError,match='unsafe'):stage(archive,tmp_path/'release')
    assert not (tmp_path/'escaped.txt').exists()


def test_weekend_reuse_needs_both_explicit_exchange_flags():
    reg={'requests':[{'kind':'native_calendar'},
        {'kind':'calendar','params':{'exchange':'SSE'}},{'kind':'calendar','params':{'exchange':'SZSE'}}]}
    data={0:['2026-09-11'],1:[{'cal_date':'20260912','is_open':0}],2:[{'cal_date':'20260912','is_open':0}]}
    assert product.calendar_covers_clock(reg,data,'2026-09-12')
    assert not product.calendar_covers_clock(reg,{0:data[0],1:data[1]},'2026-09-12')
    data[2][0]['is_open']=1
    assert not product.calendar_covers_clock(reg,data,'2026-09-12')
