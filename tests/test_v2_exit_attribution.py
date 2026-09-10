from copy import deepcopy
import json

import pytest

from tools.v2.run_event_replay import CODE, Clock, market, paper_config, run_replay
from trade_system.v2.accounts import append_account_event, import_snapshot, latest_account
from trade_system.v2.attribution import project_attribution, render_attribution_markdown
from trade_system.v2.decisions import DecisionService, RiskPolicy
from trade_system.v2.domain import canonical, identity
from trade_system.v2.exit_policy import ensure_exits, submit_confirmed_sell
from trade_system.v2.paper_storage import apply_paper_event, load_paper, open_paper, submit_confirmed_buy
from trade_system.v2.reporting import project_review
from trade_system.v2.storage import Store


@pytest.fixture
def env(tmp_path):
    clock = Clock()
    with Store(tmp_path/'paper.duckdb',clock=clock) as store:
        config = paper_config()
        config['initial_lots']=[{'instrument':CODE,'quantity':250,'cost_fen':240000,'mark_price_fen':1000,
                                'sellable_from':'2026-09-10'}]
        open_paper(store,config)
        store.register_product('quote','CNY','point','paper_confirmation','fixture')
        quote(store,'q1')
        authority = DecisionService(store,RiskPolicy('fixture','.1','.1',100,30,1000,500))
        yield store,clock,authority


def quote(store,key,price='10'):
    store.ingest('quote',CODE,store.clock(),price,key,canonical({'fixture_price':price}).encode())
    return store.freeze(store.clock())


def policy(**changes):
    return {'version':'fixture-exit','reason':'manual_reduce','reference_id':'fixture-reduce',
            'expires_at':'2026-09-10T09:35:00+08:00',**changes}


def propose(env,**changes):
    store,_,authority = env
    return authority.propose_exit('fixture-event-paper',CODE,store.freeze(store.clock()),'quote',policy(**changes))


def confirm(env,draft,qty=100,request='exit-confirm'):
    store,_,authority = env
    return authority.confirm(draft['decision_id'],quantity_requested=qty,operator='fixture-operator',request_id=request,
                             quote_manifest=store.freeze(store.clock()))


def tick(env,key='tick',price=1000,capacity=100):
    store,clock,_ = env
    return apply_paper_event(store,'fixture-event-paper',{'event_id':key,'kind':'market',
            'payload':market(clock().isoformat(),price=price,capacity=capacity)})


def test_reduce_only_ignores_buy_exposure_ceiling_and_needs_no_buy_signal(env):
    store,_,_ = env
    draft = propose(env)
    assert draft['max_quantity']==250 and not draft['blockers']
    assert store.con.execute('SELECT count(*) FROM signal_event').fetchone()[0]==0
    result = confirm(env,draft,qty=250)
    assert result['hypothetical_ready'] and result['reduce_only'] and not result['execution_ready']
    submitted = submit_confirmed_sell(store,'fixture-event-paper','exit-confirm','sell')
    assert submitted['frozen_fen']==0
    assert load_paper(store,'fixture-event-paper').available(CODE,store.clock())==0
    assert store.con.execute('SELECT status FROM exit_reservation').fetchone()[0]=='paper_order'
    assert submit_confirmed_sell(store,'fixture-event-paper','exit-confirm','sell')==submitted
    with pytest.raises(ValueError,match='another paper order'):
        submit_confirmed_sell(store,'fixture-event-paper','exit-confirm','different')
    with pytest.raises(ValueError,match='sell confirmation'):
        submit_confirmed_buy(store,'fixture-event-paper','exit-confirm','invalid-buy')


def test_separate_share_reservations_prevent_double_confirmation(env):
    store,_,_ = env
    first = propose(env)
    assert confirm(env,first,qty=200)['hypothetical_ready']
    assert not confirm(env,first,qty=100,request='second')['hypothetical_ready']
    second = propose(env)
    assert second['max_quantity']==50
    assert confirm(env,second,qty=50,request='tail')['hypothetical_ready']
    assert propose(env)['max_quantity']==0
    assert store.con.execute('SELECT sum(quantity) FROM exit_reservation').fetchone()[0]==250


@pytest.mark.parametrize('qty',[0,1,49,50,99,150,251])
def test_exit_quantity_cannot_bypass_frozen_lot_or_inventory(env,qty):
    draft = propose(env)
    if qty==0:
        with pytest.raises(ValueError): confirm(env,draft,qty=qty)
    else:
        result = confirm(env,draft,qty=qty)
        assert not result['hypothetical_ready'] and result['quantity']==0


@pytest.mark.parametrize('change',['new_quote','stale','account_event','account_snapshot','policy','inventory'])
def test_confirmed_exit_rechecks_before_order_delivery(env,change):
    store,clock,_ = env
    draft = propose(env); confirm(env,draft)
    if change=='new_quote': quote(store,'revised','10.1')
    if change=='stale': clock.set('2026-09-10T09:31:31+08:00')
    if change=='account_event': append_account_event(store,'external','fixture-event-paper','external_action_unknown',{})
    if change=='account_snapshot':
        payload = latest_account(store,'fixture-event-paper')['payload']
        payload['source']='manual_declaration'; payload.pop('ledger_hash')
        import_snapshot(store,canonical(payload).encode())
    if change=='policy':
        store.con.execute("UPDATE exit_policy SET sha256='tampered'")
    if change=='inventory':
        clock.set('2026-09-10T09:31:01+08:00'); tick(env)
    with pytest.raises(ValueError):
        submit_confirmed_sell(store,'fixture-event-paper','exit-confirm','sell')
    assert not load_paper(store,'fixture-event-paper').state['orders']


def test_unknown_reservation_stays_held_and_old_certificate_not_reusable(env):
    store,clock,authority = env
    result = confirm(env,propose(env),qty=250)
    authority.mark_exit_unknown(result['reservation_id'])
    clock.set('2026-09-10T09:31:10+08:00')
    assert propose(env)['max_quantity']==0
    with pytest.raises(ValueError,match='unknown'):
        submit_confirmed_sell(store,'fixture-event-paper','exit-confirm','sell')
    assert store.con.execute('SELECT status FROM exit_reservation').fetchone()[0]=='unknown'


def test_price_condition_requires_threshold_and_policy_is_immutable(env):
    store,_,authority = env
    draft = propose(env,reason='price_below',trigger_price_fen=900)
    assert 'exit_price_condition_not_met' in draft['blockers']
    assert not confirm(env,draft)['hypothetical_ready']
    with pytest.raises(ValueError,match='immutable'):
        propose(env,reason='price_below',trigger_price_fen=1000)
    quote(store,'down','8.9')
    fresh = authority.propose_exit('fixture-event-paper',CODE,store.freeze(store.clock()),'quote',
                                  policy(version='new-rule',reason='price_below',trigger_price_fen=900))
    assert not fresh['blockers']


def test_t_plus_and_old_valuation_block_reduce_only(tmp_path):
    clock = Clock()
    with Store(tmp_path/'paper.duckdb',clock=clock) as store:
        config = paper_config()
        config['initial_lots']=[{'instrument':CODE,'quantity':100,'cost_fen':100000,'mark_price_fen':1000,'sellable_from':'2026-09-11'}]
        open_paper(store,config); store.register_product('quote','CNY','point','paper_confirmation','fixture'); quote(store,'q')
        authority = DecisionService(store,RiskPolicy('fixture','.5','.5',100,30,1000,500))
        draft = authority.propose_exit('fixture-event-paper',CODE,store.freeze(clock()),'quote',policy())
        assert 'no_unreserved_settled_inventory' in draft['blockers']
        clock.set('2026-09-11T09:31:00+08:00'); quote(store,'next')
        later = authority.propose_exit('fixture-event-paper',CODE,store.freeze(clock()),'quote',
                                      policy(version='tomorrow',expires_at='2026-09-11T09:35:00+08:00'))
        assert 'current_valuation_incomplete' in later['blockers']


def test_partial_cancel_attribution_reconciles_without_double_counting_fees(env):
    store,clock,_ = env
    confirm(env,propose(env),qty=250)
    submit_confirmed_sell(store,'fixture-event-paper','exit-confirm','sell')
    clock.set('2026-09-10T09:31:02+08:00'); tick(env,price=1100,capacity=40)
    apply_paper_event(store,'fixture-event-paper',{'event_id':'cancel','kind':'cancel_request','payload':{'order_id':'sell'}})
    clock.set('2026-09-10T09:31:04+08:00'); tick(env,'second',price=1100,capacity=60)
    apply_paper_event(store,'fixture-event-paper',{'event_id':'ack','kind':'cancel_ack','payload':{'order_id':'sell','evidence_id':'fixture'}})
    before = store.con.execute('SELECT last_seq,last_hash FROM paper_account').fetchone()
    data = project_attribution(store,'fixture-event-paper')
    p = data['paper_portfolio_ledger']; o = p['orders'][0]
    assert o['filled_quantity']==100 and o['unfilled_quantity']==150 and o['status']=='cancelled'
    assert o['execution_price_effect_fen']==10000 and o['fees_fen']==500
    assert o['execution_effect_after_fees_fen']==9500 and o['weighted_confirmation_to_fill_seconds']=='3.2'
    assert p['components']=={'realized_pnl_fen':13500,'unrealized_pnl_fen':21000,'initial_unrealized_pnl_fen':10000,'income_fen':0}
    assert p['explained_profit_fen']==24500 and p['reconciliation_difference_fen']==0
    assert data['actual_operator_ledger']['trades'] is None and data['actual_operator_ledger']['return'] is None
    assert data['opportunity_ledger']['selection_skill_status']=='not_estimated'
    assert project_review(store,'fixture-event-paper')['attribution']['projection_id']==data['projection_id']
    assert before==store.con.execute('SELECT last_seq,last_hash FROM paper_account').fetchone()
    assert '不可把' in render_attribution_markdown(data)


def test_attribution_detects_corrupt_decision_and_exposes_stale_marks(env):
    store,clock,_ = env
    result = confirm(env,propose(env)); submit_confirmed_sell(store,'fixture-event-paper','exit-confirm','sell')
    clock.set('2026-09-10T09:31:01+08:00'); tick(env)
    clock.set('2026-09-10T09:33:00+08:00')
    assert project_attribution(store,'fixture-event-paper')['current_valuation_complete'] is False
    payload = json.loads(store.con.execute('SELECT payload FROM decision_certificate WHERE decision_id=?',[result['decision_id']]).fetchone()[0])
    payload['price_fen']=1200
    store.con.execute('UPDATE decision_certificate SET payload=? WHERE decision_id=?',[canonical(payload),result['decision_id']])
    with pytest.raises(ValueError,match='checksum'):
        project_attribution(store,'fixture-event-paper')


def test_direct_sell_rejected_and_schema_checksum_preserved(env):
    store,_,_ = env
    original = store.con.execute('SELECT version,sha256 FROM v2_schema ORDER BY version').fetchall()
    with pytest.raises(ValueError,match='approved paper decision'):
        apply_paper_event(store,'fixture-event-paper',{'event_id':'direct','kind':'submit','payload':{'side':'sell'}})
    ensure_exits(store)
    assert original==store.con.execute('SELECT version,sha256 FROM v2_schema WHERE version<>5 ORDER BY version').fetchall()
    store.con.execute("UPDATE v2_schema SET sha256='corrupt' WHERE version=5")
    with pytest.raises(ValueError,match='checksum'): ensure_exits(store)


def test_replay_artifact_has_both_approved_sides_and_linked_opportunity(tmp_path):
    result = run_replay(tmp_path/'run')
    root = tmp_path/'run/reports/runs/replay'
    data = json.loads((root/'attribution.json').read_text(encoding='utf-8'))
    assert data['ledger_state_hash']==result['replayed_state_hash']
    assert {o['side'] for o in data['paper_portfolio_ledger']['orders']}=={'buy','sell'}
    assert not data['paper_portfolio_ledger']['unlinked_order_ids']
    assert len(data['opportunity_ledger']['signals'])==1
    assert len(data['operator_decision_ledger'])==2
    assert (root/'attribution.md').exists()
    frozen = deepcopy(data); frozen.pop('projection_id')
    assert identity(frozen)==data['projection_id']


def test_sell_confirmation_idempotency_and_quantity_conflict(env):
    draft = propose(env)
    result = confirm(env,draft)
    assert confirm(env,draft)==result
    with pytest.raises(ValueError,match='idempotency'):
        confirm(env,draft,qty=200)


def test_reservation_transfer_and_projection_rollback_atomically(env,monkeypatch):
    store,_,_ = env
    result = confirm(env,propose(env))
    import trade_system.v2.paper_storage as module
    def fail(*args): raise RuntimeError('projection injected failure')
    monkeypatch.setattr(module,'_projection',fail)
    before = latest_account(store,'fixture-event-paper')['snapshot_id']
    with pytest.raises(RuntimeError,match='injected'):
        submit_confirmed_sell(store,'fixture-event-paper','exit-confirm','sell')
    assert not load_paper(store,'fixture-event-paper').state['orders']
    assert before==latest_account(store,'fixture-event-paper')['snapshot_id']
    assert store.con.execute('SELECT status FROM exit_reservation WHERE reservation_id=?',[result['reservation_id']]).fetchone()[0]=='held'


def test_blocked_and_undelivered_confirmations_are_not_reported_as_fills(env):
    store,_,_ = env
    confirm(env,propose(env),qty=1,request='blocked')
    confirm(env,propose(env),qty=100,request='held')
    data = project_attribution(store,'fixture-event-paper')
    assert {o['outcome'] for o in data['decision_outcomes']}=={'confirmation_blocked','not_delivered_or_unknown'}
    assert data['paper_portfolio_ledger']['orders']==[]
    assert data['actual_operator_ledger']['trades'] is None


def test_policy_checksum_is_rechecked_during_confirmation(env):
    store,_,_ = env
    draft = propose(env)
    store.con.execute("UPDATE v2_schema SET sha256='corrupt' WHERE version=5")
    with pytest.raises(ValueError,match='checksum'):
        confirm(env,draft)


def test_attribution_cash_flow_and_dividend_reconcile(env):
    store,_,_ = env
    before = project_attribution(store,'fixture-event-paper')
    apply_paper_event(store,'fixture-event-paper',{'event_id':'deposit','kind':'cash_transfer',
                      'payload':{'amount_fen':50000,'evidence_id':'fixture'}})
    apply_paper_event(store,'fixture-event-paper',{'event_id':'dividend','kind':'cash_dividend',
                      'payload':{'instrument':CODE,'action_id':'dividend','evidence_id':'fixture',
                                 'entitled_quantity':200,'cash_per_share_fen':10}})
    after = project_attribution(store,'fixture-event-paper')
    assert after['paper_portfolio_ledger']['explained_profit_fen']-before['paper_portfolio_ledger']['explained_profit_fen']==2000
    assert after['paper_portfolio_ledger']['reconciliation_difference_fen']==0


def test_attribution_query_never_creates_ledger_tables(tmp_path):
    with Store(tmp_path/'empty.duckdb',clock=Clock()) as store:
        before = store.con.execute('SHOW TABLES').fetchall()
        with pytest.raises(ValueError,match='never initializes'):
            project_attribution(store,'unknown')
        assert before==store.con.execute('SHOW TABLES').fetchall()
        assert store.con.execute('SELECT version FROM v2_schema').fetchall()==[(1,)]
