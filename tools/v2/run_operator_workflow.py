"""Real CLI round-trip against explicitly synthetic paper facts, never live acceptance."""
import argparse
from datetime import timedelta
import json
from pathlib import Path
import subprocess
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from trade_system.v2.decisions import DecisionService, RiskPolicy
from trade_system.v2.domain import canonical, identity, now_utc
from trade_system.v2.gap_evidence import write_json
from trade_system.v2.paper_storage import open_paper, submit_confirmed_buy, apply_paper_event
from trade_system.v2.rolling_research import file_hash
from trade_system.v2.storage import Store
from trade_system.v2.strategies import StrategyPolicy, record_signal


ROOT=Path(__file__).resolve().parents[2]


def run(folder):
    folder=Path(folder).resolve();folder.mkdir(parents=True,exist_ok=False)
    db=folder/'synthetic-paper.duckdb'
    at=now_utc();day=at.date().isoformat();expiry=(at+timedelta(minutes=5)).isoformat()
    code='SZ.000002';account='synthetic-cli-paper';rule='synthetic-not-exchange-rule'
    config={'account_id':account,'mode':'paper','trading_days':[day,(at+timedelta(days=1)).date().isoformat()],
        'opened_at':at.isoformat(),'initial_cash_fen':1000000,'initial_lots':[],
        'quote_ttl_seconds':300,'mark_ttl_seconds':300,'account_ttl_seconds':300,
        'fees':{'version':'synthetic','effective_from':day,'effective_to':day,'commission_bps':'0',
                'minimum_commission_fen':500,'transfer_bps':'0','sell_tax_bps':'0'},
        'instruments':{code:{'version':rule,'effective_from':day,'effective_to':day,'buy_lot':100,
                            'sell_lot':100,'t_plus_sessions':1,'allow_odd_sell_all':True}}}
    with Store(db) as store:
        open_paper(store,config)
        store.register_product('fixture.quote','CNY','point','paper_confirmation','synthetic_fixture')
        moment=now_utc()
        store.ingest('fixture.quote',code,moment,'10','synthetic-q',b'{"synthetic_fixture":true}')
        moment=now_utc()  # Freeze only after the quote has actually been received.
        manifest=store.freeze(moment)
        sig=record_signal(store,code,manifest,StrategyPolicy('synthetic','9','11','8',expiry),
            at=moment.isoformat(),price='10',auction_received_at=moment.isoformat(),auction_confirmed=True,
            theme_supported=True,funds_supported=True)
        draft=DecisionService(store,RiskPolicy('synthetic','.7','.2',100,300,1000,500)).propose(account,sig,manifest,'fixture.quote')
    commands=[]
    def cli(*args):
        command=[sys.executable,'-m','trade_system.v2.operator_workflow_cli','--db',str(db),*args]
        proc=subprocess.run(command,cwd=ROOT,text=True,encoding='utf-8',capture_output=True,timeout=45)
        if proc.returncode:
            raise RuntimeError(proc.stderr[-1200:])
        result=json.loads(proc.stdout)
        commands.append({'args':list(args),'exit_code':proc.returncode,'result':result})
        return result
    packet=folder/'plan.json'
    cli('export','--decision-id',draft['decision_id'],'--output',str(packet))
    args=('confirm','--packet',str(packet),'--quantity','100','--operator','synthetic-fixture-not-human',
          '--request-id','fixture-confirm','--quote-manifest',manifest,'--acknowledge-paper')
    confirmed=cli(*args)
    if not confirmed['confirmation']['hypothetical_ready'] or cli(*args)!=confirmed:
        raise ValueError('CLI confirmation failed or retry changed')
    cli('review','--account-id',account,'--output',str(folder/'before-fill'))
    with Store(db) as store:
        submit_confirmed_buy(store,account,confirmed['request_id'],'synthetic-order')
        apply_paper_event(store,account,{'event_id':'synthetic-fill','kind':'market','payload':{
            'instrument':code,'source_event_at':now_utc().isoformat(),'evidence_kind':'observed_orderbook',
            'phase':'continuous','tradable':True,'rule_version':rule,'lower_limit_fen':800,'upper_limit_fen':1200,
            'bid_fen':1000,'ask_fen':1000,'last_fen':1000,'bid_quantity':100,'ask_quantity':100}})
    cli('review','--account-id',account,'--output',str(folder/'after-fill'))
    final=json.loads((folder/'after-fill/review.json').read_text(encoding='utf-8'))
    attribution=final['review']['attribution']
    assert attribution['paper_portfolio_ledger']['orders'][0]['filled_quantity']==100
    assert attribution['paper_portfolio_ledger']['reconciliation_difference_fen']==0
    assert len(final['confirmations'])==1 and final['actual_operator_return'] is None
    write_json(folder/'commands.json',commands)
    summary={'scope':'synthetic_cli_roundtrip_not_real_account_or_market_acceptance','execution_ready':False,
        'commands':len(commands),'paper_filled_quantity':100,'confirmation_count':len(final['confirmations']),
        'duplicate_confirmation_idempotent':True,'actual_operator_return':None,
        'source':{n:file_hash(ROOT/'trade_system/v2'/n) for n in ('operator_workflow.py','operator_workflow_cli.py','service.py','decisions.py','paper_storage.py','paper_ledger.py')},
        'artifacts':{str(p.relative_to(folder)):file_hash(p) for p in folder.rglob('*') if p.is_file() and p.suffix in ('.json','.html')},
        'scenario_hash':identity(config)}
    write_json(folder/'completed.json',summary)
    print(canonical({k:v for k,v in summary.items() if k not in ('source','artifacts')}))
    return summary


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',required=True)
    run(p.parse_args().output)
