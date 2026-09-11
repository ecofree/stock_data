"""Reduce-only paper policy support for the single DecisionService authority."""
from dataclasses import dataclass
from datetime import timedelta
import hashlib
import json

from .accounts import latest_account
from .domain import canonical, identity, instrument, money, quantity, utc
from .paper_storage import load_paper


SQL = '''CREATE TABLE exit_policy(version VARCHAR PRIMARY KEY,payload JSON NOT NULL,sha256 VARCHAR NOT NULL);
CREATE TABLE exit_reservation(reservation_id VARCHAR PRIMARY KEY,decision_id VARCHAR UNIQUE NOT NULL,
 account_id VARCHAR NOT NULL,instrument VARCHAR NOT NULL,quantity BIGINT NOT NULL,status VARCHAR NOT NULL)'''


def ensure_exits(store):
    store.check_owner()
    digest = hashlib.sha256(SQL.encode()).hexdigest()
    row = store.con.execute('SELECT sha256 FROM v2_schema WHERE version=5').fetchone()
    if row:
        if row[0]!=digest:
            raise ValueError('exit migration checksum mismatch')
        return
    with store.transaction():
        store.con.execute(SQL)
        store.con.execute('INSERT INTO v2_schema VALUES (5,?)',[digest])


@dataclass(frozen=True)
class ExitPolicy:
    version: str
    reason: str
    reference_id: str
    expires_at: str
    trigger_price_fen: int | None = None

    def validate(self):
        if not self.version or not self.reference_id or self.reason not in ('manual_reduce','price_below'):
            raise ValueError('explicit versioned paper exit rationale required')
        utc(self.expires_at)
        if self.reason=='price_below':
            if quantity(self.trigger_price_fen)<=0:
                raise ValueError('explicit positive exit price threshold required')
        elif self.trigger_price_fen is not None:
            raise ValueError('manual reduction must not hide a price condition')


def freeze_exit_policy(store, policy):
    ensure_exits(store)
    policy.validate()
    encoded = canonical(policy.__dict__)
    old = store.con.execute('SELECT payload,sha256 FROM exit_policy WHERE version=?',[policy.version]).fetchone()
    if old and old!=(encoded,identity(policy.__dict__)):
        raise ValueError('exit policy version immutable or checksum mismatch')
    store.con.execute('INSERT INTO exit_policy VALUES (?,?,?) ON CONFLICT DO NOTHING',
                      [policy.version,encoded,identity(policy.__dict__)])


def lot_allowed(qty, certificate):
    return qty>0 and qty<=certificate['max_quantity'] and (qty%certificate['sell_lot']==0 or
           certificate['allow_odd_sell_all'] and qty==certificate['available_quantity'])


def evaluate_exit(authority, account_id, code, quote_manifest, quote_dataset, exit_policy, *, exclude_reservation=None):
    store,risk = authority.store,authority.policy
    store.check_owner()
    ensure_exits(store)
    policy = ExitPolicy(**exit_policy); policy.validate(); instrument(code)
    stored = store.con.execute('SELECT payload,sha256 FROM exit_policy WHERE version=?',[policy.version]).fetchone()
    if stored!=(canonical(exit_policy),identity(exit_policy)):
        raise ValueError('frozen exit policy required')
    at = utc(store.clock()); expiry = utc(policy.expires_at)
    account = latest_account(store,account_id)
    blockers = []
    if at>=expiry:
        blockers.append('exit_policy_expired')
    book = None
    if not account:
        blockers.append('account_unknown')
    else:
        if account['mode']!='paper':
            blockers.append('live_account_not_enabled')
        if not account['reconciled']:
            blockers.append('account_not_reconciled')
        if not account['asof']<=at<account['valid_until']:
            blockers.append('account_stale_or_future')
        expiry = min(expiry,account['valid_until'])
        if store.con.execute('SELECT count(*) FROM account_event WHERE account_id=? AND happened_at>=?',
                             [account_id,account['imported_at']]).fetchone()[0]:
            blockers.append('account_event_requires_reconciliation')
        exists = store.con.execute("SELECT count(*) FROM information_schema.tables WHERE table_name='paper_account'").fetchone()[0]
        if exists and store.con.execute('SELECT count(*) FROM paper_account WHERE account_id=?',[account_id]).fetchone()[0]:
            book = load_paper(store,account_id)
            if account['payload'].get('ledger_hash')!=identity(book.state) or account['payload'].get('source')!='paper_ledger_projection':
                blockers.append('account_not_current_ledger_projection')
            if not book.complete_valuation(at):
                blockers.append('current_valuation_incomplete')
        else:
            blockers.append('paper_ledger_required')
    context = store.con.execute('SELECT asof_time,mode FROM input_manifest WHERE manifest_id=?',[quote_manifest]).fetchone()
    if not context or context[1]!='system_replay' or context[0]>at:
        blockers.append('invalid_quote_context')
    rows = store.facts(quote_manifest,quote_dataset,code) if context else []
    product = store.con.execute('SELECT unit,semantics FROM data_product WHERE dataset=?',[quote_dataset]).fetchone()
    price = 0
    if not rows or product!=('CNY','point'):
        blockers.append('required_quote_missing')
    else:
        row = rows[-1]; price = money(row[4])
        expiry = min(expiry,row[1]+timedelta(seconds=risk.quote_ttl_seconds),row[2]+timedelta(seconds=risk.quote_ttl_seconds))
        if price<=0 or not row[1]<=at or not row[2]<=at or at>=expiry:
            blockers.append('quote_stale_or_invalid')
        latest = store.con.execute('''SELECT fact_id FROM fact WHERE dataset=? AND instrument=? AND event_time<=? AND known_at<=?
            ORDER BY event_time DESC,seq DESC LIMIT 1''',[quote_dataset,code,at,at]).fetchone()
        if latest!=(row[0],):
            blockers.append('quote_version_superseded')
        if book and book.day(row[1])!=book.day(at):
            blockers.append('previous_session_quote')
    if policy.reason=='price_below' and price>policy.trigger_price_fen:
        blockers.append('exit_price_condition_not_met')
    available,max_qty,lot,odd,config_hash = 0,0,1,False,None
    if book:
        config_hash = identity(book.config)
        if code not in book.config['instruments']:
            blockers.append('effective_instrument_rule_required')
        else:
            rule = book._rule(code,at)
            lot,odd = rule['sell_lot'],rule['allow_odd_sell_all']
            held = store.con.execute('''SELECT coalesce(sum(quantity),0) FROM exit_reservation
                WHERE account_id=? AND instrument=? AND status IN ('held','unknown') AND reservation_id<>?''',
                [account_id,code,exclude_reservation or '']).fetchone()[0]
            available = max(0,book.available(code,at)-held)
            cap = min(available,risk.max_quantity)
            max_qty = cap if odd and cap==available else cap//lot*lot
            if not max_qty:
                blockers.append('no_unreserved_settled_inventory')
    return {'side':'sell','reduce_only':True,'account_id':account_id,'snapshot_id':account['snapshot_id'] if account else '',
            'signal_id':'','instrument':code,'input_manifest':quote_manifest,'quote_manifest':quote_manifest,
            'quote_dataset':quote_dataset,'quote_fact_id':rows[-1][0] if rows else None,'price_fen':price,
            'asof':at.isoformat(),'expires_at':expiry.isoformat(),'policy':risk.__dict__,'exit_policy':exit_policy,
            'ledger_config_hash':config_hash,'sell_lot':lot,'allow_odd_sell_all':odd,'available_quantity':available,
            'max_quantity':0 if blockers else max_qty,'blockers':sorted(set(blockers)),
            'execution_ready':False,'hypothetical_ready':False,'scope':'paper_only',
            'allowed_actions':['observe'] if blockers else ['observe','paper_confirm']}


def submit_confirmed_sell(store, account_id, confirmation_request_id, order_id):
    from .decisions import DecisionService, RiskPolicy
    from .paper_storage import _append
    ensure_exits(store)
    book = load_paper(store,account_id,writer_session=True)
    request = store.con.execute('SELECT payload,account_id FROM operator_action WHERE action_id=?',[confirmation_request_id]).fetchone()
    if not request or request[1]!=account_id:
        raise ValueError('paper confirmation belongs to a different or unknown account')
    confirmed = json.loads(request[0])['result']
    if confirmed.get('side')!='sell' or not confirmed['hypothetical_ready'] or confirmed['execution_ready']:
        raise ValueError('approved reduce-only paper confirmation required')
    event_id = 'paper-sell:'+confirmation_request_id
    if event_id in book.seen:
        if json.loads(book.seen[event_id])['payload']['order_id']!=order_id:
            raise ValueError('confirmation already delivered to another paper order')
        return book.summary()
    reservation = confirmed['reservation_id']
    row = store.con.execute('SELECT account_id,instrument,quantity,status FROM exit_reservation WHERE reservation_id=?',[reservation]).fetchone()
    if row!=(account_id,confirmed['instrument'],confirmed['quantity'],'held'):
        raise ValueError('held sell quantity reservation required; unknown is not releasable')
    authority = DecisionService(store,RiskPolicy(**confirmed['policy']))
    current = evaluate_exit(authority,account_id,confirmed['instrument'],confirmed['quote_manifest'],
                            confirmed['quote_dataset'],confirmed['exit_policy'],exclude_reservation=reservation)
    if current['blockers'] or utc(store.clock())>=utc(confirmed['expires_at']):
        raise ValueError('paper exit conditions changed or expired; reconfirm required')
    if any(current[k]!=confirmed[k] for k in ('snapshot_id','quote_fact_id','ledger_config_hash')) or not lot_allowed(confirmed['quantity'],current):
        raise ValueError('paper exit account/quote/inventory changed; reconfirm required')
    event = {'event_id':event_id,'at':utc(store.clock()).isoformat(),'kind':'submit','payload':{
        'order_id':order_id,'instrument':confirmed['instrument'],'side':'sell','quantity':confirmed['quantity'],
        'limit_price_fen':confirmed['price_fen'],'decision_ref':confirmed['decision_id']}}
    return _append(store,book,event,transferred_exit_reservation=reservation)
