"""Synthetic parent account consuming the frozen entitlement subledger atomically.

Not a broker or live/paper Service adapter. Session, capacity, fees and settlement
are declared fixture observations, NOT certified exchange rules. Old experiments
and PaperBook are intentionally unchanged.
"""
from copy import deepcopy
from decimal import Decimal
from zoneinfo import ZoneInfo

from .domain import canonical, identity, instrument, quantity, utc
from .entitlements import EntitlementBook, POLICY as RIGHTS_POLICY, dated, day, validate_terms
from .paper_ledger import fen, rounded


SCOPE = 'synthetic_integrated_entitlement_account_not_execution'
POLICY = 'raw_mark_explicit_halt_bounded_carry_no_delisted_value_v1'


def _name(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError('explicit nonempty identity required')
    return value


class IntegratedAccount:
    max_instruments = 100
    scope = SCOPE
    rights_book = EntitlementBook
    fill_kind = 'synthetic_fill'
    initial_mark_basis = 'synthetic_initial_declaration'
    observation_basis = 'synthetic_raw_observation'
    integration = 'synthetic_only_not_service_or_broker'

    def __init__(self, config):
        config = deepcopy(config)
        if set(config) != {'scope','policy','account_id','opened_at','initial_cash_fen','lots',
                           'initial_marks','calendar','rules','max_stale_sessions','quote_ttl_seconds','actions'}:
            raise ValueError('strict integrated scenario configuration required')
        if config['scope'] != self.scope or config['policy'] != POLICY:
            raise ValueError('explicit synthetic integrated accounting policy required')
        _name(config['account_id'])
        days = config['calendar']
        if not isinstance(days, list) or not days or days != sorted(set(days)):
            raise ValueError('ordered unique declared calendar required')
        for d in days:
            dated(d)
        if day(config['opened_at']) not in days:
            raise ValueError('opening date outside calendar')
        for field, maximum in [('max_stale_sessions',20),('quote_ttl_seconds',300)]:
            value = config[field]
            if type(value) is not int or not (0 if field == 'max_stale_sessions' else 1) <= value <= maximum:
                raise ValueError('bounded explicit valuation/quote policy required')
        rules = config['rules']
        if not isinstance(rules, dict) or not rules or len(rules) > self.max_instruments:
            raise ValueError('bounded instrument rule declarations required')
        for code, rule in rules.items():
            instrument(code)
            if set(rule) != {'buy_lot','sell_lot','t_plus_sessions','allow_odd_full_exit','version'}:
                raise ValueError('strict synthetic instrument rule required')
            for key in ('buy_lot','sell_lot','t_plus_sessions'):
                if type(rule[key]) is not int or not 1 <= rule[key] <= 10000:
                    raise ValueError('positive bounded lot/settlement rule required')
            _name(rule['version'])
            if type(rule['allow_odd_full_exit']) is not bool:
                raise ValueError('explicit odd-lot assumption required')
        self.config = config
        self.actions = {}
        action_codes = set()
        for terms in config['actions']:
            validate_terms(terms)
            if terms['action_id'] in self.actions or terms['instrument'] in action_codes:
                raise ValueError('v1 requires at most one declared action per instrument')
            if terms['instrument'] not in rules or terms['record_date'] < day(config['opened_at']):
                raise ValueError('action outside initial scope')
            for key in ('record_date','ex_date','cash_pay_date','share_delivery_date','share_listing_date'):
                if terms[key] is not None and terms[key] not in days:
                    raise ValueError('action date outside frozen calendar')
            self.actions[terms['action_id']] = terms
            action_codes.add(terms['instrument'])
        self.books = {}
        self.seen = {}
        at = utc(config['opened_at']).isoformat()
        self.state = {'cash_fen': fen(config['initial_cash_fen']), 'lots': {}, 'marks': {}, 'market': {},
            'epochs': {code: 0 for code in rules}, 'last_at': at, 'effective_at': at,
            'purchased_cost_fen': 0, 'disposed_cost_fen': 0, 'realized_trading_pnl_fen': 0,
            'exit_intents': {}, 'fills': [], 'market_refs': [], 'events': [], 'zero_rights_records': {}}
        if not isinstance(config['lots'], list) or len(config['lots']) > 1000:
            raise ValueError('bounded initial lots required')
        for lot in config['lots']:
            if set(lot) != {'lot_id','instrument','quantity','cost_fen','acquired_at','restricted','sellable_from'}:
                raise ValueError('strict initial parent lot required')
            lid = _name(lot['lot_id'])
            if lid in self.state['lots'] or lot['instrument'] not in rules or not quantity(lot['quantity']):
                raise ValueError('unique positive known initial lot required')
            fen(lot['cost_fen'])
            if utc(lot['acquired_at']) > utc(at) or type(lot['restricted']) is not bool or lot['sellable_from'] not in days:
                raise ValueError('initial lot date/restriction required')
            self.state['lots'][lid] = deepcopy(lot)
        if set(config['initial_marks']) != {l['instrument'] for l in config['lots']}:
            raise ValueError('initial equity requires exact held-instrument marks')
        for code, price in config['initial_marks'].items():
            if fen(price) <= 0:
                raise ValueError('positive initial raw mark required')
            self.state['marks'][code] = {'price_fen': price, 'effective_at': at, 'epoch': 0,
                                         'basis': self.initial_mark_basis}
        self.initial_cost = sum(l['cost_fen'] for l in config['lots'])
        self.initial_book_capital = self.state['cash_fen'] + self.initial_cost
        self.initial_equity = self.state['cash_fen'] + sum(l['quantity']*config['initial_marks'][l['instrument']] for l in config['lots'])
        if self.initial_equity <= 0:
            raise ValueError('positive initial synthetic equity required')

    def apply(self, event):
        if set(event) != {'event_id','at','effective_at','kind','value','evidence_ref'}:
            raise ValueError('strict parent event envelope required')
        canonical(event)
        key, digest = _name(event['event_id']), identity(event)
        _name(event['evidence_ref'])
        if key in self.seen:
            if self.seen[key] != digest:
                raise ValueError('parent event idempotency conflict')
            return self.summary()
        at, effective = utc(event['at']), utc(event['effective_at'])
        if at < utc(self.state['last_at']) or effective > at or effective < utc(self.state['effective_at']):
            raise ValueError('parent knowledge/effective order violation; use new replay')
        if day(effective) not in self.config['calendar'] or not isinstance(event['value'], dict):
            raise ValueError('event outside calendar or unstructured value')
        prior, prior_books = deepcopy(self.state), deepcopy(self.books)
        try:
            self._apply(event)
            self.state.update(last_at=at.isoformat(), effective_at=effective.isoformat())
            self.state['events'].append(deepcopy(event))
            self.summary()
        except BaseException:
            self.state, self.books = prior, prior_books
            raise
        self.seen[key] = digest
        return self.summary()

    def _action_due(self, code, effective):
        d = day(effective)
        for aid, terms in self.actions.items():
            if terms['instrument'] == code and terms['ex_date'] <= d:
                if self.state['zero_rights_records'].get(aid,{}).get('ex_applied'):
                    continue
                if aid not in self.books or self.books[aid].state['phase'] != 'accrued':
                    return True
        return False

    def _apply(self, event):
        kind, v, effective = event['kind'], event['value'], event['effective_at']
        if kind == 'register_rights':
            if set(v) != {'action_id'} or v['action_id'] not in self.actions or v['action_id'] in self.books or v['action_id'] in self.state['zero_rights_records']:
                raise ValueError('one registered declared action required')
            terms = self.actions[v['action_id']]
            if utc(effective) != utc(terms['record_date']+'T15:00:00+08:00') or utc(terms['available_at']) > utc(event['at']):
                raise ValueError('record-close event and already known terms required')
            lots = [{k: lot[k] for k in ('lot_id','instrument','quantity','cost_fen','acquired_at','restricted')}
                    for lot in self.state['lots'].values() if lot['instrument'] == terms['instrument'] and lot['quantity']]
            if not lots:
                self.state['zero_rights_records'][v['action_id']] = {'record_at':effective,
                    'available_at':event['at'],'quantity':0,'ex_applied':False}
                return
            self.books[v['action_id']] = self.rights_book({'scope': self.rights_book.scope, 'policy': RIGHTS_POLICY,
                'terms': terms, 'snapshot': {'account_id': self.config['account_id'], 'asof': effective,
                'available_at': event['at'], 'basis': self.rights_book.snapshot_basis, 'lots': lots}})
        elif kind == 'entitlement':
            if set(v) != {'action_id','kind','value'} or (v['action_id'] not in self.books and v['action_id'] not in self.state['zero_rights_records']):
                raise ValueError('registered entitlement target required')
            if v['action_id'] in self.state['zero_rights_records']:
                zero = self.state['zero_rights_records'][v['action_id']]
                terms = self.actions[v['action_id']]
                if v['kind'] != 'ex' or v['value'] or zero['ex_applied'] or day(effective) != terms['ex_date']:
                    raise ValueError('zero ownership only acknowledges one exact ex date')
                zero['ex_applied'] = True
                self.state['epochs'][terms['instrument']] += 1
                return
            self._entitlement(event)
        elif kind == 'market':
            self._market(event)
        elif kind == 'exit_intent':
            if set(v) != {'exit_id','lot_id','quantity'}:
                raise ValueError('strict synthetic exit intention required')
            eid, qty = _name(v['exit_id']), quantity(v['quantity'])
            if eid in self.state['exit_intents'] or v['lot_id'] not in self.state['lots'] or not qty:
                raise ValueError('unique positive exit intent required')
            reserved = sum(i['remaining'] for i in self.state['exit_intents'].values() if i['lot_id'] == v['lot_id'])
            if qty + reserved > self.state['lots'][v['lot_id']]['quantity']:
                raise ValueError('exit intentions exceed retained inventory')
            self.state['exit_intents'][eid] = {**v, 'remaining': qty, 'status': 'pending',
                'requested_at': event['at'], 'is_broker_order': False}
        elif kind == self.fill_kind:
            self._fill(event)
        else:
            raise ValueError('unsupported parent event; no broker/live adapter')

    def _entitlement(self, event):
        aid = event['value']['action_id']
        book = self.books[aid]
        child = {**event, 'kind': event['value']['kind'], 'value': event['value']['value']}
        before = book.summary()
        after = book.apply(child)
        if child['kind'] == 'ex':
            for right in after['lots']:
                parent = self.state['lots'].get(right['lot_id'])
                if parent is None or any(parent[k] != right[k] for k in
                        ('instrument','quantity','cost_fen','acquired_at','restricted')):
                    raise ValueError('parent changed since record close; cost transfer cannot be applied')
                parent['cost_fen'] = right['parent_remaining_cost_fen']
            self.state['epochs'][book.config['terms']['instrument']] += 1
        elif child['kind'] == 'share_delivery':
            for right in after['lots']:
                if not right['entitled_shares']:
                    continue
                lid = 'entitlement-'+identity([aid,right['lot_id']])
                if lid in self.state['lots']:
                    raise ValueError('delivered-lot identity collision')
                self.state['lots'][lid] = {'lot_id': lid, 'instrument': right['instrument'],
                    'quantity': right['entitled_shares'], 'cost_fen': right['entitlement_cost_fen'],
                    'acquired_at': right['acquired_at'], 'restricted': right['restricted'],
                    'sellable_from': None}
        elif child['kind'] == 'share_release':
            for right in after['lots']:
                if right['released_shares']:
                    self.state['lots']['entitlement-'+identity([aid,right['lot_id']])]['sellable_from'] = day(event['effective_at'])
        delta = after['net_cash_contribution_fen'] - before['net_cash_contribution_fen']
        if self.state['cash_fen'] + delta < 0:
            raise ValueError('actual synthetic cash cannot fund tax debit')
        self.state['cash_fen'] += delta

    def _market(self, event):
        v = event['value']
        if set(v) != {'instrument','status','coverage','raw_price_fen','buy_capacity','sell_capacity'} or v['instrument'] not in self.config['rules']:
            raise ValueError('strict raw market observation required')
        code = v['instrument']
        status = v['status']
        if status not in ('trading','suspended','unknown','delisted'):
            raise ValueError('explicit session/lifecycle state required')
        if self.state['market'].get(code, {}).get('status') == 'delisted' and status != 'delisted':
            raise ValueError('delisted asset cannot silently resume in this scenario')
        if event['evidence_ref'] in self.state['market_refs']:
            raise ValueError('market evidence cannot replenish capacity twice')
        for field in ('buy_capacity','sell_capacity'):
            quantity(v[field])
        if status == 'trading':
            if v['coverage'] != 'observation' or fen(v['raw_price_fen']) <= 0 or self._action_due(code,event['effective_at']):
                raise ValueError('current raw observation requires prior due corporate accrual')
            self.state['marks'][code] = {'price_fen': v['raw_price_fen'], 'effective_at': event['effective_at'],
                'epoch': self.state['epochs'][code], 'basis': self.observation_basis}
        elif v['raw_price_fen'] is not None or v['buy_capacity'] or v['sell_capacity'] or v['coverage'] != 'full_session':
            raise ValueError('no-session evidence cannot fabricate price or capacity')
        self.state['market'][code] = {**v, 'effective_at': event['effective_at']}
        self.state['market_refs'].append(event['evidence_ref'])

    def _fill(self, event):
        v = event['value']
        if set(v) != {'side','instrument','lot_id','quantity','price_fen','fee_fen','exit_id'} or v['side'] not in ('buy','sell'):
            raise ValueError('strict synthetic fill required')
        code, qty, effective = v['instrument'], quantity(v['quantity']), event['effective_at']
        lid = _name(v['lot_id'])
        if code not in self.config['rules'] or not qty or fen(v['price_fen']) <= 0:
            raise ValueError('known instrument and positive synthetic fill required')
        expense = fen(v['fee_fen'])
        rule, market = self.config['rules'][code], self.state['market'].get(code)
        if not market or market['status'] != 'trading' or day(market['effective_at']) != day(effective):
            raise ValueError('exit/buy waits for explicit resumed trading evidence')
        age = self._quote_age(event,market)
        local_clock = utc(effective).astimezone(ZoneInfo('Asia/Shanghai')).strftime('%H:%M:%S')
        if not 0 <= age <= self.config['quote_ttl_seconds'] or not '09:30:00' <= local_clock <= '15:00:00':
            raise ValueError('fresh observation inside declared synthetic session required')
        if self._action_due(code,effective) or v['price_fen'] != market['raw_price_fen']:
            raise ValueError('fill cannot use pre-action or invented price')
        if any(self.actions[aid]['instrument'] == code and self.actions[aid]['record_date'] == day(effective)
               and local_clock >= '15:00:00' for aid in {*self.books,*self.state['zero_rights_records']}):
            raise ValueError('record-close snapshot cannot be followed by same-close trading')
        capacity = v['side']+'_capacity'
        if qty > market[capacity]:
            raise ValueError('synthetic observed capacity exhausted')
        notional = fen(qty*v['price_fen'])
        if v['side'] == 'buy':
            free = self._cash_control()['available_cash_fen']
            if lid in self.state['lots'] or v['exit_id'] is not None or qty % rule['buy_lot'] or free is None or notional+expense > free:
                raise ValueError('new buy requires unique lot, settled available cash and explicit lot rule')
            index = self.config['calendar'].index(day(effective)) + rule['t_plus_sessions']
            if index >= len(self.config['calendar']):
                raise ValueError('calendar lacks new-share settlement boundary')
            cost = notional+expense
            self.state['cash_fen'] -= cost
            self.state['purchased_cost_fen'] += cost
            self.state['lots'][lid] = {'lot_id': lid, 'instrument': code, 'quantity': qty, 'cost_fen': cost,
                'acquired_at': effective, 'restricted': False, 'sellable_from': self.config['calendar'][index]}
        else:
            lot, intent = self.state['lots'].get(lid), self.state['exit_intents'].get(v['exit_id'])
            if not lot or lot['instrument'] != code or not intent or intent['lot_id'] != lid or qty > intent['remaining'] or qty > lot['quantity']:
                raise ValueError('synthetic disposal requires matching inventory and exit intention')
            if lot['restricted'] or lot['sellable_from'] is None or lot['sellable_from'] > day(effective):
                raise ValueError('inventory is not released or settled')
            if qty % rule['sell_lot'] and not (rule['allow_odd_full_exit'] and qty == lot['quantity']):
                raise ValueError('synthetic odd-lot disposition rule violated')
            if self.state['cash_fen'] + notional - expense < 0:
                raise ValueError('cash cannot fund disposal fees')
            cost = lot['cost_fen'] if qty == lot['quantity'] else rounded(Decimal(lot['cost_fen'])*qty/lot['quantity'])
            lot['quantity'] -= qty
            lot['cost_fen'] -= cost
            self.state['cash_fen'] += notional-expense
            self.state['disposed_cost_fen'] += cost
            self.state['realized_trading_pnl_fen'] += notional-expense-cost
            intent['remaining'] -= qty
            intent['status'] = 'partial' if intent['remaining'] else 'satisfied'
        market[capacity] -= qty
        self.state['fills'].append({**v, 'effective_at': effective, 'cost_basis_fen': cost, 'actual_fill': False})

    def _quote_age(self, event, market):
        return (utc(event['at'])-utc(market['effective_at'])).total_seconds()

    def _cash_control(self):
        rights = [b.summary() for b in self.books.values() if b.state['phase'] == 'accrued']
        unknown = any(r['tax_due_fen'] is None for r in rights)
        liability = sum(r['tax_due_fen'] or 0 for r in rights)
        return {'tax_unknown': unknown, 'known_tax_liability_fen': liability,
                'available_cash_fen': None if unknown else max(0,self.state['cash_fen']-liability)}

    def _mark(self, code):
        d = day(self.state['effective_at'])
        mark, market = self.state['marks'].get(code), self.state['market'].get(code)
        reason, basis, age = None, None, None
        if self._action_due(code,self.state['effective_at']):
            reason = 'unaccrued_declared_action'
        elif market and market['status'] == 'delisted':
            reason = 'delisted_asset_retained_without_valuation'
        elif not mark:
            reason = 'missing_raw_mark'
        elif mark['epoch'] != self.state['epochs'][code]:
            reason = 'pre_action_mark_invalidated'
        else:
            age = self.config['calendar'].index(d)-self.config['calendar'].index(day(mark['effective_at']))
            if market and (market['status'] == 'unknown' or day(market['effective_at']) != d):
                reason = 'missing_current_session_evidence'
            elif market and market['status'] == 'suspended':
                if age > self.config['max_stale_sessions']:
                    reason = 'suspended_mark_expired'
                else:
                    basis = 'indicative_suspended_carry_not_tradable'
            elif age == 0:
                basis = 'current_raw_observation' if market else self.initial_mark_basis
            else:
                reason = 'missing_current_session_evidence'
        return {'instrument': code, 'price_fen': None if reason else mark['price_fen'], 'reason': reason,
                'basis': basis, 'age_sessions': age, 'price_effective_at': mark['effective_at'] if mark else None,
                'fresh': reason is None and basis != 'indicative_suspended_carry_not_tradable'}

    def summary(self):
        rights = {aid: book.summary() for aid, book in self.books.items()}
        pending_cost = sum(l['entitlement_cost_fen'] for aid,b in self.books.items()
                           for l in rights[aid]['lots'] if not b.state['delivered'])
        held_cost = sum(l['cost_fen'] for l in self.state['lots'].values())
        cost_residual = self.initial_cost+self.state['purchased_cost_fen']-self.state['disposed_cost_fen']-held_cost-pending_cost
        fen(self.state['cash_fen'])
        expected_cash = self.config['initial_cash_fen'] + sum(r['net_cash_contribution_fen'] for r in rights.values())
        expected_cash += sum((f['quantity']*f['price_fen']-f['fee_fen']) if f['side'] == 'sell'
                             else -f['cost_basis_fen'] for f in self.state['fills'])
        cash_residual = self.state['cash_fen']-expected_cash
        if cost_residual or cash_residual:
            raise AssertionError('parent/share-receivable cost or cash control failed')
        holdings = {code: sum(l['quantity'] for l in self.state['lots'].values() if l['instrument'] == code)
                    for code in self.config['rules']}
        pending = {code: sum(r['share_receivable_quantity'] for aid,r in rights.items() if self.actions[aid]['instrument'] == code)
                   for code in self.config['rules']}
        share_residual = {}
        for code in holdings:
            expected = sum(l['quantity'] for l in self.config['lots'] if l['instrument'] == code)
            expected += sum(f['quantity']*(1 if f['side'] == 'buy' else -1) for f in self.state['fills'] if f['instrument'] == code)
            expected += sum(r['delivered_quantity'] for aid,r in rights.items() if self.actions[aid]['instrument'] == code)
            share_residual[code] = holdings[code]-expected
        if any(share_residual.values()):
            raise AssertionError('parent physical-share control failed')
        marks = [self._mark(code) for code in sorted(holdings) if holdings[code]+pending[code]]
        priced = all(m['price_fen'] is not None for m in marks)
        cash_rights = sum(r['cash_receivable_fen'] for r in rights.values())
        mv = sum((holdings[m['instrument']]+pending[m['instrument']])*m['price_fen'] for m in marks) if priced else None
        control = self._cash_control()
        indicative_assets = self.state['cash_fen']+cash_rights+mv if priced else None
        net = indicative_assets-control['known_tax_liability_fen'] if priced and not control['tax_unknown'] else None
        fresh = net if net is not None and all(m['fresh'] for m in marks) else None
        income = sum(r['gross_cash_fen'] for r in rights.values())
        tax = sum(r['tax_assessed_fen'] or 0 for r in rights.values())
        reconciliation = (net-self.initial_book_capital-(self.state['realized_trading_pnl_fen']+mv-held_cost-pending_cost+income-tax)) if net is not None else None
        if reconciliation not in (0,None):
            raise AssertionError('parent net-assets/PnL control failed')
        return {'scope': self.scope, 'cash_fen': self.state['cash_fen'], **control, 'holdings': holdings,
            'share_receivable_quantity': pending, 'cash_receivable_fen': cash_rights,
            'held_cost_fen': held_cost, 'share_receivable_cost_fen': pending_cost,
            'cost_reconciliation_residual_fen': cost_residual, 'marks': marks,
            'cash_reconciliation_residual_fen': cash_residual, 'share_reconciliation_residual': share_residual,
            'indicative_assets_before_unassessed_tax_fen': indicative_assets,
            'net_indicative_equity_fen': net, 'fresh_equity_fen': fresh,
            'account_reconciliation_residual_fen': reconciliation,
            'diagnostic_marked_return': str(Decimal(fresh-self.initial_equity)/self.initial_equity) if fresh is not None else None,
            'realized_trading_pnl_fen': self.state['realized_trading_pnl_fen'], 'rights': rights,
            'lots': deepcopy(self.state['lots']), 'exit_intents': deepcopy(self.state['exit_intents']),
            'zero_rights_records': deepcopy(self.state['zero_rights_records']),
            'fills': deepcopy(self.state['fills']), 'state_sha256': identity(self.state),
            'execution_ready': False, 'portfolio_return': None, 'actual_operator_return': None,
            'can_resume_historical_portfolio': False, 'parent_integration': self.integration}


def replay(config, events):
    if not isinstance(events,list) or len(events) > 1000:
        raise ValueError('bounded synthetic parent events required')
    account = IntegratedAccount(config)
    snapshots = [account.summary()]
    for event in events:
        snapshots.append(account.apply(event))
    return {'config_id': identity(config), 'events_id': identity(events), 'snapshots': snapshots,
            'final': account.summary(), 'execution_ready': False}
