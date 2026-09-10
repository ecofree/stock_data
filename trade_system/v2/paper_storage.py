"""Append-only paper journal and account projection under the owning writer."""
from datetime import timedelta
from decimal import Decimal
import hashlib
import json

from .accounts import latest_account
from .domain import canonical, identity, utc
from .paper_ledger import PaperBook, ACTIVE


SQL = '''CREATE TABLE paper_account(account_id VARCHAR PRIMARY KEY, config JSON NOT NULL,
 config_hash VARCHAR NOT NULL, last_seq BIGINT NOT NULL, last_hash VARCHAR NOT NULL);
CREATE TABLE paper_ledger_event(account_id VARCHAR NOT NULL, event_id VARCHAR NOT NULL,
 seq BIGINT NOT NULL, known_at TIMESTAMPTZ NOT NULL, payload JSON NOT NULL,
 previous_hash VARCHAR NOT NULL, state_hash VARCHAR NOT NULL, raw_hash VARCHAR NOT NULL,
 PRIMARY KEY(account_id,event_id), UNIQUE(account_id,seq))'''


def ensure_paper(store):
    store.check_owner()
    digest = hashlib.sha256(SQL.encode()).hexdigest()
    row = store.con.execute('SELECT sha256 FROM v2_schema WHERE version=4').fetchone()
    if row:
        if row[0] != digest:
            raise ValueError('paper ledger migration checksum mismatch')
        return
    with store.transaction():
        store.con.execute(SQL)
        store.con.execute('INSERT INTO v2_schema VALUES (4,?)',[digest])


def _projection(store, book):
    def cny(value):
        return format(Decimal(value)/100, '.2f')
    at = utc(book.state['last_at'])
    positions = []
    codes = sorted({lot['instrument'] for lot in book.state['lots'] if lot['quantity']})
    for code in codes:
        positions.append({'instrument':code,'quantity':book.holding(code),'sellable':book.holding(code,at),
                          'mark_price':cny(book.state['marks'][code]['price_fen'])})
    orders = [{'order_id':o['order_id'],'instrument':o['instrument'],'remaining_quantity':o['remaining'],
               'side':o['side'],'status':o['status'],'frozen_cash':cny(book.reserve(o))}
              for o in book.state['orders'].values() if o['status'] in ACTIVE]
    payload = {'account_id':book.config['account_id'],'mode':'paper','asof':at.isoformat(),
               'valid_until':(at+timedelta(seconds=book.config['account_ttl_seconds'])).isoformat(),
               'cash':cny(book.state['cash_fen']),'frozen_cash':cny(book.frozen()),
               'equity':cny(book.equity()),'positions':positions,'open_orders':orders,
               'declared_complete':True,'source':'paper_ledger_projection','ledger_hash':identity(book.state)}
    key = identity(payload)
    raw_hash = store.archive(canonical(payload).encode())
    seq = store.con.execute('SELECT coalesce(max(seq),0)+1 FROM account_snapshot').fetchone()[0]
    store.con.execute('INSERT INTO account_snapshot VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT DO NOTHING',
                      [key,payload['account_id'],'paper',at,utc(payload['valid_until']),book.state['cash_fen'],
                       book.frozen(),book.equity(),book.complete_valuation(at),raw_hash,canonical(payload),at,seq])


def open_paper(store, config):
    ensure_paper(store)
    if utc(config['opened_at'])>utc(store.clock()):
        raise ValueError('cannot initialize a future paper snapshot')
    book = PaperBook(config)
    old = store.con.execute('SELECT config_hash FROM paper_account WHERE account_id=?',[config['account_id']]).fetchone()
    if old:
        if old[0] != identity(config):
            raise ValueError('paper account configuration immutable')
        return load_paper(store,config['account_id']).summary()
    if latest_account(store,config['account_id']) is not None:
        raise ValueError('choose a new paper identity; never overwrite existing account facts')
    with store.transaction():
        store.con.execute('INSERT INTO paper_account VALUES (?,?,?,?,?)',
                          [config['account_id'],canonical(config),identity(config),0,identity(book.state)])
        _projection(store,book)
    return book.summary()


def load_paper(store, account_id):
    ensure_paper(store)
    row = store.con.execute('SELECT config,config_hash,last_seq,last_hash FROM paper_account WHERE account_id=?',[account_id]).fetchone()
    if not row:
        raise ValueError('unknown paper ledger')
    config = json.loads(row[0])
    if identity(config)!=row[1]:
        raise ValueError('paper config checksum mismatch')
    book = PaperBook(config)
    events = store.con.execute('''SELECT seq,payload,previous_hash,state_hash,raw_hash FROM paper_ledger_event
        WHERE account_id=? ORDER BY seq''',[account_id]).fetchall()
    for i,(seq,body,previous_hash,state_hash,raw_hash) in enumerate(events,1):
        if seq!=i or previous_hash!=identity(book.state) or hashlib.sha256(body.encode()).hexdigest()!=raw_hash:
            raise ValueError('paper journal sequence/input checksum mismatch')
        book.apply(json.loads(body))
        if identity(book.state)!=state_hash:
            raise ValueError('paper deterministic replay checksum mismatch')
    if len(events)!=row[2] or identity(book.state)!=row[3]:
        raise ValueError('paper journal head mismatch; do not silently roll back facts')
    return book


def _append(store, book, event, *, transferred_reservation=None, transferred_exit_reservation=None):
    account_id = book.config['account_id']
    prior = identity(book.state)
    book.apply(event)
    raw_hash = store.archive(canonical(event).encode())
    with store.transaction():
        head = store.con.execute('SELECT last_seq,last_hash FROM paper_account WHERE account_id=?',[account_id]).fetchone()
        if head[1]!=prior:
            raise ValueError('paper head changed before commit')
        store.con.execute('INSERT INTO paper_ledger_event VALUES (?,?,?,?,?,?,?,?)',
                          [account_id,event['event_id'],head[0]+1,utc(event['at']),canonical(event),prior,identity(book.state),raw_hash])
        store.con.execute('UPDATE paper_account SET last_seq=?,last_hash=? WHERE account_id=?',
                          [head[0]+1,identity(book.state),account_id])
        if transferred_reservation:
            store.con.execute("UPDATE reservation SET status='paper_order' WHERE reservation_id=? AND status='held'",[transferred_reservation])
        if transferred_exit_reservation:
            store.con.execute("UPDATE exit_reservation SET status='paper_order' WHERE reservation_id=? AND status='held'",[transferred_exit_reservation])
        _projection(store,book)
    return book.summary()


def apply_paper_event(store, account_id, event):
    book = load_paper(store,account_id)
    if set(event)!={'event_id','kind','payload'}:
        raise ValueError('service assigns receipt time; event requires id/kind/payload only')
    prior = store.con.execute('SELECT payload FROM paper_ledger_event WHERE account_id=? AND event_id=?',[account_id,event['event_id']]).fetchone()
    if prior:
        previous = json.loads(prior[0]); previous.pop('at')
        if previous!=event:
            raise ValueError('paper request idempotency conflict')
        return book.summary()
    if event['kind']=='submit':
        raise ValueError('paper orders must consume an approved paper decision reservation')
    body = {**event,'at':utc(store.clock()).isoformat()}
    return _append(store,book,body)


def submit_confirmed_buy(store, account_id, confirmation_request_id, order_id):
    book = load_paper(store,account_id)
    request = store.con.execute('SELECT payload,account_id FROM operator_action WHERE action_id=?',[confirmation_request_id]).fetchone()
    if not request or request[1]!=account_id:
        raise ValueError('paper confirmation belongs to a different or unknown account')
    confirmed = json.loads(request[0])['result']
    if confirmed.get('side','buy')!='buy':
        raise ValueError('buy delivery cannot consume a sell confirmation')
    reservation_id = confirmed.get('reservation_id')
    if not confirmed['hypothetical_ready'] or confirmed['execution_ready'] or not reservation_id:
        raise ValueError('approved paper-only confirmation required')
    event_id = 'paper-buy:'+confirmation_request_id
    if event_id in book.seen:
        if json.loads(book.seen[event_id])['payload']['order_id']!=order_id:
            raise ValueError('confirmation already delivered to another paper order')
        return book.summary()
    at = utc(store.clock())
    latest = latest_account(store,account_id)
    if at>=utc(confirmed['expires_at']) or not latest or latest['snapshot_id']!=confirmed['snapshot_id']:
        raise ValueError('paper certificate expired or account changed; reconfirm required')
    if (latest['payload'].get('ledger_hash')!=identity(book.state) or latest['payload'].get('source')!='paper_ledger_projection'
        or latest['mode']!='paper' or not latest['reconciled']):
        raise ValueError('confirmation must bind the current reconciled paper ledger projection')
    if store.con.execute('SELECT count(*) FROM account_event WHERE account_id=? AND happened_at>=?',
                         [account_id,latest['imported_at']]).fetchone()[0]:
        raise ValueError('account event requires reconciliation before paper submission')
    from .strategies import signal
    from .event_bridge import recheck_signal_evidence
    sig = signal(store,confirmed['signal_id'])
    newest = store.con.execute('''SELECT signal_id,state FROM signal_event WHERE instrument=? AND strategy_version=?
        ORDER BY asof_time DESC,rowid DESC LIMIT 1''',[sig['instrument'],sig['strategy_version']]).fetchone()
    if newest!=(confirmed['signal_id'],'triggered') or at>=utc(sig['policy']['expires_at']):
        raise ValueError('signal superseded or expired; reconfirm required')
    if recheck_signal_evidence(store,sig):
        raise ValueError('paper signal event conditions changed; reconfirm required')
    reserved = store.con.execute('SELECT account_id,instrument,amount_fen,status FROM reservation WHERE reservation_id=?',[reservation_id]).fetchone()
    if not reserved or reserved[:2]!=(account_id,confirmed['instrument']) or reserved[3]!='held':
        raise ValueError('held paper reservation required; unknown is not releasable')
    amount = confirmed['quantity']*confirmed['price_fen']
    if amount+book.fee(amount,'buy')>reserved[2]:
        raise ValueError('confirmed fee buffer insufficient for frozen paper fee schedule')
    event = {'event_id':event_id,'at':at.isoformat(),'kind':'submit','payload':{
        'order_id':order_id,'instrument':confirmed['instrument'],'side':'buy','quantity':confirmed['quantity'],
        'limit_price_fen':confirmed['price_fen'],'decision_ref':confirmed['decision_id']}}
    return _append(store,book,event,transferred_reservation=reservation_id)
