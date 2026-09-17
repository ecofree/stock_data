"""Rebuildable SQLite lookup over immutable human events, never event authority.

Only explicit maintenance and existing judgement writers update this cache.
Reads use indexed lookups and verify selected event files. A directory change
after an interrupted append invalidates the cache; the next writer reconciles
it before retrying. Legacy workspaces without a cache remain read-only readable.
"""
from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3

from .domain import identity, utc
from .gap_evidence import read_json

FOLDERS = {'note': ('notes', 'notes/attention'),
           'review': ('notes/reviews', 'notes/attention/reviews'),
           'plan': ('notes/plans',)}
SCHEMA = 1


def cache_path(output):
    return Path(output).resolve()/'notes/.index/events.sqlite'


def signatures(output):
    result = {}
    for folders in FOLDERS.values():
        for folder in folders:
            path = Path(output)/folder
            result[folder] = [path.stat().st_mtime_ns, path.stat().st_ino] if path.exists() else None
    return json.dumps(result, sort_keys=True)


def paths(output, kind):
    for folder in FOLDERS[kind]:
        yield from (Path(output)/folder).glob('*.json')


def event(path, kind):
    value = read_json(path)[0]
    key = kind+'_id'
    if value.get(key) != path.stem or path.stem != identity({k:v for k,v in value.items() if k != key}):
        raise ValueError('journal event identity changed')
    return value


def create_schema(con):
    con.executescript('''
        CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS events(
          kind TEXT NOT NULL, id TEXT NOT NULL, path TEXT NOT NULL,
          received TEXT NOT NULL, request TEXT, command TEXT, parent TEXT,
          note TEXT, conclusion TEXT, PRIMARY KEY(kind,id));
        CREATE UNIQUE INDEX IF NOT EXISTS by_request ON events(kind,request);
        CREATE INDEX IF NOT EXISTS by_recent ON events(kind,received DESC,id DESC);
        CREATE INDEX IF NOT EXISTS by_parent ON events(kind,parent);
        CREATE INDEX IF NOT EXISTS by_review ON events(kind,note,conclusion);
    ''')


def insert(con, output, kind, path, value):
    con.execute('INSERT INTO events VALUES(?,?,?,?,?,?,?,?,?)',
        (kind, value[kind+'_id'], path.relative_to(output).as_posix(),
         utc(value['received_at']).isoformat(), value.get('request_id'), value.get('command_id'),
         value.get('supersedes') or None, value.get('note_id'), value.get('conclusion')))


def valid(con, output):
    meta = dict(con.execute('SELECT key,value FROM meta'))
    return meta.get('schema') == str(SCHEMA) and meta.get('signature') == signatures(output)


def stamp(con, output):
    con.executemany('INSERT OR REPLACE INTO meta VALUES(?,?)',
                    [('schema', str(SCHEMA)), ('signature', signatures(output))])


def rebuild(output):
    """Caller holds judgement.guard. Full validation is explicit, not a GET side effect."""
    output = Path(output)
    destination = cache_path(output)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(destination) as con:
        create_schema(con)
        con.execute('BEGIN IMMEDIATE')
        before = signatures(output)
        con.execute('DELETE FROM events')
        for kind in FOLDERS:
            for path in paths(output, kind):
                insert(con, output, kind, path, event(path, kind))
        if before != signatures(output):
            raise RuntimeError('journal changed during rebuild; retry under judgement guard')
        stamp(con, output)
        counts = dict(con.execute('SELECT kind,count(*) FROM events GROUP BY kind'))
        con.executemany('INSERT OR REPLACE INTO meta VALUES(?,?)',
                        [('count_'+kind, str(counts.get(kind, 0))) for kind in FOLDERS])
    return {'schema': SCHEMA, 'counts': counts, 'authority': 'immutable_event_files', 'business_events_created': 0}


def ensure(output):
    """Existing writers call this before exposing an event, under the same guard."""
    path = cache_path(output)
    if path.exists():
        with sqlite3.connect(path.as_uri()+'?mode=ro', uri=True) as con:
            if valid(con, output):
                return
    rebuild(output)


def appended(output, kind, path):
    # Event rename/fsync precedes this transaction. A crash before commit leaves
    # mismatched directory stamps, so a retry rebuilds from the saved event.
    with sqlite3.connect(cache_path(output)) as con:
        insert(con, Path(output), kind, path, event(path, kind))
        con.execute("UPDATE meta SET value=CAST(value AS INTEGER)+1 WHERE key=?", ('count_'+kind,))
        stamp(con, output)


@contextmanager
def reader(output):
    path = cache_path(output)
    if not path.exists():
        # Compatibility for pre-index releases. No cache writes from a read.
        con = sqlite3.connect(':memory:')
        create_schema(con)
        for kind in FOLDERS:
            for item in paths(output, kind):
                insert(con, Path(output), kind, item, event(item, kind))
            con.execute('INSERT INTO meta VALUES(?,?)', ('count_'+kind, str(con.execute('SELECT count(*) FROM events WHERE kind=?', (kind,)).fetchone()[0])))
    else:
        con = sqlite3.connect(path.as_uri()+'?mode=ro', uri=True)
        try:
            con.execute('BEGIN')
            if not valid(con, output):
                raise RuntimeError('Journal index stale; run journal-index or retry the same saved request')
        except BaseException:
            con.close()
            raise
    try:
        yield con
    finally:
        con.close()


def recent(output, kind, limit=100):
    if not 1 <= limit <= 100:
        raise ValueError('bounded journal page required')
    with reader(output) as con:
        rows = list(con.execute('SELECT path FROM events WHERE kind=? ORDER BY received DESC,id DESC LIMIT ?', (kind, limit)))
        count = int(con.execute('SELECT value FROM meta WHERE key=?', ('count_'+kind,)).fetchone()[0])
    return [Path(output)/row[0] for row in rows], count


def history(output, kind='note', before=None, limit=100):
    """Keyset pagination: old receipts remain accessible without OFFSET scans."""
    if kind not in FOLDERS or not 1 <= limit <= 100:
        raise ValueError('valid bounded journal history required')
    with reader(output) as con:
        args=[kind]; clause=''
        if before:
            anchor=con.execute('SELECT received,id FROM events WHERE kind=? AND id=?', (kind,before)).fetchone()
            if not anchor:raise ValueError('unknown journal history cursor')
            clause=' AND (received,id)<(?,?)';args.extend(anchor)
        rows=con.execute('SELECT path,id FROM events WHERE kind=?'+clause+' ORDER BY received DESC,id DESC LIMIT ?', (*args,limit+1)).fetchall()
    return {'kind':kind,'events':[event(Path(output)/r[0],kind) for r in rows[:limit]],
            'next_before':rows[limit-1][1] if len(rows)>limit else None,'limit':limit}


def lookup(output, kind, column, value):
    if column not in ('request', 'parent'):
        raise ValueError('unsupported journal lookup')
    with reader(output) as con:
        rows = con.execute(f'SELECT path FROM events WHERE kind=? AND {column}=? LIMIT 2', (kind, value)).fetchall()
    if len(rows) > 1:
        raise ValueError('ambiguous journal request or revision')
    return event(Path(output)/rows[0][0], kind) if rows else None


def invalidations(output, plans):
    """Use only indexed references relevant to the displayed/confirmed plans."""
    result = {'plans': set(), 'notes': set(), 'invalid_notes': set()}
    with reader(output) as con:
        for plan in plans:
            for kind, key, field in (('plan', 'plans', 'plan_id'), ('note', 'notes', 'note_id')):
                rows = con.execute('SELECT path FROM events WHERE kind=? AND parent=? LIMIT 2', (kind, plan[field])).fetchall()
                for row in rows:
                    if event(Path(output)/row[0], kind).get('supersedes') != plan[field]:
                        raise ValueError('journal index reference changed')
                    result[key].add(plan[field])
            row = con.execute("SELECT path FROM events WHERE kind='review' AND note=? AND conclusion='triggered' LIMIT 1", (plan['note_id'],)).fetchone()
            if row:
                from .research_journal import read_review
                review = read_review(output, Path(row[0]).stem)
                if review['note_id'] != plan['note_id'] or review['conclusion'] != 'triggered':
                    raise ValueError('journal index review changed')
                result['invalid_notes'].add(plan['note_id'])
    return result
