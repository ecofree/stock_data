"""Synthetic event-to-paper-ledger vertical slice; no real market performance."""
import argparse
from dataclasses import asdict
import json
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from trade_system.v2.domain import identity, utc
from trade_system.v2.event_bridge import KINDS
from trade_system.v2.decisions import RiskPolicy
from trade_system.v2.paper_storage import load_paper
from trade_system.v2.publisher import publish
from trade_system.v2.service import Service
from trade_system.v2.storage import Store
from trade_system.v2.attribution import project_attribution, render_attribution_markdown


CODE = 'SZ.000002'


class Clock:
    def __init__(self, at='2026-09-10T09:31:00+08:00'):
        self.value = utc(at)
    def __call__(self):
        return self.value
    def set(self, at):
        self.value = utc(at)


def paper_config():
    return {'account_id':'fixture-event-paper','mode':'paper',
        'trading_days':['2026-09-10','2026-09-11','2026-09-14'],
        'opened_at':'2026-09-10T09:30:00+08:00','initial_cash_fen':1000000,'initial_lots':[],
        'quote_ttl_seconds':10,'mark_ttl_seconds':60,'account_ttl_seconds':3600,
        'fees':{'version':'fixture-fees-not-exchange-rules','effective_from':'2026-09-10','effective_to':'2026-09-14',
                'commission_bps':'0','minimum_commission_fen':500,'transfer_bps':'0','sell_tax_bps':'0'},
        'instruments':{CODE:{'version':'fixture-instrument-v1','effective_from':'2026-09-10','effective_to':'2026-09-14',
                      'buy_lot':100,'sell_lot':100,'t_plus_sessions':1,'allow_odd_sell_all':True}}}


def policies():
    strategy = {'version':'fixture-auction-funds-v1','entry_low':'9','entry_high':'11','invalid_below':'8',
                'expires_at':'2026-09-10T09:40:00+08:00','test_only':True}
    events = {'version':'fixture-events-v1','datasets':{k:'fixture.'+k for k in KINDS if k!='auction_indicative'},
              'theme_id':'fixture-theme','membership_version':'fixture-members-v1','fund_metric_version':'fixture-net-v1',
              'entry_not_before':'2026-09-10T09:30:00+08:00','auction_not_before':'2026-09-10T09:25:00+08:00',
              'min_auction_relative_volume':'1.5','min_fund_delta_cny':'100',
              'fund_window_seconds':300,'fresh_seconds':30,'min_theme_coverage':'.8','min_theme_advancing':'.6'}
    return strategy,events


def market(at, *, capacity=100, price=1000, **overrides):
    return {'instrument':CODE,'source_event_at':at,'evidence_kind':'observed_orderbook','phase':'continuous',
            'tradable':True,'rule_version':'fixture-instrument-v1','lower_limit_fen':800,'upper_limit_fen':1200,
            'bid_fen':price,'ask_fen':price,'last_fen':price,'bid_quantity':capacity,'ask_quantity':capacity,**overrides}


def run_replay(output):
    output = Path(output).resolve()
    output.mkdir(parents=True,exist_ok=False)
    clock = Clock('2026-09-10T09:30:00+08:00')
    trace = []
    strategy,event_policy = policies()
    risk = asdict(RiskPolicy('fixture-risk-v1','.7','.2',100,60,1000,500))
    with Service(output/'paper.duckdb',clock=clock) as service:
        def call(command,**data):
            result = service.submit(command,**data).result(30)
            trace.append({'at':clock().isoformat(),'command':command,'request':data,'result':result})
            return result
        for kind,(unit,semantics) in KINDS.items():
            call('product',dataset='fixture.'+kind,unit=unit,semantics=semantics,consumer='event:'+kind,origin='synthetic_fixture')
        call('paper_open',config=paper_config())
        def event(kind, at, payload, key):
            return call('market_event',dataset='fixture.'+kind,code=CODE,kind=kind,event_at=at,payload=payload,source_event_id=key)
        event('auction_indicative','2026-09-10T09:24:00+08:00',{'price':'10','final':False,'relative_volume':'2'},'indication')
        call('event_signal',code=CODE,strategy_policy=strategy,event_policy=event_policy)
        event('auction_final','2026-09-10T09:25:00+08:00',{'price':'10','final':True,'relative_volume':'2'},'final')
        call('event_signal',code=CODE,strategy_policy=strategy,event_policy=event_policy)
        event('funds_cumulative','2026-09-10T09:30:00+08:00',{'net_cny':'1000','metric_version':'fixture-net-v1'},'fund-1')
        clock.set('2026-09-10T09:31:00+08:00')
        event('funds_cumulative',clock().isoformat(),{'net_cny':'1250','metric_version':'fixture-net-v1'},'fund-2')
        event('theme_breadth',clock().isoformat(),{'expected':10,'observed':9,'advancing':7,
              'theme_id':'fixture-theme','membership_version':'fixture-members-v1'},'theme')
        event('quote',clock().isoformat(),{'price':'10','phase':'continuous'},'quote')
        sig = call('event_signal',code=CODE,strategy_policy=strategy,event_policy=event_policy)
        manifest = call('freeze',asof=clock().isoformat())
        draft = call('propose',risk_policy=risk,account_id='fixture-event-paper',signal_id=sig,
                     quote_manifest=manifest,quote_dataset='fixture.quote')
        approved = call('confirm',risk_policy=risk,decision_id=draft['decision_id'],quantity_requested=100,
                        operator='synthetic-operator',request_id='fixture-confirm',quote_manifest=manifest)
        if not approved['hypothetical_ready']:
            raise ValueError(approved['blockers'])
        call('paper_buy',account_id='fixture-event-paper',confirmation_request_id='fixture-confirm',order_id='buy-1')
        def ledger(key,kind,payload):
            return call('paper_event',account_id='fixture-event-paper',event={'event_id':key,'kind':kind,'payload':payload})
        clock.set('2026-09-10T09:31:01+08:00')
        ledger('fill-40','market',market(clock().isoformat(),capacity=40))
        clock.set('2026-09-10T09:31:02+08:00')
        ledger('fill-60','market',market(clock().isoformat(),capacity=60))
        before = call('paper_status',account_id='fixture-event-paper')
        ledger('fill-60','market',market(clock().isoformat(),capacity=60))
        assert before==call('paper_status',account_id='fixture-event-paper')
        clock.set('2026-09-11T09:31:00+08:00')
        ledger('exit-mark','market',market(clock().isoformat(),price=1100))
        event('quote',clock().isoformat(),{'price':'11','phase':'continuous'},'exit-quote')
        exit_manifest = call('freeze',asof=clock().isoformat())
        exit_policy = {'version':'fixture-exit-v1','reason':'manual_reduce','reference_id':'fixture-next-session-exit',
                       'expires_at':'2026-09-11T09:35:00+08:00'}
        exit_draft = call('propose_exit',risk_policy=risk,account_id='fixture-event-paper',code=CODE,
                          quote_manifest=exit_manifest,quote_dataset='fixture.quote',exit_policy=exit_policy)
        exit_approved = call('confirm',risk_policy=risk,decision_id=exit_draft['decision_id'],quantity_requested=100,
                             operator='synthetic-operator',request_id='fixture-exit-confirm',quote_manifest=exit_manifest)
        if not exit_approved['hypothetical_ready']:
            raise ValueError(exit_approved['blockers'])
        call('paper_sell',account_id='fixture-event-paper',confirmation_request_id='fixture-exit-confirm',order_id='sell-1')
        clock.set('2026-09-11T09:31:01+08:00')
        ledger('sell-fill','market',market(clock().isoformat(),price=1100))
        final = call('paper_status',account_id='fixture-event-paper')
    with Store(output/'paper.duckdb',clock=clock) as store:
        book = load_paper(store,'fixture-event-paper')
        attribution = project_attribution(store,'fixture-event-paper')
        assert book.summary()==final and final['realized_pnl_fen']==9000
        signals = [json.loads(r[0]) for r in store.con.execute('SELECT payload FROM signal_event ORDER BY asof_time,rowid').fetchall()]
        data = {'scope':'synthetic_fixture_only_not_market_performance','execution_ready':False,'trace':trace,
                'opportunity_ledger':signals,'paper_portfolio_ledger':book.state,'actual_operator_ledger':[],
                'actual_account_status':'not_provided','summary':final,'replayed_state_hash':identity(book.state),
                'limitations':['synthetic_source_finality_and_orderbook','unverified_fixture_rules_and_fees',
                               'displayed_capacity_is_not_exchange_queue_or_fill_proof','exit_is_explicit_reduce_only_not_validated_auto_exit_strategy']}
    publish(output/'reports','replay',{'evidence.json':json.dumps(data,ensure_ascii=False,indent=2).encode(),
            'attribution.json':json.dumps(attribution,ensure_ascii=False,indent=2).encode(),
            'attribution.md':render_attribution_markdown(attribution).encode('utf-8')},generation=1)
    return {'scope':data['scope'],'output':str(output),'signals':[s['state'] for s in signals],
            'fills':len(book.state['fills']),'summary':final,'replayed_state_hash':data['replayed_state_hash']}


if __name__=='__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    print(json.dumps(run_replay(parser.parse_args().output),ensure_ascii=False))
