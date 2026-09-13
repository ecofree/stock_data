"""Explicit account imports and append-only operator/external facts."""
import json

from .domain import canonical, identity, instrument, money, quantity, utc


def import_snapshot(store, raw: bytes):
    store.check_owner()
    data = json.loads(raw)
    for key in ('account_id', 'mode', 'asof', 'valid_until', 'cash', 'frozen_cash',
                'equity', 'positions', 'open_orders', 'declared_complete', 'source'):
        if key not in data:
            raise ValueError(f'missing account field: {key}')
    if not isinstance(data['account_id'], str) or not data['account_id'].strip():
        raise ValueError('account identity required')
    if data['mode'] not in ('paper', 'live') or data['declared_complete'] is not True:
        raise ValueError('account mode and explicit completeness declaration required')
    if data['source'] not in ('manual_declaration', 'broker_export'):
        raise ValueError('account source must be declared')
    asof, until, received = utc(data['asof']), utc(data['valid_until']), utc(store.clock())
    if not asof <= received < until:
        raise ValueError('account snapshot is future-dated or expired')
    cash, frozen, equity = money(data['cash']), money(data['frozen_cash']), money(data['equity'])
    if frozen > cash or equity <= 0:
        raise ValueError('invalid cash control totals')
    if not isinstance(data['positions'], list) or not isinstance(data['open_orders'], list):
        raise ValueError('explicit positions and open-order lists required')
    held, codes = 0, set()
    for pos in data['positions']:
        code = instrument(pos['instrument'])
        if code in codes:
            raise ValueError('duplicate position; consolidate explicitly before import')
        codes.add(code)
        qty, sellable = quantity(pos['quantity']), quantity(pos['sellable'])
        price = money(pos['mark_price'])
        if sellable > qty or (qty and price <= 0):
            raise ValueError('invalid sellable quantity or mark')
        held += qty * price
    # Unknown/pending external actions require reconciliation. Do not
    # assume their absence or silently release their frozen money.
    pending_cash = 0
    orders = set()
    for order in data['open_orders']:
        if not order.get('order_id') or order['order_id'] in orders:
            raise ValueError('unique external order id required')
        orders.add(order['order_id'])
        instrument(order['instrument'])
        quantity(order['remaining_quantity'])
        pending_cash += money(order['frozen_cash'])
    reconciled = abs(cash + held - equity) <= 1 and pending_cash == frozen
    source_hash = store.archive(raw)
    key = identity(data)
    with store.transaction():
        previous = store.con.execute('SELECT mode FROM account_snapshot WHERE account_id=? LIMIT 1',
                                     [data['account_id']]).fetchone()
        if previous and previous[0] != data['mode']:
            raise ValueError('paper and live accounts cannot share an identity')
        latest = store.con.execute('SELECT max(asof_time) FROM account_snapshot WHERE account_id=?', [data['account_id']]).fetchone()[0]
        if latest and asof < latest:
            raise ValueError('out-of-order account import requires an explicit correction')
        seq = store.con.execute('SELECT coalesce(max(seq),0)+1 FROM account_snapshot').fetchone()[0]
        store.con.execute('INSERT INTO account_snapshot VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT DO NOTHING',
                          [key, data['account_id'], data['mode'], asof, until, cash, frozen, equity,
                           reconciled, source_hash, canonical(data), received, seq])
    return {'snapshot_id': key, 'reconciled': reconciled, 'mode': data['mode']}


def latest_account(store, account_id):
    store.check_owner()
    return read_account(store.con,account_id)


def read_account(con, account_id):
    """Same account authority for an owning writer or a read-only projection."""
    row = con.execute('''SELECT snapshot_id,mode,asof_time,valid_until,cash_fen,frozen_fen,
         equity_fen,reconciled,payload,imported_at FROM account_snapshot WHERE account_id=?
         ORDER BY seq DESC LIMIT 1''', [account_id]).fetchone()
    if row is None:
        return None
    result = dict(zip(('snapshot_id', 'mode', 'asof', 'valid_until', 'cash_fen', 'frozen_fen',
                       'equity_fen', 'reconciled', 'payload', 'imported_at'), row))
    result['payload'] = json.loads(result['payload'])
    return result


def risk_snapshot(source, account_id, as_of):
    """Never imports, reserves or reconciles while presenting an account."""
    from pathlib import Path
    import duckdb
    at=utc(as_of)
    with duckdb.connect(str(Path(source).resolve(strict=True)),read_only=True) as con:
        con.execute('BEGIN TRANSACTION')
        account=read_account(con,account_id)
        if account is None:return {'status':'account_unknown','account_id':account_id,'execution_ready':False}
        blockers=[]
        if not utc(account['asof'])<=at<utc(account['valid_until']):blockers.append('account_stale_or_future')
        if not account['reconciled']:blockers.append('account_unreconciled')
        pending=con.execute("SELECT instrument,amount_fen,status FROM reservation WHERE account_id=? AND status IN ('held','unknown')",[account_id]).fetchall()
        if any(r[2]=='unknown' for r in pending):blockers.append('unresolved_internal_action')
        orders=account['payload']['open_orders']
        if orders:blockers.append('open_external_orders_require_reconciliation')
        result={'status':'risk_blocked' if blockers else 'account_snapshot_available_not_execution',
            'account_id':account_id,'snapshot_id':account['snapshot_id'],'mode':account['mode'],
            'as_of':account['asof'].isoformat(),'valid_until':account['valid_until'].isoformat(),
            'cash_fen':account['cash_fen'],'frozen_fen':account['frozen_fen'],
            'positions':account['payload']['positions'],'open_orders':orders,
            'internal_reservations':[{'instrument':c,'amount_fen':v,'status':s} for c,v,s in pending],
            'declared_complete':account['payload']['declared_complete'],'reconciled':account['reconciled'],
            'blockers':blockers,'execution_ready':False,'source_mode':'read_only_core_account'}
        return result


def append_account_event(store, event_id, account_id, kind, payload):
    store.check_owner()
    if kind not in ('fill', 'fee', 'cash_transfer', 'corporate_action', 'correction', 'external_action_unknown'):
        raise ValueError('unknown account event')
    encoded = canonical(payload)
    previous = store.con.execute('SELECT account_id,kind,payload FROM account_event WHERE event_id=?', [event_id]).fetchone()
    if previous:
        if previous != (account_id, kind, encoded):
            raise ValueError('idempotency conflict')
        return False
    if kind == 'correction' and not store.con.execute(
        'SELECT count(*) FROM account_event WHERE account_id=? AND event_id=?',
        [account_id, payload.get('corrects')],
    ).fetchone()[0]:
        raise ValueError('correction must refer to an existing account event')
    store.con.execute('INSERT INTO account_event VALUES (?,?,?,?,?)',
                      [event_id, account_id, utc(store.clock()), kind, encoded])
    return True
