"""Field-scoped exchange correction of frozen conflicts, never production overwrite."""
import argparse
from decimal import Decimal
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tools.v2 import probe_price_conflicts as conflict
from tools.v2.build_identity_candidate import separate
from trade_system.v2.daily_session import seal
from trade_system.v2.domain import canonical, file_hash, identity, now_utc, number, utc
from trade_system.v2.gap_evidence import read_json, write_json
from trade_system.v2.research_receipts import sealed

ENDPOINT = 'https://www.szse.cn/api/market/ssjjhq/getHistoryData'
CONTRACT_SHA = '246926d9c3d4e16e421c7ef1032b751de45b7943ed04cbb746f2dc435a3b5be2'
POLICY = {'version': 'exchange_field_correction_v1',
    'turnover_authority': 'szse_official_chart_CNY_exact_match_to_bound_relay',
    'volume_authority': 'retain_only_exact_agreement_of_bound_native_and_relay_shares',
    'exchange_volume': 'whole_hands_only_never_expand_to_claim_exact_shares',
    'price_authority': 'exchange_open_high_low_close_exact_agreement',
    'scope': 'frozen_registered_cases_only_no_global_provider_priority_change'}


def unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result: raise ValueError('duplicate raw response key')
        result[key] = value
    return result


def parse_response(receipt, request, started):
    if receipt['request'] != request or not utc(started) <= utc(receipt['received_at']) <= now_utc():
        raise ValueError('exchange request/time differs')
    raw = receipt['raw_response']
    if not isinstance(raw, str) or len(raw.encode('utf-8')) > 1_000_000:
        raise ValueError('bounded original exchange response required')
    result = json.loads(raw, parse_float=Decimal, object_pairs_hook=unique)
    data = result['data']
    if result['code'] != '0' or data['code'] != request['params']['code']:
        raise ValueError('exchange response code/identity differs')
    rows = data['picupdata']
    if not 1 <= len(rows) <= 1000: raise ValueError('bounded daily chart required')
    parsed = {}
    for row in rows:
        if not isinstance(row, list) or len(row) != 9 or row[0] in parsed:
            raise ValueError('duplicate or invalid exchange daily row')
        # Check all numeric fields using Decimal, preserving source precision.
        values = {k: str(number(row[i])) for k, i in
            [('open', 1), ('close', 2), ('low', 3), ('high', 4),
             ('volume_whole_hands', 7), ('turnover_cny', 8)]}
        if (number(values['low']) > min(number(values[k]) for k in ('open', 'close', 'high'))
                or number(values['high']) < max(number(values[k]) for k in ('open', 'close'))
                or any(number(v) < 0 for v in values.values())
                or number(values['volume_whole_hands']) % 1):
            raise ValueError('invalid exchange values')
        parsed[row[0]] = values
    return parsed


def resolve_case(code, case, exchange, evidence):
    sources = {r['provider']: r['values'] for r in case['evidence']}
    if len(sources) != len(case['evidence']) or not {'hithink_native', 'xiaodefa_relay'} <= sources.keys():
        raise ValueError('unique bound native and relay evidence required')
    native, relay = sources['hithink_native'], sources['xiaodefa_relay']
    if any(number(exchange[k]) != number(source[k]) for k in ('open', 'high', 'low', 'close')
           for source in (native, relay)):
        raise ValueError('exchange OHLC conflict requires fresh case review')
    if number(exchange['turnover_cny']) != number(relay['turnover_cny']):
        raise ValueError('exchange amount lacks exact compatible corroboration')
    volume_agrees = number(native['volume_shares']) == number(relay['volume_shares'])
    result = {'stock_code': code[:6], 'date': case['date'],
        'parent_record_id': case['parent_record_id'],
        'status': 'corrected_price_observation' if volume_agrees else 'partial_correction_volume_quarantined',
        'values': {**{k: exchange[k] for k in ('open', 'high', 'low', 'close', 'turnover_cny')},
            'volume_shares': str(number(native['volume_shares'])) if volume_agrees else None},
        'exchange_volume_whole_hands': exchange['volume_whole_hands'],
        'turnover_corrected': number(native['turnover_cny']) != number(exchange['turnover_cny']),
        'precise_volume_resolved': volume_agrees, 'source_evidence': case['evidence'],
        'exchange_evidence': evidence, 'point_in_time_qualified': False,
        'research_ready': False, 'execution_ready': False, 'production_cutover': False}
    result['record_id'] = identity(result)
    return result


def payload(parent, receipts, contracts, db):
    source_sha = file_hash(__file__)
    binding = conflict.parent(parent); members = sealed(receipts)
    if file_hash(db) != binding['database_sha256']: raise ValueError('frozen database differs')
    receipts, contracts = Path(receipts), Path(contracts)
    reg = read_json(receipts/'registration.json')[0]
    requests = [{'code': code, 'endpoint': ENDPOINT,
        'params': {'code': code[:6], 'cycleType': 32, 'marketId': 1}} for code in sorted(binding['cases'])]
    if (reg['analysis_sha256'] != binding['analysis_sha256'] or reg['requests'] != requests
            or reg['automatic_retries'] != 0 or reg['max_requests'] != len(requests)):
        raise ValueError('exact frozen exchange registration required')
    expected = {'registration.json', 'comparison.json',
        *(f'{kind}-{i:02d}.json' for kind in ('receipt', 'status') for i in range(len(requests)))}
    if set(members) != expected: raise ValueError('exact exchange evidence membership required')
    hashes = reg['contract_files']
    if hashes.get('szse-k.min.js') != CONTRACT_SHA:
        raise ValueError('pinned official chart unit/schema contract required')
    if any(Path(name).name != name or file_hash(contracts/name) != digest for name, digest in hashes.items()):
        raise ValueError('exchange contract differs')
    records = []
    for index, request in enumerate(requests):
        name = f'receipt-{index:02d}.json'; receipt = read_json(receipts/name)[0]
        parsed = parse_response(receipt, request, reg['started_at'])
        status = read_json(receipts/f'status-{index:02d}.json')[0]
        if status != {'index': index, 'code': request['code'], 'status': 'observed',
                      'chart_rows': len(parsed), 'case_rows': len(binding['cases'][request['code']])}:
            raise ValueError('exchange observed status differs')
        for case in binding['cases'][request['code']]:
            if case['date'] not in parsed: raise ValueError('missing registered exchange date')
            records.append(resolve_case(request['code'], case, parsed[case['date']],
                {'request': request, 'receipt_file': name, 'receipt_sha256': members[name],
                 'received_at': receipt['received_at'], 'contract_sha256': CONTRACT_SHA}))
    if (sealed(receipts) != members or conflict.parent(parent) != binding or file_hash(__file__) != source_sha
            or any(file_hash(contracts/name) != digest for name, digest in hashes.items())
            or file_hash(db) != binding['database_sha256']):
        raise ValueError('price correction inputs changed')
    return {'policy': POLICY, 'source_sha256': source_sha, 'database_sha256': binding['database_sha256'],
        'parent_sha256': binding['analysis_sha256'], 'receipt_manifest_id': identity(members),
        'contract_hashes': hashes, 'records': records,
        'summary': {'scoped_rows': len(records), 'turnover_corrected': sum(r['turnover_corrected'] for r in records),
            'complete_price_rows': sum(r['precise_volume_resolved'] for r in records),
            'volume_quarantined_rows': sum(not r['precise_volume_resolved'] for r in records)},
        'original_rows_deleted': 0, 'source_database_modified': False,
        'point_in_time_qualified': False, 'research_ready': False,
        'execution_ready': False, 'production_cutover': False}


def build(parent, receipts, contracts, db, output):
    output = separate(output, parent, receipts, contracts, db)
    result = payload(parent, receipts, contracts, db)
    output.mkdir(parents=True); write_json(output/'corrections.json', result); seal(output)
    return result


def verify(parent, receipts, contracts, db, output):
    members = sealed(output)
    if set(members) != {'corrections.json'}: raise ValueError('exact correction members required')
    result = read_json(Path(output)/'corrections.json')[0]
    if result != payload(parent, receipts, contracts, db) or sealed(output) != members:
        raise ValueError('corrections differ from raw replay')
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['build', 'verify'])
    for field in ('parent', 'receipts', 'contracts', 'db', 'output'): parser.add_argument('--'+field, required=True)
    args = vars(parser.parse_args()); action = args.pop('action')
    print(canonical(globals()[action](**args)['summary']))
