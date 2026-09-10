"""One owning thread/connection, append-only facts and frozen PIT inputs."""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import threading
import time
import uuid

import duckdb

from trade_system.file_lock import FileLock
from .domain import canonical, identity, instrument, now_utc, number, utc


SCHEMA = """
CREATE TABLE v2_schema(version INTEGER PRIMARY KEY, sha256 VARCHAR NOT NULL);
CREATE TABLE data_product(dataset VARCHAR PRIMARY KEY, unit VARCHAR NOT NULL,
 semantics VARCHAR NOT NULL, consumer VARCHAR NOT NULL, origin VARCHAR NOT NULL);
CREATE TABLE fact(fact_id VARCHAR PRIMARY KEY, dataset VARCHAR NOT NULL, instrument VARCHAR NOT NULL,
 event_time TIMESTAMPTZ NOT NULL, received_at TIMESTAMPTZ NOT NULL, known_at TIMESTAMPTZ NOT NULL,
 seq BIGINT UNIQUE NOT NULL, value DOUBLE NOT NULL, revision VARCHAR NOT NULL,
 raw_hash VARCHAR NOT NULL, effective_at TIMESTAMPTZ);
CREATE TABLE input_manifest(manifest_id VARCHAR PRIMARY KEY, asof_time TIMESTAMPTZ NOT NULL,
 mode VARCHAR NOT NULL, fact_ids JSON NOT NULL);
CREATE TABLE account_snapshot(snapshot_id VARCHAR PRIMARY KEY, account_id VARCHAR NOT NULL,
 mode VARCHAR NOT NULL, asof_time TIMESTAMPTZ NOT NULL, valid_until TIMESTAMPTZ NOT NULL,
 cash_fen BIGINT NOT NULL, frozen_fen BIGINT NOT NULL, equity_fen BIGINT NOT NULL,
 reconciled BOOLEAN NOT NULL, source_hash VARCHAR NOT NULL, payload JSON NOT NULL,
 imported_at TIMESTAMPTZ NOT NULL, seq BIGINT UNIQUE NOT NULL);
CREATE TABLE account_event(event_id VARCHAR PRIMARY KEY, account_id VARCHAR NOT NULL,
 happened_at TIMESTAMPTZ NOT NULL, kind VARCHAR NOT NULL, payload JSON NOT NULL);
CREATE TABLE signal_event(signal_id VARCHAR PRIMARY KEY, instrument VARCHAR NOT NULL,
 strategy_version VARCHAR NOT NULL, state VARCHAR NOT NULL, asof_time TIMESTAMPTZ NOT NULL,
 manifest_id VARCHAR NOT NULL, payload JSON NOT NULL);
CREATE TABLE decision_certificate(decision_id VARCHAR PRIMARY KEY, account_id VARCHAR NOT NULL,
 snapshot_id VARCHAR NOT NULL, signal_id VARCHAR NOT NULL, expires_at TIMESTAMPTZ NOT NULL,
 payload JSON NOT NULL);
CREATE TABLE reservation(reservation_id VARCHAR PRIMARY KEY, decision_id VARCHAR UNIQUE NOT NULL,
 account_id VARCHAR NOT NULL, instrument VARCHAR NOT NULL, amount_fen BIGINT NOT NULL,
 status VARCHAR NOT NULL);
CREATE TABLE operator_action(action_id VARCHAR PRIMARY KEY, account_id VARCHAR NOT NULL,
 happened_at TIMESTAMPTZ NOT NULL, payload JSON NOT NULL);
"""


class Store:
    def __init__(self, path, *, clock=now_utc):
        self.path = Path(path).resolve()
        self.clock = clock
        self.con = None
        self.owner = None
        self.command_deadline = None
        self.guard = FileLock(self.path.with_suffix(self.path.suffix + '.owner.guard'))

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.guard.__enter__()
        try:
            existing = self.path.exists()
            if existing:
                with duckdb.connect(str(self.path), read_only=True) as check:
                    if not check.execute("SELECT count(*) FROM information_schema.tables WHERE table_name='v2_schema'").fetchone()[0]:
                        raise ValueError('not a V2 database; legacy databases must not be opened for V2 writes')
            self.con = duckdb.connect(str(self.path))
            self.owner = threading.get_ident()
            schema_hash = hashlib.sha256(SCHEMA.encode()).hexdigest()
            if not existing:
                with self.transaction():
                    self.con.execute(SCHEMA)
                    self.con.execute('INSERT INTO v2_schema VALUES (1, ?)', [schema_hash])
            elif self.con.execute('SELECT sha256 FROM v2_schema WHERE version=1').fetchone() != (schema_hash,):
                raise RuntimeError('V2 schema checksum mismatch; explicit migration required')
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def __exit__(self, *_):
        if self.con is not None:
            self.con.close()
            self.con = None
        self.owner = None
        self.guard.__exit__(None, None, None)

    def check_owner(self):
        if self.con is None or self.owner != threading.get_ident():
            raise RuntimeError('only the owning service thread may access the live connection')
        self.check_deadline()

    def check_deadline(self):
        if self.command_deadline is not None and time.monotonic()>=self.command_deadline:
            raise TimeoutError('command deadline exceeded at safe boundary; inspect durable state before retry')

    @contextmanager
    def transaction(self):
        self.check_owner()
        self.con.execute('BEGIN TRANSACTION')
        try:
            yield
            self.check_deadline()
            self.con.execute('COMMIT')
        except BaseException:
            self.con.execute('ROLLBACK')
            raise

    def archive(self, payload: bytes):
        self.check_owner()
        digest = hashlib.sha256(payload).hexdigest()
        root = self.path.parent / (self.path.name + '.raw')
        root.mkdir(exist_ok=True)
        target = root / digest
        if target.exists():
            if hashlib.sha256(target.read_bytes()).hexdigest() != digest:
                raise RuntimeError('raw archive corrupted')
            return digest
        temp = root / ('.pending-' + uuid.uuid4().hex)
        with temp.open('xb') as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        temp.replace(target)
        return digest

    def register_product(self, dataset, unit, semantics, consumer, origin):
        self.check_owner()
        if not all(isinstance(v, str) and v.strip() for v in (dataset, unit, consumer, origin)):
            raise ValueError('unit, consumer and origin are mandatory')
        if semantics not in ('cumulative', 'increment', 'point', 'announced_event'):
            raise ValueError('unknown semantics')
        values = (dataset, unit, semantics, consumer, origin)
        previous = self.con.execute('SELECT * FROM data_product WHERE dataset=?', [dataset]).fetchone()
        if previous and previous != values:
            raise ValueError('product definition immutable; register a new version')
        if not previous:
            self.con.execute('INSERT INTO data_product VALUES (?,?,?,?,?)', values)

    def ingest(self, dataset, code, event_time, value, revision, raw, *, effective_at=None):
        self.check_owner()
        instrument(code)
        product = self.con.execute('SELECT * FROM data_product WHERE dataset=?', [dataset]).fetchone()
        if product is None:
            raise ValueError('unregistered data product')
        event, received = utc(event_time), utc(self.clock())
        if event > received:
            raise ValueError('event/publication time cannot be in the future')
        if not isinstance(revision, str) or not revision:
            raise ValueError('source revision required')
        value = float(number(value))
        canonical(value)  # reject Decimal overflow when converted to DOUBLE
        if effective_at is not None and product[2] != 'announced_event':
            raise ValueError('effective_at is only for announced events')
        effective = utc(effective_at) if effective_at is not None else None
        if not isinstance(raw, bytes):
            raise ValueError('raw response must be bytes')
        raw_hash = hashlib.sha256(raw).hexdigest()
        # Source revision is the idempotency boundary, not the numeric value.
        # Validate the entire semantic content before deduplication or raw I/O.
        previous = self.con.execute('''SELECT fact_id,value,raw_hash,effective_at FROM fact
            WHERE dataset=? AND instrument=? AND event_time=? AND revision=?''',
            [dataset,code,event,revision]).fetchall()
        if previous:
            if len(previous) != 1 or previous[0][1:] != (value,raw_hash,effective):
                raise ValueError('source revision content conflict; a new explicit revision is required')
            return {'fact_id': previous[0][0], 'inserted': 0, 'deduplicated': 1}
        key = identity([dataset,code,event.isoformat(),value,revision,
                        effective.isoformat() if effective else None,raw_hash])
        self.archive(raw)
        with self.transaction():
            seq = self.con.execute('SELECT coalesce(max(seq),0)+1 FROM fact').fetchone()[0]
            self.con.execute('INSERT INTO fact VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                             [key, dataset, code, event, received, utc(self.clock()), seq, value,
                              revision, raw_hash, effective])
        return {'fact_id': key, 'inserted': 1, 'deduplicated': 0}

    def freeze(self, asof, *, mode='system_replay', datasets=None, codes=None, since=None):
        self.check_owner()
        asof = utc(asof)
        if mode not in ('system_replay', 'historical_research') or asof > utc(self.clock()):
            raise ValueError('invalid query context')
        filters, params = '', [asof,asof,asof]
        for name,values in (('dataset',datasets),('instrument',codes)):
            if values is not None:
                if not isinstance(values,(list,tuple)) or not 0<len(values)<=1000 or not all(isinstance(v,str) and v for v in values):
                    raise ValueError('bounded nonempty snapshot scope required')
                filters += f' AND {name} IN (SELECT unnest(?))'
                params.append(list(values))
        if since is not None:
            since = utc(since)
            if since > asof:
                raise ValueError('snapshot window starts after cutoff')
            filters += ' AND event_time>=?'
            params.append(since)
        # Historical imports are known only at import time. Backdating a
        # mode never makes them available to actual system replay.
        ids = [r[0] for r in self.con.execute('''SELECT fact_id FROM fact
            WHERE event_time<=? AND received_at<=? AND known_at<=?
            ''' + filters + '''
            QUALIFY row_number() OVER (PARTITION BY dataset,instrument,event_time ORDER BY seq DESC)=1
            ORDER BY seq LIMIT 100001''', params).fetchall()]
        if len(ids)>100000:
            raise ValueError('snapshot exceeds 100000 facts; supply dataset/instrument/time scope')
        key = identity([asof.isoformat(), mode, ids])
        self.con.execute('INSERT INTO input_manifest VALUES (?,?,?,?) ON CONFLICT DO NOTHING',
                         [key, asof, mode, canonical(ids)])
        return key

    def facts(self, manifest_id, dataset, code):
        self.check_owner()
        row = self.con.execute('SELECT fact_ids FROM input_manifest WHERE manifest_id=?', [manifest_id]).fetchone()
        if row is None:
            raise ValueError('frozen manifest required; unbounded latest reads are forbidden')
        ids = json.loads(row[0])
        return self.con.execute('''SELECT fact_id,event_time,received_at,known_at,value FROM fact
            WHERE fact_id IN (SELECT unnest(?)) AND dataset=? AND instrument=?
            ORDER BY event_time,seq''', [ids, dataset, code]).fetchall()

    def metric(self, manifest_id, dataset, code):
        rows = self.facts(manifest_id, dataset, code)
        if not rows:
            return None
        semantics = self.con.execute('SELECT semantics FROM data_product WHERE dataset=?', [dataset]).fetchone()[0]
        return sum(r[4] for r in rows) if semantics == 'increment' else rows[-1][4]
