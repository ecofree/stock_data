"""Immutable artifact bundles and one atomically switched, verified pointer."""
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import uuid

from trade_system.file_lock import FileLock
from .domain import canonical, now_utc


def safe_child(root, name):
    name = PurePosixPath(name)
    if name.is_absolute() or '..' in name.parts or '\\' in str(name) or ':' in str(name):
        raise ValueError('unsafe artifact path')
    path = (root / str(name)).resolve()
    if root.resolve() not in path.parents:
        raise ValueError('artifact must stay within bundle')
    return path


def publish(root, run_id, artifacts, *, generation):
    root = Path(root).resolve()
    if any((root/name).exists() for name in ('pipeline_run_latest.json','daily_review_latest.html')):
        raise ValueError('V2 publisher requires a separate namespace; legacy latest is not a fallback')
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', run_id) or type(generation) is not int or generation < 0:
        raise ValueError('invalid run identity or generation')
    if not artifacts or 'manifest.json' in artifacts:
        raise ValueError('nonempty explicit artifact list required; manifest name is reserved')
    run = safe_child(root, 'runs/' + run_id)
    hashes = {}
    for name, content in artifacts.items():
        safe_child(run, name)
        if not isinstance(content, bytes):
            raise ValueError('artifact content must be bytes')
        hashes[name] = hashlib.sha256(content).hexdigest()
    root.mkdir(parents=True, exist_ok=True)
    with FileLock(root / 'publish.guard'):
        current = root / 'current.json'
        history = {}
        if current.exists():
            previous, previous_files = read_current(root)
            if previous['generation'] >= generation:
                raise ValueError('older or equal generation cannot replace current bundle')
            if previous['artifacts'] == hashes:
                return json.loads(current.read_text(encoding='utf-8'))
            history = dict(previous.get('published_days', {}))
            if 'desk.json' in previous_files:
                day = (json.loads(previous_files['desk.json']).get('market') or {}).get('trade_date')
                if day:
                    history[day] = json.loads(current.read_text(encoding='utf-8'))
        # Only a changed, valid bundle creates a new run directory.
        run.mkdir(parents=True, exist_ok=False)
        temp = root / ('.current-' + uuid.uuid4().hex)
        try:
            for name, content in artifacts.items():
                file = safe_child(run, name)
                file.parent.mkdir(parents=True, exist_ok=True)
                with file.open('xb') as handle:
                    handle.write(content)
                    handle.flush()
                    os.fsync(handle.fileno())
            manifest = {'run_id': run_id, 'generation': generation, 'artifacts': hashes}
            if set(artifacts) == {'desk.json', 'index.html'}:
                data = json.loads(artifacts['desk.json'])
                if data.get('market') and data.get('report_id'):
                    from . import research_product_view as view
                    marker = '<script type="application/json" id="data">'
                    head, body = artifacts['index.html'].decode('utf-8').split(marker)
                    payload, tail = body.split('</script>', 1)
                    expected_head, expected_body = view.render(data).split(marker)
                    expected_payload, expected_tail = expected_body.split('</script>', 1)
                    if (head != expected_head or tail != expected_tail
                            or json.loads(payload) != json.loads(expected_payload)):
                        raise ValueError('new research publication must match its renderer')
                    manifest['content_contract'] = {
                        'name': 'inline_research_desk', 'version': 1,
                        'renderer_sha256': hashlib.sha256(Path(view.__file__).read_bytes()).hexdigest(),
                        'projection_sha256': hashlib.sha256(canonical(view.export_projection(data)).encode()).hexdigest()}
                    manifest['published_at'] = now_utc().isoformat()
                    manifest['publication_counts'] = {
                        'scope': 'inline_desk_entities_not_source_rows_or_collection_success',
                        'stocks': len(data['market'].get('stocks', [])),
                        'themes': len(data['market'].get('themes', [])),
                        'artifacts': len(artifacts),
                        'visibility': 'only_when_referenced_by_verified_current_or_history_pointer'}
                    # The new atomic pointer attests these previously current bundles.
                    # A staged directory is never admitted by scanning runs/.
                    manifest['published_days'] = {day: history[day] for day in sorted(history)[-64:]}
            manifest_bytes = canonical(manifest).encode()
            with (run / 'manifest.json').open('xb') as handle:
                handle.write(manifest_bytes)
                handle.flush()
                os.fsync(handle.fileno())
            pointer = {'run_id': run_id, 'generation': generation,
                       'manifest_sha256': hashlib.sha256(manifest_bytes).hexdigest()}
            owner = root / 'v2-publication-owner.json'
            if not owner.exists():
                with owner.open('x', encoding='utf-8') as stream:
                    stream.write(canonical({'scope': 'v2_versioned_publication_only', 'legacy_writes': 'forbidden'}))
            with temp.open('xb') as handle:
                handle.write(canonical(pointer).encode())
                handle.flush()
                os.fsync(handle.fileno())
            temp.replace(current)
        except BaseException:
            # Remove only this attempt, never any previously published run.
            temp.unlink(missing_ok=True)
            shutil.rmtree(run)
            raise
    return pointer


def read_current(root):
    root = Path(root).resolve()
    pointer = json.loads((root / 'current.json').read_text(encoding='utf-8'))
    return read_bundle(root, pointer)


def read_bundle(root, pointer):
    """Verify one pointer supplied by current or its sealed publication history."""
    root = Path(root).resolve()
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', pointer['run_id']):
        raise ValueError('invalid published run identity')
    run = safe_child(root, 'runs/' + pointer['run_id'])
    path = run / 'manifest.json'
    if path.stat().st_size > 100_000:
        raise ValueError('publication manifest exceeds read budget')
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != pointer['manifest_sha256']:
        raise ValueError('manifest checksum mismatch')
    manifest = json.loads(raw)
    if manifest['run_id'] != pointer['run_id'] or manifest['generation'] != pointer['generation']:
        raise ValueError('pointer/bundle identity mismatch')
    files = {}
    for name, expected in manifest['artifacts'].items():
        path = safe_child(run, name)
        if path.stat().st_size > 20_000_000:
            raise ValueError('publication artifact exceeds read budget')
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != expected:
            raise ValueError(f'artifact checksum mismatch: {name}')
        files[name] = raw
    return manifest, files
