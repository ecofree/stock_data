"""Bounded native evidence collection. Secrets stay in memory/stdin, never artifacts."""
from collections import Counter
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import time
from urllib.parse import urlencode

from .domain import canonical, identity, now_utc, utc
from .gap_audit import adjudicate_audit, read_package
from .gap_evidence import ingest_response, read_evidence, read_json, sha, write_json
from .native_gap_sources import RELAY, VERSION, build_requests, convert_response, request_spec
from .rolling_research import file_hash


def fingerprint():
    return {n: file_hash(Path(__file__).with_name(n)) for n in ('native_gap_sources.py', 'native_gap_run.py')}


def credentials():
    from trade_system.config import SETTINGS
    def value(key):
        return str(os.environ.get(key) or SETTINGS.get(key) or '').strip()
    relay_url = value('TUSHARE_FAST_RELAY_URL') or value('TUSHARE_RELAY_URL') or RELAY
    if relay_url.rstrip('/') != RELAY:
        raise ValueError('configured relay differs from inspected allowed HTTPS origin')
    return {'hithink_official': value('HITHINK_FINANCE_API_KEY'),
            'xiaodefa_tushare': value('TUSHARE_FAST_RELAY_TOKEN') or value('TUSHARE_RELAY_TOKEN') or value('TUSHARE_TOKEN')}


def reserve_rate_slot(family, max_wait=5):
    # Cooperates with the existing shared host limiter but has a bounded wait.
    from trade_system.host_limiter import shared_host_limiter
    host = 'tushare_relay' if family == 'xiaodefa_tushare' else 'hithink_gap_evidence'
    deadline = time.monotonic() + max_wait
    while True:
        now = time.time()
        with sqlite3.connect(shared_host_limiter.db_path, timeout=1) as con:
            con.execute('BEGIN IMMEDIATE')
            row = con.execute('SELECT last_started,cooldown_until FROM host_rate_limit WHERE host=?', [host]).fetchone()
            previous, cooldown = row or (0, 0)
            delay = max(1.0-(now-previous), cooldown-now, 0)
            if delay <= 0:
                con.execute('''INSERT INTO host_rate_limit(host,last_started,cooldown_until,updated_at) VALUES(?,?,0,?)
                    ON CONFLICT(host) DO UPDATE SET last_started=excluded.last_started,updated_at=excluded.updated_at''', [host, now, now])
                return
        if time.monotonic()+delay > deadline:
            raise TimeoutError('rate slot unavailable in bounded budget')
        time.sleep(min(delay, .5))


def http_fetch(spec, secret):
    """curl config on stdin prevents both auth headers and POST token in argv.

    Redirects are not followed. Curl defaults keep certificate verification on.
    Errors are represented by codes only; stderr may contain sensitive details.
    """
    if request_spec(spec, spec['api']) != spec:
        raise ValueError('request not in exact read-only allowlist')
    reserve_rate_slot(spec['family'])
    url = spec['url']
    headers = ['Accept: application/json', 'User-Agent: stock-data-gap-evidence/1']
    body = None
    if spec['method'] == 'GET':
        url += '?' + urlencode(spec['params'])
        headers.append('X-api-key: ' + secret)
    else:
        headers.append('Content-Type: application/json')
        headers += ['Origin: '+RELAY, 'Referer: '+RELAY+'/']
        body = canonical({'api_name': spec['api'], 'token': secret, 'params': spec['params'], 'fields': spec['fields']})
    config = ['url = '+json.dumps(url), 'request = '+json.dumps(spec['method'])]
    config += ['header = '+json.dumps(header) for header in headers]
    if body is not None:
        config.append('data = '+json.dumps(body, ensure_ascii=False))
    proc = subprocess.run(['curl.exe' if os.name == 'nt' else 'curl', '--silent', '--show-error',
        '--max-time', '15', '--max-filesize', '4194304', '--proto', '=https', '--config', '-',
        '--write-out', '\n%{http_code}'], input=('\n'.join(config)+'\n').encode('utf-8'),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=18)
    if proc.returncode:
        return {'http_status': None, 'transport_error': 'curl_exit_'+str(proc.returncode), 'raw': b''}
    raw, _, status = proc.stdout.rpartition(b'\n')
    if len(raw) > 4*1024*1024:
        raise ValueError('native response byte budget exceeded')
    return {'http_status': int(status), 'transport_error': None, 'raw': raw}


def collect(audit_folder, output, *, fetch=http_fetch, secrets=None, clock=now_utc, wall_seconds=240,
            resume_from=None, historical_source=None):
    audit_folder, output = Path(audit_folder).resolve(), Path(output).resolve()
    if output.exists():
        raise FileExistsError('new native collection folder required')
    if type(wall_seconds) is not int or not 1 <= wall_seconds <= 240:
        raise ValueError('bounded collection wall budget required')
    audit = read_package(audit_folder)
    requests = build_requests(audit['cases'])
    previous, previous_evidence, spent, eligible = {}, [], 0, None
    if resume_from is not None:
        parent = Path(resume_from).resolve()
        old = verify_collection(parent, historical_source=historical_source)
        old_reg, _ = read_json(parent/'registration.json')
        if old_reg['audit_manifest_sha256'] != file_hash(audit_folder/'completed.json') or old_reg.get('resume_parent'):
            raise ValueError('one bounded continuation of the same audit only')
        spent = old['transport_attempts']
        decisions = read_package(parent/'adjudication')['decisions']
        eligible = {d['case_id'] for d in decisions if d['classification'] in ('unknown', 'conflicting_evidence')}
        for sub in (parent/'requests').iterdir():
            req, _ = read_json(sub/'request.json'); status, _ = read_json(sub/'status.json')
            previous[identity(req)] = status
            previous_evidence.extend(sorted((sub/'evidence').glob('*')))
    keys = credentials() if secrets is None else secrets
    output.mkdir(parents=True, exist_ok=False)
    registration = {'version': VERSION, 'source_hashes': fingerprint(), 'requests': requests,
        'registered_at': utc(clock()).isoformat(), 'audit_manifest_sha256': file_hash(audit_folder/'completed.json'),
        'request_budget': 28, 'wall_budget_seconds': wall_seconds, 'no_retries': True,
        'collector_version': 'native-collector-v2',
        'resume_parent': str(Path(resume_from).resolve()) if resume_from else None,
        'resume_parent_manifest_sha256': file_hash(Path(resume_from)/'completed.json') if resume_from else None,
        'resume_parent_source_archive': str(Path(historical_source).resolve()) if historical_source else None,
        'previous_transport_attempts': spent,
        'configured_sources': {family: bool(keys.get(family)) for family in ('hithink_official', 'xiaodefa_tushare')},
        'execution_ready': False, 'account_application': 'disabled'}
    registration['registration_id'] = identity(registration)
    write_json(output/'registration.json', registration)
    attempts, evidence_folders, disabled, disabled_sources = [], previous_evidence, set(), set()
    cooling_sources = {p['family'] for p in previous.values() if p.get('http_status') == 429 and
                       (utc(clock())-utc(p['received_at'])).total_seconds() < 60}
    began = time.monotonic()
    try:
        for index, spec in enumerate(requests):
            folder = output/'requests'/f'{index:02d}'
            folder.mkdir(parents=True)
            write_json(folder/'request.json', spec)
            endpoint = (spec['family'], spec['api'])
            prior = previous.get(identity(spec), {})
            reason = ('case_already_has_candidate' if eligible is not None and spec['case_id'] not in eligible else
                      'previous_response_retained' if prior.get('status') in ('mapped', 'empty_or_unresolved') or prior.get('business_code') == 1002 else
                      'request_budget_exhausted' if spent >= 28 else
                      'source_not_configured' if not keys.get(spec['family']) else
                      'source_circuit_open' if spec['family'] in disabled_sources else
                      'endpoint_circuit_open' if endpoint in disabled else
                      'rate_cooldown_not_elapsed' if spec['family'] in cooling_sources else
                      'wall_budget_exhausted' if time.monotonic()-began+23 > wall_seconds else None)
            status = {'request_index': index, 'api': spec['api'], 'family': spec['family'], 'case_id': spec['case_id'],
                      'status': reason, 'evidence_ids': [], 'transport_attempted': False}
            if not reason:
                status['transport_attempted'] = True
                spent += 1
                try:
                    response = fetch(spec, keys[spec['family']])
                    received = utc(clock()).isoformat()
                    raw = response['raw']
                    if any(secret and secret.encode('utf-8') in raw for secret in keys.values()):
                        status.update(status='credential_echo_response_withheld', response_sha256=sha(raw))
                        disabled.add(endpoint)
                    elif response['transport_error']:
                        status.update(status='transport_error', error_code=response['transport_error'])
                        disabled.add(endpoint)
                    else:
                        with (folder/'native.json').open('xb') as stream:
                            stream.write(raw)
                        status.update(http_status=response['http_status'], received_at=received, native_sha256=sha(raw))
                        if response['http_status'] != 200:
                            status['status'] = 'http_error'
                            if response['http_status'] in (401, 403, 429):
                                disabled_sources.add(spec['family'])
                            else:
                                disabled.add(endpoint)
                        else:
                            mapped = convert_response(spec, raw)
                            write_json(folder/'mapping.json', mapped)
                            status.update(status=mapped['status'], business_code=mapped['business_code'], native_rows=mapped['native_rows'])
                            if mapped['status'] == 'provider_error':
                                if mapped['business_code'] in (-1, 2001, 2002):
                                    disabled_sources.add(spec['family'])
                                elif mapped['business_code'] != 1002:
                                    disabled.add(endpoint)
                            structured = {'schema_version': 1, 'rows': [r['claim'] for r in mapped['claims']]}
                            write_json(folder/'structured.json', structured)
                            for j, claim in enumerate(mapped['claims']):
                                receipt = {'schema_version': 1, 'source_family': spec['family'],
                                    'source_api': spec['url']+'#'+spec['api'], 'origin_family': spec['family']+'_declared_origin',
                                    'reference': 'native_sha256:'+sha(raw)+';row:'+str(claim['native_row_index'])+';adapter:'+VERSION,
                                    'source_revision': sha(raw), 'source_event_at': received, 'provider_received_at': received,
                                    'license_status': 'unverified', 'row_index': j, 'raw_sha256': file_hash(folder/'structured.json')}
                                receipt_path = folder/f'receipt-{j}.json'
                                write_json(receipt_path, receipt)
                                dest = folder/'evidence'/str(j)
                                record = ingest_response(receipt_path, folder/'structured.json', dest, clock=clock)
                                evidence_folders.append(dest)
                                status['evidence_ids'].append(record['evidence_id'])
                except Exception as exc:
                    # Never persist arbitrary exception text or HTTP request objects.
                    status.update(status='request_or_mapping_failed', error_type=type(exc).__name__)
                    disabled.add(endpoint)
            write_json(folder/'status.json', status)
            attempts.append(status)
        result = {'registration_id': registration['registration_id'], 'attempts': attempts,
            'transport_attempts': sum(a['transport_attempted'] for a in attempts),
            'status_counts': dict(Counter(a['status'] for a in attempts)), 'evidence_count': len(evidence_folders),
            'cumulative_transport_attempts': spent,
            'execution_ready': False, 'portfolio_resumed': False,
            'time_semantics': 'source_event_at/provider_received_at mark the observed HTTP response, not historical publication; market date stays in claim',
            'origin_authentication': 'configured HTTPS delivery only; no independent upstream authentication or license acceptance'}
        write_json(output/'report.json', result)
        adjudicate_audit(audit_folder, evidence_folders, output/'adjudication', clock=clock)
        manifest = {'registration_id': registration['registration_id'],
                    'artifact_hashes': {p.relative_to(output).as_posix(): file_hash(p) for p in output.rglob('*') if p.is_file()}}
        write_json(output/'completed.json', {**manifest, 'manifest_id': identity(manifest)})
        return result
    except BaseException as exc:
        write_json(output/'failed.json', {'error_type': type(exc).__name__, 'partial_outputs_are_not_success': True})
        raise


def verify_collection(folder, *, historical_source=None):
    folder = Path(folder).resolve()
    reg, _ = read_json(folder/'registration.json')
    rid = reg.pop('registration_id')
    hashes = fingerprint()
    if historical_source is not None:
        hashes = {name: file_hash(Path(historical_source)/name) for name in reg['source_hashes']}
        if hashes.get('native_gap_sources.py') != fingerprint()['native_gap_sources.py']:
            raise ValueError('historical mapper differs; current semantic verifier cannot certify it')
    if identity(reg) != rid or reg['source_hashes'] != hashes:
        raise ValueError('native registration or source mismatch')
    if reg.get('resume_parent'):
        if file_hash(Path(reg['resume_parent'])/'completed.json') != reg['resume_parent_manifest_sha256']:
            raise ValueError('native continuation parent changed')
        verify_collection(reg['resume_parent'], historical_source=reg.get('resume_parent_source_archive'))
    manifest, _ = read_json(folder/'completed.json')
    mid = manifest.pop('manifest_id')
    if identity(manifest) != mid or manifest['registration_id'] != rid:
        raise ValueError('native completion mismatch')
    actual = {}
    for p in folder.rglob('*'):
        if p.is_file() and p != folder/'completed.json':
            if p.is_symlink() or folder not in p.resolve().parents:
                raise ValueError('native artifact escapes package')
            actual[p.relative_to(folder).as_posix()] = file_hash(p)
    if actual != manifest['artifact_hashes']:
        raise ValueError('native artifact member/hash mismatch')
    for sub in sorted((folder/'requests').iterdir()):
        if not (sub/'mapping.json').exists():
            continue
        spec, _ = read_json(sub/'request.json')
        mapping, _ = read_json(sub/'mapping.json')
        _, raw = read_json(sub/'native.json')
        if convert_response(spec, raw) != mapping:
            raise ValueError('native mapping not reproducible')
        structured, _ = read_json(sub/'structured.json')
        if structured != {'schema_version': 1, 'rows': [r['claim'] for r in mapping['claims']]}:
            raise ValueError('structured claims do not derive from native response')
        for evidence in (sub/'evidence').glob('*'):
            read_evidence(evidence)
    read_package(folder/'adjudication')
    return read_json(folder/'report.json')[0]
