"""Capture a registered bounded extension, preserving completed child checkpoints."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from trade_system.v2 import research_receipts as rr
from trade_system.v2.research_history import create_collection, MAX_SESSIONS, MAX_BATCHES
from trade_system.v2.domain import identity, file_hash, now_utc
from trade_system.v2.gap_evidence import read_json, write_json


def extension_plan(days, existing, sessions):
    if (not 6 <= sessions <= MAX_SESSIONS or not 3 <= len(existing) <= sessions
        or len(days) < sessions or existing != days[:len(existing)]):
        raise ValueError('bounded continuous prefix required')
    result = []; covered = len(existing)
    while covered < sessions:
        begin = covered-2
        stop = min(begin+10, sessions)
        result.append({'start': days[begin], 'end': days[stop-3], 'days': days[begin:stop]})
        covered = stop
    if len(result)+1 > MAX_BATCHES:
        raise ValueError('batch budget exceeded')
    return result


def run(calendar, reuse, sessions, output):
    reg, _, meta = rr.verify(reuse)
    if reg['schema'] != 1 or reg['origin'] != 'xiaodefa_relay':
        raise ValueError('one native initial batch required')
    if identity(rr.sealed(calendar)) != reg['calendar_manifest_id']:
        raise ValueError('same sealed calendar required')
    days = read_json(Path(calendar)/'calendar-overlay.json')[0]['open_days']
    days = [d for d in days if d >= reg['start']]
    plan = extension_plan(days, reg['days'], sessions)
    output = Path(output).resolve(); output.mkdir(parents=True, exist_ok=False)
    write_json(output/'plan.json', {'batches': plan, 'reuse_manifest_id': meta['manifest_id'],
        'calendar_manifest_id': reg['calendar_manifest_id'], 'unique_sessions': sessions,
        'max_new_requests': sum(len(p['days'])*2 for p in plan), 'retries': 0,
        'registered_at': now_utc().isoformat(), 'source_sha256': file_hash(Path(__file__)),
        'research_ready': False, 'execution_ready': False})
    paths = [Path(reuse).resolve()]
    try:
        for i, batch in enumerate(plan, 1):
            child = output/f'batch-{i}'
            result = rr.capture(calendar, batch['start'], batch['end'], child)
            paths.append(child)
            print(json.dumps({'batch_completed': i, 'receipts': len(result['receipts'])}), flush=True)
        return create_collection(paths, output/'collection')
    except Exception as exc:
        write_json(output/'failed.json', {'error_type': type(exc).__name__, 'completed_new_batches': len(paths)-1,
                                        'execution_ready': False})
        raise


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    for name in ('calendar', 'reuse', 'output'): p.add_argument('--'+name, required=True)
    p.add_argument('--sessions', type=int, default=32)
    a = p.parse_args()
    try:
        print(json.dumps(run(a.calendar, a.reuse, a.sessions, a.output)))
    except Exception as exc:
        print(json.dumps({'status': 'failed', 'error_type': type(exc).__name__}))
        raise SystemExit(1) from None
