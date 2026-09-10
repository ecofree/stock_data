"""Historical repair hypotheses on the SAME cash/share accounting implementation.

This is a case-level initial-holding diagnostic, not a strategy portfolio or a
replay of what the system knew then. All generated events are assumptions.
"""
from copy import deepcopy
from decimal import Decimal

from .domain import identity, number, utc
from .entitlements import EntitlementBook
from .integrated_account import IntegratedAccount, POLICY as ACCOUNT_POLICY
from .paper_ledger import rounded
from .portfolio_diagnostic import fee


SCOPE = 'historical_daily_case_hypothesis_not_selection_backtest'
ASSUMPTIONS = {
    'version':'daily-case-v1',
    'knowledge_mode':'historical_repair_not_system_replay',
    'initial_holding':'100_settled_unrestricted_shares_costed_at_previous_raw_close',
    'fill':'case_day_close_or_first_later_known_raw_close',
    'capacity':'unconstrained_hypothesis_not_observed_liquidity',
    'cash_payment':'csdc_delegated_schedule_assumed_receipt_not_actual',
    'missing_share_delivery':'assume_declared_unrestricted_listing_day_not_source_fact',
    'share_allocation':'exact_per_lot_only_reject_fractional_shares',
    'tax':'flat_20_percent_gross_cash_at_ex_withheld_at_assumed_payment_not_legal_rate',
    'share_tax':'zero_capitalization_tax_scenario_not_account_assessment',
    'halt':'only_explicit_bounded_span_or_exact_start_day_never_extend_unknown',
    'missing_price':'retain_assets_and_null_valuation_never_impute',
    'max_stale_sessions':1, 'exit_wait_sessions':2,
    'initial_cash_fen':10000000,
    'fees':{'version':'case_sensitivity_not_broker_schedule','commission_bps':'3',
            'minimum_commission_fen':500,'transfer_bps':'0.1','sell_tax_bps':'5'},
}


class HistoricalRights(EntitlementBook):
    scope = 'historical_assumed_entitlements_not_actual_receipts'
    snapshot_basis = 'hypothetical_historical_record_close'


class HistoricalAccount(IntegratedAccount):
    scope = SCOPE
    rights_book = HistoricalRights
    fill_kind = 'hypothetical_daily_fill'
    initial_mark_basis = 'historical_raw_close_initial_holding_assumption'
    observation_basis = 'historical_daily_price_not_intraday_observation'
    integration = 'historical_case_hypothesis_not_service_or_broker'

    def __init__(self, config, *, repair_asof, assumptions):
        if assumptions != ASSUMPTIONS:
            raise ValueError('frozen case assumptions required; new policy needs new implementation/version')
        self.repair_asof = utc(repair_asof).isoformat()
        if utc(config['opened_at']) > utc(self.repair_asof):
            raise ValueError('historical case must precede repair cutoff')
        if any(utc(t['available_at']) > utc(self.repair_asof) for t in config['actions']):
            raise ValueError('future terms unavailable to repair')
        super().__init__(config)

    def apply(self, event):
        if utc(event['at']).isoformat() != self.repair_asof or not event['evidence_ref'].startswith('assumption:'):
            raise ValueError('explicit repair-time assumed event provenance required')
        if event['kind'] == 'synthetic_fill':
            raise ValueError('historical input cannot masquerade as synthetic fixture')
        return super().apply(event)

    def _quote_age(self, event, market):
        # Historical daily observation ordering, NOT a claim of old live freshness.
        return (utc(event['effective_at'])-utc(market['effective_at'])).total_seconds()

    def summary(self):
        result = super().summary()
        result.update(knowledge_mode='historical_repair_not_system_replay',
                      market_freshness_is_historical_hypothesis=True,
                      account_receipts_are_assumed=True, selection_advantage=None)
        return result


def _terms(case, at, assumptions):
    v = case['nonconflicting_disclosure_values']
    if case['conflicting_fields'] or case['native_cash_conflict_evidence_ids']:
        raise ValueError('unresolved source conflict blocks assumed entitlement input')
    required = {'record_date','ex_date','gross_cny_per_share','bonus_shares_per_share','capitalization_shares_per_share'}
    if not required <= set(v) or number(v['bonus_shares_per_share']) != 0:
        raise ValueError('v1 supports explicitly zero bonus component only; no tax-component inference')
    cash, shares = number(v['gross_cny_per_share']),number(v['capitalization_shares_per_share'])
    if cash and v.get('cash_payment_scope') != 'csdc_delegated_A_share_holders_not_self_distribution':
        raise ValueError('explicit declared cash beneficiary scope required')
    delivery = v.get('share_delivery_date')
    assumptions_used = []
    if shares and delivery is None:
        delivery = v.get('share_listing_date')
        if delivery is None:
            raise ValueError('missing share schedule cannot infer an ex-date delivery')
        assumptions_used.append({'field':'share_delivery_date','assumed_value':delivery,
            'source_value':None,'policy':assumptions['missing_share_delivery']})
    term = {'action_id':'daily-case-'+case['case_id'],'instrument':case['instrument'],
        'action_type':'cash_and_capitalization' if cash and shares else 'capitalization' if shares else 'cash_dividend',
        'record_date':v['record_date'],'ex_date':v['ex_date'],
        'cash_pay_date':v.get('cash_pay_date') if cash else None,
        'share_delivery_date':delivery if shares else None,
        'share_listing_date':v.get('share_listing_date') if shares else None,
        'gross_cny_per_share':str(cash),'shares_per_share':str(shares),
        'available_at':at,'evidence_refs':['assumption:registered-case-disclosure-'+case['case_id']]}
    return term,assumptions_used


def diagnose_case(bundle, case, *, repair_asof, assumptions):
    if assumptions != ASSUMPTIONS:
        raise ValueError('exact registered daily hypothesis required')
    original = identity([bundle,case])
    code, target = case['instrument'],case['date']
    days = bundle['calendar']
    index = days.index(target)
    if not index or index+assumptions['exit_wait_sessions'] >= len(days):
        raise ValueError('complete bounded case horizon required')
    start,end = days[index-1],days[index+assumptions['exit_wait_sessions']]
    bars = {b['date']:b for b in bundle['bars'] if b['instrument'] == code}
    previous = bars.get(start)
    if not previous or not previous['close_fen'] or previous['factor'] is None:
        raise ValueError('known prior raw close/factor required for declared initial holding')
    at = utc(repair_asof).isoformat()
    v = case['nonconflicting_disclosure_values']
    terms, assumed_fields = [],[]
    if 'ex_date' in v:
        term,assumed_fields = _terms(case,at,assumptions)
        if term['record_date'] != start:
            raise ValueError('v1 case must begin at explicitly known record close')
        terms = [term]
    elif 'halt_start_date' not in v or case['conflicting_fields']:
        raise ValueError('known halt start or full corporate candidate required')
    config = {'scope':SCOPE,'policy':ACCOUNT_POLICY,'account_id':'hypothetical-'+case['case_id'],
        'opened_at':start+'T15:00:00+08:00','initial_cash_fen':assumptions['initial_cash_fen'],
        'lots':[{'lot_id':'initial','instrument':code,'quantity':100,'cost_fen':100*previous['close_fen'],
                 'acquired_at':start+'T15:00:00+08:00','restricted':False,'sellable_from':start}],
        'initial_marks':{code:previous['close_fen']},'calendar':days,
        'rules':{code:{'buy_lot':100,'sell_lot':1,'t_plus_sessions':1,'allow_odd_full_exit':True,
                       'version':'declared_case_hypothesis_not_exchange_certification'}},
        'max_stale_sessions':assumptions['max_stale_sessions'],'quote_ttl_seconds':60,'actions':terms}
    account = HistoricalAccount(config,repair_asof=at,assumptions=assumptions)
    events, traces = [],[]

    def emit(kind, effective, value, basis):
        event = {'event_id':'case-'+str(len(events)), 'at':at,'effective_at':effective,
                 'kind':kind,'value':value,'evidence_ref':'assumption:'+basis+':'+str(len(events))}
        account.apply(event)
        events.append(event)

    if terms:
        emit('register_rights',start+'T15:00:00+08:00',{'action_id':terms[0]['action_id']},'initial-record-holding')
    last_factor = previous['factor']
    for d in days[index:index+assumptions['exit_wait_sessions']+1]:
        opening,closing = d+'T09:30:00+08:00',d+'T15:00:00+08:00'
        if terms:
            term = terms[0]
            aid = term['action_id']
            def rights(kind,value):
                emit('entitlement',opening,{'action_id':aid,'kind':kind,'value':value},'scheduled-'+kind)
            if d == term['ex_date']:
                rights('ex',{})
                gross = account.books[aid].summary()['gross_cash_fen']
                rights('tax_assessment',{'total_fen':rounded(Decimal(gross)*Decimal('.20')),
                    'basis_ref':'assumption:'+assumptions['tax']})
            if d == term['cash_pay_date']:
                r = account.books[aid].summary()
                rights('cash_payment',{'net_fen':r['gross_cash_fen']-r['tax_assessed_fen'],
                                       'withheld_fen':r['tax_assessed_fen']})
            if d == term['share_delivery_date']:
                rights('share_delivery',{'by_lot':{r['lot_id']:r['entitled_shares'] for r in account.books[aid].summary()['lots']}})
            if d == term['share_listing_date']:
                rights('share_release',{})
        bar = bars.get(d)
        # Unknown end permits ONLY the exact declared start; never infinite carry.
        halt = ('halt_start_date' in v and (d == v['halt_start_date'] or
                v.get('halt_end_date_inclusive') is not None and v['halt_start_date'] <= d <= v['halt_end_date_inclusive']))
        factor_block = bool(bar and bar['factor'] is not None and number(bar['factor']) != number(last_factor)
                            and not (terms and d == terms[0]['ex_date']))
        price = bar['close_fen'] if bar and bar['close_fen'] and bar['factor'] is not None and not factor_block else None
        status = 'suspended' if halt and price is None else 'unknown' if halt or price is None else 'trading'
        price = price if status == 'trading' else None
        emit('market',closing,{'instrument':code,'status':status,
            'coverage':'observation' if status == 'trading' else 'full_session',
            'raw_price_fen':price,'buy_capacity':0,'sell_capacity':1000000 if price else 0},
            'raw-daily-close-assumed-capacity' if price else 'missing-or-explicit-halt')
        if status == 'trading':
            last_factor = bar['factor']
        # Intent is diagnostic only, retains inventory on missing price/settlement.
        for lid,lot in list(account.state['lots'].items()):
            if not lot['quantity']:
                continue
            eid = 'exit-'+lid
            if eid not in account.state['exit_intents']:
                emit('exit_intent',closing,{'exit_id':eid,'lot_id':lid,'quantity':lot['quantity']},'due-close-exit')
            if price and not lot['restricted'] and lot['sellable_from'] is not None and lot['sellable_from'] <= d:
                emit('hypothetical_daily_fill',closing,{'side':'sell','instrument':code,'lot_id':lid,
                    'quantity':lot['quantity'],'price_fen':price,'fee_fen':fee(assumptions,lot['quantity']*price,'sell'),
                    'exit_id':eid},assumptions['fill'])
        traces.append({'date':d,'raw_bar_present':bar is not None,'unexplained_factor_change':factor_block,
                       'status_used':status,'summary':account.summary()})
    final = account.summary()
    liquidated = not any(final['holdings'].values()) and not any(final['share_receivable_quantity'].values())
    settled = all(b['settlement_complete'] for b in final['rights'].values())
    complete = liquidated and settled
    if identity([bundle,case]) != original:
        raise AssertionError('source inputs mutated')
    return {'scope':SCOPE,'case_id':case['case_id'],'instrument':code,'start':start,'end':end,
        'input_id':original,'assumptions':deepcopy(assumptions),'assumed_source_fields':assumed_fields,
        'config':config,'events':events,'daily':traces,'final':final,'case_hypothesis_complete':complete,
        'incomplete_reasons':[] if complete else ['retained_inventory_or_unsettled_rights_at_exit_horizon'],
        'hypothetical_case_return':final['diagnostic_marked_return'] if complete else None,
        'portfolio_return':None,'selection_advantage':None,'execution_ready':False,
        'initial_holdings_are_declared_not_strategy_positions':True,'knowledge_replay_validated':False}
