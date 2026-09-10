"""Portable golden scenarios and immutable integrated-account replay packages."""
from pathlib import Path

from .domain import identity
from .entitlement_run import golden_scenario as rights_fixture
from .gap_evidence import read_json, write_json
from .integrated_account import POLICY, SCOPE, replay
from .rolling_research import file_hash


def implementation():
    return {name: file_hash(Path(__file__).with_name(name)) for name in
            ('integrated_account.py','integrated_run.py','entitlements.py','entitlement_run.py',
             'domain.py','paper_ledger.py','gap_evidence.py')}


def event(key, kind, at, value):
    return {'event_id': key, 'at': at, 'effective_at': at, 'kind': kind,
            'value': value, 'evidence_ref': 'synthetic-evidence-'+key}


def market(key, at, status='trading', price=1000, buy=1000, sell=1000):
    return event(key,'market',at,{'instrument':'SZ.000001','status':status,
        'coverage':'observation' if status == 'trading' else 'full_session',
        'raw_price_fen':price if status == 'trading' else None,
        'buy_capacity':buy if status == 'trading' else 0,'sell_capacity':sell if status == 'trading' else 0})


def fill(key, at, side, lot, qty, price, expense=0, exit_id=None):
    return event(key,'synthetic_fill',at,{'side':side,'instrument':'SZ.000001','lot_id':lot,
        'quantity':qty,'price_fen':price,'fee_fen':expense,'exit_id':exit_id})


def golden_scenarios():
    child = rights_fixture()
    config = {'scope':SCOPE,'policy':POLICY,'account_id':'synthetic-parent',
        'opened_at':'2026-05-18T10:00:00+08:00','initial_cash_fen':1000000,
        'lots':[{**lot,'sellable_from':'2026-05-18'} for lot in child['config']['snapshot']['lots']],
        'initial_marks':{'SZ.000001':1000},'calendar':['2026-05-'+str(d) for d in range(18,23)],
        'rules':{'SZ.000001':{'buy_lot':100,'sell_lot':1,'t_plus_sessions':1,
                            'allow_odd_full_exit':True,'version':'synthetic-only-not-exchange-rules'}},
        'max_stale_sessions':1,'quote_ttl_seconds':60,'actions':[child['config']['terms']]}
    aid = config['actions'][0]['action_id']
    register = event('record','register_rights','2026-05-18T15:00:00+08:00',{'action_id':aid})
    register['at'] = '2026-05-18T16:00:00+08:00'
    ex = event('ex','entitlement','2026-05-19T09:30:00+08:00',{'action_id':aid,'kind':'ex','value':{}})
    tax = event('tax','entitlement','2026-05-19T09:30:01+08:00',
                {'action_id':aid,'kind':'tax_assessment','value':{'total_fen':75,'basis_ref':'fixture-assessed-tax'}})
    stream = [register,ex,tax,market('ex-raw','2026-05-19T10:00:00+08:00',price=700),
        event('exit-original','exit_intent','2026-05-19T10:00:01+08:00',{'exit_id':'exit-a','lot_id':'a','quantity':100}),
        fill('sell-original','2026-05-19T10:00:02+08:00','sell','a',100,700,10,'exit-a'),
        fill('new-buy','2026-05-19T10:00:03+08:00','buy','new-after-record',100,700,10)]
    for child_event in child['events'][1:4]:
        stream.append({**child_event,'event_id':'rights-'+child_event['event_id'],
            'kind':'entitlement','value':{'action_id':aid,'kind':child_event['kind'],'value':child_event['value']}})
    bonus_lot = 'entitlement-'+identity([aid,'a'])
    stream += [market('final-raw','2026-05-22T11:00:00+08:00',price=750,sell=40),
        event('exit-bonus','exit_intent','2026-05-22T11:00:01+08:00',{'exit_id':'exit-bonus','lot_id':bonus_lot,'quantity':40}),
        fill('sell-bonus','2026-05-22T11:00:02+08:00','sell',bonus_lot,40,750,5,'exit-bonus'),
        event('pay-tax','entitlement','2026-05-22T11:00:03+08:00',
              {'action_id':aid,'kind':'tax_payment','value':{'amount_fen':45}})]
    halted = {**config,'actions':[],'initial_cash_fen':100000,'lots':[{**config['lots'][0],'cost_fen':100000}]}
    halt_stream = [market('halt-day1','2026-05-19T09:30:00+08:00',status='suspended'),
        event('wait-exit','exit_intent','2026-05-19T09:30:01+08:00',{'exit_id':'wait','lot_id':'a','quantity':100}),
        market('halt-expired','2026-05-20T09:30:00+08:00',status='suspended'),
        market('resume','2026-05-22T10:00:00+08:00',price=800,sell=50),
        fill('partial-exit','2026-05-22T10:00:01+08:00','sell','a',50,800,10,'wait'),
        market('resume-second-observation','2026-05-22T10:01:00+08:00',price=800,sell=50),
        fill('finish-exit','2026-05-22T10:01:01+08:00','sell','a',50,800,10,'wait')]
    delisted = [halt_stream[0],halt_stream[1],market('delist','2026-05-20T09:30:00+08:00',status='delisted')]
    return {'entitlement_parent':{'config':config,'events':stream},
            'suspension_exit':{'config':halted,'events':halt_stream},
            'delisted_retention':{'config':halted,'events':delisted}}


def run_scenarios(scenarios, output):
    if not isinstance(scenarios,dict) or not 1 <= len(scenarios) <= 20:
        raise ValueError('bounded named integrated scenarios required')
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    reg = {'scope':SCOPE,'scenarios_id':identity(scenarios),'implementation':implementation()}
    write_json(output/'registration.json',reg)
    write_json(output/'scenarios.json',scenarios)
    result = {name:replay(**scenario) for name,scenario in scenarios.items()}
    write_json(output/'report.json',result)
    manifest = {'registration_id':identity(reg),'artifact_hashes':{p.name:file_hash(p) for p in output.iterdir()}}
    write_json(output/'completed.json',{**manifest,'manifest_id':identity(manifest)})
    return result


def verify_scenarios(folder):
    folder = Path(folder).resolve()
    manifest,_ = read_json(folder/'completed.json')
    mid = manifest.pop('manifest_id')
    if identity(manifest) != mid:
        raise ValueError('integrated manifest changed')
    paths = list(folder.iterdir())
    if any(not p.is_file() or p.is_symlink() for p in paths):
        raise ValueError('invalid integrated package member')
    if {p.name:file_hash(p) for p in paths if p.name != 'completed.json'} != manifest['artifact_hashes']:
        raise ValueError('integrated package membership/hash changed')
    reg,_ = read_json(folder/'registration.json')
    scenarios,_ = read_json(folder/'scenarios.json')
    result,_ = read_json(folder/'report.json')
    if identity(reg) != manifest['registration_id'] or reg['implementation'] != implementation() or identity(scenarios) != reg['scenarios_id']:
        raise ValueError('integrated input/source binding changed')
    if {name:replay(**scenario) for name,scenario in scenarios.items()} != result:
        raise ValueError('integrated results not reproducible')
    return result
