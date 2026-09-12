"""Bounded quote capture, pure replay and retired transport contract."""
from datetime import datetime
import json

import duckdb
import pytest

from trade_system import quote_transport as transport
from trade_system.v2 import observation_capture as capture,observation_workspace as watch
from trade_system.v2 import research_product as product


def raw(code='000001',event='20260911100000',price='10'):
    parts=['']*50;parts[1]='测试';parts[2]=code;parts[3]=price;parts[30]=event;parts[32]='0'
    return (f'v_{transport.market_prefix(code)}{code}="'+ '~'.join(parts)+'";').encode('gb18030')


def test_shared_transport_bounds_identity_and_duplicates():
    assert transport.canonical_codes(['sz000001','000001.SZ'])==['000001']
    assert transport.canonical_codes(['920001.BJ'])==['920001']
    assert len(transport.batches([f'{i:06}' for i in range(200)]))==4
    for codes in [['../evil'],['000001.SH'],[f'{i:06}' for i in range(201)]]:
        with pytest.raises(ValueError):transport.batches(codes)
    for payload in [raw()+raw(),raw('000002'),raw().replace(b'~000001~',b'~000002~'),b'bad']:
        with pytest.raises(ValueError):transport.parse_parts(payload,['000001'])
    assert transport.parse_parts(raw(),['000001'])['000001'][32]=='0'


def test_quote_capture_retains_bytes_replays_without_fetch_and_detects_tamper(tmp_path):
    calls=[]
    def fetch(codes):calls.append(codes);return raw()
    result=capture.capture(tmp_path/'receipts',['000001'],fetch=fetch)
    assert calls==[['000001']] and result['origin']=='synthetic_fixture'
    assert result['rows'][0]['source_event_time']==datetime(2026,9,11,10)
    assert (tmp_path/'receipts/raw-0.bin').read_bytes()==raw()
    replay=capture.replay(tmp_path/'receipts')
    assert replay==result and len(calls)==1
    (tmp_path/'receipts/raw-0.bin').write_bytes(raw(price='99'))
    with pytest.raises(ValueError):capture.replay(tmp_path/'receipts')


def test_failed_response_is_sealed_without_retry_or_fake_price(tmp_path):
    calls=[]
    def fetch(codes):calls.append(codes);return b'not quote data'
    result=capture.capture(tmp_path/'receipts',['000001'],fetch=fetch)
    assert len(calls)==1 and result['failures']==1 and not result['rows']
    assert (tmp_path/'receipts/raw-0.bin').read_bytes()==b'not quote data'


def test_existing_operational_quote_table_is_consumed_read_only():
    con=duckdb.connect(':memory:')
    con.execute('''CREATE TABLE executable_quote_snapshot(trade_date DATE,stock_code VARCHAR,
        price DOUBLE,change_pct DOUBLE,provider VARCHAR,quote_time VARCHAR,fetched_at TIMESTAMP)''')
    con.execute("INSERT INTO executable_quote_snapshot VALUES ('2026-09-11','000001',10,0,'tencent_spot_quote','20260911100000','2026-09-11 10:00:01')")
    good=watch.project(con,['000001'],'2026-09-11T10:01:00+08:00')
    assert good['qualified']==1 and good['rows'][0]['price']==10
    assert watch.project(con,['000001'],'2026-09-12T10:01:00+08:00')['qualified']==0
    assert con.execute('SELECT count(*) FROM executable_quote_snapshot').fetchone()[0]==1
    con.close()


def test_current_native_source_beats_new_fallback_and_conflicts_are_not_hidden():
    row={'asset_code':'000001','source_date':datetime(2026,9,11).date(),'price':10,
         'provider':'hithink','is_stale':False,'source_event_time':datetime(2026,9,11,10),
         'fetched_at':datetime(2026,9,11,10,0,1)}
    fallback=dict(row,provider='tencent_spot_quote',price=11,fetched_at=datetime(2026,9,11,10,0,2))
    result=watch.project_rows([row,fallback],['000001'],'2026-09-11T10:01:00+08:00')
    assert result['rows'][0]['provider']=='hithink'
    result=watch.project_rows([row,dict(row,price=12),fallback],['000001'],'2026-09-11T10:01:00+08:00')
    assert result['qualified']==0 and result['rows'][0]['state']=='conflicting_same_priority_quotes'


def test_explicit_capture_then_readonly_and_repeated_capture_reuse(tmp_path,monkeypatch):
    (tmp_path/'workspace-config.json').write_text(json.dumps({'read_only':True,'market_database':'synthetic'}))
    monkeypatch.setattr(product,'saved_projection',lambda out:{'prediction':{'rows':[{'instrument':'000001'}]},'notes':[]})
    monkeypatch.setattr(watch,'load_rows',lambda *args:[])
    calls=[]
    original=capture.capture
    def fake(folder,codes):
        calls.append(codes)
        result=original(folder,codes,fetch=lambda batch:raw())
        # Product fixture explicitly emulates the network origin; never production output.
        return dict(result,origin='tencent_https')
    monkeypatch.setattr(capture,'capture',fake)
    original_replay=capture.replay
    monkeypatch.setattr(capture,'replay',lambda folder:dict(original_replay(folder),origin='tencent_https'))
    first=product.observe(tmp_path,capture_quotes=True)
    assert first['provider_requests']==1 and len(calls)==1
    second=product.observe(tmp_path)
    third=product.observe(tmp_path,capture_quotes=True)
    assert second['provider_requests']==third['provider_requests']==0
    assert second['received_rows']==third['received_rows']==1 and len(calls)==1
    assert not first['qualified']  # old source date is never promoted by fresh fetch


def test_old_quote_entry_delegates_to_shared_transport(monkeypatch):
    from trade_system.stock_data_sources import _from_tencent_quote
    calls=[]
    monkeypatch.setattr(transport,'fetch_parts',lambda codes:calls.append(codes) or {'000001':['retained']})
    assert _from_tencent_quote(['000001'])=={'000001':['retained']}
    assert calls==[['000001']]
