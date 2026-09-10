"""Frozen full-universe daily hypotheses using the shared cash/share kernel.

Unknown retained inventory stops new risk but not known exits/settlement. Every
variant keeps the same requested horizon; incomplete returns stay null.
"""
from collections import defaultdict
from copy import deepcopy
from decimal import Decimal

from .daily_account import ASSUMPTIONS as CASE_ASSUMPTIONS, HistoricalAccount, HistoricalRights, _terms
from .domain import identity, number, utc
from .entitlements import day, validate_terms
from .gap_audit import potential_windows
from .historical_adapter import validate_bundle
from .integrated_account import IntegratedAccount, POLICY as KERNEL_POLICY
from .paper_ledger import rounded
from .portfolio_diagnostic import fee, validate_policy


SCOPE = 'historical_full_oos_daily_portfolio_hypothesis_not_execution'
POLICY = {'version':'full-oos-v1','scope':SCOPE,'knowledge_mode':'historical_repair_not_system_replay',
    'initial_holdings':'empty','entry':'frozen_top_k_next_raw_open_no_replacement',
    'ticket':'fixed_initial_cash_fraction','cash_reuse':'no_close_proceeds_at_same_open',
    'exit':'due_raw_close_then_max_two_declared_sessions',
    'missing':'block_new_risk_continue_known_exits_retain_unpriced_assets',
    'corporate':'explicit_cash_capitalization_schedule_hypotheses_no_factor_ratio_inference',
    'fees':'per_lot_exit_minimum_not_broker_certified',
    'calendar':'frozen_original_only_no_weekday_extension',
    'settlement_hypotheses':CASE_ASSUMPTIONS,
    'max_selected_instruments':2000,'exit_wait_sessions':2}


class PortfolioAccount(IntegratedAccount):
    max_instruments = 2000
    scope = SCOPE
    rights_book = HistoricalRights
    fill_kind = 'hypothetical_daily_fill'
    initial_mark_basis = 'historical_empty_account'
    observation_basis = 'historical_daily_price_not_intraday_observation'
    integration = 'historical_portfolio_hypothesis_not_service_or_broker'
    _quote_age = HistoricalAccount._quote_age

    def __init__(self, config, repair_asof):
        self.repair_asof = utc(repair_asof).isoformat()
        if config['lots'] or config['initial_marks']:
            raise ValueError('full OOS account must start empty')
        super().__init__(config)

    def apply(self, event):
        if utc(event['at']).isoformat() != self.repair_asof or not event['evidence_ref'].startswith('assumption:'):
            raise ValueError('historical repair provenance required')
        return super().apply(event)

    def _apply(self, event):
        if event['kind'] == 'clock':
            if event['value']:
                raise ValueError('empty portfolio clock value required')
            return
        return super()._apply(event)


def action_terms(cases, prior_actions, repair_asof, calendar):
    terms, gaps = {},[]
    for c in cases:
        v = c['nonconflicting_disclosure_values']
        if 'ex_date' in v:
            try:
                t, assumed = _terms(c,repair_asof,CASE_ASSUMPTIONS)
                validate_terms(t)
            except (ValueError,TypeError) as exc:
                gaps.append({'instrument':c['instrument'],'date':c['date'],'reason':str(exc)})
                continue
            terms[c['instrument']] = {'terms':t,'assumed_fields':assumed}
    for a in prior_actions:
        if not a.get('terms_schema_complete'):
            continue
        t = deepcopy(a['terms'])
        if utc(t['available_at']) > utc(repair_asof):
            raise ValueError('future action terms')
        validate_terms(t)
        if t['instrument'] in terms:
            raise ValueError('multiple/conflicting full action declarations')
        terms[t['instrument']] = {'terms':t,'assumed_fields':[]}
    for code,a in list(terms.items()):
        if any(v not in calendar for k,v in a['terms'].items() if k.endswith('_date') and v is not None):
            gaps.append({'instrument':code,'date':a['terms']['ex_date'],'reason':'action_schedule_outside_frozen_calendar'})
            del terms[code]
    return terms,gaps


def replay_variant(bundle, base_policy, policy, cases, prior_actions, variant, *, repair_asof):
    if policy != POLICY:
        raise ValueError('exact preregistered full OOS hypothesis required')
    validate_bundle(bundle,base_policy)
    validate_policy(base_policy)
    if variant not in bundle['predictions']:
        raise ValueError('unknown frozen variant')
    days = bundle['calendar']
    indexes = {d:i for i,d in enumerate(days)}
    windows = [w for w in potential_windows(bundle,base_policy) if w['variant'] == variant]
    codes = sorted({w['instrument'] for w in windows})
    if len(codes) > policy['max_selected_instruments']:
        raise ValueError('selected universe exceeds declared engine capacity')
    terms,source_gaps = action_terms(cases,prior_actions,repair_asof,days)
    terms = {c:a for c,a in terms.items() if c in codes}
    opening = days[0]+'T09:00:00+08:00'
    config = {'scope':SCOPE,'policy':KERNEL_POLICY,'account_id':'historical-'+variant,'opened_at':opening,
        'initial_cash_fen':base_policy['initial_cash_fen'],'lots':[],'initial_marks':{},'calendar':days,
        'rules':{code:{'buy_lot':base_policy['buy_lot'],'sell_lot':1,'t_plus_sessions':base_policy['t_plus_sessions'],
            'allow_odd_full_exit':True,'version':'daily_hypothesis_not_exchange_certified'} for code in codes},
        'max_stale_sessions':CASE_ASSUMPTIONS['max_stale_sessions'],'quote_ttl_seconds':60,
        'actions':[v['terms'] for v in terms.values()]}
    account = PortfolioAccount(config,repair_asof)
    market = {(b['date'],b['instrument']):b for b in bundle['bars']}
    entries = defaultdict(list)
    for w in windows:
        entries[w['days'][0]].append(w)
    halt_cases = {c['instrument']:c['nonconflicting_disclosure_values'] for c in cases if 'halt_start_date' in c['nonconflicting_disclosure_values']}
    due, last_factor, nav, gaps, skips, audit_events = {},{},[],{},[],[]
    blocked_at, fatal = None,None

    def emit(kind,d,clock,value,basis):
        event = {'event_id':str(len(audit_events)),'at':utc(repair_asof).isoformat(),
            'effective_at':d+'T'+clock+'+08:00','kind':kind,'value':value,
            'evidence_ref':'assumption:'+basis+':'+str(len(audit_events))}
        account.apply(event)
        audit_events.append(event)

    def retained():
        return {l['instrument'] for l in account.state['lots'].values() if l['quantity']} | {
            account.actions[aid]['instrument'] for aid,b in account.books.items() if b.summary()['share_receivable_quantity']}

    def observe(code,d,field,clock):
        nonlocal blocked_at
        bar = market.get((d,code))
        h = halt_cases.get(code,{})
        halted = h and (d == h['halt_start_date'] or h.get('halt_end_date_inclusive') is not None
                       and h['halt_start_date'] <= d <= h['halt_end_date_inclusive'])
        reason = None
        price = bar[field] if bar else None
        if not price or not bar or bar['factor'] is None:
            reason = 'missing_raw_price_or_factor'
        elif code in retained() and code in last_factor and number(last_factor[code]) != number(bar['factor']) and not (
                code in terms and d == terms[code]['terms']['ex_date']):
            reason = 'unexplained_held_factor_change'
        elif halted:
            reason = 'halt_conflicts_with_raw_price'
        status = 'suspended' if halted and not price else 'unknown' if reason else 'trading'
        emit('market',d,clock,{'instrument':code,'status':status,'coverage':'observation' if status == 'trading' else 'full_session',
            'raw_price_fen':price if status == 'trading' else None,
            'buy_capacity':100000000 if status == 'trading' else 0,'sell_capacity':100000000 if status == 'trading' else 0},
            'raw-'+field+'-assumed-capacity' if status == 'trading' else status)
        if status == 'trading':
            last_factor[code] = bar['factor']
            return price
        key = identity([code,d,field])
        gaps[key] = {'instrument':code,'date':d,'field':field,'reason':reason or 'explicit_halt',
                     'held':code in retained(),'provider_priority':['hithink_official','xiaodefa_tushare','other_last_resort'],
                     'can_impute':False}
        if code in retained():
            blocked_at = blocked_at or d
        return None

    for d in days:
        emit('clock',d,'09:00:00',{},'session-clock')
        for code,a in terms.items():
            t,aid = a['terms'],a['terms']['action_id']
            def rights(kind,value):
                emit('entitlement',d,'09:30:00',{'action_id':aid,'kind':kind,'value':value},'scheduled-'+kind)
            try:
                if d == t['ex_date']:
                    rights('ex',{})
                    if aid in account.books:
                        gross = account.books[aid].summary()['gross_cash_fen']
                        rights('tax_assessment',{'total_fen':rounded(Decimal(gross)*Decimal('.20')),
                            'basis_ref':'assumption:flat-cash-tax-not-legal-rate'})
                if aid not in account.books:
                    continue
                book = account.books[aid]
                if d == t['cash_pay_date']:
                    r = book.summary()
                    rights('cash_payment',{'net_fen':r['gross_cash_fen']-r['tax_assessed_fen'],
                                          'withheld_fen':r['tax_assessed_fen']})
                if d == t['share_delivery_date']:
                    rights('share_delivery',{'by_lot':{r['lot_id']:r['entitled_shares'] for r in book.summary()['lots']}})
                    for r in book.summary()['lots']:
                        lid = 'entitlement-'+identity([aid,r['lot_id']])
                        if lid in account.state['lots']:
                            due[lid] = due[r['lot_id']]
                if d == t['share_listing_date']:
                    rights('share_release',{})
            except ValueError as exc:
                fatal = {'date':d,'instrument':code,'reason':str(exc),'phase':'corporate_action'}
                break
        if fatal:
            break
        # Check all retained inventory before taking new risk at today's open.
        for code in sorted(retained()):
            observe(code,d,'open_fen','09:30:00')
        for w in entries[d]:
            code = w['instrument']
            reason = 'account_risk_blocked' if blocked_at else 'already_held' if code in retained() else (
                'position_limit' if len(retained()) >= base_policy['max_positions'] else None)
            if reason:
                skips.append({'date':d,'instrument':code,'reason':reason})
                continue
            price = observe(code,d,'open_fen','09:30:00')
            if price is None:
                skips.append({'date':d,'instrument':code,'reason':'entry_observation_missing'})
                continue
            free = account.summary()['available_cash_fen']
            ticket = min(free or 0,base_policy['initial_cash_fen']*base_policy['ticket_bps']//10000)
            qty = ticket//(price*base_policy['buy_lot'])*base_policy['buy_lot']
            while qty and qty*price+fee(base_policy,qty*price,'buy') > ticket:
                qty -= base_policy['buy_lot']
            if not qty:
                skips.append({'date':d,'instrument':code,'reason':'cash_or_lot_limit'})
                continue
            lid = identity([variant,d,code])
            emit('hypothetical_daily_fill',d,'09:30:00',{'side':'buy','instrument':code,'lot_id':lid,
                'quantity':qty,'price_fen':price,'fee_fen':fee(base_policy,qty*price,'buy'),'exit_id':None},'frozen-next-open')
            due[lid] = indexes[w['days'][-1]]
        for code in sorted(retained()):
            price = observe(code,d,'close_fen','15:00:00')
            for lid,lot in list(account.state['lots'].items()):
                if lot['instrument'] != code or not lot['quantity'] or indexes[d] < due[lid]:
                    continue
                eid = 'exit-'+lid
                if eid not in account.state['exit_intents']:
                    emit('exit_intent',d,'15:00:00',{'exit_id':eid,'lot_id':lid,'quantity':lot['quantity']},'frozen-due-exit')
                if indexes[d] > due[lid]+policy['exit_wait_sessions']:
                    blocked_at = blocked_at or d
                    continue
                if price and lot['sellable_from'] is not None and lot['sellable_from'] <= d and not lot['restricted']:
                    emit('hypothetical_daily_fill',d,'15:00:00',{'side':'sell','instrument':code,'lot_id':lid,
                        'quantity':lot['quantity'],'price_fen':price,'fee_fen':fee(base_policy,lot['quantity']*price,'sell'),
                        'exit_id':eid},'frozen-due-close')
        # Record rights AFTER all close executions, including a zero-rights snapshot.
        for a in terms.values():
            t = a['terms']
            if d == t['record_date']:
                emit('register_rights',d,'15:00:00',{'action_id':t['action_id']},'derived-record-close-positions')
        f = account.summary()
        nav.append({'date':d,'cash_fen':f['cash_fen'],'fresh_equity_fen':f['fresh_equity_fen'],
            'held_cost_fen':f['held_cost_fen'],'cash_receivable_fen':f['cash_receivable_fen'],
            'retained_positions':len(retained()),'risk_blocked_since':blocked_at})
    final = account.summary()
    complete = fatal is None and not retained() and all(b['settlement_complete'] for b in final['rights'].values())
    gaps_list = sorted(gaps.values(),key=lambda r:(r['date'],r['instrument'],r['field']))
    return {'scope':SCOPE,'variant':variant,'requested_start':days[0],'requested_end':days[-1],
        'evaluated_until':day(account.state['effective_at']),'full_input_id':identity(bundle),
        'potential_windows':len(windows),'selected_instruments':len(codes),'initial_holdings':[],
        'complete_requested_period':complete,'blocked_new_risk_since':blocked_at,'fatal':fatal,
        'portfolio_return':final['diagnostic_marked_return'] if complete else None,
        'final':final,'daily':nav,'fills':final['fills'],'skips':skips,'data_requests':gaps_list,
        'corporate_source_gaps':source_gaps,'action_hypotheses':terms,
        'event_count':len(audit_events),'events_id':identity(audit_events),
        'selection_advantage':None,'execution_ready':False,'actual_operator_return':None}
