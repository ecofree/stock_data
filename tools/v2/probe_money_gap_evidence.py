"""Offline targeted money-gap evidence replay; never imports or merges rows."""
import argparse
from datetime import datetime
from pathlib import Path
import json
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tools.v2 import probe_identity_sources as old
from tools.v2.probe_price_conflicts import normalize
from trade_system.v2.daily_session import CST, seal
from trade_system.v2.domain import file_hash, identity, now_utc, number, utc
from trade_system.v2.gap_evidence import read_json, write_json
from trade_system.v2.research_receipts import sealed, verify

SCOPE = 'targeted_money_missingness_not_repair_or_identity_merge_authority'


def plan():
    requests = []
    for code in ['603595.SH', '689009.SH']:
        start, end = '2024-01-02', '2024-01-22'
        stamp = lambda d: int(datetime.fromisoformat(d).replace(tzinfo=CST).timestamp()*1000)
        requests.append(dict(provider='hithink_native', api=old.PRICES, code=code,
            start=start, end=end, params=dict(thscode=code, interval='1d', start=stamp(start),
            end=stamp(end)+86400000-1, adjust='none', offset=0)))
    for code, api, start, end in [
        ('603595.SH', 'daily', '2024-01-02', '2024-01-22'),
        ('689009.SH', 'daily', '2024-01-02', '2024-01-22'),
        ('603595.SH', 'moneyflow', '2024-01-02', '2024-01-22'),
        ('603595.SH', 'moneyflow', '2024-01-08', '2024-01-08'),
        ('689009.SH', 'moneyflow', '2024-01-02', '2024-01-22'),
        ('689009.SH', 'moneyflow', '2024-01-18', '2024-01-18')]:
        requests.append(dict(provider='xiaodefa_relay', api=api, code=code, start=start,
            end=end, params=dict(ts_code=code, start_date=start.replace('-', ''),
                                end_date=end.replace('-', ''))))
    return requests


def replay(folder, collection_id, identity_id, capture_script):
    folder = Path(folder); members = sealed(folder)
    expected = {'registration.json', 'result.json',
        *(f'{kind}-{i:02d}.json' for kind in ('status', 'receipt') for i in range(8))}
    if set(members) != expected:
        raise ValueError('exact eight-response package required')
    reg = read_json(folder/'registration.json')[0]
    bindings = dict(scope=SCOPE, requests=plan(), max_requests=8, automatic_retries=0,
        collection_manifest_id=collection_id, identity_manifest_id=identity_id,
        source_sha256=file_hash(capture_script), execution_ready=False, production_cutover=False)
    if (any(reg.get(k) != v for k, v in bindings.items())
        or reg.get('execution_ready') is not False or reg.get('production_cutover') is not False):
        raise ValueError('capture registration binding differs')
    parsed = {}; empty = []
    for i, request in enumerate(plan()):
        receipt = read_json(folder/f'receipt-{i:02d}.json')[0]
        status = read_json(folder/f'status-{i:02d}.json')[0]
        if (receipt.get('request') != request or status.get('request') != request
            or type(status.get('index')) is not int or status['index'] != i or status.get('receipt_sha256') != members[f'receipt-{i:02d}.json']
            or not utc(reg['received_after']) <= utc(receipt['received_at']) <= now_utc()):
            raise ValueError('response request/time/hash differs')
        data = receipt['data']
        if status.get('status') == 'response_contract_rejected':
            if (i not in (5, 7) or data != {'fields': [], 'items': []}
                or status.get('error_type') != 'ValueError' or status.get('empty_payload') is not True
                or 'rows' in status or 'dates' in status):
                raise ValueError('only exact observed empty payload rejection supported')
            parsed[i] = {}; empty.append(i)
        elif status.get('status') == 'observed':
            rows = normalize(request, data)
            if type(status.get('rows')) is not int or len(rows) != status['rows'] or sorted(rows) != status.get('dates'):
                raise ValueError('response row count/date derivation differs')
            parsed[i] = rows
        else:
            raise ValueError('unrecognized response status')
    return members, parsed, empty


def replay_identity(folder):
    folder = Path(folder); members = sealed(folder)
    reg = read_json(folder/'registration.json')[0]
    if (reg.get('requests') != old.plan() or reg.get('max_requests') != 16
        or reg.get('retries') != 0 or reg.get('execution_ready') is not False
        or reg.get('origin') != 'native_and_relay'
        or reg.get('scope') != 'retrospective_identity_source_probe_not_merge_authority'):
        raise ValueError('legacy identity registration differs')
    result = {}
    for i in (6, 9, 12, 15):
        receipt = read_json(folder/f'receipt-{i:02d}.json')[0]
        status = read_json(folder/f'status-{i:02d}.json')[0]
        request = old.plan()[i]
        if (receipt.get('request') != request or type(status.get('index')) is not int or status['index'] != i
            or not utc(reg['received_after']) <= utc(receipt['received_at']) <= now_utc()):
            raise ValueError('legacy money response binding differs')
        if i == 9:
            if receipt['data'] != {'fields': [], 'items': []} or status.get('status') != 'failed' or status.get('error_type') != 'ValueError':
                raise ValueError('legacy new-code empty response differs')
            result[i] = {}
        else:
            values = old.rows_for(request, receipt['data'])
            if status.get('status') != 'observed' or type(status.get('rows')) is not int or status['rows'] != len(values):
                raise ValueError('legacy money row count differs')
            result[i] = values
    return members, result


def money_diff(broad, targeted):
    errors = []
    for key in old.API_FIELDS['moneyflow'][2:]:
        a, b = broad[key], targeted[key]
        if (a is None) != (b is None) or (a is not None and number(a) != number(b)*10000):
            errors.append(key)
    return errors


def calculate(days, baseline, parsed, identities):
    cases = []; common = 0; mismatches = 0; positive = 0; gaps = 0; price_mismatches = 0; max_amount = number(0)
    for code, native, daily, money in [('603595.SH', 0, 2, 4), ('689009.SH', 1, 3, 6)]:
        rows = []
        for day in [d for d in days if '2024-01-02' <= d <= '2024-01-22']:
            broad = baseline.get((code, day)); targeted = parsed[money].get(day)
            a, b = parsed[native].get(day), parsed[daily].get(day)
            fields = money_diff(broad, targeted) if broad is not None and targeted is not None else []
            common += int(broad is not None and targeted is not None); mismatches += bool(fields)
            price_equal = a is not None and b is not None and all(number(a[k]) == number(b[k]) for k in ('open', 'high', 'low', 'close', 'volume_shares'))
            price_mismatches += not price_equal
            if a is not None and b is not None:
                max_amount = max(max_amount, abs(number(a['turnover_cny'])-number(b['turnover_cny'])))
            missing = broad is None and targeted is None
            has_volume = a is not None and b is not None and number(a['volume_shares']) > 0 and number(b['volume_shares']) > 0
            gaps += missing; positive += missing and has_volume
            rows.append(dict(date=day, broad_money_present=broad is not None, targeted_money_present=targeted is not None,
                common_money_mismatches=fields, missing_both=missing, positive_volume_both=has_volume,
                native_relay_ohlcv_equal=price_equal, native_price=a, relay_price=b))
        cases.append(dict(code=code, rows=rows))
    old_rows = []
    for day, row in sorted(identities[6].items()):
        broad = baseline.get(('300114.SZ', day))
        old_rows.append(dict(date=day, broad_present=broad is not None,
            mismatches=money_diff(broad, row) if broad is not None else ['missing_broad_row']))
    transition = {'old_dates': sorted(identities[12]), 'new_dates': sorted(identities[15]),
        'overlap': sorted(set(identities[12]) & set(identities[15])),
        'effective_date_boundary_matches': bool(identities[12]) and bool(identities[15])
          and all(d < '2025-02-17' for d in identities[12]) and all(d >= '2025-02-17' for d in identities[15])}
    return dict(cases=cases, old_code_against_all_market=old_rows, transition=transition,
        summary=dict(missing_both_rows=gaps, missing_with_positive_volume_rows=positive,
            shared_money_rows=common, shared_money_mismatch_rows=mismatches, price_mismatch_rows=price_mismatches,
            max_price_amount_difference_cny=str(max_amount), old_code_rows=len(old_rows),
            old_code_mismatch_rows=sum(bool(r['mismatches']) for r in old_rows)))


def analyze(receipts, collection, identity_folder, capture_script, output):
    output = Path(output).resolve()
    inputs = [Path(p).resolve() for p in (receipts, collection, identity_folder, capture_script)]
    if output.exists() or any(output == p or output in p.parents or p in output.parents for p in inputs):
        raise ValueError('separate new output namespace required')
    code_hash = file_hash(__file__); script_hash = file_hash(capture_script)
    collection_members = sealed(collection); identity_members, identities = replay_identity(identity_folder)
    reg, data, meta = verify(collection)
    if meta['origin'] != 'xiaodefa_relay':
        raise ValueError('real relay collection required')
    members, parsed, empty = replay(receipts, identity(collection_members), identity(identity_members), capture_script)
    baseline = {(r['ts_code'], r['date']): r for r in data['moneyflow']}
    result = calculate(reg['days'], baseline, parsed, identities)
    result.update(scope=SCOPE, receipt_manifest_id=identity(members), collection_manifest_id=identity(collection_members),
        identity_manifest_id=identity(identity_members), source_sha256=code_hash, capture_source_sha256=script_hash,
        rejected_empty_response_indices=empty, original_derived_result_used=False, new_money_rows_imported=0,
        identity_merge_authorized=False, historical_PIT_qualified=False, research_ready=False,
        execution_ready=False, production_cutover=False)
    if (members != sealed(receipts) or collection_members != sealed(collection) or identity_members != sealed(identity_folder)
        or code_hash != file_hash(__file__) or script_hash != file_hash(capture_script)):
        raise ValueError('inputs changed during replay')
    # Re-verify referenced child receipt packages, not only the collection index.
    if verify(collection)[2] != meta:
        raise ValueError('collection children changed during replay')
    result['inputs_unchanged'] = True
    output.mkdir(parents=True, exist_ok=False)
    write_json(output/'result.json', result); seal(output)
    return result['summary']


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    for name in ('receipts', 'collection', 'identity', 'capture-script', 'output'):
        p.add_argument('--'+name, required=True)
    a = p.parse_args()
    print(json.dumps(analyze(a.receipts, a.collection, a.identity, a.capture_script, a.output)))
