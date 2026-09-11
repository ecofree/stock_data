"""Third endpoint observations for frozen conflicts, never majority-vote repair."""
import argparse
from collections import Counter
from datetime import date
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
import urllib.parse

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tools.v2.probe_price_conflicts import parent
from trade_system.v2.daily_session import seal
from trade_system.v2.domain import canonical, file_hash, identity, now_utc, number, utc
from trade_system.v2.gap_evidence import read_json, write_json
from trade_system.v2.research_receipts import sealed

SCOPE = 'third_endpoint_diagnostic_not_independent_lineage_or_canonical_authority'
INTERPRETATION = 'existing_project_mapping_volume_times_100_amount_times_1_not_provider_certification'
FIELDS = ('open', 'high', 'low', 'close', 'volume_shares', 'turnover_cny')
DAYS = ('2025-11-27', '2025-11-28', '2025-12-01')
MAX_BYTES = 512_000


def request_for(code):
    if not re.fullmatch(r'\d{6}\.SZ', code):
        raise ValueError('explicit Shenzhen conflict code required')
    return {'endpoint': 'https://push2his.eastmoney.com/api/qt/stock/kline/get',
            'code': code, 'params': {'secid': '0.' + code[:6], 'fields1': 'f1,f2,f3',
                'fields2': 'f51,f52,f53,f54,f55,f56,f57', 'klt': '101', 'fqt': '0',
                'beg': '20251127', 'end': '20251201'}}


class Client:
    def query(self, request):
        url = request['endpoint'] + '?' + urllib.parse.urlencode(request['params'])
        executable = shutil.which('curl.exe') or shutil.which('curl')
        if not executable:
            raise ValueError('curl is required for this explicit public transport')
        response = subprocess.run([executable, '--disable', '--silent', '--show-error', '--fail',
            '--proto', '=https', '--max-time', '15', '--max-filesize', str(MAX_BYTES), url],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20, check=False)
        if response.returncode:
            raise ValueError('public transport failed; details withheld')
        raw = response.stdout
        if len(raw) > MAX_BYTES:
            raise ValueError('response byte budget exceeded')
        return raw.decode('utf-8')


def normalize(request, raw):
    if not isinstance(raw, str) or len(raw.encode('utf-8')) > MAX_BYTES:
        raise ValueError('bounded original response text required')
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('duplicate response key')
            result[key] = value
        return result
    data = json.loads(raw, object_pairs_hook=unique)
    if type(data.get('rc')) is not int or data['rc'] != 0:
        raise ValueError('successful provider response required')
    item = data.get('data')
    if (not isinstance(item, dict) or item.get('code') != request['code'][:6]
            or type(item.get('market')) is not int or item['market'] != 0):
        raise ValueError('response instrument differs')
    lines = item.get('klines')
    if not isinstance(lines, list) or len(lines) > len(DAYS):
        raise ValueError('bounded three-session response required')
    rows = {}
    for line in lines:
        if not isinstance(line, str):
            raise ValueError('original CSV row required')
        cells = line.split(',')
        if len(cells) != 7 or cells[0] not in DAYS or cells[0] in rows:
            raise ValueError('unique in-window seven-field row required')
        date.fromisoformat(cells[0])
        o, c, h, l, v, a = [number(x) for x in cells[1:]]
        if not 0 < l <= min(o, c) <= max(o, c) <= h or v < 0 or a < 0:
            raise ValueError('invalid price or quantity bounds')
        # This existing-project interpretation is explicitly NOT certified
        # by an Eastmoney field contract. Preserve original precision too.
        rows[cells[0]] = {'raw_cells': cells, 'interpretation': INTERPRETATION,
            'values': dict(zip(FIELDS, map(str, (o, h, l, c, v * 100, a))))}
    return rows


def registration(binding, limit, origin):
    if type(limit) is not int or not 1 <= limit <= 38:
        raise ValueError('one to 38 codes required')
    codes = sorted(binding['cases'])[:limit]
    return {'scope': SCOPE, 'origin': origin, 'analysis_sha256': binding['analysis_sha256'],
        'codes': codes, 'requests': [request_for(c) for c in codes],
        'max_requests': len(codes), 'automatic_retries': 0,
        'transport': 'curl_verified_https_no_redirect_no_config_no_retry',
        'interpretation': INTERPRETATION, 'independent_upstream_verified': False,
        'canonical_replacement_authorized': False, 'execution_ready': False}


def separate(output, *inputs):
    output = Path(output).resolve()
    if output.exists() or any(output == Path(p).resolve() or Path(p).resolve() in output.parents
                             or output in Path(p).resolve().parents for p in inputs):
        raise ValueError('separate new output namespace required')
    return output


def capture(analysis, output, *, limit=4, client=None):
    binding = parent(analysis)
    output = separate(output, analysis)
    expected = registration(binding, limit, 'eastmoney_public' if client is None else 'synthetic_fixture')
    output.mkdir(parents=True)
    source = file_hash(Path(__file__))
    write_json(output / 'registration.json', {**expected, 'source_sha256': source,
                                             'started_at': now_utc().isoformat()})
    client = client or Client()
    failures = 0
    for i, request in enumerate(expected['requests']):
        status = {'index': i}
        if failures >= 3:
            status.update(status='skipped', reason='three_consecutive_failures')
        else:
            try:
                time.sleep(.75)
                raw = client.query(request)
                if not isinstance(raw, str) or len(raw.encode('utf-8')) > MAX_BYTES:
                    raise ValueError('bounded original response required')
                write_json(output / f'receipt-{i:02d}.json', {'request': request,
                    'received_at': now_utc().isoformat(), 'raw_response': raw})
                rows = normalize(request, raw)
                status.update(status='observed', rows=len(rows))
                failures = 0
            except Exception as exc:
                failures += 1
                status.update(status='failed', error_type=type(exc).__name__)
        write_json(output / f'status-{i:02d}.json', status)
        print(canonical({'code': request['code'], **status}), flush=True)
    if parent(analysis) != binding or file_hash(Path(__file__)) != source:
        raise ValueError('capture inputs changed')
    seal(output)


def compare(cases, observations):
    result = []
    for case in cases:
        observed = observations.get(case['date'])
        evidence = {p: next(e['values'] for e in case['evidence'] if e['provider'] == p)
                    for p in ('hithink_native', 'xiaodefa_relay')}
        comparisons = {}
        if observed:
            for provider, values in evidence.items():
                delta = {k: number(observed['values'][k]) - number(values[k]) for k in FIELDS}
                comparisons[provider] = {'deltas_third_minus_source': {k: str(v) for k, v in delta.items()},
                    'ohlc_exact': all(delta[k] == 0 for k in FIELDS[:4]),
                    'volume_exact_under_interpretation': delta['volume_shares'] == 0,
                    'volume_within_50_shares_under_interpretation': abs(delta['volume_shares']) <= 50,
                    'turnover_within_0_50_cny_under_interpretation': abs(delta['turnover_cny']) <= number('.50')}
        result.append({'date': case['date'], 'parent_record_id': case['parent_record_id'],
            'status': 'observed_diagnostic_only' if observed else 'missing_third_observation',
            'observation': observed, 'parent_evidence': evidence, 'comparisons': comparisons,
            'independent_upstream_verified': False, 'canonical_replacement_authorized': False,
            'research_ready': False, 'execution_ready': False})
    return result


def analyze(analysis, receipts, output):
    binding = parent(analysis)
    receipts = Path(receipts)
    output = separate(output, analysis, receipts)
    members = sealed(receipts)
    reg = read_json(receipts / 'registration.json')[0]
    expected = registration(binding, len(reg.get('codes', [])), 'eastmoney_public')
    if {k: v for k, v in reg.items() if k not in ('source_sha256', 'started_at')} != expected:
        raise ValueError('exact real capture registration required')
    required = {'registration.json', *(f'status-{i:02d}.json' for i in range(len(reg['codes'])))}
    optional = {f'receipt-{i:02d}.json' for i in range(len(reg['codes']))}
    if not required <= set(members) <= required | optional:
        raise ValueError('capture membership differs')
    reports = {}; statuses = Counter(); counts = Counter()
    for i, code in enumerate(reg['codes']):
        status = read_json(receipts / f'status-{i:02d}.json')[0]
        if type(status.get('index')) is not int or status['index'] != i or status.get('status') not in ('observed', 'failed', 'skipped'):
            raise ValueError('request status differs')
        statuses[status['status']] += 1
        path = receipts / f'receipt-{i:02d}.json'; rows = {}
        if path.name in members:
            receipt = read_json(path)[0]
            if receipt['request'] != expected['requests'][i] or not utc(reg['started_at']) <= utc(receipt['received_at']) <= now_utc():
                raise ValueError('receipt request or time differs')
        if status['status'] == 'observed':
            if path.name not in members:
                raise ValueError('observed receipt absent')
            rows = normalize(expected['requests'][i], receipt['raw_response'])
            if type(status.get('rows')) is not int or status['rows'] != len(rows):
                raise ValueError('row count differs')
        reports[code] = compare(binding['cases'][code], rows)
        counts.update(row['status'] for row in reports[code])
    if members != sealed(receipts) or binding != parent(analysis):
        raise ValueError('analysis inputs changed')
    output.mkdir(parents=True)
    for code, rows in reports.items():
        write_json(output / (code + '.json'), {'code': code, 'cases': rows})
    result = {'scope': SCOPE, 'interpretation': INTERPRETATION, 'source_sha256': file_hash(Path(__file__)),
        'analysis_sha256': binding['analysis_sha256'], 'receipt_manifest_id': identity(members),
        'codes': len(reports), 'cases': sum(counts.values()), 'case_statuses': dict(counts),
        'request_statuses': dict(statuses), 'canonical_replacements': 0,
        'independent_upstream_verified': False, 'research_ready': False,
        'execution_ready': False, 'production_cutover': False}
    write_json(output / 'result.json', result); seal(output)
    return result


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('action', choices=['capture', 'analyze']); p.add_argument('--analysis', required=True)
    p.add_argument('--output', required=True); p.add_argument('--receipts'); p.add_argument('--limit', type=int, default=4)
    args = p.parse_args()
    if args.action == 'capture':
        capture(args.analysis, args.output, limit=args.limit)
    else:
        print(canonical(analyze(args.analysis, args.receipts, args.output)))
