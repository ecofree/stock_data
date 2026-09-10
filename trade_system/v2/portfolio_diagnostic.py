"""Cash/share OOS diagnostics under explicit daily-price assumptions, never orders.

This module does NOT turn daily bars into observed order books. A missing mark
or an unresolved adjustment-factor change stops the affected account, retaining
positions; the requested-period return stays null. This is not an execution
simulator and must not confer eligibility on signals or models.
"""
from collections import Counter, defaultdict
from datetime import date
from decimal import Decimal
import math

from .domain import identity, instrument, number
from .paper_ledger import fen, rounded


SCOPE = 'historical_daily_price_assumption_not_executable'


def validate_policy(policy):
    if policy.get('scope') != SCOPE or policy.get('exposure_status') != 'previously_inspected_not_untouched':
        raise ValueError('explicit historical proxy and exposure disclosure required')
    if policy.get('fill_assumption') != 'unconstrained_next_open_and_due_close':
        raise ValueError('only explicit daily-price hypothesis supported')
    if policy.get('calendar_assumption') != 'SSE_sessions_shared_not_exchange_certified':
        raise ValueError('calendar coverage assumption must be explicit')
    if policy.get('missing_mark_policy') != 'stop_account' or policy.get('corporate_action_policy') != 'stop_on_held_factor_change':
        raise ValueError('unresolved accounting must stop, not liquidate or erase holdings')
    for key, upper in [('top_k', 100), ('max_positions', 1000), ('buy_lot', 10000),
                       ('hold_sessions', 60), ('t_plus_sessions', 60), ('ticket_bps', 10000)]:
        if type(policy[key]) is not int or not 1 <= policy[key] <= upper:
            raise ValueError('positive bounded policy integer required: ' + key)
    if policy['hold_sessions'] < policy['t_plus_sessions'] + 1:
        raise ValueError('holding period must include settlement: entry is session one')
    if fen(policy['initial_cash_fen']) <= 0:
        raise ValueError('positive initial cash required')
    fees = policy['fees']
    if not fees.get('version'):
        raise ValueError('explicit hypothetical fee version required')
    fen(fees['minimum_commission_fen'])
    for key in ('commission_bps', 'transfer_bps', 'sell_tax_bps'):
        if not 0 <= number(fees[key]) <= 10000:
            raise ValueError('invalid hypothetical fee')


def fee(policy, notional, side):
    if side not in ('buy', 'sell'):
        raise ValueError('buy or sell required')
    fen(notional)
    if not notional:
        return 0
    p = policy['fees']
    return (max(p['minimum_commission_fen'], rounded(Decimal(notional)*number(p['commission_bps'])/10000))
            + rounded(Decimal(notional)*number(p['transfer_bps'])/10000)
            + (rounded(Decimal(notional)*number(p['sell_tax_bps'])/10000) if side == 'sell' else 0))


def _positive(value):
    return value is not None and number(value) > 0


def evaluate_portfolio(predictions, bars, calendar, identity_map, policy):
    """Pure, deterministic, one isolated account per variant; no label input.

    predictions: [date, research_code, score]. bars: date, instrument,
    open_fen/close_fen (unadjusted), factor. Calendar is a declared session list.
    Fixed initial-capital tickets; no averaging into existing holdings, no
    replacement below Top-K, no cash reuse from today's close at today's open.
    """
    validate_policy(policy)
    if not calendar or calendar != sorted(set(calendar)):
        raise ValueError('unique ordered session calendar required')
    for day in calendar:
        date.fromisoformat(day)
    indexes = {day: i for i, day in enumerate(calendar)}
    if len(set(identity_map.values())) != len(identity_map):
        raise ValueError('ambiguous research-to-exchange identity')
    for code in identity_map.values():
        instrument(code)
    market = {}
    for bar in bars:
        key = (bar['date'], instrument(bar['instrument']))
        if key in market or key[0] not in indexes:
            raise ValueError('duplicate bar or bar outside declared calendar')
        for field in ('open_fen', 'close_fen'):
            if bar[field] is not None:
                fen(bar[field])
        if bar['factor'] is not None and not _positive(bar['factor']):
            raise ValueError('invalid adjustment factor')
        market[key] = bar
    scores = defaultdict(list)
    seen = set()
    for day, code, score in predictions:
        if day not in indexes or code not in identity_map or (day, code) in seen or not math.isfinite(score):
            raise ValueError('invalid, duplicate or unmapped prediction')
        seen.add((day, code))
        scores[day].append((code, score))
    if not scores:
        raise ValueError('nonempty OOS prediction set required')
    first, last = min(indexes[d] for d in scores), max(indexes[d] for d in scores)
    # Last signal -> next-session open -> hold_sessions-th close, plus bounded
    # delay sessions if future adapters supply an explicit non-fill reason.
    end = last + policy['hold_sessions']
    if end >= len(calendar):
        raise ValueError('calendar lacks full planned liquidation horizon')
    cash = initial = policy['initial_cash_fen']
    ticket = initial * policy['ticket_bps'] // 10000
    holdings, fills, nav, skips = {}, [], [], []
    realized = total_fees = 0
    peak, max_drawdown = initial, Decimal(0)
    blocker = None

    def trade(day, code, side, qty, price, cost):
        nonlocal cash, realized, total_fees
        notional = qty * price
        expense = fee(policy, notional, side)
        cash += notional - expense if side == 'sell' else -notional - expense
        total_fees += expense
        if side == 'sell':
            realized += notional - expense - cost
        fills.append({'date': day, 'instrument': code, 'side': side, 'quantity': qty,
                      'price_fen': price, 'fee_fen': expense, 'cost_basis_fen': cost,
                      'scope': SCOPE, 'actual_fill': False})

    for index in range(first, end + 1):
        day = calendar[index]
        # Corporate-action state is checked before any hypothetical liquidation;
        # never discard future-affected candidates retrospectively.
        for code, lot in holdings.items():
            bar = market.get((day, code))
            reason = ('missing_held_factor' if not bar or bar['factor'] is None else
                      'unresolved_corporate_action' if number(bar['factor']) != number(lot['factor']) else None)
            if reason:
                blocker = {'date': day, 'instrument': code, 'reason': reason}
                break
        if blocker:
            break
        previous = calendar[index-1] if index else None
        selected = sorted(scores.get(previous, []), key=lambda row: (-row[1], row[0]))[:policy['top_k']]
        for research_code, score in selected:
            code = identity_map[research_code]
            bar = market.get((day, code))
            reason = ('already_held' if code in holdings else
                      'position_cap' if len(holdings) >= policy['max_positions'] else
                      'missing_entry_bar' if not bar or not bar['open_fen'] else
                      'missing_entry_factor' if bar['factor'] is None else None)
            if not reason:
                price = bar['open_fen']
                lot_size = policy['buy_lot']
                budget = min(ticket, cash)
                low, high = 0, budget // price // lot_size
                while low < high:
                    middle = (low + high + 1) // 2
                    notional = middle * lot_size * price
                    if notional + fee(policy, notional, 'buy') <= budget:
                        low = middle
                    else:
                        high = middle - 1
                qty = low * lot_size
                if not qty:
                    reason = 'insufficient_ticket_or_cash'
            if reason:
                skips.append({'signal_date': previous, 'entry_date': day, 'instrument': code, 'reason': reason})
                continue
            cost = qty*price + fee(policy, qty*price, 'buy')
            trade(day, code, 'buy', qty, price, cost)
            holdings[code] = {'quantity': qty, 'cost_fen': cost, 'factor': bar['factor'],
                              'entry_date': day, 'due_index': index + policy['hold_sessions'] - 1,
                              'sellable_index': index + policy['t_plus_sessions']}
        for code, lot in list(holdings.items()):
            bar = market.get((day, code))
            if not bar or not bar['close_fen']:
                blocker = {'date': day, 'instrument': code, 'reason': 'missing_current_mark'}
                break
            if index >= max(lot['due_index'], lot['sellable_index']):
                trade(day, code, 'sell', lot['quantity'], bar['close_fen'], lot['cost_fen'])
                del holdings[code]
        if blocker:
            break
        marked = sum(lot['quantity']*market[(day, code)]['close_fen'] for code, lot in holdings.items())
        unrealized = marked - sum(lot['cost_fen'] for lot in holdings.values())
        equity = cash + marked
        if cash < 0 or realized + unrealized != equity - initial:
            raise AssertionError('integer cash/share accounting did not reconcile')
        peak = max(peak, equity)
        drawdown = Decimal(peak - equity) / peak
        max_drawdown = max(max_drawdown, drawdown)
        nav.append({'date': day, 'cash_fen': cash, 'equity_fen': equity, 'positions': len(holdings),
                    'realized_pnl_fen': realized, 'unrealized_pnl_fen': unrealized,
                    'fees_fen': total_fees, 'drawdown': str(drawdown), 'reconciliation_residual_fen': 0})
    complete = blocker is None and not holdings
    return {'scope': SCOPE, 'execution_ready': False, 'signal_impact': 'disabled',
            'requested_period': [calendar[first], calendar[end]], 'period_complete': complete,
            'blocker': blocker, 'cash_fen': cash, 'open_positions': holdings, 'fees_fen': total_fees,
            'realized_pnl_fen': realized, 'last_fully_valued': nav[-1] if nav else None,
            'hypothetical_return': str(Decimal(cash-initial)/initial) if complete else None,
            'hypothetical_max_daily_drawdown': str(max_drawdown) if complete else None,
            'portfolio_return': None, 'actual_operator_return': None,
            'prediction_identity_sha256': identity(sorted([d, c] for d, c in seen)),
            'fills': fills, 'nav': nav, 'skips': skips, 'skip_counts': dict(Counter(r['reason'] for r in skips)),
            'limitations': ['unconstrained daily prices, not observed liquidity or execution evidence',
                           'no auction queue, price-limit, halt, capacity, spread or impact certification',
                           'shared SSE calendar and uniform lot/fees are scenario assumptions',
                           'factor stability is a necessary check, not proof of complete corporate actions',
                           'evaluable-label-conditioned sampled pool and historical timestamps, not PIT certification',
                           'do not compare incomplete accounts on unequal truncated horizons']}
