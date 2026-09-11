"""Deterministic integer-money paper execution ledger, not a broker simulator.

Only explicitly observed order-book capacity may fill an order. Rules,
calendar and fee assumptions are supplied and versioned, never inferred from
stock-code prefixes or advertised as verified exchange rules.
"""
from copy import deepcopy
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from zoneinfo import ZoneInfo

from .domain import canonical, identity, instrument, number, quantity, utc


ACTIVE = {'open','partial','cancel_pending','unknown'}


def fen(value):
    if type(value) is not int or not 0 <= value <= 10**16:
        raise ValueError('nonnegative integer fen required')
    return value


def rounded(value):
    return int(Decimal(value).quantize(Decimal(1),rounding=ROUND_HALF_UP))


class PaperBook:
    def __init__(self, config):
        self.config = deepcopy(config)
        self.bounded = config.get('state_format') == 'bounded_hot_v3'
        if config.get('state_format') not in (None, 'bounded_hot_v3'):
            raise ValueError('unsupported paper state format')
        self.identity_used = lambda kind, key: False
        if self.bounded:
            self.identity_used = None
        self.cold_batch = {}
        if self.bounded:
            limits = config.get('hot_limits', {})
            if set(limits) != {'orders','lots','instruments'} or any(type(v) is not int or not 1 <= v <= 10000 for v in limits.values()):
                raise ValueError('explicit bounded active entity limits required')
            if len(config['instruments']) > limits['instruments']:
                raise ValueError('instrument hot limit exceeded')
        canonical(config)
        if config.get('mode') != 'paper' or not config.get('account_id'):
            raise ValueError('explicit paper account required; live is unsupported')
        days = config['trading_days']
        if not days or days != sorted(set(days)):
            raise ValueError('explicit ordered trading calendar required')
        for day in days:
            date.fromisoformat(day)
        for key in ('commission_bps','transfer_bps','sell_tax_bps'):
            if not 0 <= number(config['fees'][key]) <= 10000:
                raise ValueError('finite explicit fee rate required')
        fen(config['fees']['minimum_commission_fen'])
        if not config['fees'].get('version') or not config['instruments']:
            raise ValueError('versioned instrument and fee rules required')
        for policy in (config['fees'],*config['instruments'].values()):
            if date.fromisoformat(policy['effective_from'])>date.fromisoformat(policy['effective_to']):
                raise ValueError('invalid effective rule date range')
        for key in ('quote_ttl_seconds','mark_ttl_seconds','account_ttl_seconds'):
            if type(config.get(key)) is not int or config[key]<=0:
                raise ValueError('explicit positive quote/mark/account age limits required')
        for code,rule in config['instruments'].items():
            instrument(code)
            if not rule.get('version') or any(type(rule[k]) is not int or rule[k]<=0 for k in ('buy_lot','sell_lot','t_plus_sessions')):
                raise ValueError('positive frozen lot/T+N rules required')
            if type(rule['allow_odd_sell_all']) is not bool:
                raise ValueError('explicit odd-lot rule required')
        self.seen = {}
        at = utc(config['opened_at']).isoformat()
        for code in config['instruments']:
            self._rule(code,at)
        self.state = {'cash_fen':fen(config['initial_cash_fen']),'lots':[], 'orders':{},'fills':[],
                      'marks':{},'last_quotes':{},'corporate_actions':[], 'realized_pnl_fen':0,
                      'external_cash_fen':0,'income_fen':0,'fees_fen':0,'last_at':at,'nav_history':[]}
        for i,lot in enumerate(config.get('initial_lots',[])):
            code = instrument(lot['instrument'])
            self._rule(code,at)
            qty = quantity(lot['quantity'])
            if not qty or lot['sellable_from'] not in days or fen(lot['mark_price_fen'])<=0:
                raise ValueError('explicit initial quantity, mark and sellable date required')
            self.state['lots'].append({'lot_id':'initial-'+str(i),'instrument':code,'quantity':qty,
                                      'cost_fen':fen(lot['cost_fen']),'sellable_from':lot['sellable_from']})
            self.state['marks'][code] = {'price_fen':lot['mark_price_fen'],'at':at,'basis':'initial_declaration'}
        initial = self.equity()
        if initial <= 0:
            raise ValueError('positive declared starting equity required')
        self.state.update(initial_equity_fen=initial,units=str(initial),high_water_nav='1')
        if self.bounded:
            self.state.update(history_hash=identity({'format':'bounded_hot_v3'}), history_count=0,
                              max_drawdown='0')
        self._snapshot('initial',at)

    def day(self, at):
        return utc(at).astimezone(ZoneInfo('Asia/Shanghai')).date().isoformat()

    def _rule(self, code, at):
        rule = self.config['instruments'][code]
        day = self.day(at)
        if day not in self.config['trading_days']:
            raise ValueError('date absent from frozen trading calendar')
        for policy in (rule,self.config['fees']):
            if not policy['effective_from'] <= day <= policy['effective_to']:
                raise ValueError('no effective frozen rule/fee version for date')
        return rule

    def fee(self, notional, side):
        if not notional:
            return 0
        rules = self.config['fees']
        commission = max(rules['minimum_commission_fen'],rounded(Decimal(notional)*number(rules['commission_bps'])/10000))
        transfer = rounded(Decimal(notional)*number(rules['transfer_bps'])/10000)
        tax = rounded(Decimal(notional)*number(rules['sell_tax_bps'])/10000) if side=='sell' else 0
        return commission+transfer+tax

    def reserve(self, order):
        if order['side'] != 'buy' or order['status'] not in ACTIVE:
            return 0
        remainder = order['remaining']*order['limit_price_fen']
        return remainder+self.fee(order['notional_fen']+remainder,'buy')-order['fee_fen']

    def frozen(self):
        return sum(self.reserve(order) for order in self.state['orders'].values())

    def holding(self, code, at=None):
        return sum(lot['quantity'] for lot in self.state['lots'] if lot['instrument']==code and
                   (at is None or lot['sellable_from']<=self.day(at)))

    def available(self, code, at):
        reserved = sum(o['remaining'] for o in self.state['orders'].values() if
                       o['instrument']==code and o['side']=='sell' and o['status'] in ACTIVE)
        return self.holding(code,at)-reserved

    def equity(self):
        return self.state['cash_fen']+sum(lot['quantity']*self.state['marks'][lot['instrument']]['price_fen'] for lot in self.state['lots'])

    def complete_valuation(self, at):
        return all(self.day(self.state['marks'][lot['instrument']]['at'])==self.day(at) and
                   0 <= (utc(at)-utc(self.state['marks'][lot['instrument']]['at'])).total_seconds() <= self.config['mark_ttl_seconds']
                   for lot in self.state['lots'] if lot['quantity'])

    def apply(self, event):
        if self.bounded and not callable(self.identity_used):
            raise ValueError('bounded ledger requires durable identity backend')
        canonical(event)
        key = event['event_id']
        if not isinstance(key,str) or not key:
            raise ValueError('explicit ledger event identity required')
        encoded = canonical(event)
        if key in self.seen:
            if self.seen[key] != encoded:
                raise ValueError('ledger event idempotency conflict')
            return self.summary()
        at = utc(event['at']).isoformat()
        if utc(at) < utc(self.state['last_at']):
            raise ValueError('ledger events must be delivered in knowledge-time order')
        prior = deepcopy(self.state)
        try:
            self._apply(event,at)
            if self.state['cash_fen'] < self.frozen() or any(l['quantity']<0 or l['cost_fen']<0 for l in self.state['lots']):
                raise ValueError('cash/inventory control total violation')
            self.state['last_at'] = at
            self._snapshot(key,at)
        except BaseException:
            self.state = prior
            raise
        self.seen[key] = encoded
        return self.summary()

    def _apply(self, event, at):
        kind, p = event['kind'],event['payload']
        if kind == 'submit':
            code = instrument(p['instrument']); rule = self._rule(code,at)
            qty = quantity(p['quantity']); price = fen(p['limit_price_fen']); side = p['side']
            if side not in ('buy','sell') or not qty or not price or not p.get('decision_ref'):
                raise ValueError('explicit side, quantity, price and paper decision reference required')
            if not isinstance(p['order_id'],str) or not p['order_id'] or p['order_id'] in self.state['orders'] or self.identity_used('order',p['order_id']):
                raise ValueError('order identity already used')
            if side=='buy' and qty%rule['buy_lot']:
                raise ValueError('buy quantity violates frozen lot rule')
            if side=='sell':
                available = self.available(code,at)
                if qty>available:
                    raise ValueError('T+N or reserved inventory prevents sale')
                if qty%rule['sell_lot'] and not (rule['allow_odd_sell_all'] and qty==available):
                    raise ValueError('sell quantity violates frozen odd-lot rule')
            order = {'order_id':p['order_id'],'instrument':code,'side':side,'quantity':qty,'remaining':qty,
                     'limit_price_fen':price,'status':'open','created_at':at,'notional_fen':0,'fee_fen':0,
                     'decision_ref':p['decision_ref'],'rule_version':rule['version'],'last_blocker':None}
            self.state['orders'][p['order_id']] = order
        elif kind in ('cancel_request','cancel_ack','reject','unknown','resolve_open'):
            order = self.state['orders'][p['order_id']]
            if order['status'] not in ACTIVE:
                raise ValueError('terminal order cannot change status')
            if kind in ('cancel_ack','reject','resolve_open') and not p.get('evidence_id'):
                raise ValueError('explicit paper resolution evidence required')
            if kind=='reject' and order['remaining'] != order['quantity']:
                raise ValueError('partially filled order cannot be retroactively rejected')
            if kind=='resolve_open' and order['status'] != 'unknown':
                raise ValueError('resolve_open is only for unknown order state')
            if kind=='cancel_request' and order['status']=='unknown':
                raise ValueError('unknown order must be resolved explicitly; cancellation intent is not resolution')
            order['status'] = {'cancel_request':'cancel_pending','cancel_ack':'cancelled',
                               'reject':'rejected','unknown':'unknown','resolve_open':'open'}[kind]
        elif kind=='market':
            self._market(event,at)
        elif kind=='cash_transfer':
            amount = p['amount_fen']
            if type(amount) is not int or not p.get('evidence_id') or not self.complete_valuation(at):
                raise ValueError('cash transfer needs integer amount, evidence and current valuation')
            nav = Decimal(self.equity())/Decimal(self.state['units'])
            if nav<=0 or self.equity()+amount<=0:
                raise ValueError('cannot price external cash flow into nonpositive equity')
            self.state['units'] = str(Decimal(self.state['units'])+Decimal(amount)/nav)
            self.state['cash_fen'] += amount
            self.state['external_cash_fen'] += amount
        elif kind in ('cash_dividend','split'):
            code = instrument(p['instrument']); self._rule(code,at)
            if not p.get('action_id') or not p.get('evidence_id') or p['action_id'] in self.state['corporate_actions'] or self.identity_used('action',p['action_id']):
                raise ValueError('unique corporate action and entitlement evidence required')
            if kind=='cash_dividend':
                # Entitlement is explicit, not incorrectly inferred from
                # ex-date holdings after record-date trading.
                amount = quantity(p['entitled_quantity'])*fen(p['cash_per_share_fen'])
                self.state['cash_fen'] += amount
                self.state['income_fen'] += amount
            else:
                if any(o['instrument']==code and o['status'] in ACTIVE for o in self.state['orders'].values()):
                    raise ValueError('corporate action with open orders requires explicit cancellation/adjustment')
                n,d = quantity(p['numerator']),quantity(p['denominator'])
                if not n or not d or not fen(p['post_action_mark_fen']):
                    raise ValueError('explicit positive split ratio and post-action mark required')
                for lot in self.state['lots']:
                    if lot['instrument']==code:
                        if lot['quantity']*n%d:
                            raise ValueError('fractional split requires explicit cash-in-lieu handling')
                        lot['quantity'] = lot['quantity']*n//d
                self.state['marks'][code] = {'price_fen':p['post_action_mark_fen'],'at':at,'basis':'declared_corporate_action'}
            self.state['corporate_actions'].append(p['action_id'])
        else:
            raise ValueError('unsupported paper ledger event')

    def _market(self, event, at):
        p = event['payload']; code = instrument(p['instrument']); rule = self._rule(code,at)
        source = utc(p['source_event_at'])
        if source>utc(at):
            raise ValueError('cannot fill from a quote not yet received')
        if self.day(source)!=self.day(at):
            raise ValueError('previous-session quote is not current execution evidence')
        if (utc(at)-source).total_seconds()>self.config['quote_ttl_seconds']:
            for order in self.state['orders'].values():
                if order['instrument']==code and order['status'] in ACTIVE:
                    order['last_blocker']='stale_quote'
            return
        if code in self.state['last_quotes'] and source<=utc(self.state['last_quotes'][code]):
            return  # repeated/late snapshot cannot refill displayed capacity
        if p.get('evidence_kind')!='observed_orderbook' or p.get('phase')!='continuous' or p.get('tradable') is not True:
            for order in self.state['orders'].values():
                if order['instrument']==code and order['status'] in ACTIVE:
                    order['last_blocker']='no_observable_continuous_liquidity'
            return
        if p.get('rule_version')!=rule['version']:
            raise ValueError('quote trading-rule version mismatch')
        lower,upper = fen(p['lower_limit_fen']),fen(p['upper_limit_fen'])
        bid,ask,last = (fen(p[k]) for k in ('bid_fen','ask_fen','last_fen'))
        bid_qty,ask_qty = quantity(p['bid_quantity']),quantity(p['ask_quantity'])
        if not 0<lower<=last<=upper or (bid_qty and not lower<=bid<=upper) or (ask_qty and not lower<=ask<=upper):
            raise ValueError('invalid versioned price limits/orderbook')
        if bid_qty and ask_qty and bid>ask:
            raise ValueError('crossed orderbook is not a fill assumption')
        self.state['last_quotes'][code] = source.isoformat()
        self.state['marks'][code] = {'price_fen':last,'at':source.isoformat(),'basis':'observed_orderbook'}
        capacity = {'buy':ask_qty,'sell':bid_qty}
        for order in self.state['orders'].values():
            if order['instrument']!=code or order['status'] not in ('open','partial','cancel_pending'):
                continue
            side = order['side']; price = ask if side=='buy' else bid
            if source<=utc(order['created_at']):
                order['last_blocker']='quote_not_after_order'
                continue
            if not capacity[side] or (side=='buy' and price>order['limit_price_fen']) or (side=='sell' and price<order['limit_price_fen']):
                order['last_blocker']='no_capacity_at_limit'
                continue
            fill_qty = min(order['remaining'],capacity[side])
            self._fill(order,fill_qty,price,at,event['event_id'])
            capacity[side] -= fill_qty

    def _fill(self, order, qty, price, at, quote_id):
        code,side = order['instrument'],order['side']
        value = qty*price
        cumulative_fee = self.fee(order['notional_fen']+value,side)
        expense = cumulative_fee-order['fee_fen']
        if side=='buy':
            rule = self._rule(code,at)
            index = self.config['trading_days'].index(self.day(at))+rule['t_plus_sessions']
            if index >= len(self.config['trading_days']):
                raise ValueError('calendar lacks required settlement date')
            self.state['cash_fen'] -= value+expense
            self.state['lots'].append({'lot_id':order['order_id']+':'+quote_id,'instrument':code,'quantity':qty,
                                      'cost_fen':value+expense,'sellable_from':self.config['trading_days'][index]})
            cost = value+expense
        else:
            remaining,cost = qty,0
            for lot in self.state['lots']:
                if lot['instrument']!=code or lot['sellable_from']>self.day(at) or not remaining:
                    continue
                used = min(lot['quantity'],remaining)
                allocated = lot['cost_fen'] if used==lot['quantity'] else rounded(Decimal(lot['cost_fen'])*used/lot['quantity'])
                lot['quantity'] -= used; lot['cost_fen'] -= allocated
                remaining -= used; cost += allocated
            if remaining:
                raise ValueError('insufficient settled lots')
            self.state['cash_fen'] += value-expense
            self.state['realized_pnl_fen'] += value-expense-cost
        order['remaining'] -= qty; order['notional_fen'] += value; order['fee_fen'] = cumulative_fee
        order['status'] = ('cancel_pending' if order['status']=='cancel_pending' else 'partial') if order['remaining'] else 'filled'
        order['last_blocker'] = None
        self.state['fees_fen'] += expense
        self.state['fills'].append({'order_id':order['order_id'],'quote_event_id':quote_id,'at':at,'side':side,
                                   'instrument':code,'quantity':qty,'price_fen':price,'fee_fen':expense,
                                   'cost_basis_fen':cost,'scope':'paper_observed_capacity_proxy'})

    def _snapshot(self, event_id, at):
        nav = Decimal(self.equity())/Decimal(self.state['units'])
        peak = max(Decimal(self.state['high_water_nav']),nav)
        self.state['high_water_nav'] = str(peak)
        self.state['nav_history'].append({'event_id':event_id,'at':at,'cash_fen':self.state['cash_fen'],
            'equity_fen':self.equity(),'frozen_fen':self.frozen(),'unit_nav':str(nav),
            'drawdown':str((peak-nav)/peak),'valuation_complete':self.complete_valuation(at)})
        if self.bounded:
            terminal = {k:v for k,v in self.state['orders'].items() if v['status'] not in ACTIVE}
            self.cold_batch = {'nav':self.state['nav_history'][-1], 'orders':terminal,
                               'fills':self.state['fills'], 'actions':self.state['corporate_actions']}
            self.state['history_hash'] = identity({'previous':self.state['history_hash'],'batch':self.cold_batch})
            self.state['history_count'] += 1
            self.state['max_drawdown'] = str(max(Decimal(self.state['max_drawdown']), (peak-nav)/peak))
            self.state['nav_history'] = self.state['nav_history'][-1:]
            self.state['orders'] = {k:v for k,v in self.state['orders'].items() if k not in terminal}
            self.state['lots'] = [lot for lot in self.state['lots'] if lot['quantity']]
            self.state['fills'], self.state['corporate_actions'] = [], []
            for key in ('orders','lots'):
                if len(self.state[key]) > self.config['hot_limits'][key]:
                    raise ValueError('active '+key+' hot limit exceeded; no existing exposure discarded')

    def summary(self):
        return {'account_id':self.config['account_id'],'mode':'paper','execution_ready':False,
                'asof':self.state['last_at'],
                'cash_fen':self.state['cash_fen'],'equity_fen':self.equity(),'frozen_fen':self.frozen(),
                'free_cash_fen':self.state['cash_fen']-self.frozen(),'fees_fen':self.state['fees_fen'],
                'realized_pnl_fen':self.state['realized_pnl_fen'],'income_fen':self.state['income_fen'],
                'profit_ex_external_cash_fen':self.equity()-self.state['initial_equity_fen']-self.state['external_cash_fen'],
                'max_observed_drawdown':self.state['max_drawdown'] if self.bounded else str(max(Decimal(r['drawdown']) for r in self.state['nav_history'])),
                'valuation_complete':self.complete_valuation(self.state['last_at']),
                'scope':'paper_observed_capacity_proxy_not_actual_fills'}
