"""Complete declared offline rehearsal bundles, with verified isolated restores.

Only paths inside a new rehearsal workspace are admitted. No remote transport,
credential loading, scheduler registration, source cleanup or startup occurs.
"""
from contextlib import ExitStack, closing
import re
import shutil
import sqlite3
import time

import duckdb

from trade_system.file_lock import FileLock
from .domain import file_hash, identity, now_utc, utc
from .isolated_workspace import child, workspace, safe_path, read_json, write_json, seal, checked
from .publisher import read_current, read_bundle

COMPONENTS = {'code', 'environment', 'workspace', 'raw_facts', 'databases'}
EXCLUDED = {'.git', '__pycache__', '.index'}


def inventory(folder):
    result = {}
    for path in folder.rglob('*'):
        safe_path(path)
        relative = path.relative_to(folder)
        if any(part in EXCLUDED for part in relative.parts) or path.suffix == '.guard':
            continue
        if not path.is_file():
            continue
        if (path.name.startswith('.env') and path.name != '.env.example') or path.suffix.lower() in ('.pem', '.key', '.pfx'):
            raise ValueError('credential file must be held separately, not bundled')
        if path.name.lower().endswith(('.wal', '-wal', '-journal')):
            raise ValueError('unclean database sidecar; stop the owner normally first')
        result[relative.as_posix()] = {'sha256': file_hash(path), 'bytes': path.stat().st_size}
        if len(result) > 100000:
            raise ValueError('bundle file budget exceeded')
    return result


def database_controls(path):
    if path.suffix.lower() == '.sqlite':
        with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as con:
            con.execute('BEGIN')
            if con.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
                raise ValueError('SQLite integrity check failed')
            tables = con.execute("SELECT name,sql FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()
            counts = {name: con.execute('SELECT count(*) FROM "' + name.replace('"', '""') + '"').fetchone()[0] for name, _ in tables}
            if 'feedback_schema' in counts:
                from .feedback_ledger import _inspect_connection
                _inspect_connection(con)
            return {'tables': tables, 'counts': counts}
    with duckdb.connect(str(path), read_only=True, config={'enable_external_access': False}) as con:
        rows = con.execute('SELECT schema_name,table_name,sql FROM duckdb_tables() WHERE NOT internal ORDER BY 1,2').fetchall()
        counts = {}
        for schema, name, _ in rows:
            quote = lambda s: '"' + s.replace('"', '""') + '"'
            counts[schema + '.' + name] = con.execute('SELECT count(*) FROM ' + quote(schema) + '.' + quote(name)).fetchone()[0]
        return {'tables': rows, 'counts': counts}


def validate_workspace(folder):
    from .acceptance_scope import for_workspace
    for_workspace(folder)  # Unknown/conflicting declared scope cannot be sealed.
    publication = folder / 'publication'
    manifest, _ = read_current(publication)
    for pointer in manifest.get('published_days', {}).values():
        read_bundle(publication, pointer)
    # Reuse business readers without rebuilding the non-authoritative index.
    from .journal_index import event
    from .research_journal import read_note, read_review, find_prediction
    from .operator_workflow import read_observation_plan
    readers = {'note': read_note, 'review': read_review, 'plan': read_observation_plan}
    records = {}
    def reference(value):
        if not isinstance(value, str) or not re.fullmatch('[a-f0-9]{64}', value):
            raise ValueError('human reference identity required')
        return value
    for kind, directory in (('note', 'notes'), ('note', 'notes/attention'),
                            ('review', 'notes/reviews'), ('review', 'notes/attention/reviews'),
                            ('plan', 'notes/plans')):
        for path in (folder / directory).glob('*.json'):
            value = event(path, kind)
            if (kind, path.stem) in records:
                raise ValueError('ambiguous duplicate human record')
            if len(records) >= 100000:
                raise ValueError('human reference inspection budget exceeded')
            for key in ('supersedes', 'note_id' if kind != 'note' else 'prediction_id'):
                if value.get(key):
                    reference(value[key])
            if kind == 'note':
                if not {'instrument', 'operator', 'invalidation', 'received_at', 'prediction_id'} <= set(value):
                    raise ValueError('human note contract incomplete')
                if value.get('schema') == 2:
                    reference(value.get('evidence_file_id'))
                    reference(value.get('evidence_id'))
                else:
                    reference(value.get('prediction_id'))
                    prediction = find_prediction(folder, value['prediction_id'])
                    if value.get('model_id') != prediction.get('model_id'):
                        raise ValueError('human prediction model differs')
            utc(value['received_at'])
            records[(kind, path.stem)] = value
    for (kind, key), value in records.items():
        if readers[kind](folder, key) != value:
            raise ValueError('human reader identity differs')
        if value.get('supersedes'):
            parent = records.get((kind, value['supersedes']))
            if not parent:
                raise ValueError('human revision parent missing')
            fields = ('note_id', 'operator') if kind == 'plan' else ('instrument', 'operator', 'prediction_id', 'evidence_id')
            if any(parent.get(k) != value.get(k) for k in fields) or utc(parent['received_at']) > utc(value['received_at']):
                raise ValueError('human revision parent contract differs')
    # Iterative walk: do not recurse on user-provided history depth.
    complete = set()
    for start in records:
        trail, key = set(), start
        while key not in complete:
            if key in trail:
                raise ValueError('cyclic human revision history')
            trail.add(key)
            parent = records[key].get('supersedes')
            if not parent:
                break
            key = (key[0], parent)
        complete.update(trail)
    return {'current_generation': manifest['generation'], 'run_id': manifest['run_id'],
            'history_pointers': len(manifest.get('published_days', {})),
            'configuration_rebinding_required_before_any_activation': True}


def capture(root, plan, destination):
    root = workspace(root)
    if set(plan) != {'components', 'credential_custody_reference'} or set(plan['components']) != COMPONENTS:
        raise ValueError('explicit code/environment/workspace/raw_facts/databases scope required')
    if not isinstance(plan['credential_custody_reference'], str) or not 1 <= len(plan['credential_custody_reference']) <= 200:
        raise ValueError('credential custody reference required; never put a credential here')
    sources = {k: child(root, v) for k, v in plan['components'].items()}
    output = child(root, destination)
    if output.exists() or any(not p.is_dir() or output.is_relative_to(p) or p.is_relative_to(output) for p in sources.values()):
        raise ValueError('existing component folders and separate new bundle required')
    if any(a.is_relative_to(b) or b.is_relative_to(a) for i, a in enumerate(sources.values()) for b in list(sources.values())[i+1:]):
        raise ValueError('component roots must be disjoint')
    with FileLock(root / 'capabilities.guard'), ExitStack() as stack:
        for name in ('update.guard', 'judgement.guard', 'publication.guard', 'publication/publish.guard'):
            path = sources['workspace'] / name
            path.parent.mkdir(parents=True, exist_ok=True)
            stack.enter_context(FileLock(path))
        before = {k: inventory(p) for k, p in sources.items()}
        if any(not files for files in before.values()):
            raise ValueError('empty component requires explicit evidence preparation')
        controls = {}
        for component, files in before.items():
            for name in files:
                if name.lower().endswith(('.duckdb', '.sqlite')):
                    path = child(sources[component], name)
                    if path.suffix.lower() == '.duckdb':
                        stack.enter_context(duckdb.connect(str(path), read_only=True, config={'enable_external_access': False}))
                    else:
                        connection = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)
                        stack.callback(connection.close)
                        connection.execute('BEGIN')
                        connection.execute('SELECT count(*) FROM sqlite_master').fetchone()
                    controls[component + '/' + name] = database_controls(path)
        status = validate_workspace(sources['workspace'])
        size = sum(record['bytes'] for files in before.values() for record in files.values())
        if shutil.disk_usage(root).free < size * 1.2:
            raise ValueError('insufficient bundle capacity')
        output.mkdir(parents=True, exist_ok=False)
        for component, files in before.items():
            for name in files:
                target = child(output, component + '/' + name)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(child(sources[component], name), target)
        if before != {k: inventory(p) for k, p in sources.items()}:
            raise ValueError('source changed during capture; incomplete bundle retained')
        expected = {c + '/' + n: r for c, files in before.items() for n, r in files.items()}
        if inventory(output) != expected:
            raise ValueError('bundle bytes mismatch')
        result = seal({'schema': 'isolated_business_bundle_v1', 'captured_at': now_utc().isoformat(),
                       'plan': plan, 'files': expected, 'database_controls': controls,
                       'publication': status, 'execution_ready': False,
                       'offsite_accepted': False, 'scope_complete': 'declared_components_only'})
        write_json(output / 'completed.json', result)
    verify(root, destination)
    return result


def verify(root, bundle):
    root = workspace(root)
    folder = child(root, bundle)
    receipt = checked(read_json(folder / 'completed.json'))
    if receipt.get('schema') != 'isolated_business_bundle_v1' or receipt.get('execution_ready') is not False:
        raise ValueError('wrong bundle contract')
    actual = inventory(folder)
    actual.pop('completed.json', None)
    if actual != receipt['files']:
        raise ValueError('bundle membership or checksum mismatch')
    for name, expected in receipt['database_controls'].items():
        if identity(database_controls(child(folder, name))) != identity(expected):
            raise ValueError('database controls differ')
    if validate_workspace(folder / 'workspace') != receipt['publication']:
        raise ValueError('publication controls differ')
    return receipt


def restore(root, bundle, destination):
    root = workspace(root)
    output = child(root, destination)
    original = child(root, bundle)
    if output.exists() or output.is_relative_to(original) or original.is_relative_to(output):
        raise ValueError('restore requires a separate new directory')
    started = time.monotonic()
    with FileLock(root / 'capabilities.guard'):
        receipt = verify(root, bundle)
        if shutil.disk_usage(root).free < sum(r['bytes'] for r in receipt['files'].values()) * 1.2:
            raise ValueError('insufficient restore capacity')
        output.mkdir(parents=True, exist_ok=False)
        for name in receipt['files']:
            target = child(output, name)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(child(original, name), target)
        if inventory(output) != receipt['files']:
            raise ValueError('restored bytes differ; incomplete restore retained')
        for name, expected in receipt['database_controls'].items():
            if identity(database_controls(child(output, name))) != identity(expected):
                raise ValueError('restored database controls differ')
        validate_workspace(output / 'workspace')
        verify(root, bundle)
        result = seal({'schema': 'isolated_business_restore_v1', 'bundle_id': receipt['receipt_id'],
                       'bundle': bundle, 'restored': destination, 'files': receipt['files'],
                       'elapsed_seconds': time.monotonic() - started, 'rpo_seconds': None,
                       'rpo_target_seconds': None, 'rto_target_seconds': None,
                       'offsite_accepted': False, 'production_activation': False,
                       'execution_ready': False, 'configuration_rebinding_required': True})
        write_json(output / 'restored.json', result)
        return result


def verified_restore(root, destination):
    root = workspace(root)
    folder = child(root, destination)
    receipt = checked(read_json(folder / 'restored.json'))
    if receipt.get('schema') != 'isolated_business_restore_v1' or receipt['restored'] != destination:
        raise ValueError('wrong restore receipt')
    bundle = verify(root, receipt['bundle'])
    actual = inventory(folder)
    actual.pop('restored.json', None)
    if receipt['bundle_id'] != bundle['receipt_id'] or receipt['files'] != bundle['files'] or actual != receipt['files']:
        raise ValueError('restore proof no longer matches actual restored files')
    return receipt
