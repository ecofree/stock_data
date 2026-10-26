"""Reproduce stale held marks and restart FIFO, without external accounts."""
from datetime import timedelta
import json

import pytest

from tools.v2.run_event_replay import CODE, Clock, paper_config, policies, market
from trade_system.v2.storage import Store
from trade_system.v2.event_bridge import KINDS, ingest_event, event_signal
from trade_system.v2.paper_storage import open_paper, load_paper, submit_confirmed_buy, apply_paper_event, _append
from trade_system.v2.decisions import RiskPolicy, DecisionService
from trade_system.v2.domain import canonical, utc


def setup_buy(store, clock):
    config = paper_config()
    config['opened_at']='2026-09-10T09:30:02+08:00'
    other = 'SZ.000001'
    config['instruments'][other] = dict(config['instruments'][CODE])
    config['initial_lots'] = [{'instrument':other,'quantity':1000,'cost_fen':1_000_000,
        'sellable_from':'2026-09-10','mark_price_fen':1000}]
    for kind,(unit,semantics) in KINDS.items():
        store.register_product('fixture.'+kind,unit,semantics,'event:'+kind,'synthetic_fixture')
    open_paper(store,config)
    def event(kind,payload,key,at):
        if kind=='funds_cumulative':
            payload['counter_epoch']='fixture-epoch'
        return ingest_event(store,'fixture.'+kind,CODE,kind,at,payload,key)
    event('auction_final',{'price':'10','final':True,'relative_volume':'2'},'auction','2026-09-10T09:25:00+08:00')
    event('funds_cumulative',{'net_cny':'1000','metric_version':'fixture-net-v1'},'fund1','2026-09-10T09:30:01+08:00')
    event('funds_cumulative',{'net_cny':'1250','metric_version':'fixture-net-v1'},'fund2',clock().isoformat())
    event('theme_breadth',{'expected':10,'observed':9,'advancing':7,'theme_id':'fixture-theme',
        'membership_version':'fixture-members-v1'},'theme',clock().isoformat())
    event('quote',{'price':'10','phase':'continuous'},'quote',clock().isoformat())
    signal = event_signal(store,CODE,*policies())
    manifest = store.freeze(clock())
    ds = DecisionService(store,RiskPolicy('fixture-risk','.7','.2',100,60,1000,500))
    return config, ds, signal, manifest


@pytest.mark.parametrize('seconds',[59,61])
def test_buy_uses_current_holding_valuation_and_clips_certificate(tmp_path,seconds):
    clock=Clock('2026-09-10T09:30:02+08:00')
    clock.value+=timedelta(seconds=seconds)
    with Store(tmp_path/'paper.duckdb',clock=clock) as store:
        config,ds,signal,manifest=setup_buy(store,clock)
        proposal=ds.propose(config['account_id'],signal,manifest,'fixture.quote')
        if seconds==61:
            assert 'holding_valuation_stale_or_incomplete' in proposal['blockers']
            assert proposal['max_quantity']==0
        else:
            assert proposal['blockers']==[]
            confirmed=ds.confirm(proposal['decision_id'],quantity_requested=100,operator='fixture',
                request_id='confirm',quote_manifest=manifest)
            assert confirmed['hypothetical_ready'] and not confirmed['execution_ready']
            assert utc(confirmed['expires_at'])==utc('2026-09-10T09:31:02+08:00')
            clock.value+=timedelta(seconds=2)
            with pytest.raises(ValueError,match='expired'):
                submit_confirmed_buy(store,config['account_id'],'confirm','order')
            # A persisted pre-fix confirmation had only the longer account TTL.
            body=json.loads(store.con.execute("SELECT payload FROM operator_action WHERE action_id='confirm'").fetchone()[0])
            body['result']['expires_at']='2026-09-10T09:40:00+08:00'
            store.con.execute("UPDATE operator_action SET payload=? WHERE action_id='confirm'",[canonical(body)])
            with pytest.raises(ValueError,match='current complete holding valuation'):
                submit_confirmed_buy(store,config['account_id'],'confirm','order')
            assert not load_paper(store,config['account_id']).state['orders']


@pytest.mark.parametrize('legacy_checkpoint',[False,True])
def test_restart_preserves_submission_fifo_with_limited_market_capacity(tmp_path,legacy_checkpoint):
    clock=Clock('2026-09-10T09:31:00+08:00')
    config=paper_config()
    path=tmp_path/'paper.duckdb'
    with Store(path,clock=clock) as store:
        open_paper(store,config)
        for key in ('z-first','a-second'):
            _append(store,load_paper(store,config['account_id']),{'event_id':key,'at':clock().isoformat(),
                'kind':'submit','payload':{'order_id':key,'instrument':CODE,'side':'buy','quantity':100,
                    'limit_price_fen':1000,'decision_ref':'internal-storage-fixture'}})
            clock.value+=timedelta(seconds=1)
        for i in range(62):
            apply_paper_event(store,config['account_id'],{'event_id':f'padding-{i}',
                'kind':'cash_transfer','payload':{'amount_fen':0,'evidence_id':'zero-fixture'}})
            clock.value+=timedelta(seconds=1)
            if i==60:
                normal=load_paper(store,config['account_id'])
                assert list(normal.state['orders'])==['z-first','a-second']
                assert normal.state==load_paper(store,config['account_id'],full_replay=True).state
        body=json.loads(store.con.execute('SELECT payload FROM paper_checkpoint').fetchone()[0])
        assert body['format']=='state_delta_fifo_v3'
        assert body['order_sequence']==['z-first','a-second']
        if legacy_checkpoint:
            # Recreate the old envelope: no sequence field, same state and prefix.
            body.pop('order_sequence')
            body['format']='state_delta_v2'
            encoded=canonical(body)
            raw=store.archive(encoded.encode())
            store.con.execute('UPDATE paper_checkpoint SET payload=?,raw_hash=?',[encoded,raw])
    with Store(path,clock=clock) as store:
        assert list(load_paper(store,config['account_id']).state['orders'])==['z-first','a-second']
        apply_paper_event(store,config['account_id'],{'event_id':'scarce','kind':'market',
            'payload':market(clock().isoformat(),capacity=100)})
        normal=load_paper(store,config['account_id'])
        replay=load_paper(store,config['account_id'],full_replay=True)
        assert normal.state['fills'][0]['order_id']=='z-first'
        assert normal.state==replay.state
        assert not normal.summary()['execution_ready']
