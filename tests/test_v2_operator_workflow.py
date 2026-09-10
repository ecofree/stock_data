from copy import deepcopy
import json

import pytest

from tools.v2.run_event_replay import CODE, Clock, market, paper_config
from trade_system.v2.accounts import append_account_event, import_snapshot, latest_account
from trade_system.v2.decisions import DecisionService, RiskPolicy
from trade_system.v2.domain import canonical, identity
from trade_system.v2.operator_workflow import ACK, confirm_plan, export_plan, review_desk, render_desk
from trade_system.v2.paper_storage import open_paper, submit_confirmed_buy, apply_paper_event
from trade_system.v2.service import Service
from trade_system.v2.storage import Store
from trade_system.v2.strategies import StrategyPolicy, record_signal


def seed(store):
    open_paper(store,paper_config())
    store.register_product('quote','CNY','point','paper_confirmation','synthetic_fixture')
    store.ingest('quote',CODE,store.clock(),'10','q1',b'{"synthetic_fixture":true}')
    manifest=store.freeze(store.clock())
    sig=record_signal(store,CODE,manifest,StrategyPolicy('fixture','9','11','8','2026-09-10T09:40:00+08:00'),
        at=store.clock().isoformat(),price='10',auction_received_at='2026-09-10T09:26:00+08:00',
        auction_confirmed=True,theme_supported=True,funds_supported=True)
    draft=DecisionService(store,RiskPolicy('fixture','.7','.2',100,60,1000,500)).propose(
        'fixture-event-paper',sig,manifest,'quote')
    return draft,manifest


@pytest.fixture
def env(tmp_path):
    clock=Clock()
    with Store(tmp_path/'paper.duckdb',clock=clock) as store:
        draft,manifest=seed(store)
        yield store,clock,export_plan(store,draft['decision_id']),manifest


def confirm(env,**changes):
    store,_,packet,manifest=env
    args={'quantity_requested':100,'operator':'synthetic-operator','request_id':'intent-1',
          'quote_manifest':manifest,'acknowledgement':ACK,**changes}
    return confirm_plan(store,packet,**args)


def test_packet_confirmation_delivery_and_review_share_identity(env):
    store,clock,packet,_=env
    assert store.con.execute('SELECT count(*) FROM reservation').fetchone()[0]==0
    receipt=confirm(env)
    assert receipt['confirmation']['hypothetical_ready'] and not receipt['paper_order_created']
    before=review_desk(store,'fixture-event-paper')
    assert before['confirmations'][0]['request_id']==receipt['request_id']
    assert before['review']['attribution']['decision_outcomes'][0]['outcome']=='not_delivered_or_unknown'
    submit_confirmed_buy(store,'fixture-event-paper',receipt['request_id'],'fixture-order')
    clock.set('2026-09-10T09:31:01+08:00')
    apply_paper_event(store,'fixture-event-paper',{'event_id':'fixture-fill','kind':'market','payload':market(clock().isoformat())})
    after=review_desk(store,'fixture-event-paper')
    attribution=after['review']['attribution']
    assert attribution['paper_portfolio_ledger']['orders'][0]['filled_quantity']==100
    assert attribution['paper_portfolio_ledger']['reconciliation_difference_fen']==0
    assert attribution['actual_operator_ledger']['return'] is None and not after['execution_ready']
    assert packet['certificate']['snapshot_id']==receipt['confirmation']['snapshot_id']
    page=render_desk(after)
    assert '已有纸面订单' in page and '06 / 确认与成交复盘' in page
    assert '合成测试场景' in page and '真实操作者收益' in page


def test_retry_survives_expiry_and_service_restart(tmp_path):
    clock=Clock();path=tmp_path/'paper.duckdb'
    with Store(path,clock=clock) as store:draft,manifest=seed(store)
    with Service(path,clock=clock) as service:
        packet=service.submit('paper_plan_export',decision_id=draft['decision_id']).result(10)
        args={'packet':packet,'quantity_requested':100,'operator':'fixture','request_id':'one',
            'quote_manifest':manifest,'acknowledgement':ACK}
        first=service.submit('paper_plan_confirm',**args).result(10)
    clock.set('2026-09-10T10:00:00+08:00')
    with Service(path,clock=clock) as service:
        assert service.submit('paper_plan_confirm',**args).result(10)==first
        result=service.submit('paper_desk_review',account_id='fixture-event-paper').result(10)
        assert len(result['confirmations'])==1
        with pytest.raises(ValueError,match='idempotency'):
            service.submit('paper_plan_confirm',**{**args,'quantity_requested':200}).result(10)


@pytest.mark.parametrize('change',['policy','price','scope','command','decision_id'])
def test_resealed_client_packet_never_overrides_database(env,change):
    store,clock,p,manifest=env;p=deepcopy(p)
    if change=='policy':p['certificate']['policy']['max_quantity']=100000
    if change=='price':p['certificate']['price_fen']=1
    if change=='scope':p['scope']='live'
    if change=='command':p['command']='paper_buy'
    if change=='decision_id':p['decision_id']='unknown'
    p['packet_id']=identity({k:v for k,v in p.items() if k!='packet_id'})
    with pytest.raises(ValueError):confirm((store,clock,p,manifest))
    assert store.con.execute('SELECT count(*) FROM reservation').fetchone()[0]==0


@pytest.mark.parametrize('change',['expired','account','external','quote','signal'])
def test_fresh_conditions_rechecked_at_confirmation(env,change):
    store,clock,packet,_=env
    if change=='expired':clock.set('2026-09-10T09:32:01+08:00')
    if change=='account':
        a=latest_account(store,'fixture-event-paper')['payload'];a.pop('ledger_hash')
        a['source']='manual_declaration'
        import_snapshot(store,canonical(a).encode())
    if change=='external':append_account_event(store,'external','fixture-event-paper','external_action_unknown',{})
    args={}
    if change=='quote':
        store.ingest('quote',CODE,store.clock(),'12','q2',b'{"synthetic_fixture":true}')
        args['quote_manifest']=store.freeze(store.clock())
    if change=='signal':
        manifest=store.freeze(store.clock())
        record_signal(store,CODE,manifest,StrategyPolicy('fixture','9','11','8','2026-09-10T09:40:00+08:00'),
            at=store.clock().isoformat(),price='7',auction_received_at=None,auction_confirmed=False,theme_supported=False,funds_supported=False)
    result=confirm(env,**args)['confirmation']
    assert result['blockers'] and not result['hypothetical_ready']
    assert store.con.execute('SELECT count(*) FROM reservation').fetchone()[0]==0


def test_ack_and_operator_are_explicit(env):
    for kwargs in ({'acknowledgement':''},{'operator':'  '},{'request_id':''},{'quantity_requested':True}):
        with pytest.raises(ValueError):confirm(env,**kwargs)


def test_expired_export_and_confirmed_certificate_not_exportable(env):
    store,clock,packet,_=env
    confirmed=confirm(env)['confirmation']
    with pytest.raises(ValueError,match='original'):export_plan(store,confirmed['decision_id'])
    clock.set('2026-09-10T10:00:00+08:00')
    with pytest.raises(ValueError,match='expired'):export_plan(store,packet['decision_id'])


def test_review_catches_corrupted_confirmation(env):
    store,_,_,_=env;confirm(env)
    raw=json.loads(store.con.execute('SELECT payload FROM operator_action').fetchone()[0])
    raw['result']['quantity']=999
    store.con.execute('UPDATE operator_action SET payload=?',[canonical(raw)])
    with pytest.raises(ValueError):review_desk(store,'fixture-event-paper')


def test_html_escapes_declared_operator_and_rejects_changed_report(env):
    confirm(env,operator='<script>alert(1)</script>')
    report=review_desk(env[0],'fixture-event-paper')
    assert '<script>alert(1)</script>' not in render_desk(report)
    report['confirmations'][0]['request']['quantity']=999
    with pytest.raises(ValueError,match='fingerprint'):render_desk(report)
