"""Prepare a three-file legacy hotfix, never modify the live checkout or tasks."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[2]


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def prepare(live, output):
    live, output = Path(live).resolve(), Path(output).resolve()
    if live == output or live in output.parents:
        raise ValueError('hotfix preparation must be outside live checkout')
    output.mkdir(parents=True, exist_ok=False)
    renderer = live / 'trade_system/review_web.py'
    original = renderer.read_text(encoding='utf-8')
    marker = 'compact_overview=True,'
    if original.count(marker) != 1:
        raise ValueError('live compact renderer differs from audited baseline')
    # Only change embedding, not legacy indicator semantics or write authority.
    files = {'trade_system/review_web.py': original.replace(marker, 'compact_overview=False,').encode('utf-8')}
    for name in ('scripts/audit_daily_review_artifact.py', 'scripts/check_kpl_connectivity.py'):
        files[name] = (ROOT / name).read_bytes()
    manifest = {'scope': 'three_file_legacy_report_hotfix_not_full_migration',
                'production_changes': 0, 'execution_ready': False, 'files': {}}
    for name, content in files.items():
        target = output / name; target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        manifest['files'][name] = {'before_sha256': sha(live / name), 'after_sha256': sha(target)}
    (output / 'manifest.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')
    return manifest


def rehearse(live, bundle, database, output, trade_date):
    live, bundle, output = Path(live).resolve(), Path(bundle).resolve(), Path(output).resolve()
    if live == output or live in output.parents:
        raise ValueError('rehearsal must be outside live checkout')
    manifest = json.loads((bundle / 'manifest.json').read_text())
    for name, expected in manifest['files'].items():
        if sha(live / name) != expected['before_sha256'] or sha(bundle / name) != expected['after_sha256']:
            raise ValueError('live or prepared source changed; repeat preparation')
    output.mkdir(parents=True, exist_ok=False)
    # A fresh child imports every dependency from LIVE, overriding only the
    # one patched renderer. This is not a rehearsal of the whole V2 checkout.
    code = '''import importlib.util,sys
from pathlib import Path
live,bundle,database,out,day=sys.argv[1:]
sys.path.insert(0,live)
spec=importlib.util.spec_from_file_location('trade_system.review_web',Path(bundle)/'trade_system/review_web.py')
module=importlib.util.module_from_spec(spec);sys.modules[spec.name]=module;spec.loader.exec_module(module)
module.write_review_web(database,Path(out)/'daily_review_latest.html',day)
'''
    with (output / 'render.log').open('w', encoding='utf-8') as log:
        result = subprocess.run([sys.executable, '-X', 'utf8', '-B', '-c', code,
                                 str(live), str(bundle), str(Path(database).resolve()), str(output), trade_date],
                                cwd=output, stdout=log, stderr=subprocess.STDOUT, timeout=300)
    if result.returncode:
        raise RuntimeError('isolated legacy hotfix render failed; inspect render.log')
    result = subprocess.run([sys.executable, '-X', 'utf8', '-B', str(bundle / 'scripts/audit_daily_review_artifact.py'),
                             '--db', str(Path(database).resolve()), '--date', trade_date,
                             '--html', str(output / 'daily_review_latest.html'), '--out', str(output / 'audit.md')],
                            capture_output=True, text=True, encoding='utf-8', timeout=60)
    if not result.stdout.strip():
        raise RuntimeError('hotfix audit did not return evidence')
    audit = json.loads(result.stdout)
    unchanged = all(sha(live / name) == expected['before_sha256'] for name, expected in manifest['files'].items())
    receipt = {'audit': audit, 'live_source_unchanged': unchanged, 'production_changes': 0,
               'passed': result.returncode == 0 and audit['status'] == 'pass' and unchanged,
               'manifest_sha256': sha(bundle / 'manifest.json')}
    (output / 'rehearsal.json').write_text(json.dumps(receipt, indent=2, ensure_ascii=False), encoding='utf-8')
    return receipt


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', required=True); parser.add_argument('--bundle', required=True)
    parser.add_argument('--rehearse', action='store_true'); parser.add_argument('--db')
    parser.add_argument('--output'); parser.add_argument('--date')
    args = parser.parse_args()
    if args.rehearse:
        if not all((args.db, args.output, args.date)): parser.error('rehearsal requires db, output and date')
        result = rehearse(args.live, args.bundle, args.db, args.output, args.date)
        print(json.dumps(result)); sys.exit(0 if result['passed'] else 1)
    else:
        print(json.dumps(prepare(args.live, args.bundle)))
