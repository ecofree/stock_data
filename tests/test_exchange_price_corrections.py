from copy import deepcopy
import json

import pytest

from tools.v2 import resolve_exchange_price_conflicts as p


def fixture():
    request = {'code': '000001.SZ', 'endpoint': p.ENDPOINT,
        'params': {'code': '000001', 'cycleType': 32, 'marketId': 1}}
    raw = {'code': '0', 'data': {'code': '000001', 'picupdata':
        [['2025-11-27', '10', '11', '9', '12', '1', '10', 101, 12345.25]]}}
    receipt = {'request': request, 'received_at': '2026-09-11T00:00:00+00:00', 'raw_response': json.dumps(raw)}
    source = dict(open='10', close='11', low='9', high='12', volume_shares='10110', turnover_cny='12345.25')
    case = {'date': '2025-11-27', 'parent_record_id': 'synthetic', 'evidence':
        [{'provider': 'hithink_native', 'values': {**source, 'turnover_cny': '123'}},
         {'provider': 'xiaodefa_relay', 'values': source}]}
    return request, raw, receipt, case


def test_amount_corrected_while_originals_and_exact_volume_preserved():
    request, _, receipt, case = fixture(); original = deepcopy(case)
    values = p.parse_response(receipt, request, '2026-09-10T00:00:00+00:00')['2025-11-27']
    result = p.resolve_case(request['code'], case, values, {})
    assert result['values']['turnover_cny'] == '12345.25'
    assert result['values']['volume_shares'] == '10110'  # NOT displayed hands * 100
    assert result['status'] == 'corrected_price_observation'
    assert not result['research_ready'] and case == original


def test_disputed_volume_stays_null_even_if_relay_close_to_exchange_hands():
    request, _, receipt, case = fixture()
    case['evidence'][0]['values']['volume_shares'] = '1011'
    values = p.parse_response(receipt, request, '2026-09-10T00:00:00+00:00')['2025-11-27']
    result = p.resolve_case(request['code'], case, values, {})
    assert result['values']['volume_shares'] is None
    assert result['values']['turnover_cny'] == '12345.25'
    assert result['status'] == 'partial_correction_volume_quarantined'


@pytest.mark.parametrize('mode', ['identity', 'code', 'duplicate_day', 'negative', 'fractional_hand', 'shape', 'duplicate_json', 'time', 'request'])
def test_invalid_exchange_response_rejected(mode):
    request, raw, receipt, _ = fixture()
    if mode == 'identity': raw['data']['code'] = '000002'
    if mode == 'code': raw['code'] = '1'
    if mode == 'duplicate_day': raw['data']['picupdata'] *= 2
    if mode == 'negative': raw['data']['picupdata'][0][8] = -1
    if mode == 'fractional_hand': raw['data']['picupdata'][0][7] = 1.1
    if mode == 'shape': raw['data']['picupdata'][0].pop()
    if mode == 'time': receipt['received_at'] = '2999-01-01T00:00:00Z'
    if mode == 'request': receipt['request'] = {**request, 'endpoint': 'untrusted'}
    receipt['raw_response'] = json.dumps(raw)
    if mode == 'duplicate_json': receipt['raw_response'] = '{"code":"0","code":"1"}'
    with pytest.raises(ValueError): p.parse_response(receipt, request, '2026-09-10T00:00:00+00:00')


@pytest.mark.parametrize('field', ['open', 'high', 'low', 'close', 'turnover_cny'])
def test_correction_cannot_hide_new_field_disagreement(field):
    request, _, receipt, case = fixture()
    values = p.parse_response(receipt, request, '2026-09-10T00:00:00+00:00')['2025-11-27']
    values[field] = str(p.number(values[field])+1)
    with pytest.raises(ValueError): p.resolve_case(request['code'], case, values, {})


def test_resealed_modified_correction_fails_raw_replay(tmp_path, monkeypatch):
    expected = {'summary': {}, 'records': [{'values': {'turnover_cny': '100'}}]}
    monkeypatch.setattr(p, 'payload', lambda *args: deepcopy(expected))
    output = tmp_path/'new'
    args = [tmp_path/'parent', tmp_path/'receipts', tmp_path/'contracts', tmp_path/'db', output]
    p.build(*args)
    assert p.verify(*args) == expected
    changed = deepcopy(expected); changed['records'][0]['values']['turnover_cny'] = '1'
    (output/'corrections.json').unlink(); (output/'completed.json').unlink()
    p.write_json(output/'corrections.json', changed); p.seal(output)
    with pytest.raises(ValueError, match='raw replay'): p.verify(*args)
