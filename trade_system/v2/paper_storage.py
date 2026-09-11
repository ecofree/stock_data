"""Append-only paper journal and account projection under the owning writer."""
from datetime import timedelta
from decimal import Decimal
import hashlib
import json
from collections.abc import MutableMapping
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor

from .accounts import latest_account
from .domain import canonical, identity, utc
from .paper_ledger import PaperBook, ACTIVE


SQL = '''CREATE TABLE paper_account(account_id VARCHAR PRIMARY KEY, config JSON NOT NULL,
 config_hash VARCHAR NOT NULL, last_seq BIGINT NOT NULL, last_hash VARCHAR NOT NULL);
CREATE TABLE paper_ledger_event(account_id VARCHAR NOT NULL, event_id VARCHAR NOT NULL,
 seq BIGINT NOT NULL, known_at TIMESTAMPTZ NOT NULL, payload JSON NOT NULL,
 previous_hash VARCHAR NOT NULL, state_hash VARCHAR NOT NULL, raw_hash VARCHAR NOT NULL,
PRIMARY KEY(account_id,event_id), UNIQUE(account_id,seq))'''

CHECKPOINT_SQL='''CREATE TABLE paper_checkpoint(account_id VARCHAR NOT NULL, seq BIGINT NOT NULL,
 config_hash VARCHAR NOT NULL, state_hash VARCHAR NOT NULL, prefix_hash VARCHAR NOT NULL,
 payload JSON NOT NULL, raw_hash VARCHAR NOT NULL, PRIMARY KEY(account_id,seq))'''
CHECKPOINT_INTERVAL=64
HOT_SQL='''CREATE TABLE paper_history(account_id VARCHAR NOT NULL, seq BIGINT NOT NULL,
 payload JSON NOT NULL, raw_hash VARCHAR NOT NULL, PRIMARY KEY(account_id,seq));
CREATE TABLE paper_identity(account_id VARCHAR NOT NULL, kind VARCHAR NOT NULL,
 identity_key VARCHAR NOT NULL, seq BIGINT NOT NULL, PRIMARY KEY(account_id,kind,identity_key))'''


def ensure_hot(store, *, create=False):
    digest=hashlib.sha256(HOT_SQL.encode()).hexdigest()
    found=store.con.execute('SELECT sha256 FROM v2_schema WHERE version=7').fetchone()
    if found:
        if found!=(digest,):
            raise ValueError('hot state migration checksum mismatch')
    elif create:
        with store.transaction():
            store.con.execute(HOT_SQL)
            store.con.execute('INSERT INTO v2_schema VALUES (7,?)',[digest])
    else:
        raise ValueError('hot state schema missing')


def _bind_history(store, book, seq):
    account=book.config['account_id']
    book.seen=_JournalSeen(store,account,seq)
    book.identity_used=lambda kind,key: store.con.execute('''SELECT 1 FROM paper_identity
        WHERE account_id=? AND kind=? AND identity_key=? AND seq<=?''',[account,kind,key,seq]).fetchone() is not None


def _event_identity(event):
    if event['kind']=='submit':
        return ('order',event['payload']['order_id'])
    if event['kind'] in ('split','cash_dividend'):
        return ('action',event['payload']['action_id'])
    return None


def _archive_body(store, body, raw):
    path=store.path.parent/(store.path.name+'.raw')/raw
    expected=body.encode()
    if hashlib.sha256(expected).hexdigest()!=raw:
        raise ValueError('paper archive/checksum mismatch')
    try:
        with path.open('rb') as stream:
            archived=stream.read(len(expected)+1)
    except OSError as exc:
        raise ValueError('paper archive/checksum mismatch') from exc
    if len(archived)!=len(expected) or hashlib.sha256(archived).hexdigest()!=raw:
        raise ValueError('paper archive/checksum mismatch')


def _verify_hot_archives(store, rows):
    """Bounded read-only file I/O; no worker accesses DuckDB or mutates a book."""
    jobs=[]
    for row in rows:
        jobs.extend(((row[1],row[4]),(row[7],row[8])))
        if row[0]%CHECKPOINT_INTERVAL==0:jobs.append((row[14],row[15]))
    if any(not isinstance(body,str) or not isinstance(raw,str) for body,raw in jobs):
        raise ValueError('paper archive/checksum missing')
    def verify(job):
        store.check_deadline()
        _archive_body(store,*job)
        store.check_deadline()
    # Active file reads are joined before returning even on timeout. This is
    # a cooperative safe-boundary budget, not forced cancellation of OS I/O.
    with ThreadPoolExecutor(max_workers=4,thread_name_prefix='paper-archive-read') as pool:
        for _ in pool.map(verify,jobs):
            store.check_deadline()


def _cold_write(store, book, seq):
    body=canonical(book.cold_batch)
    raw=store.archive(body.encode())
    store.con.execute('INSERT INTO paper_history VALUES (?,?,?,?)',[book.config['account_id'],seq,body,raw])


def _load_hot(store, config, head):
    """Cold-start full audit, streamed in pages; never keep history in hot RAM.

    Deliberately O(history) recovery integrity. Warm owning-writer operations
    use an authenticated local head and bounded state; offline edits require
    a restart or explicit audit, not a claim of continuous disk monitoring.
    """
    ensure_hot(store)
    book=PaperBook(config)
    account=config['account_id']
    expected_identities=0
    def verify_cold(row, *, archive_verified=False):
        if not row or row[0]!=canonical(book.cold_batch):
            raise ValueError('paper cold history missing or changed')
        if not archive_verified:_archive_body(store,*row)
    verify_cold(store.con.execute('SELECT payload,raw_hash FROM paper_history WHERE account_id=? AND seq=0',[account]).fetchone())
    chain=identity({'format':'bounded_hot_v3','config':identity(config)})
    seq=0
    while True:
        store.check_deadline()
        # Every identity is checked against the durable UNIQUE key and its
        # original sequence. No growing Python seen-set or per-event SQL.
        rows=store.con.execute('''WITH page AS (
            SELECT * FROM paper_ledger_event WHERE account_id=? AND seq>? ORDER BY seq LIMIT 256)
            SELECT e.seq,e.payload,e.previous_hash,e.state_hash,e.raw_hash,e.event_id,CAST(e.known_at AS VARCHAR),
                   h.payload,h.raw_hash,i.kind,i.identity_key,
                   c.config_hash,c.state_hash,c.prefix_hash,c.payload,c.raw_hash
            FROM page e LEFT JOIN paper_history h ON h.account_id=e.account_id AND h.seq=e.seq
            LEFT JOIN paper_identity i ON i.account_id=e.account_id AND i.seq=e.seq
            LEFT JOIN paper_checkpoint c ON c.account_id=e.account_id AND c.seq=e.seq
            ORDER BY e.seq LIMIT 257''',[account,seq]).fetchall()
        if not rows:
            break
        _verify_hot_archives(store,rows)
        for current,body,previous,state_hash,raw,event_id,known_at,cold,cold_raw,kind,key,*cp in rows:
            store.check_deadline()
            if current!=seq+1 or previous!=identity(book.state):
                raise ValueError('paper journal sequence/input checksum mismatch')
            book.seen={}
            book.identity_used=lambda kind,key: False
            event=json.loads(body)
            if event['event_id']!=event_id or utc(event['at'])!=utc(known_at):
                raise ValueError('paper journal identity/receipt mismatch')
            item=_event_identity(event)
            if item!=(None if kind is None and key is None else (kind,key)):
                raise ValueError('paper identity index mismatch')
            book.apply(event)
            if identity(book.state)!=state_hash:
                raise ValueError('paper deterministic replay checksum mismatch')
            seq=current
            verify_cold((cold,cold_raw),archive_verified=True)
            if item:
                expected_identities+=1
            chain=identity([chain,seq,raw,previous,state_hash])
            if seq%CHECKPOINT_INTERVAL==0:
                body_cp=canonical({'format':'bounded_hot_v3','state':book.state})
                if tuple(cp[:4])!=(identity(config),state_hash,chain,body_cp):
                    raise ValueError('paper hot checkpoint checksum mismatch')
            elif any(v is not None for v in cp):
                raise ValueError('unexpected hot checkpoint sequence')
    counts=store.con.execute('''SELECT
        (SELECT count(*) FROM paper_history WHERE account_id=?),
        (SELECT count(*) FROM paper_identity WHERE account_id=?),
        (SELECT count(*) FROM paper_checkpoint WHERE account_id=?),
        (SELECT count(DISTINCT event_id) FROM paper_ledger_event WHERE account_id=?),
        (SELECT count(DISTINCT (kind,identity_key)) FROM paper_identity WHERE account_id=?)''',[account]*5).fetchone()
    if (seq,identity(book.state))!=tuple(head) or counts!=(seq+1,expected_identities,seq//CHECKPOINT_INTERVAL,seq,expected_identities):
        raise ValueError('paper hot history/head control total mismatch')
    store.check_deadline()
    book.journal_chain=chain
    book.journal_seq=seq
    _bind_history(store,book,seq)
    return book


def paper_history(store, book):
    """Explicit offline reporting materialization; never used on append path."""
    if not book.bounded:
        return book.state['orders'],book.state['fills']
    orders,fills=dict(book.state['orders']),[]
    for body,raw in store.con.execute('SELECT payload,raw_hash FROM paper_history WHERE account_id=? ORDER BY seq',[book.config['account_id']]).fetchall():
        _archive_body(store,body,raw)
        obj=json.loads(body)
        orders.update(obj['orders']); fills.extend(obj['fills'])
    return orders,fills


class _JournalSeen(MutableMapping):
    """Indexed durable idempotency; event bodies are not copied into checkpoints."""
    def __init__(self, store, account, seq):
        self.store, self.account, self.seq, self.pending = store, account, seq, {}

    def __getitem__(self, key):
        if key in self.pending:
            return self.pending[key]
        row = self.store.con.execute('''SELECT payload,raw_hash FROM paper_ledger_event
            WHERE account_id=? AND event_id=? AND seq<=?''',[self.account,key,self.seq]).fetchone()
        if row is None:
            raise KeyError(key)
        if hashlib.sha256(row[0].encode()).hexdigest()!=row[1]:
            raise ValueError('paper idempotency journal checksum mismatch')
        return row[0]

    def __setitem__(self, key, value):
        self.pending[key] = value

    def __delitem__(self, key):
        raise ValueError('paper idempotency facts cannot be removed')

    def __iter__(self):
        keys = [r[0] for r in self.store.con.execute('SELECT event_id FROM paper_ledger_event WHERE account_id=? AND seq<=?', [self.account,self.seq]).fetchall()]
        return iter(dict.fromkeys([*keys,*self.pending]))

    def __len__(self):
        return sum(1 for _ in self)


def _state_delta(before, after):
    delta = {}
    for key, value in after.items():
        old = before.get(key)
        if value == old:
            continue
        if isinstance(value,list) and isinstance(old,list) and value[:len(old)]==old:
            delta[key] = {'append':value[len(old):]}
        elif isinstance(value,dict) and isinstance(old,dict):
            delta[key] = {'map':{k:v for k,v in value.items() if k not in old or old[k]!=v},
                          'remove':[k for k in old if k not in value]}
        else:
            delta[key] = {'set':value}
    return delta


def _checkpoint_state(store, account_id, seq):
    """Reconstruct versioned deltas; old full snapshots remain readable."""
    state, previous = None, 0
    for current, body, raw, state_hash in store.con.execute('''SELECT seq,payload,raw_hash,state_hash
        FROM paper_checkpoint WHERE account_id=? AND seq<=? ORDER BY seq''',[account_id,seq]).fetchall():
        path = store.path.parent/(store.path.name+'.raw')/raw
        if hashlib.sha256(body.encode()).hexdigest()!=raw or not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest()!=raw:
            raise ValueError('paper checkpoint archive/checksum mismatch')
        obj = json.loads(body)
        if obj.get('format') == 'state_delta_v2':
            if obj['parent_seq']!=previous:
                raise ValueError('paper checkpoint parent missing')
            state = state or {}
            for key, change in obj['delta'].items():
                if 'set' in change:
                    state[key] = change['set']
                elif 'append' in change:
                    state[key].extend(change['append'])
                else:
                    state[key].update(change['map'])
                    for removed in change['remove']:
                        del state[key][removed]
        else:
            state = obj['state']
        previous = current
    if previous!=seq:
        raise ValueError('paper checkpoint missing')
    if identity(state)!=state_hash:
        raise ValueError('paper checkpoint state checksum mismatch')
    return state


def ensure_checkpoints(store, *, create=True):
    digest=hashlib.sha256(CHECKPOINT_SQL.encode()).hexdigest()
    row=store.con.execute('SELECT sha256 FROM v2_schema WHERE version=6').fetchone()
    if row:
        if row[0]!=digest:
            raise ValueError('paper checkpoint migration checksum mismatch')
        return
    if not create:
        return
    with store.transaction():
        store.con.execute(CHECKPOINT_SQL)
        store.con.execute('INSERT INTO v2_schema VALUES (6,?)',[digest])


def journal_prefix(store,account_id,seq):
    # Still O(history) SQL verification, but no repeated Python event replay.
    # The prefix commitment detects deletion, edited payloads and chain edits.
    count,invalid,digest=store.con.execute('''SELECT count(*),
        count(*) FILTER (WHERE sha256(CAST(payload AS VARCHAR))<>raw_hash),
        sha256(coalesce(string_agg(CAST(seq AS VARCHAR)||':'||raw_hash||':'||previous_hash||':'||state_hash,
            '|' ORDER BY seq),'')) FROM paper_ledger_event WHERE account_id=? AND seq<=?''',[account_id,seq]).fetchone()
    if count!=seq or invalid:
        raise ValueError('paper checkpoint journal prefix checksum mismatch')
    return digest


def ensure_paper(store, *, create=True):
    store.check_owner()
    digest = hashlib.sha256(SQL.encode()).hexdigest()
    row = store.con.execute('SELECT sha256 FROM v2_schema WHERE version=4').fetchone()
    if row:
        if row[0] != digest:
            raise ValueError('paper ledger migration checksum mismatch')
        ensure_checkpoints(store,create=create)
        return
    if not create:
        raise ValueError('unknown paper ledger; read projections never initialize it')
    with store.transaction():
        store.con.execute(SQL)
        store.con.execute('INSERT INTO v2_schema VALUES (4,?)',[digest])
    ensure_checkpoints(store)


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
    if book.bounded:
        ensure_hot(store,create=True)
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
        if book.bounded:
            _cold_write(store,book,0)
    return book.summary()


def load_paper(store, account_id, *, full_replay=False, writer_session=False):
    ensure_paper(store,create=False)
    row = store.con.execute('SELECT config,config_hash,last_seq,last_hash FROM paper_account WHERE account_id=?',[account_id]).fetchone()
    if not row:
        raise ValueError('unknown paper ledger')
    config = json.loads(row[0])
    if identity(config)!=row[1]:
        raise ValueError('paper config checksum mismatch')
    if config.get('state_format')=='bounded_hot_v3':
        cache=getattr(store,'paper_hot_cache',{})
        cached=cache.get(account_id) if writer_session and not full_replay else None
        if cached is not None:
            if (cached.journal_seq,identity(cached.state))!=(row[2],row[3]) or identity(cached.config)!=row[1]:
                cache.pop(account_id,None)
                raise ValueError('owning writer hot head changed; explicit audit required')
            return cached
        cache.pop(account_id,None)
        book=_load_hot(store,config,row[2:])
        if writer_session:
            if len(cache)>=8:
                cache.pop(next(iter(cache)))
            cache[account_id]=book
            store.paper_hot_cache=cache
        return book
    book = PaperBook(config)
    start_seq=0
    has_checkpoints=store.con.execute('SELECT count(*) FROM v2_schema WHERE version=6').fetchone()[0]
    checkpoint=None if full_replay or not has_checkpoints else store.con.execute('''SELECT seq,config_hash,state_hash,prefix_hash,payload,raw_hash
        FROM paper_checkpoint WHERE account_id=? ORDER BY seq DESC LIMIT 1''',[account_id]).fetchone()
    if checkpoint:
        start_seq,config_hash,state_hash,prefix_hash,body,raw_hash=checkpoint
        if config_hash!=row[1] or start_seq>row[2] or hashlib.sha256(body.encode()).hexdigest()!=raw_hash:
            raise ValueError('paper checkpoint checksum/head mismatch')
        archived=store.path.parent/(store.path.name+'.raw')/raw_hash
        if not archived.is_file() or hashlib.sha256(archived.read_bytes()).hexdigest()!=raw_hash:
            raise ValueError('paper checkpoint raw archive checksum mismatch')
        if journal_prefix(store,account_id,start_seq)!=prefix_hash:
            raise ValueError('paper checkpoint journal prefix checksum mismatch')
        restored=_checkpoint_state(store,account_id,start_seq)
        if identity(restored)!=state_hash:
            raise ValueError('paper checkpoint state checksum mismatch')
        book.state=restored
        book.seen=_JournalSeen(store,account_id,start_seq)
    book.checkpoint_state=deepcopy(book.state) if checkpoint else {}
    book.checkpoint_seq=start_seq
    events = store.con.execute('''SELECT seq,payload,previous_hash,state_hash,raw_hash FROM paper_ledger_event
        WHERE account_id=? AND seq>? ORDER BY seq''',[account_id,start_seq]).fetchall()
    for i,(seq,body,previous_hash,state_hash,raw_hash) in enumerate(events,start_seq+1):
        if seq!=i or previous_hash!=identity(book.state) or hashlib.sha256(body.encode()).hexdigest()!=raw_hash:
            raise ValueError('paper journal sequence/input checksum mismatch')
        book.apply(json.loads(body))
        if identity(book.state)!=state_hash:
            raise ValueError('paper deterministic replay checksum mismatch')
    if start_seq+len(events)!=row[2] or identity(book.state)!=row[3]:
        raise ValueError('paper journal head mismatch; do not silently roll back facts')
    return book


def _append(store, book, event, *, transferred_reservation=None, transferred_exit_reservation=None):
    if book.bounded:
        return _append_hot(store,book,event,transferred_reservation,transferred_exit_reservation)
    ensure_paper(store)
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
        if (head[0]+1)%CHECKPOINT_INTERVAL==0:
            body=canonical({'format':'state_delta_v2','parent_seq':book.checkpoint_seq,
                            'delta':_state_delta(book.checkpoint_state,book.state)})
            raw=store.archive(body.encode())
            store.con.execute('INSERT INTO paper_checkpoint VALUES (?,?,?,?,?,?,?)',
                [account_id,head[0]+1,identity(book.config),identity(book.state),
                 journal_prefix(store,account_id,head[0]+1),body,raw])
    return book.summary()


def _append_hot(store, book, event, buy_reservation, sell_reservation):
    store.check_owner()
    account=book.config['account_id']
    prior=identity(book.state)
    seq=book.journal_seq+1
    try:
        book.apply(event)
        body=canonical(event)
        raw=store.archive(body.encode())
        state_hash=identity(book.state)
        chain=identity([book.journal_chain,seq,raw,prior,state_hash])
        with store.transaction():
            if store.con.execute('SELECT last_seq,last_hash FROM paper_account WHERE account_id=?',[account]).fetchone()!=(seq-1,prior):
                raise ValueError('paper head changed before commit')
            store.con.execute('INSERT INTO paper_ledger_event VALUES (?,?,?,?,?,?,?,?)',
                [account,event['event_id'],seq,utc(event['at']),body,prior,state_hash,raw])
            item=_event_identity(event)
            if item:
                store.con.execute('INSERT INTO paper_identity VALUES (?,?,?,?)',[account,*item,seq])
            _cold_write(store,book,seq)
            store.con.execute('UPDATE paper_account SET last_seq=?,last_hash=? WHERE account_id=?',[seq,state_hash,account])
            if buy_reservation:
                store.con.execute("UPDATE reservation SET status='paper_order' WHERE reservation_id=? AND status='held'",[buy_reservation])
            if sell_reservation:
                store.con.execute("UPDATE exit_reservation SET status='paper_order' WHERE reservation_id=? AND status='held'",[sell_reservation])
            _projection(store,book)
            if seq%CHECKPOINT_INTERVAL==0:
                cp=canonical({'format':'bounded_hot_v3','state':book.state})
                cp_raw=store.archive(cp.encode())
                store.con.execute('INSERT INTO paper_checkpoint VALUES (?,?,?,?,?,?,?)',
                    [account,seq,identity(book.config),state_hash,chain,cp,cp_raw])
        book.journal_seq,book.journal_chain=seq,chain
        _bind_history(store,book,seq)
        return book.summary()
    except BaseException:
        # Ambiguous commit invalidates memory. Retry first audits durable facts.
        getattr(store,'paper_hot_cache',{}).pop(account,None)
        raise


def apply_paper_event(store, account_id, event):
    book = load_paper(store,account_id,writer_session=True)
    if set(event)!={'event_id','kind','payload'}:
        raise ValueError('service assigns receipt time; event requires id/kind/payload only')
    prior = store.con.execute('SELECT payload,raw_hash FROM paper_ledger_event WHERE account_id=? AND event_id=?',[account_id,event['event_id']]).fetchone()
    if prior:
        _archive_body(store,*prior)
        previous = json.loads(prior[0]); previous.pop('at')
        if previous!=event:
            raise ValueError('paper request idempotency conflict')
        return book.summary()
    if event['kind']=='submit':
        raise ValueError('paper orders must consume an approved paper decision reservation')
    body = {**event,'at':utc(store.clock()).isoformat()}
    return _append(store,book,body)


def submit_confirmed_buy(store, account_id, confirmation_request_id, order_id):
    book = load_paper(store,account_id,writer_session=True)
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
    from .strategies import signal, latest_instance_signal
    from .event_bridge import recheck_signal_evidence
    sig = signal(store,confirmed['signal_id'])
    newest = latest_instance_signal(store,sig)
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
