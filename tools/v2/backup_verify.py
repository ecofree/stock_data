"""Coordinated read-only backup and restore verification; never repairs source."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import duckdb
from trade_system.pipeline_runtime import PipelineLock


PROTECTED = ('portfolio_snapshot', 'trade_plan', 'watchlist', 'trade_journal', 'operator_trade_outcome')


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as handle:
        for block in iter(lambda: handle.read(8 * 1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def controls(con):
    tables = [r[0] for r in con.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema='main' AND table_type='BASE TABLE' ORDER BY table_name"
    ).fetchall()]
    counts = {}
    for name in tables:
        quoted = '"' + name.replace('"', '""') + '"'
        counts[name] = con.execute(f'SELECT count(*) FROM {quoted}').fetchone()[0]
    totals = {}
    if 'portfolio_snapshot' in tables:
        totals['portfolio_position_sum'] = con.execute('SELECT sum(position_pct) FROM portfolio_snapshot').fetchone()[0]
    return {'table_rows': counts, 'protected_rows': {t: counts.get(t) for t in PROTECTED},
            'controls': totals}


def backup_verify(source: Path, destination: Path):
    source, destination = source.resolve(strict=True), destination.resolve()
    if not source.is_file() or destination.exists():
        raise ValueError('source must be a file and destination must be a new directory')
    if shutil.disk_usage(destination.parent).free < source.stat().st_size * 1.1:
        raise RuntimeError('insufficient free space for verified backup')
    with PipelineLock(source, 'v2-backup'):
        # Refuse an uncheckpointed database. Native read-only lock then
        # prevents direct writers while the file and control totals are read.
        if source.with_suffix(source.suffix + '.wal').exists():
            raise RuntimeError('WAL present; coordinate clean writer shutdown first')
        with duckdb.connect(str(source), read_only=True) as con:
            before = controls(con)
            destination.mkdir()
            copied = destination / source.name
            shutil.copy2(source, copied)
            original_hash, copied_hash = sha256(source), sha256(copied)
            if original_hash != copied_hash:
                raise RuntimeError('backup hash mismatch; partial output retained for investigation')
            for table, count in before['protected_rows'].items():
                if count is not None:
                    output = str(destination / f'{table}.parquet').replace("'", "''")
                    con.execute(f"COPY {table} TO '{output}' (FORMAT PARQUET)")
        with duckdb.connect(str(copied), read_only=True) as restored:
            after = controls(restored)
        if before != after:
            raise RuntimeError('restored control totals differ')
        result = {'source': str(source), 'backup': str(copied), 'sha256': copied_hash,
                  'restore_verified': True, 'source_modified': False, **after}
        (destination / 'verification.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--destination', type=Path, required=True)
    args = parser.parse_args()
    result = backup_verify(args.source, args.destination)
    print(json.dumps({k: result[k] for k in ('backup', 'sha256', 'restore_verified')}, ensure_ascii=False))
