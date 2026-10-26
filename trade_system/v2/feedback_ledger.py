"""Independent statement feedback and revision history; no order submission.

Input is an explicit normalized statement plus its original bytes. A declared
broker origin is not evidence of authenticity. Synthetic accounts stay synthetic.
"""
from datetime import date, timedelta, timezone
import hashlib
import json
import re
import sqlite3

from trade_system.file_lock import FileLock
from .domain import canonical, identity, instrument, utc, now_utc
from .isolated_workspace import workspace, child, read_json, checked, seal

SCHEMA = '''
CREATE TABLE IF NOT EXISTS account(account_id TEXT PRIMARY KEY,source_kind TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS original(sha256 TEXT PRIMARY KEY,content BLOB NOT NULL);
CREATE TABLE IF NOT EXISTS statement(statement_id TEXT PRIMARY KEY,account_id TEXT NOT NULL,
 original_sha256 TEXT NOT NULL,payload TEXT NOT NULL,received_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS event(account_id TEXT NOT NULL,event_id TEXT NOT NULL,revision INTEGER NOT NULL,
 payload TEXT NOT NULL,sha256 TEXT NOT NULL,statement_id TEXT NOT NULL,
 PRIMARY KEY(account_id,event_id,revision));
CREATE TABLE IF NOT EXISTS reconciliation(receipt_id TEXT PRIMARY KEY,payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS confirmation(receipt_id TEXT PRIMARY KEY,operator TEXT NOT NULL,
 original_sha256 TEXT NOT NULL,confirmed_at TEXT NOT NULL);
'''
EVENT_FIELDS = {'event_id', 'revision', 'supersedes_sha256', 'kind', 'status', 'order_id',
                'instrument', 'side', 'quantity', 'amount_fen', 'occurred_at', 'trade_date'}
STATEMENT_FIELDS = {'schema', 'account_id', 'source_kind', 'original_sha256', 'period_start',
                    'period_end', 'calendar', 'opening', 'closing', 'events', 'declared_complete',
                    'fees_complete'}


def _name(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,100}', value):
        raise ValueError('bounded stable account/event/order identity required')
    return value


def _integer(value, *, signed=False):
    if type(value) is not int or abs(value) > 10**15 or (not signed and value < 0):
        raise ValueError('bounded exact integer quantity/money required')
    return value


def _day(value):
    if not isinstance(value, str) or date.fromisoformat(value).isoformat() != value:
        raise ValueError('ISO session date required')
    return value


def _snapshot(value):
    if not isinstance(value, dict) or set(value) != {'cash_fen', 'positions'} or not isinstance(value['positions'], dict):
        raise ValueError('explicit cash and position controls required')
    _integer(value['cash_fen'], signed=True)
    if len(value['positions']) > 10000:
        raise ValueError('position budget exceeded')
    for code, qty in value['positions'].items():
        instrument(code)
        _integer(qty)


def validate(data):
    if not isinstance(data, dict) or set(data) != STATEMENT_FIELDS or data['schema'] != 'stockdata_feedback_statement_v1':
        raise ValueError('strict normalized statement contract required')
    _name(data['account_id'])
    if data['source_kind'] not in ('synthetic', 'broker_export'):
        raise ValueError('explicit synthetic or broker export source required; paper is not real feedback')
    if not isinstance(data['original_sha256'], str) or not re.fullmatch('[a-f0-9]{64}', data['original_sha256']):
        raise ValueError('original statement hash required')
    if _day(data['period_start']) > _day(data['period_end']):
        raise ValueError('ordered statement interval required')
    calendar = data['calendar']
    if not isinstance(calendar, list) or len(calendar) > 3700 or len(set(calendar)) != len(calendar):
        raise ValueError('bounded unique declared trading calendar required')
    for day in calendar:
        if not data['period_start'] <= _day(day) <= data['period_end']:
            raise ValueError('calendar outside statement interval')
    if type(data['declared_complete']) is not bool or type(data['fees_complete']) is not bool:
        raise ValueError('explicit completeness declarations required')
    _snapshot(data['opening'])
    _snapshot(data['closing'])
    if not isinstance(data['events'], list) or len(data['events']) > 100000:
        raise ValueError('bounded explicit event list required')
    seen = set()
    for event in data['events']:
        if not isinstance(event, dict) or set(event) != EVENT_FIELDS:
            raise ValueError('strict event envelope required')
        key = (_name(event['event_id']), _integer(event['revision']))
        if not event['revision'] or key in seen:
            raise ValueError('positive unique event revision required')
        seen.add(key)
        if event['revision'] == 1:
            if event['supersedes_sha256'] is not None:
                raise ValueError('initial revision cannot supersede')
        elif not isinstance(event['supersedes_sha256'], str) or not re.fullmatch('[a-f0-9]{64}', event['supersedes_sha256']):
            raise ValueError('revision must name the exact preceding content hash')
        if event['status'] not in ('active', 'void') or (event['status'] == 'void' and event['revision'] == 1):
            raise ValueError('void must be an explicit revision')
        if not data['period_start'] <= _day(event['trade_date']) <= data['period_end']:
            raise ValueError('event outside statement interval')
        moment = utc(event['occurred_at'])
        if moment.astimezone(timezone(timedelta(hours=8))).date().isoformat() != event['trade_date']:
            raise ValueError('event clock and China session date disagree')
        _integer(event['quantity'])
        _integer(event['amount_fen'], signed=event['kind'] == 'cash_transfer')
        if event['kind'] in ('fill', 'cancel'):
            _name(event['order_id'])
            instrument(event['instrument'])
            if event['side'] not in ('buy', 'sell') or event['quantity'] <= 0 or event['trade_date'] not in calendar:
                raise ValueError('trade/cancel requires positive quantity and declared trading session')
            if event['kind'] == 'fill' and event['amount_fen'] <= 0:
                raise ValueError('fill requires positive gross amount excluding separately recorded fees')
            if event['kind'] == 'cancel' and event['amount_fen'] != 0:
                raise ValueError('cancellation is not a cash or position movement')
        elif event['kind'] in ('fee', 'cash_transfer'):
            if event['instrument'] is not None or event['side'] is not None or event['quantity'] != 0:
                raise ValueError('cash event cannot carry a trade quantity')
            if event['order_id'] is not None:
                _name(event['order_id'])
            if event['amount_fen'] == 0:
                raise ValueError('cash event must have a nonzero amount')
        else:
            raise ValueError('unsupported event must be explicitly modeled, never ignored')
    canonical(data)


def _connect(root, *, existing=False):
    db = child(root, 'feedback/ledger.sqlite')
    schema_hash = identity(SCHEMA)
    exists = db.exists()
    if existing:
        con = sqlite3.connect(db.as_uri() + '?mode=ro', uri=True)
        try:
            if con.execute('SELECT sha256 FROM feedback_schema').fetchall() != [(schema_hash,)]:
                raise ValueError('feedback schema differs')
        except BaseException:
            con.close()
            raise
        return con
    db.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db)
    try:
        if exists:
            if con.execute('SELECT sha256 FROM feedback_schema').fetchall() != [(schema_hash,)]:
                raise ValueError('feedback schema differs')
        else:
            con.execute('PRAGMA journal_mode=DELETE')
            con.executescript(SCHEMA)
            con.execute('CREATE TABLE feedback_schema(sha256 TEXT NOT NULL)')
            con.execute('INSERT INTO feedback_schema VALUES(?)', [schema_hash])
            con.commit()
    except BaseException:
        con.close()
        raise
    return con


def _active(con, account):
    rows = con.execute('''SELECT e.payload,e.sha256 FROM event e JOIN
        (SELECT event_id,max(revision) revision FROM event WHERE account_id=? GROUP BY event_id) latest
        ON e.event_id=latest.event_id AND e.revision=latest.revision WHERE e.account_id=?''', [account, account]).fetchall()
    if len(rows) > 100000:
        raise ValueError('account event budget exceeded')
    result = []
    for payload, digest in rows:
        value = json.loads(payload)
        if identity(value) != digest:
            raise ValueError('stored event content is corrupted')
        result.append(value)
    return result


def _reconcile(con, data, statement_id, *, persist=True, events=None):
    events = _active(con, data['account_id']) if events is None else events
    events = [e for e in events if data['period_start'] <= e['trade_date'] <= data['period_end']]
    cash = data['opening']['cash_fen']
    positions = dict(data['opening']['positions'])
    fees = 0
    blockers = []
    if not data['declared_complete']:
        blockers.append('statement_completeness_not_declared')
    if not data['fees_complete']:
        blockers.append('fees_incomplete')
    applied = []
    active = sorted((e for e in events if e['status'] == 'active' and data['period_start'] <= e['trade_date'] <= data['period_end']),
                    key=lambda e: (utc(e['occurred_at']), e['event_id']))
    for event in active:
        applied.append(identity(event))
        qty, amount, code = event['quantity'], event['amount_fen'], event['instrument']
        if event['kind'] == 'fill':
            direction = 1 if event['side'] == 'buy' else -1
            positions[code] = positions.get(code, 0) + direction * qty
            cash -= direction * amount
            if positions[code] < 0:
                blockers.append('negative_inventory_requires_account_rules')
        elif event['kind'] == 'fee':
            cash -= amount
            fees += amount
        elif event['kind'] == 'cash_transfer':
            cash += amount
    differences = {code: positions.get(code, 0) - data['closing']['positions'].get(code, 0)
                   for code in set(positions) | set(data['closing']['positions'])}
    differences = {code: qty for code, qty in differences.items() if qty}
    cash_difference = cash - data['closing']['cash_fen']
    if cash_difference or differences:
        blockers.append('closing_controls_differ')
    # Historical revisions may be included; the highest declared revision is
    # the asserted current state. A void head is still part of that contract.
    declared = {}
    for event in data['events']:
        if event['revision'] > declared.get(event['event_id'], {}).get('revision', 0):
            declared[event['event_id']] = event
    expected_ids = {identity(e) for e in declared.values() if e['status'] == 'active'}
    if set(applied) - expected_ids:
        blockers.append('retained_active_events_omitted_from_statement')
    if expected_ids - set(applied):
        blockers.append('declared_active_events_not_applied')
    current = {e['event_id']: e for e in events}
    if any(current.get(eid) != event for eid, event in declared.items()):
        blockers.append('declared_event_revision_not_current')
    if set(current) - set(declared):
        blockers.append('retained_event_heads_omitted_from_statement')
    receipt = seal({'schema': 'isolated_feedback_reconciliation_v2', 'statement_id': statement_id,
                    'account_id': data['account_id'], 'source_kind': data['source_kind'],
                    'original_sha256': data['original_sha256'], 'applied_event_hashes': applied,
                    'event_version_hashes': sorted(identity(e) for e in events),
                    'computed_cash_fen': cash, 'computed_positions': positions, 'fees_fen': fees,
                    'cash_difference_fen': cash_difference, 'position_differences': differences,
                    'blockers': sorted(set(blockers)), 'arithmetic_reconciled': not blockers,
                    'source_authenticity_verified': False, 'real_statement_acceptance': False,
                    'execution_ready': False})
    if persist:
        con.execute('INSERT OR IGNORE INTO reconciliation VALUES(?,?)', [receipt['receipt_id'], canonical(receipt)])
    return receipt


def import_statement(root, normalized, original):
    root = workspace(root)
    data = read_json(child(root, normalized))
    validate(data)
    raw_path = child(root, original)
    if raw_path.stat().st_size > 16_000_000:
        raise ValueError('bounded original statement required')
    raw = raw_path.read_bytes()
    raw_hash = hashlib.sha256(raw).hexdigest()
    if raw_hash != data['original_sha256']:
        raise ValueError('original statement checksum mismatch')
    statement_id = identity(data)
    with FileLock(root / 'capabilities.guard'):
        con = _connect(root)
        try:
            with con:
                con.execute('BEGIN IMMEDIATE')
                _inspect_connection(con)
                previous = con.execute('SELECT source_kind FROM account WHERE account_id=?', [data['account_id']]).fetchone()
                if previous and previous[0] != data['source_kind']:
                    raise ValueError('synthetic and broker accounts cannot share an identity')
                con.execute('INSERT OR IGNORE INTO account VALUES(?,?)', [data['account_id'], data['source_kind']])
                stored = con.execute('SELECT content FROM original WHERE sha256=?', [raw_hash]).fetchone()
                if stored and stored[0] != raw:
                    raise ValueError('stored original is corrupted')
                con.execute('INSERT OR IGNORE INTO original VALUES(?,?)', [raw_hash, raw])
                con.execute('INSERT OR IGNORE INTO statement VALUES(?,?,?,?,?)',
                            [statement_id, data['account_id'], raw_hash, canonical(data), now_utc().isoformat()])
                for event in sorted(data['events'], key=lambda e: (e['event_id'], e['revision'])):
                    key = [data['account_id'], event['event_id'], event['revision']]
                    digest = identity(event)
                    found = con.execute('SELECT sha256,payload FROM event WHERE account_id=? AND event_id=? AND revision=?', key).fetchone()
                    if found:
                        if found != (digest, canonical(event)):
                            raise ValueError('event idempotency conflict')
                        continue
                    latest = con.execute('SELECT revision,sha256,payload FROM event WHERE account_id=? AND event_id=? ORDER BY revision DESC LIMIT 1', key[:2]).fetchone()
                    if latest:
                        old = json.loads(latest[2])
                        if event['revision'] != latest[0] + 1 or event['supersedes_sha256'] != latest[1]:
                            raise ValueError('revision gap or stale predecessor')
                        if event['trade_date'] != old['trade_date']:
                            raise ValueError('cross-period correction requires an explicit multi-period contract')
                    elif event['revision'] != 1:
                        raise ValueError('missing initial event revision')
                    con.execute('INSERT INTO event VALUES(?,?,?,?,?,?)', [*key, canonical(event), digest, statement_id])
                result = _reconcile(con, data, statement_id)
        finally:
            con.close()
    return result


def confirm(root, receipt_id, operator, original_sha256):
    """An explicit human acknowledgement only; never an authenticity upgrade."""
    root = workspace(root)
    _name(operator)
    with FileLock(root / 'capabilities.guard'):
        # Do not create an empty ledger when an unknown confirmation is requested.
        db = child(root, 'feedback/ledger.sqlite')
        if not db.is_file():
            raise ValueError('existing feedback ledger required')
        con = _connect(root)
        try:
            with con:
                con.execute('BEGIN IMMEDIATE')
                inspection = _inspect_connection(con)
                row = con.execute('SELECT payload FROM reconciliation WHERE receipt_id=?', [receipt_id]).fetchone()
                if not row:
                    raise ValueError('unknown reconciliation')
                value = checked(json.loads(row[0]))
                if not value['arithmetic_reconciled'] or value['original_sha256'] != original_sha256:
                    raise ValueError('matching reconciled statement required')
                state = next(r for r in inspection['records'] if r['receipt_id'] == receipt_id)
                if state['superseded'] or state['requires_revalidation']:
                    raise ValueError('reconciliation superseded; confirm the revised result')
                if not state['arithmetic_reconciled']:
                    raise ValueError('current reconciled statement required')
                old = con.execute('SELECT operator,original_sha256 FROM confirmation WHERE receipt_id=?', [receipt_id]).fetchone()
                if old and old != (operator, original_sha256):
                    raise ValueError('confirmation identity conflict')
                con.execute('INSERT OR IGNORE INTO confirmation VALUES(?,?,?,?)',
                            [receipt_id, operator, original_sha256, now_utc().isoformat()])
        finally:
            con.close()
    return {'receipt_id': receipt_id, 'human_acknowledged': True,
            'real_statement_acceptance': False, 'execution_ready': False}


def _inspect_connection(con):
    """One snapshot/transaction for reads, import admission, confirmation and restore.

    This detects missing referenced records, not deletion of an entire mutually
    consistent history. An independently held recovery manifest is still needed.
    """
    if con.execute('SELECT sha256 FROM feedback_schema').fetchall() != [(identity(SCHEMA),)]:
        raise ValueError('feedback schema differs')
    if con.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
        raise ValueError('feedback database corrupted')
    counts = {table: con.execute('SELECT count(*) FROM ' + table).fetchone()[0]
              for table in ('account', 'original', 'statement', 'event', 'reconciliation', 'confirmation')}
    if any(n > 100000 for n in counts.values()):
        raise ValueError('ledger inspection budget exceeded')
    accounts = dict(con.execute('SELECT account_id,source_kind FROM account'))
    for account, source in accounts.items():
        _name(account)
        if source not in ('synthetic', 'broker_export'):
            raise ValueError('account source corrupted')
    originals = set()
    for digest, raw in con.execute('SELECT sha256,content FROM original'):
        if hashlib.sha256(raw).hexdigest() != digest:
            raise ValueError('original evidence corrupted')
        originals.add(digest)
    statements, expected, declared, latest_statement = {}, {}, {}, {}
    work = 0
    for sid, account, raw_hash, payload, received in con.execute('SELECT * FROM statement ORDER BY rowid'):
        data = json.loads(payload)
        validate(data)
        utc(received)
        if identity(data) != sid or data['account_id'] != account or data['original_sha256'] != raw_hash:
            raise ValueError('statement identity corrupted')
        if accounts.get(account) != data['source_kind'] or raw_hash not in originals:
            raise ValueError('statement account or original missing or corrupted')
        statements[sid] = data
        latest_statement[(account, data['period_start'], data['period_end'])] = sid
        declared[sid] = set()
        work += len(data['events'])
        if work > 1000000:
            raise ValueError('ledger reference inspection budget exceeded')
        for event in data['events']:
            key = (account, event['event_id'], event['revision'])
            digest = identity(event)
            if key in expected and expected[key] != digest:
                raise ValueError('statement event identity conflict')
            expected[key] = digest
            declared[sid].add(digest)
    if originals != {d['original_sha256'] for d in statements.values()} or set(accounts) != {d['account_id'] for d in statements.values()}:
        raise ValueError('orphan original or account; statement history missing')
    present, versions, heads = {}, {}, {}
    for account, eid, revision, payload, digest, sid in con.execute('SELECT * FROM event ORDER BY account_id,event_id,revision'):
        value = json.loads(payload)
        previous = heads.get((account, eid))
        if (identity(value) != digest or value['event_id'] != eid or value['revision'] != revision
                or revision != (previous['revision'] + 1 if previous else 1)
                or value['supersedes_sha256'] != (identity(previous) if previous else None)
                or (previous and value['trade_date'] != previous['trade_date'])
                or sid not in statements or statements[sid]['account_id'] != account
                or digest not in declared[sid]):
            raise ValueError('event revision chain corrupted')
        present[(account, eid, revision)] = digest
        versions[(account, digest)] = value
        heads[(account, eid)] = value
    if present != expected:
        raise ValueError('statement event revision missing or corrupted')
    by_account = {}
    for (account, _), event in heads.items():
        by_account.setdefault(account, []).append(event)
    receipts, states = {}, {}
    for rid, payload in con.execute('SELECT receipt_id,payload FROM reconciliation ORDER BY receipt_id'):
        receipt = checked(json.loads(payload))
        statement = statements.get(receipt['statement_id'])
        if (rid != receipt['receipt_id'] or not statement
                or any(receipt[k] != statement[k] for k in ('account_id', 'source_kind', 'original_sha256'))
                or any(receipt[k] is not False for k in ('source_authenticity_verified', 'real_statement_acceptance', 'execution_ready'))):
            raise ValueError('reconciliation identity corrupted')
        legacy = receipt['schema'] == 'isolated_feedback_reconciliation_v1'
        if not legacy and receipt['schema'] != 'isolated_feedback_reconciliation_v2':
            raise ValueError('unknown reconciliation contract')
        account = receipt['account_id']
        current_events = [e for e in by_account.get(account, []) if statement['period_start'] <= e['trade_date'] <= statement['period_end']]
        hashes = receipt['applied_event_hashes'] if legacy else receipt['event_version_hashes']
        work += len(hashes) + len(current_events) + len(statement['events'])
        if work > 1000000:
            raise ValueError('ledger reference inspection budget exceeded')
        if not isinstance(hashes, list) or len(hashes) != len(set(hashes)):
            raise ValueError('reconciliation event identities corrupted')
        replay = []
        for digest in hashes:
            event = versions.get((account, digest))
            if not event or not statement['period_start'] <= event['trade_date'] <= statement['period_end']:
                raise ValueError('reconciliation event missing or outside period')
            replay.append(event)
        if len({e['event_id'] for e in replay}) != len(replay):
            raise ValueError('multiple reconciliation heads for one event')
        if not legacy and _reconcile(con, statement, receipt['statement_id'], persist=False, events=replay) != receipt:
            raise ValueError('reconciliation does not match recorded ledger arithmetic')
        superseded = (legacy or sorted(identity(e) for e in current_events) != sorted(hashes)
                      or latest_statement[(account, statement['period_start'], statement['period_end'])] != receipt['statement_id'])
        receipts[rid] = receipt
        states[rid] = {'receipt_id': rid, 'account_id': account, 'source_kind': receipt['source_kind'],
                       'superseded': superseded, 'requires_revalidation': legacy,
                       'arithmetic_reconciled': receipt['arithmetic_reconciled'] and not superseded,
                       'human_acknowledged': False, 'cash_difference_fen': receipt['cash_difference_fen'],
                       'position_differences': receipt['position_differences'], 'blockers': receipt['blockers']}
    if set(statements) != {r['statement_id'] for r in receipts.values()}:
        raise ValueError('statement reconciliation missing')
    for rid, operator, digest, confirmed in con.execute('SELECT * FROM confirmation'):
        receipt = receipts.get(rid)
        if not receipt or digest != receipt['original_sha256'] or not receipt['arithmetic_reconciled']:
            raise ValueError('confirmation evidence mismatch')
        _name(operator)
        utc(confirmed)
        states[rid]['human_acknowledged'] = not states[rid]['superseded']
    return {'schema': 'isolated_feedback_inspection_v1', 'counts': counts, 'records': list(states.values()),
            'integrity_verified': True, 'real_statement_acceptance': False, 'execution_ready': False}


def inspect(root):
    """Read-only integrity and current-revision view, including stale acknowledgements."""
    root = workspace(root)
    con = _connect(root, existing=True)
    try:
        con.execute('BEGIN')
        return _inspect_connection(con)
    finally:
        con.close()
