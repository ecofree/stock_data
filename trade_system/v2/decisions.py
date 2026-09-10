"""Single paper decision authority, atomic reservations and current rechecks.

Live-account and broker execution are deliberately unavailable here. Paper
approval is hypothetical_ready, never execution_ready. No default risk budget.
"""
from dataclasses import dataclass
from datetime import timedelta
import json

from .accounts import latest_account
from .domain import canonical, identity, number, quantity, utc
from .strategies import signal


@dataclass(frozen=True)
class RiskPolicy:
    version: str
    max_total_fraction: str
    max_single_fraction: str
    lot_size: int
    quote_ttl_seconds: int
    max_quantity: int
    fee_buffer_fen: int
    test_only: bool = True

    def validate(self):
        if not self.version or not all(0 < number(v) <= 1 for v in (self.max_total_fraction, self.max_single_fraction)):
            raise ValueError('risk policy fractions must be explicitly configured in (0,1]')
        for value in (self.lot_size, self.quote_ttl_seconds, self.max_quantity):
            if quantity(value) <= 0:
                raise ValueError('positive rule units, TTL and capacity required')
        quantity(self.fee_buffer_fen)


class DecisionService:
    def __init__(self, store, policy):
        policy.validate()
        self.store, self.policy = store, policy

    def evaluate(self, account_id, signal_id, quote_manifest, quote_dataset):
        store, policy = self.store, self.policy
        store.check_owner()
        at = utc(store.clock())
        sig, account = signal(store, signal_id), latest_account(store, account_id)
        blockers = []
        expires = utc(sig['policy']['expires_at'])
        if sig['state'] != 'triggered':
            blockers.append('signal_not_triggered')
        if at >= expires or utc(sig['asof']) > at:
            blockers.append('signal_expired_or_future')
        newest = store.con.execute('''SELECT state,signal_id FROM signal_event WHERE instrument=? AND strategy_version=?
            ORDER BY asof_time DESC, rowid DESC LIMIT 1''', [sig['instrument'], sig['strategy_version']]).fetchone()
        if newest and newest[0] != 'triggered':
            blockers.append('signal_no_longer_triggered')
        if newest and newest[1] != signal_id:
            blockers.append('signal_version_superseded')
        if sig['mode'] != 'system_replay':
            blockers.append('historical_assumption_only')
        if sig.get('evidence'):
            from .event_bridge import recheck_signal_evidence
            blockers.extend(recheck_signal_evidence(store,sig))
        if account is None:
            blockers.append('account_unknown')
        else:
            if account['mode'] != 'paper':
                blockers.append('live_account_not_enabled')
            if not account['reconciled']:
                blockers.append('account_not_reconciled')
            if not account['asof'] <= at < account['valid_until']:
                blockers.append('account_stale_or_future')
            if store.con.execute('SELECT count(*) FROM account_event WHERE account_id=? AND happened_at>=?',
                                 [account_id, account['imported_at']]).fetchone()[0]:
                blockers.append('account_event_requires_reconciliation')
            expires = min(expires, account['valid_until'])
        context = store.con.execute('SELECT asof_time,mode FROM input_manifest WHERE manifest_id=?', [quote_manifest]).fetchone()
        if context is None or context[1] != 'system_replay' or context[0] > at:
            blockers.append('invalid_quote_context')
        rows = store.facts(quote_manifest, quote_dataset, sig['instrument']) if context else []
        product = store.con.execute('SELECT unit,semantics FROM data_product WHERE dataset=?', [quote_dataset]).fetchone()
        price_fen = 0
        if not rows or product != ('CNY', 'point'):
            blockers.append('required_quote_missing')
        else:
            row = rows[-1]
            from .domain import money
            price_fen = money(row[4])
            if price_fen <= 0 or (at - row[1]).total_seconds() > policy.quote_ttl_seconds or (at - row[2]).total_seconds() > policy.quote_ttl_seconds:
                blockers.append('quote_stale_or_invalid')
            if not money(sig['policy']['entry_low']) <= price_fen <= money(sig['policy']['entry_high']):
                blockers.append('price_outside_frozen_strategy_range')
            expires = min(expires, row[1] + timedelta(seconds=policy.quote_ttl_seconds),
                          row[2] + timedelta(seconds=policy.quote_ttl_seconds))
        max_qty = 0
        if not blockers:
            reservations = store.con.execute('''SELECT instrument,amount_fen FROM reservation
                WHERE account_id=? AND status IN ('held','unknown')''', [account_id]).fetchall()
            reserved = sum(r[1] for r in reservations)
            same_reserved = sum(r[1] for r in reservations if r[0] == sig['instrument'])
            held = account['equity_fen'] - account['cash_fen']
            same_held = sum(p['quantity'] * int(number(p['mark_price']) * 100)
                            for p in account['payload']['positions'] if p['instrument'] == sig['instrument'])
            same_pending = sum(int(number(o['frozen_cash']) * 100) for o in account['payload']['open_orders']
                               if o['instrument'] == sig['instrument'])
            budget = min(
                account['cash_fen'] - account['frozen_fen'] - reserved,
                int(number(policy.max_total_fraction) * account['equity_fen']) - held - reserved - account['frozen_fen'],
                int(number(policy.max_single_fraction) * account['equity_fen']) - same_held - same_reserved - same_pending,
            ) - policy.fee_buffer_fen
            max_qty = max(0, min(policy.max_quantity, budget // price_fen)) // policy.lot_size * policy.lot_size
            if max_qty == 0:
                blockers.append('risk_budget_exhausted')
        return {'account_id': account_id, 'snapshot_id': account['snapshot_id'] if account else '',
                'signal_id': signal_id, 'instrument': sig['instrument'], 'input_manifest': sig['manifest_id'],
                'quote_manifest': quote_manifest, 'quote_dataset': quote_dataset,
                'quote_fact_id': rows[-1][0] if rows else None, 'price_fen': price_fen,
                'asof': at.isoformat(), 'expires_at': expires.isoformat(), 'policy': policy.__dict__,
                'max_quantity': max_qty, 'blockers': sorted(set(blockers)),
                'execution_ready': False, 'hypothetical_ready': False, 'scope': 'paper_only',
                'allowed_actions': ['observe'] if blockers else ['observe', 'paper_confirm']}

    def propose(self, account_id, signal_id, quote_manifest, quote_dataset):
        certificate = self.evaluate(account_id, signal_id, quote_manifest, quote_dataset)
        key = identity(certificate)
        self.store.con.execute('INSERT INTO decision_certificate VALUES (?,?,?,?,?,?) ON CONFLICT DO NOTHING',
                               [key, account_id, certificate['snapshot_id'], signal_id,
                                utc(certificate['expires_at']), canonical(certificate)])
        return {'decision_id': key, **certificate}

    def propose_exit(self, account_id, code, quote_manifest, quote_dataset, exit_policy):
        from .exit_policy import ExitPolicy, evaluate_exit, freeze_exit_policy
        policy = ExitPolicy(**exit_policy)
        freeze_exit_policy(self.store,policy)
        certificate = evaluate_exit(self,account_id,code,quote_manifest,quote_dataset,policy.__dict__)
        key = identity(certificate)
        self.store.con.execute('INSERT INTO decision_certificate VALUES (?,?,?,?,?,?) ON CONFLICT DO NOTHING',
                               [key,account_id,certificate['snapshot_id'],'',utc(certificate['expires_at']),canonical(certificate)])
        return {'decision_id':key,**certificate}

    def confirm(self, decision_id, *, quantity_requested, operator, request_id, quote_manifest):
        store = self.store
        store.check_owner()
        qty = quantity(quantity_requested)
        if not operator or not request_id or not qty:
            raise ValueError('explicit operator, request identity and positive quantity required')
        request = {'decision_id': decision_id, 'quantity': qty, 'operator': operator, 'quote_manifest': quote_manifest}
        with store.transaction():
            prior = store.con.execute('SELECT payload FROM operator_action WHERE action_id=?', [request_id]).fetchone()
            if prior:
                previous = json.loads(prior[0])
                if previous['request'] != request:
                    raise ValueError('confirmation idempotency conflict')
                return previous['result']
            row = store.con.execute('SELECT payload FROM decision_certificate WHERE decision_id=?', [decision_id]).fetchone()
            if row is None:
                raise ValueError('unknown decision certificate')
            original = json.loads(row[0])
            is_exit = original.get('side')=='sell'
            if is_exit:
                from .exit_policy import evaluate_exit, lot_allowed
                current = evaluate_exit(self,original['account_id'],original['instrument'],quote_manifest,
                                        original['quote_dataset'],original['exit_policy'])
            else:
                current = self.evaluate(original['account_id'], original['signal_id'], quote_manifest, original['quote_dataset'])
            if 'paper_confirm' not in original['allowed_actions']:
                current['blockers'].append('original_certificate_does_not_allow_confirmation')
            if qty > original['max_quantity']:
                current['blockers'].append('quantity_exceeds_original_certificate')
            if utc(store.clock()) >= utc(original['expires_at']):
                current['blockers'].append('original_certificate_expired')
            if current['snapshot_id'] != original['snapshot_id'] or current['policy'] != original['policy']:
                current['blockers'].append('account_or_policy_changed')
            valid_lot = lot_allowed(qty,current) if is_exit else qty<=current['max_quantity'] and qty%self.policy.lot_size==0
            if not valid_lot:
                current['blockers'].append('quantity_exceeds_current_budget_or_lot')
            reservation_table = 'exit_reservation' if is_exit else 'reservation'
            if store.con.execute(f'SELECT count(*) FROM {reservation_table} WHERE decision_id=?', [decision_id]).fetchone()[0]:
                current['blockers'].append('decision_already_reserved')
            current['blockers'] = sorted(set(current['blockers']))
            current['hypothetical_ready'] = not current['blockers']
            current['allowed_actions'] = ['paper_review'] if current['hypothetical_ready'] else ['observe']
            current['quantity'] = qty if current['hypothetical_ready'] else 0
            current['reservation_id'] = None
            if current['hypothetical_ready']:
                reservation_id = identity([decision_id, request_id])
                amount = qty if is_exit else qty * current['price_fen'] + self.policy.fee_buffer_fen
                store.con.execute(f'INSERT INTO {reservation_table} VALUES (?,?,?,?,?,?)',
                                  [reservation_id, decision_id, original['account_id'], original['instrument'], amount, 'held'])
                current['reservation_id'] = reservation_id
            key = identity(current)
            store.con.execute('INSERT INTO decision_certificate VALUES (?,?,?,?,?,?) ON CONFLICT DO NOTHING',
                              [key, original['account_id'], current['snapshot_id'], original['signal_id'],
                               utc(current['expires_at']), canonical(current)])
            result = {'decision_id': key, **current}
            store.con.execute('INSERT INTO operator_action VALUES (?,?,?,?)',
                              [request_id, original['account_id'], utc(store.clock()), canonical({'request': request, 'result': result})])
            return result

    def mark_unknown(self, reservation_id):
        self.store.check_owner()
        self.store.con.execute("UPDATE reservation SET status='unknown' WHERE reservation_id=? AND status='held'", [reservation_id])

    def mark_exit_unknown(self, reservation_id):
        from .exit_policy import ensure_exits
        ensure_exits(self.store)
        self.store.con.execute("UPDATE exit_reservation SET status='unknown' WHERE reservation_id=? AND status='held'",[reservation_id])
