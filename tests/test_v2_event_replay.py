from copy import deepcopy
import json

import pytest

from tools.v2.run_event_replay import CODE, Clock, market, paper_config, policies, run_replay
from trade_system.v2.accounts import append_account_event, latest_account
from trade_system.v2.decisions import DecisionService, RiskPolicy
from trade_system.v2.domain import identity
from trade_system.v2.event_bridge import KINDS, EventPolicy, derive_inputs, event_signal, ingest_event, recheck_signal_evidence
from trade_system.v2.paper_ledger import PaperBook
from trade_system.v2.paper_storage import apply_paper_event, load_paper, open_paper, submit_confirmed_buy
from trade_system.v2.storage import Store
from trade_system.v2.strategies import signal


@pytest.fixture
def setup(tmp_path):
    clock = Clock()
    with Store(tmp_path/'paper.duckdb',clock=clock) as store:
        for kind,(unit,semantics) in KINDS.items():
            store.register_product('fixture.'+kind,unit,semantics,'event:'+kind,'synthetic_fixture')
        yield store,clock


def event(store,kind,payload,key=None,at=None,mode='system_replay'):
    return ingest_event(store,'fixture.'+kind,CODE,kind,at or store.clock().isoformat(),payload,
                        key or kind,mode=mode)


def ready(store):
    event(store,'auction_final',{'price':'10','final':True,'relative_volume':'2'},at='2026-09-10T09:25:00+08:00')
    event(store,'funds_cumulative',{'net_cny':'1000','metric_version':'fixture-net-v1'},'f1','2026-09-10T09:30:00+08:00')
    event(store,'funds_cumulative',{'net_cny':'1250','metric_version':'fixture-net-v1'},'f2')
    event(store,'theme_breadth',{'expected':10,'observed':9,'advancing':7,'theme_id':'fixture-theme','membership_version':'fixture-members-v1'})
    event(store,'quote',{'price':'10','phase':'continuous'})
    return event_signal(store,CODE,*policies())


def confirm(store,fee=500):
    open_paper(store,paper_config())
    sig = ready(store)
    ds = DecisionService(store,RiskPolicy('fixture-risk','.7','.2',100,60,1000,fee))
    manifest = store.freeze(store.clock())
    draft = ds.propose('fixture-event-paper',sig,manifest,'fixture.quote')
    result = ds.confirm(draft['decision_id'],quantity_requested=100,operator='fixture',request_id='confirm',quote_manifest=manifest)
    assert result['hypothetical_ready']
    return result


def test_event_receipt_bridge_and_cumulative_delta(setup):
    store,_ = setup
    key = ready(store)
    sig = signal(store,key)
    assert sig['state']=='triggered' and sig['evidence']['fund_delta_cny']=='250'
    count = store.con.execute('SELECT count(*) FROM market_event').fetchone()[0]
    assert not event(store,'funds_cumulative',{'net_cny':'1250','metric_version':'fixture-net-v1'},'f2')['inserted']
    assert count==store.con.execute('SELECT count(*) FROM market_event').fetchone()[0]
    with pytest.raises(ValueError,match='idempotency'):
        event(store,'funds_cumulative',{'net_cny':'1251','metric_version':'fixture-net-v1'},'f2')
    assert recheck_signal_evidence(store,sig)==[]


def test_indication_never_becomes_final_and_requires_two_times(setup):
    store,_ = setup
    event(store,'auction_indicative',{'price':'10','final':False,'relative_volume':'3'})
    sig = event_signal(store,CODE,*policies())
    assert signal(store,sig)['state']=='watch'
    with pytest.raises(ValueError,match='finality'):
        event(store,'auction_final',{'price':'10','final':False,'relative_volume':'3'})
    event(store,'funds_cumulative',{'net_cny':'1','metric_version':'fixture-net-v1'},'f1')
    event(store,'funds_cumulative',{'net_cny':'1000','metric_version':'fixture-net-v1'},'f2')
    _,evidence = derive_inputs(store,CODE,EventPolicy(**policies()[1]))
    assert evidence['fund_delta_cny'] is None


@pytest.mark.parametrize('change',['stale','theme','funds','phase','historical','policy'])
def test_confirmation_rechecks_frozen_event_evidence(setup,change):
    store,clock = setup
    sig = signal(store,ready(store))
    if change=='stale':
        clock.set('2026-09-10T09:31:31+08:00')
    elif change=='theme':
        event(store,'theme_breadth',{'expected':10,'observed':10,'advancing':1,'theme_id':'fixture-theme','membership_version':'fixture-members-v1'},'new-theme')
    elif change=='funds':
        event(store,'funds_cumulative',{'net_cny':'900','metric_version':'fixture-net-v2'},'new-funds')
    elif change in ('phase','historical'):
        event(store,'quote',{'price':'10','phase':'auction' if change=='phase' else 'continuous'},'new-quote',
              mode='historical_research' if change=='historical' else 'system_replay')
    else:
        strategy,ep = policies(); ep['min_fund_delta_cny']='200'
        with pytest.raises(ValueError,match='new strategy version'):
            event_signal(store,CODE,strategy,ep)
        return
    assert recheck_signal_evidence(store,sig)


def test_future_and_wrong_product_rejected(setup):
    store,_ = setup
    with pytest.raises(ValueError,match='future'):
        event(store,'quote',{'price':'10','phase':'continuous'},at='2026-09-10T09:32:00+08:00')
    with pytest.raises(ValueError,match='contract'):
        ingest_event(store,'fixture.quote',CODE,'funds_cumulative',store.clock(),{},'bad')


def test_event_checksum_and_additive_migrations(setup):
    store,_ = setup
    ready(store); open_paper(store,paper_config())
    assert [r[0] for r in store.con.execute('SELECT version FROM v2_schema ORDER BY version').fetchall()]==[1,3,4]
    store.con.execute("UPDATE market_event SET raw_hash='tampered' WHERE kind='quote'")
    with pytest.raises(ValueError,match='checksum'):
        derive_inputs(store,CODE,EventPolicy(**policies()[1]))
    store.con.execute("UPDATE v2_schema SET sha256='tampered' WHERE version=4")
    with pytest.raises(ValueError,match='migration checksum'):
        load_paper(store,'fixture-event-paper')


def apply(book,key,kind,payload,at='2026-09-10T09:31:00+08:00'):
    return book.apply({'event_id':key,'kind':kind,'payload':payload,'at':at})


def order(book,key='buy',side='buy',qty=100,price=1000,at='2026-09-10T09:31:00+08:00'):
    return apply(book,key,'submit',{'order_id':key,'instrument':CODE,'side':side,'quantity':qty,
                                   'limit_price_fen':price,'decision_ref':'fixture'},at)


def tick(book,key='tick',at='2026-09-10T09:31:01+08:00',**kwargs):
    return apply(book,key,'market',market(at,**kwargs),at)


def test_partial_fees_settlement_and_cost_basis_gold():
    book = PaperBook(paper_config())
    order(book)
    assert book.frozen()==100500
    tick(book,capacity=40)
    assert book.state['cash_fen']==959500 and book.frozen()==60000
    tick(book,'tick2','2026-09-10T09:31:02+08:00',capacity=60)
    assert book.holding(CODE)==100 and book.state['fees_fen']==500
    before = deepcopy(book.state)
    with pytest.raises(ValueError,match=r'T\+N'):
        order(book,'sell','sell',at='2026-09-10T09:31:03+08:00')
    assert before==book.state
    order(book,'sell','sell',price=1100,at='2026-09-11T09:31:00+08:00')
    tick(book,'sell-tick','2026-09-11T09:31:01+08:00',price=1100)
    assert book.state['cash_fen']==1009000 and book.state['realized_pnl_fen']==9000
    assert book.state['fees_fen']==1000 and sum(l['cost_fen'] for l in book.state['lots'])==0


@pytest.mark.parametrize('changes',[{'ask_quantity':0},{'tradable':False},{'phase':'auction'},
                                    {'evidence_kind':'daily_bar'},{'ask_fen':1100}])
def test_nonobservable_or_unfillable_liquidity_never_fills(changes):
    book = PaperBook(paper_config()); order(book)
    tick(book,**changes)
    assert not book.state['fills'] and book.frozen()==100500


def test_capacity_fifo_and_repeated_snapshot_cannot_double_fill():
    book = PaperBook(paper_config()); order(book); order(book,'buy2')
    tick(book,capacity=150)
    assert [o['remaining'] for o in book.state['orders'].values()]==[0,50]
    tick(book,'same-source',capacity=150)
    assert book.holding(CODE)==150


def test_order_does_not_fill_on_preexisting_quote_and_stale_quote():
    book = PaperBook(paper_config()); order(book)
    tick(book,at='2026-09-10T09:31:00+08:00')
    assert not book.state['fills']
    apply(book,'stale','market',market('2026-09-10T09:31:01+08:00'),'2026-09-10T09:32:00+08:00')
    assert not book.state['fills'] and book.state['orders']['buy']['last_blocker']=='stale_quote'


def test_cancel_pending_can_fill_but_unknown_requires_resolution():
    book = PaperBook(paper_config()); order(book)
    apply(book,'request','cancel_request',{'order_id':'buy'})
    tick(book,capacity=40)
    assert book.state['orders']['buy']['status']=='cancel_pending' and book.frozen()==60000
    apply(book,'ack','cancel_ack',{'order_id':'buy','evidence_id':'explicit'},'2026-09-10T09:31:02+08:00')
    assert book.frozen()==0 and book.holding(CODE)==40
    order(book,'unknown-buy',at='2026-09-10T09:31:02+08:00')
    apply(book,'unknown','unknown',{'order_id':'unknown-buy'},'2026-09-10T09:31:02+08:00')
    tick(book,'later','2026-09-10T09:31:03+08:00')
    assert book.holding(CODE)==40 and book.frozen()==100500
    with pytest.raises(ValueError,match='resolved'):
        apply(book,'cancel','cancel_request',{'order_id':'unknown-buy'},'2026-09-10T09:31:03+08:00')
    apply(book,'resolve','resolve_open',{'order_id':'unknown-buy','evidence_id':'explicit'},'2026-09-10T09:31:03+08:00')
    tick(book,'resolved-tick','2026-09-10T09:31:04+08:00')
    assert book.holding(CODE)==140


def test_cash_flows_dividends_and_splits_are_not_price_profit():
    cfg = paper_config()
    cfg['initial_lots']=[{'instrument':CODE,'quantity':100,'cost_fen':100000,'mark_price_fen':1000,'sellable_from':'2026-09-10'}]
    book = PaperBook(cfg)
    apply(book,'deposit','cash_transfer',{'amount_fen':100000,'evidence_id':'fixture'},cfg['opened_at'])
    assert book.summary()['profit_ex_external_cash_fen']==0 and book.summary()['max_observed_drawdown']=='0'
    apply(book,'split','split',{'instrument':CODE,'action_id':'split','evidence_id':'fixture','numerator':2,
                             'denominator':1,'post_action_mark_fen':500},cfg['opened_at'])
    assert book.holding(CODE)==200 and book.state['lots'][0]['cost_fen']==100000
    assert book.summary()['profit_ex_external_cash_fen']==0
    apply(book,'dividend','cash_dividend',{'instrument':CODE,'action_id':'dividend','evidence_id':'record-date-entitlement',
                                        'entitled_quantity':100,'cash_per_share_fen':10},cfg['opened_at'])
    assert book.summary()['income_fen']==1000


def test_drawdown_and_incomplete_marks():
    book = PaperBook(paper_config()); order(book); tick(book)
    tick(book,'drop','2026-09-10T09:31:02+08:00',price=900)
    assert float(book.summary()['max_observed_drawdown'])==.0105
    assert not book.complete_valuation('2026-09-11T09:31:00+08:00')
    with pytest.raises(ValueError,match='current valuation'):
        apply(book,'deposit','cash_transfer',{'amount_fen':100,'evidence_id':'fixture'},'2026-09-11T09:31:00+08:00')


def test_odd_lot_exit_and_sell_inventory_reservation():
    cfg = paper_config()
    cfg['initial_lots']=[{'instrument':CODE,'quantity':150,'cost_fen':150000,'mark_price_fen':1000,'sellable_from':'2026-09-10'}]
    book = PaperBook(cfg)
    with pytest.raises(ValueError,match='odd-lot'):
        order(book,'bad','sell',qty=50)
    order(book,'sell','sell',qty=150)
    with pytest.raises(ValueError,match=r'T\+N'):
        order(book,'double-sell','sell',qty=100)
    tick(book,capacity=40)
    tick(book,'second','2026-09-10T09:31:02+08:00',capacity=110)
    assert book.state['realized_pnl_fen']==-500 and book.state['fees_fen']==500
    assert book.holding(CODE)==0 and book.state['lots'][0]['cost_fen']==0


def test_generic_buy_and_backdated_service_events_are_rejected(setup):
    store,_ = setup
    open_paper(store,paper_config())
    request = {'event_id':'buy','kind':'submit','payload':{'side':'buy'}}
    with pytest.raises(ValueError,match='approved paper decision'):
        apply_paper_event(store,'fixture-event-paper',request)
    with pytest.raises(ValueError,match='receipt time'):
        apply_paper_event(store,'fixture-event-paper',{**request,'at':'2026-09-09T09:31:00+08:00'})


@pytest.mark.parametrize('case',['cash','lot','calendar','split_open'])
def test_invalid_events_roll_back_whole_state(case):
    cfg = paper_config()
    if case=='cash': cfg['initial_cash_fen']=100000
    if case=='calendar': cfg['trading_days']=['2026-09-10']
    book = PaperBook(cfg)
    if case in ('calendar','split_open'): order(book)
    before = deepcopy(book.state)
    with pytest.raises(ValueError):
        if case=='cash': order(book)
        elif case=='lot': order(book,qty=99)
        elif case=='calendar': tick(book)
        else: apply(book,'split','split',{'instrument':CODE,'action_id':'split','evidence_id':'fixture',
                                         'numerator':2,'denominator':1,'post_action_mark_fen':500})
    assert before==book.state


def test_reservation_transfer_idempotency_and_restart(setup):
    store,clock = setup
    confirmed = confirm(store)
    result = submit_confirmed_buy(store,'fixture-event-paper','confirm','buy')
    assert result['frozen_fen']==100500
    assert store.con.execute('SELECT status FROM reservation WHERE reservation_id=?',[confirmed['reservation_id']]).fetchone()[0]=='paper_order'
    assert latest_account(store,'fixture-event-paper')['frozen_fen']==100500
    assert submit_confirmed_buy(store,'fixture-event-paper','confirm','buy')==result
    with pytest.raises(ValueError,match='another paper order'):
        submit_confirmed_buy(store,'fixture-event-paper','confirm','buy2')
    clock.set('2026-09-10T09:31:01+08:00')
    request = {'event_id':'fill','kind':'market','payload':market(clock().isoformat())}
    applied = apply_paper_event(store,'fixture-event-paper',request)
    assert apply_paper_event(store,'fixture-event-paper',request)==applied
    assert load_paper(store,'fixture-event-paper').summary()==applied
    assert latest_account(store,'fixture-event-paper')['payload']['ledger_hash']==identity(load_paper(store,'fixture-event-paper').state)


@pytest.mark.parametrize('change',['unknown','fee','account_event','new_signal','stale','tamper'])
def test_delivery_and_journal_fail_closed(setup,change):
    store,clock = setup
    result = confirm(store,fee=0 if change=='fee' else 500)
    if change=='unknown': DecisionService(store,RiskPolicy('x','.7','.2',100,60,1000,500)).mark_unknown(result['reservation_id'])
    if change=='account_event': append_account_event(store,'external','fixture-event-paper','external_action_unknown',{})
    if change=='stale': clock.set('2026-09-10T09:32:00+08:00')
    if change=='new_signal':
        event(store,'quote',{'price':'9.5','phase':'continuous'},'new-quote')
        event_signal(store,CODE,*policies())
    if change=='tamper':
        submit_confirmed_buy(store,'fixture-event-paper','confirm','buy')
        store.con.execute('DELETE FROM paper_ledger_event')
        with pytest.raises(ValueError,match='head mismatch'):
            load_paper(store,'fixture-event-paper')
        return
    with pytest.raises(ValueError):
        submit_confirmed_buy(store,'fixture-event-paper','confirm','buy')
    assert not load_paper(store,'fixture-event-paper').state['orders']


def test_service_vertical_slice_and_separate_ledgers(tmp_path):
    result = run_replay(tmp_path/'replay')
    assert result['signals']==['watch','armed','triggered'] and result['fills']==3
    data = json.loads((tmp_path/'replay/reports/runs/replay/evidence.json').read_text(encoding='utf-8'))
    assert data['actual_operator_ledger']==[] and data['execution_ready'] is False
    assert data['summary']['realized_pnl_fen']==9000
    assert data['replayed_state_hash']==result['replayed_state_hash']


def test_fee_config_and_money_projection_exact(setup):
    store,_ = setup
    cfg = paper_config(); cfg['initial_cash_fen']=9999999999999999
    open_paper(store,cfg)
    assert latest_account(store,cfg['account_id'])['payload']['cash']=='99999999999999.99'
    cfg['fees']['effective_to']='2026-09-09'
    with pytest.raises(ValueError,match='effective'):
        PaperBook(cfg)
