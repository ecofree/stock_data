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
    from copy import deepcopy
    from trade_system.v2 import journal_index
    (tmp_path/'workspace-config.json').write_text(json.dumps({'read_only':True,'market_database':'synthetic'}))
    desk={'prediction':{'rows':[{'instrument':'600888'}]},'notes':[]}
    attention=['000001']
    # Synthetic effective human attention is explicit observation authorization;
    # a retained model forecast is not an additional capture subject.
    monkeypatch.setattr(journal_index,'effective',lambda out,kind:
        [{'instrument':code} for code in attention] if kind=='note' else [])
    monkeypatch.setattr(product,'saved_projection',lambda out:deepcopy(desk))
    monkeypatch.setattr(watch,'load_rows',lambda *args:[])
    calls=[]
    original=capture.capture
    def fake(folder,codes):
        calls.append(codes)
        result=original(folder,codes,fetch=lambda batch:b''.join(raw(c) for c in batch))
        # Product fixture explicitly emulates the network origin; never production output.
        return dict(result,origin='tencent_https')
    monkeypatch.setattr(capture,'capture',fake)
    original_replay=capture.replay
    monkeypatch.setattr(capture,'replay',lambda folder:dict(original_replay(folder),origin='tencent_https'))
    first=product.observe(tmp_path,capture_quotes=True)
    assert first['provider_requests']==1 and len(calls)==1
    _,files=product.read_current(tmp_path/'observation-publication')
    scope=json.loads(files['observation.json'])['live_scope']
    assert [r['instrument'] for r in scope]==['000001']
    assert scope[0]['roles']==['human_attention']
    assert desk['prediction']['rows']==[{'instrument':'600888'}]
    second=product.observe(tmp_path)
    third=product.observe(tmp_path,capture_quotes=True)
    assert second['provider_requests']==third['provider_requests']==0
    assert second['received_rows']==third['received_rows']==1 and len(calls)==1
    assert not first['qualified']  # old source date is never promoted by fresh fetch
    # A new risk subject must not repeat the just-received (stale) old quote.
    desk['plans']={'account':{'status':'risk_blocked','positions':[{'instrument':'SZ.000002','quantity':1}]}}
    product.observe(tmp_path,capture_quotes=True)
    assert calls==[['000001'],['000002']]
    product.observe(tmp_path,capture_quotes=True)
    assert calls==[['000001'],['000002']]  # overlapping cache segments survive expansion
    (tmp_path/'sampling-current.json').write_text('{broken')
    product.observe(tmp_path)
    _,files=product.read_current(tmp_path/'observation-publication')
    value=json.loads(files['observation.json'])
    assert {r['instrument'] for r in value['rows']}=={'000001','000002'}
    assert 'no_verified_pre_session_scope_for_today' in value['warnings']
    desk['plans']={'account':{'status':'risk_unavailable'}}
    attention[:]=[f'{i:06}' for i in range(1,202)]
    product.observe(tmp_path)
    _,files=product.read_current(tmp_path/'observation-publication')
    value=json.loads(files['observation.json'])
    assert len(value['rows'])==201 and sum(r['state']=='capacity_blocked' for r in value['rows'])==1
    assert any(r['instrument']=='000002' and r['risk_related'] for r in value['live_scope'])
    deferred={r['instrument'] for r in value['rows'] if r['state']=='capacity_blocked'}
    # The next bounded pass rotates the regular tail, even with a fixed risk priority.
    product.observe(tmp_path)
    _,files=product.read_current(tmp_path/'observation-publication')
    rotated=json.loads(files['observation.json'])
    assert not deferred & {r['instrument'] for r in rotated['rows'] if r['state']=='capacity_blocked'}
    monkeypatch.setattr(journal_index,'effective',lambda out,kind: [{'instrument':'600999'}] if kind=='note' else [])
    # A note omitted from the display projection is still a live attention subject.
    product.observe(tmp_path)
    _,files=product.read_current(tmp_path/'observation-publication')
    assert '600999' in {r['instrument'] for r in json.loads(files['observation.json'])['live_scope']}

    # A damaged cache is optional evidence, not a failure of the whole view.
    cache=json.loads((tmp_path/'quote-capture-current.json').read_text())
    from pathlib import Path
    (Path(cache['folder'])/'raw-0.bin').write_bytes(b'changed')
    product.observe(tmp_path)
    _,files=product.read_current(tmp_path/'observation-publication')
    assert 'quote_cache_unusable' in json.loads(files['observation.json'])['warnings']


def test_pre_session_scope_keeps_holdings_rejections_and_controls(monkeypatch,tmp_path):

    from trade_system.v2 import research_journal as journal,operator_workflow
    from trade_system.v2.domain import identity,utc
    db=tmp_path/'calendar.duckdb'
    with duckdb.connect(str(db)) as con:
        con.execute('CREATE TABLE tushare_trade_cal(exchange VARCHAR,is_open INTEGER,cal_date DATE)')
        con.execute("INSERT INTO tushare_trade_cal VALUES ('SSE',1,'2026-09-11'),('SZSE',1,'2026-09-11')")
    (tmp_path/'workspace-config.json').write_text(json.dumps({'read_only':True,'market_database':str(db)}))
    def add(intent,code,at):
        n={'instrument':code,'intent':intent,'received_at':at,'supersedes':None}
        n['note_id']=identity(n);journal.durable_event(tmp_path/'notes',n['note_id'],n)
    add('reject','000002','2026-09-10T08:00:00+08:00')
    add('observe','000003','2026-09-12T08:00:00+08:00')
    monkeypatch.setattr(operator_workflow,'configured_account_risk',lambda *a:{'status':'account_stale_or_future',
        'snapshot_id':'synthetic','positions':[{'instrument':'SZ.000001','quantity':1}]})
    reg=capture.register_sampling(tmp_path,'2026-09-11',['000004'],clock=lambda:utc('2026-09-11T08:00:00+08:00'))
    assert reg['codes']==['000001','000002','000004'] and reg['account_status']=='account_stale_or_future'
    assert capture.register_sampling(tmp_path,'2026-09-11',['000004'],clock=lambda:utc('2026-09-11T08:30:00+08:00'))==reg
    assert capture.read_sampling(tmp_path/'sampling'/reg['sampling_id'],'2026-09-11')==reg
    with pytest.raises(ValueError):capture.read_sampling(tmp_path/'sampling'/reg['sampling_id'],'2026-09-12')
    with pytest.raises(ValueError):capture.register_sampling(tmp_path,'2026-09-11',clock=lambda:utc('2026-09-11T09:15:00+08:00'))
    with pytest.raises(ValueError,match='verified open session'):
        capture.register_sampling(tmp_path,'2026-09-12',['000004'],clock=lambda:utc('2026-09-12T08:00:00+08:00'))
