"""Bounded historical receipt collections; overlapping revisions fail closed."""
import argparse
import json
import os
from pathlib import Path

from . import research_receipts as rr
from .daily_session import seal
from .domain import identity, now_utc, file_hash
from .gap_evidence import read_json, write_json

MAX_SESSIONS = 32
MAX_BATCHES = 8


def combine(paths, *, clock=now_utc):
    if not 1 <= len(paths) <= MAX_BATCHES:
        raise ValueError('bounded one to eight batches required')
    resolved = [Path(p).resolve(strict=True) for p in paths]
    if len(set(resolved)) != len(resolved) or any(Path(p).is_symlink() for p in paths):
        raise ValueError('unique non-symlink batches required')
    days = []; groups = {api: {} for api in rr.FIELDS}; batches = []; overlap_checks = 0
    first = None
    for path in resolved:
        if 'collection.json' in rr.sealed(path):
            raise ValueError('nested collections refused')
        reg, data, meta = rr.verify(path, clock=clock)
        if first is None:
            first = reg
        elif (reg['calendar_manifest_id'] != first['calendar_manifest_id'] or reg['origin'] != first['origin']):
            raise ValueError('same calendar and origin required')
        overlap = set(days) & set(reg['days'])
        if days and (reg['days'][:2] != days[-2:] or overlap != set(days[-2:])):
            raise ValueError('exact two-session batch overlap required')
        if len(set(days) | set(reg['days'])) > MAX_SESSIONS:
            raise ValueError('at most 32 unique sessions required')
        for api, rows in data.items():
            daily = {day: [] for day in reg['days']}
            for row in rows:
                daily[row['date']].append(row)
            for day, values in daily.items():
                if day in groups[api]:
                    if groups[api][day] != values:
                        raise ValueError('overlapping receipt revision conflict')
                    overlap_checks += 1
                else:
                    groups[api][day] = values
        days.extend(d for d in reg['days'] if d not in overlap)
        batches.append({'manifest_id': meta['manifest_id'], 'days': reg['days']})
    reg = {**first, 'schema': 2, 'days': days, 'end': days[-3],
           'max_requests': sum(2*len(b['days']) for b in batches)}
    data = {api: [row for day in days for row in groups[api][day]] for api in rr.FIELDS}
    meta = {'origin': first['origin'], 'scope': first['scope'], 'money_amount_unit': 'CNY',
            'historical_PIT_qualified': False, 'research_ready': False, 'execution_ready': False,
            'batches': batches, 'overlap_api_days_verified': overlap_checks,
            'unique_sessions': len(days), 'max_sessions': MAX_SESSIONS,
            'rows': {api: len(rows) for api, rows in data.items()},
            'revision_policy': 'exact_normalized_overlap_equality_no_silent_merge'}
    return reg, data, meta


def create_collection(paths, output):
    output = Path(output).resolve()
    reg, _, meta = combine(paths)
    refs = [{'path': os.path.relpath(Path(p).resolve(), output), 'manifest_id': b['manifest_id']}
            for p, b in zip(paths, meta['batches'])]
    output.mkdir(parents=True, exist_ok=False)
    write_json(output/'collection.json', {'schema': 1, 'batches': refs, 'registration': reg,
                                         'summary': meta, 'source_sha256': file_hash(Path(__file__))})
    seal(output)
    return verify_collection(output)[2]


def verify_collection(folder, *, clock=now_utc):
    folder = Path(folder).resolve(strict=True)
    members = rr.sealed(folder)
    if set(members) != {'collection.json'}:
        raise ValueError('exact collection membership required')
    index = read_json(folder/'collection.json')[0]
    refs = index['batches']
    if index['schema'] != 1 or not isinstance(refs, list) or not 1 <= len(refs) <= MAX_BATCHES:
        raise ValueError('bounded collection index required')
    paths = []
    for ref in refs:
        if set(ref) != {'path', 'manifest_id'} or Path(ref['path']).is_absolute():
            raise ValueError('relative receipt reference required')
        path = folder/ref['path']
        if path.is_symlink() or path.resolve() == folder:
            raise ValueError('independent non-symlink receipt required')
        if identity(rr.sealed(path)) != ref['manifest_id']:
            raise ValueError('child receipt binding changed')
        paths.append(path)
    reg, data, meta = combine(paths, clock=clock)
    if index['registration'] != reg or index['summary'] != meta:
        raise ValueError('collection derivation changed')
    return reg, data, {**meta, 'manifest_id': identity(members)}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--batch', action='append', required=True)
    p.add_argument('--output', required=True)
    a = p.parse_args()
    print(json.dumps(create_collection(a.batch, a.output)))


if __name__ == '__main__':
    main()
