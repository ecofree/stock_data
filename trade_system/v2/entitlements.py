"""Isolated corporate-entitlement subledger; no orders or production-account writes.

Amounts and cost allocation are explicit diagnostic assumptions, not investor tax
advice or CSDC allocation rules. Only synthetic scenarios are executable in v1.
An announcement is a schedule, never proof of an account receipt or release.
"""
from copy import deepcopy
from datetime import date
from decimal import Decimal
from zoneinfo import ZoneInfo

from .domain import canonical, identity, instrument, number, quantity, utc
from .paper_ledger import fen, rounded


SCOPE = 'synthetic_entitlement_subledger_not_account_execution'
POLICY = 'proportional_cost_account_cash_half_up_exact_shares_v1'


def day(value):
    return utc(value).astimezone(ZoneInfo('Asia/Shanghai')).date().isoformat()


def dated(value):
    if not isinstance(value, str) or date.fromisoformat(value).isoformat() != value:
        raise ValueError('canonical date required')
    return value


def validate_terms(terms):
    required = {'action_id', 'instrument', 'action_type', 'record_date', 'ex_date',
                'cash_pay_date', 'share_delivery_date', 'share_listing_date',
                'gross_cny_per_share', 'shares_per_share', 'available_at', 'evidence_refs'}
    if set(terms) != required or not isinstance(terms['action_id'], str) or not terms['action_id']:
        raise ValueError('strict dated entitlement terms required')
    instrument(terms['instrument'])
    utc(terms['available_at'])
    if not isinstance(terms['evidence_refs'], list) or not terms['evidence_refs'] or any(
            not isinstance(x, str) or not x for x in terms['evidence_refs']):
        raise ValueError('explicit evidence references required')
    record, ex = dated(terms['record_date']), dated(terms['ex_date'])
    if record >= ex:
        raise ValueError('record close must precede ex date')
    cash, shares = number(terms['gross_cny_per_share']), number(terms['shares_per_share'])
    if not 0 <= cash <= 100000 or not 0 <= shares <= 100 or not cash + shares:
        raise ValueError('bounded nonnegative entitlement rates required')
    kind = terms['action_type']
    if kind == 'cash_dividend':
        valid = cash > 0 and shares == 0
    elif kind in ('bonus_share', 'capitalization'):
        valid = shares > 0 and cash == 0
    elif kind in ('cash_and_bonus', 'cash_and_capitalization'):
        valid = cash > 0 and shares > 0
    else:
        valid = False
    if not valid:
        raise ValueError('action semantics inconsistent; bonus/capitalization are not split')
    for field, required_component in [('cash_pay_date', cash), ('share_delivery_date', shares),
                                       ('share_listing_date', shares)]:
        value = terms[field]
        if required_component:
            if dated(value) < ex:
                raise ValueError('scheduled settlement cannot precede ex date')
        elif value is not None:
            raise ValueError('absent component must have no settlement date')
    canonical(terms)


def _allocate_cash(lots, rate):
    """Round the account total once; allocate residual fen deterministically.

    This is an internal scenario allocation, NOT the actual cash/tax statement.
    """
    exact = [Decimal(l['quantity']) * rate * 100 for l in lots]
    amounts = [int(v) for v in exact]
    remainder = rounded(sum(exact)) - sum(amounts)
    order = sorted(range(len(lots)), key=lambda i: (-(exact[i] - amounts[i]), lots[i]['lot_id']))
    for i in order[:remainder]:
        amounts[i] += 1
    return amounts


class EntitlementBook:
    """Frozen record-close rights -> receivables -> explicit receipts -> release.

    Parent positions remain outside this subledger. The cost-transfer output must
    be consumed atomically by a future parent ledger, before any ex-date disposal.
    It is deliberately not connected to PaperBook, Service, or old experiments.
    """
    scope = SCOPE
    snapshot_basis = 'synthetic_record_close'

    def __init__(self, config):
        config = deepcopy(config)
        if set(config) != {'scope', 'policy', 'terms', 'snapshot'} or config['scope'] != self.scope or config['policy'] != POLICY:
            raise ValueError('explicit synthetic scope and accounting policy required')
        validate_terms(config['terms'])
        terms, snapshot = config['terms'], config['snapshot']
        if set(snapshot) != {'account_id', 'asof', 'available_at', 'basis', 'lots'} or snapshot['basis'] != self.snapshot_basis:
            raise ValueError('synthetic closing entitlement snapshot required')
        if not isinstance(snapshot['account_id'], str) or not snapshot['account_id']:
            raise ValueError('explicit synthetic account identity required')
        close = utc(terms['record_date'] + 'T15:00:00+08:00')
        if utc(snapshot['asof']) != close or utc(snapshot['available_at']) < close:
            raise ValueError('snapshot must describe record-date close, not payment holdings')
        lots = snapshot['lots']
        if not isinstance(lots, list) or not 1 <= len(lots) <= 1000:
            raise ValueError('bounded nonempty record lots required')
        seen = set()
        for lot in lots:
            if set(lot) != {'lot_id', 'instrument', 'quantity', 'cost_fen', 'acquired_at', 'restricted'}:
                raise ValueError('strict record lot required')
            lid = lot['lot_id']
            if not isinstance(lid, str) or not lid or lid in seen:
                raise ValueError('unique record lot identity required')
            seen.add(lid)
            if lot['instrument'] != terms['instrument'] or not quantity(lot['quantity']):
                raise ValueError('positive matching record quantity required')
            fen(lot['cost_fen'])
            if utc(lot['acquired_at']) > close or type(lot['restricted']) is not bool:
                raise ValueError('record ownership/restriction must be explicit')
        self.config = config
        self.seen = {}
        self.state = {'phase': 'recorded', 'last_at': max(utc(terms['available_at']),
                      utc(snapshot['available_at'])).isoformat(), 'lots': [], 'cash_received_fen': 0,
                      'withheld_fen': 0, 'tax_assessed_fen': None, 'tax_paid_later_fen': 0,
                      'assessed': False, 'paid': False, 'delivered': False, 'released': False,
                      'events': []}

    def apply(self, event):
        if set(event) != {'event_id', 'at', 'effective_at', 'kind', 'value', 'evidence_ref'}:
            raise ValueError('strict settlement event envelope required')
        canonical(event)
        key = event['event_id']
        if not isinstance(key, str) or not key or not isinstance(event['evidence_ref'], str) or not event['evidence_ref']:
            raise ValueError('settlement event identity and evidence reference required')
        digest = identity(event)
        if key in self.seen:
            if self.seen[key] != digest:
                raise ValueError('settlement idempotency conflict')
            return self.summary()
        at, effective = utc(event['at']), utc(event['effective_at'])
        if at < utc(self.state['last_at']) or effective > at:
            raise ValueError('knowledge order / future effective event violation')
        if self.state['events'] and effective < utc(self.state['events'][-1]['effective_at']):
            raise ValueError('late effective events need a new replay, not retroactive mutation')
        prior = deepcopy(self.state)
        try:
            self._apply(event)
            self.state['last_at'] = at.isoformat()
            self.state['events'].append(deepcopy(event))
            self.summary()
        except BaseException:
            self.state = prior
            raise
        self.seen[key] = digest
        return self.summary()

    def _apply(self, event):
        terms, state = self.config['terms'], self.state
        kind, value, effective_day = event['kind'], event['value'], day(event['effective_at'])
        if not isinstance(value, dict):
            raise ValueError('structured settlement value required')
        if kind == 'ex':
            if value or state['phase'] != 'recorded' or effective_day != terms['ex_date']:
                raise ValueError('one exact ex-date accrual required')
            lots = sorted(self.config['snapshot']['lots'], key=lambda r: r['lot_id'])
            cash = _allocate_cash(lots, number(terms['gross_cny_per_share']))
            for lot, cash_amount in zip(lots, cash):
                shares = Decimal(lot['quantity']) * number(terms['shares_per_share'])
                if shares != int(shares):
                    raise ValueError('fractional shares require actual registry allocation; no local rounding')
                quantity(int(shares))
                # Cost is redistributed, not erased, and is NOT a tax basis rule.
                transfer = rounded(Decimal(lot['cost_fen']) * shares / (lot['quantity'] + shares))
                state['lots'].append({**lot, 'entitled_shares': int(shares), 'gross_cash_fen': fen(cash_amount),
                    'parent_remaining_cost_fen': lot['cost_fen'] - transfer,
                    'entitlement_cost_fen': transfer, 'delivered_shares': 0, 'released_shares': 0})
            state['phase'] = 'accrued'
            return
        if state['phase'] != 'accrued' or effective_day < terms['ex_date']:
            raise ValueError('settlement requires ex-date accrual')
        gross = sum(l['gross_cash_fen'] for l in state['lots'])
        shares = sum(l['entitled_shares'] for l in state['lots'])
        if kind == 'cash_payment':
            if set(value) != {'net_fen', 'withheld_fen'} or not gross or state['paid']:
                raise ValueError('one explicit cash receipt required')
            if effective_day < terms['cash_pay_date']:
                raise ValueError('cash receipt precedes scheduled pay date')
            net, withheld = fen(value['net_fen']), fen(value['withheld_fen'])
            if net + withheld != gross or (state['assessed'] and withheld > state['tax_assessed_fen']):
                raise ValueError('cash receipt does not reconcile to gross / tax')
            state.update(paid=True, cash_received_fen=net, withheld_fen=withheld)
        elif kind == 'share_delivery':
            if set(value) != {'by_lot'} or not shares or state['delivered'] or effective_day < terms['share_delivery_date']:
                raise ValueError('one dated share delivery required')
            expected = {l['lot_id']: l['entitled_shares'] for l in state['lots']}
            if not isinstance(value['by_lot'], dict) or any(type(v) is not int for v in value['by_lot'].values()) or value['by_lot'] != expected:
                raise ValueError('delivered shares do not match frozen rights')
            for lot in state['lots']:
                lot['delivered_shares'] = lot['entitled_shares']
            state['delivered'] = True
        elif kind == 'share_release':
            if value or not state['delivered'] or state['released'] or effective_day < terms['share_listing_date']:
                raise ValueError('release requires delivery and the scheduled listing boundary')
            for lot in state['lots']:
                if not lot['restricted']:
                    lot['released_shares'] = lot['delivered_shares']
            state['released'] = True
        elif kind == 'tax_assessment':
            if set(value) != {'total_fen', 'basis_ref'} or state['assessed'] or not isinstance(value['basis_ref'], str) or not value['basis_ref']:
                raise ValueError('one explicit tax assessment and basis required')
            tax = fen(value['total_fen'])
            if tax < state['withheld_fen']:
                raise ValueError('tax below withheld needs explicit refund workflow, unsupported')
            state.update(assessed=True, tax_assessed_fen=tax)
        elif kind == 'tax_payment':
            if set(value) != {'amount_fen'} or not state['assessed'] or (gross and not state['paid']):
                raise ValueError('tax payment requires assessment and dividend settlement')
            amount = fen(value['amount_fen'])
            due = state['tax_assessed_fen'] - state['withheld_fen'] - state['tax_paid_later_fen']
            if not amount or amount > due:
                raise ValueError('invalid or excessive tax payment')
            state['tax_paid_later_fen'] += amount
        else:
            raise ValueError('unsupported subledger event; no trade or live routing')

    def summary(self):
        state = self.state
        lots = state['lots']
        gross = sum(l['gross_cash_fen'] for l in lots)
        shares = sum(l['entitled_shares'] for l in lots)
        cost_residual = sum(l['cost_fen']-l['parent_remaining_cost_fen']-l['entitlement_cost_fen'] for l in lots)
        cash_due = 0 if state['paid'] else gross
        cash_residual = gross - cash_due - state['cash_received_fen'] - state['withheld_fen']
        delivered = sum(l['delivered_shares'] for l in lots)
        tax_due = (state['tax_assessed_fen'] - state['withheld_fen'] - state['tax_paid_later_fen']) if state['assessed'] else None
        if cost_residual or cash_residual or (tax_due is not None and tax_due < 0):
            raise AssertionError('entitlement control totals failed')
        complete = state['phase'] == 'accrued' and (not gross or state['paid']) and (not shares or state['delivered']) and tax_due == 0
        return {'scope': self.scope, 'phase': state['phase'], 'config_id': identity(self.config),
                'gross_cash_fen': gross, 'cash_receivable_fen': cash_due,
                'cash_received_fen': state['cash_received_fen'], 'withheld_fen': state['withheld_fen'],
                'tax_assessed_fen': state['tax_assessed_fen'], 'tax_due_fen': tax_due,
                'net_cash_contribution_fen': state['cash_received_fen']-state['tax_paid_later_fen'],
                'share_receivable_quantity': shares-delivered, 'delivered_quantity': delivered,
                'released_unrestricted_quantity': sum(l['released_shares'] for l in lots),
                'cost_transfer_fen': sum(l['entitlement_cost_fen'] for l in lots),
                'cost_residual_fen': cost_residual, 'cash_residual_fen': cash_residual,
                'tax_status': 'assessed' if state['assessed'] else 'unknown_not_zero',
                'settlement_complete': complete, 'lots': deepcopy(lots), 'state_sha256': identity(state),
                'can_apply_to_account': False, 'can_resume_portfolio': False, 'execution_ready': False,
                'portfolio_return': None, 'parent_integration': 'not_implemented'}


def replay(config, events):
    if not isinstance(events, list) or len(events) > 1000:
        raise ValueError('bounded event stream required')
    book = EntitlementBook(config)
    snapshots = [book.summary()]
    for event in events:
        snapshots.append(book.apply(event))
    return {'config_id': identity(config), 'events_id': identity(events), 'snapshots': snapshots,
            'final': book.summary(), 'execution_ready': False}
