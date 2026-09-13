"""Isolated synthetic plans linked to the actual paper decision and ledger core."""
from datetime import timedelta

import pytest

from trade_system.v2 import operator_workflow as workflow, research_product as product
from trade_system.v2.domain import now_utc
from trade_system.v2.storage import Store
from trade_system.v2.paper_storage import submit_confirmed_buy,apply_paper_event
from tests.test_model_independent_workspace import market,command
from tests.test_v2_operator_workflow import seed
from tools.v2.run_event_replay import Clock,CODE,market as paper_market


def make_plan(output, *, until=None, instrument='000001'):
    product.publish_desk(output,market=market())
    note=product.save_note(output,dict(command(),instrument=instrument))
    values={'note_id':note,'operator':command()['operator'],'request_id':'a'*32,
        'condition':'manual','valid_until':until or (now_utc()+timedelta(days=1)).isoformat()}
    key=workflow.save_observation_plan(output,values)
    assert workflow.save_observation_plan(output,values)==key
    return workflow.read_observation_plan(output,key)


def test_no_account_plan_is_durable_expiry_is_not_condition_success(tmp_path):
    p=make_plan(tmp_path)
    assert p['quantity'] is None and p['account_snapshot_id'] is None
    assert workflow.evaluate_observation_plan(p,p['received_at'])['state']=='pending_human_condition'
    assert workflow.evaluate_observation_plan(p,p['valid_until'])['state']=='expired'
    assert workflow.observation_plans(tmp_path,now_utc().isoformat())['rows'][0]['plan_id']==p['plan_id']


def test_price_condition_needs_same_security_current_quote(tmp_path):
    p=dict(make_plan(tmp_path),condition='price_above',threshold='10')
    at=p['received_at'];quote={'instrument':p['instrument'],'price':11,'state':'current_observation_not_executable','valid_until':p['valid_until']}
    assert workflow.evaluate_observation_plan(p,at,quote=quote)['state']=='triggered'
    for changed in (dict(quote,price=None),dict(quote,instrument='600001'),dict(quote,valid_until=at)):
        assert workflow.evaluate_observation_plan(p,at,quote=changed)['state']=='data_insufficient'


def test_human_invalidation_cannot_be_cleared_by_price_or_later_note(tmp_path):
    from trade_system.v2.research_journal import save_review
    p=make_plan(tmp_path)
    save_review(tmp_path,{'note_id':p['note_id'],'request_id':'b'*32,'reviewer':'SYNTHETIC TEST ONLY',
        'evidence':'synthetic invalidation observed','conclusion':'triggered'})
    save_review(tmp_path,{'note_id':p['note_id'],'request_id':'c'*32,'reviewer':'SYNTHETIC TEST ONLY',
        'evidence':'later synthetic observation','conclusion':'not_triggered'})
    assert workflow.observation_plans(tmp_path,now_utc().isoformat())['rows'][0]['evaluation']['state']=='invalid'


def test_linked_plan_uses_actual_core_confirmation_fill_and_retry(tmp_path,monkeypatch):
    from trade_system.v2 import domain
    clock=Clock();monkeypatch.setattr(domain,'now_utc',clock)
    p=make_plan(tmp_path,until='2026-09-10T09:39:00+08:00',instrument=CODE.split('.')[-1])
    with Store(tmp_path/'paper.duckdb',clock=clock) as store:
        draft,manifest=seed(store)
        linked=workflow.link_observation_paper_plan(store,tmp_path,p['plan_id'],draft['decision_id'])
        args=dict(quantity_requested=100,operator='SYNTHETIC TEST ONLY',request_id='intent1',quote_manifest=manifest,acknowledgement=workflow.ACK)
        with pytest.raises(ValueError,match='manual condition'):
            workflow.confirm_linked_observation(store,tmp_path,linked,**args)
        first=workflow.confirm_linked_observation(store,tmp_path,linked,condition_confirmed=True,**args)
        assert first['confirmation']['hypothetical_ready'] and not first['paper_order_created']
        submit_confirmed_buy(store,'fixture-event-paper',first['request_id'],'synthetic-linked-order')
        clock.set('2026-09-10T09:31:01+08:00')
        apply_paper_event(store,'fixture-event-paper',{'event_id':'synthetic-linked-fill','kind':'market','payload':paper_market(clock().isoformat())})
        review=workflow.review_desk(store,'fixture-event-paper')
        assert review['review']['attribution']['paper_portfolio_ledger']['orders'][0]['filled_quantity']==100
        clock.set('2026-09-10T10:00:00+08:00')
        assert workflow.confirm_linked_observation(store,tmp_path,linked,condition_confirmed=True,**args)==first
