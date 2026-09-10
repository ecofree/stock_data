import json
from types import SimpleNamespace

import pytest

from trade_system.v2.domain import canonical, utc
from trade_system.v2.gap_audit import seal
from trade_system.v2.gap_evidence import write_json
from trade_system.v2 import native_gap_sources as source
from trade_system.v2 import native_gap_run as run


CASE = {'case_id': 'case-one', 'instrument': 'SZ.000609', 'date': '2026-04-22', 'reasons': ['missing_frozen_raw_bar']}
NOW = '2026-09-10T12:00:00+08:00'


def payload(api, rows):
    if api.startswith('hithink'):
        data = {'item': rows, 'thscode': '000609.SZ', 'ticker': '000609'}
    else:
        fields = source.FIELDS[api].split(',')
        data = {'fields': fields, 'items': [[r.get(k) for k in fields] for r in rows]}
    return canonical({'code': 0, 'data': data}).encode()


def convert(api, rows):
    return source.convert_response(source.request_spec(CASE, api), payload(api, rows))


def price():
    return {'ts_code': '000609.SZ', 'trade_date': '20260422', 'open': 10, 'high': 11, 'low': 9, 'close': 10.5,
            'vol': 100, 'amount': 105}


def dividend():
    return {'ts_code': '000609.SZ', 'ex_date': '20260422', 'div_proc': '实施', 'cash_div_tax': .2, 'stk_div': 0}


def test_exact_request_identity_dates_raw_adjust_and_order():
    spec = source.request_spec(CASE, 'hithink_daily')
    assert spec['params']['thscode'] == '000609.SZ' and spec['params']['adjust'] == 'none'
    assert source.day_ms(spec['params']['start']) == CASE['date']
    assert source.day_ms(spec['params']['end']) == CASE['date']
    assert [r['api'] for r in source.build_requests([CASE])] == ['hithink_daily', 'daily', 'suspend_d', 'stock_basic']
    with pytest.raises(ValueError):
        source.request_spec({**CASE, 'instrument': '000609'}, 'daily')
    with pytest.raises(ValueError):
        source.request_spec(CASE, 'delete_portfolio')
    with pytest.raises(ValueError):
        source.build_requests([CASE]*12)


def test_both_raw_price_shapes_and_no_synthetic_volume():
    r = convert('daily', [price()])
    assert r['claims'][0]['claim']['value']['close'] == '10.5'
    bar = {'date_ms': source.ms(CASE['date']), **{k+'_price': v for k, v in price().items() if k in ('open','high','low','close')}}
    assert convert('hithink_daily', [bar])['claims'][0]['claim'] == r['claims'][0]['claim']
    assert convert('daily', [{**price(), 'open': 0}])['status'] == 'empty_or_unresolved'


@pytest.mark.parametrize('api,row', [
    ('daily', {**price(), 'ts_code': '600193.SH'}), ('daily', {**price(), 'trade_date': '20260423'}),
    ('daily', {**price(), 'close': 99}), ('daily', {**price(), 'close': 10.123}),
    ('dividend', {**dividend(), 'ex_date': '20260423'}), ('dividend', {**dividend(), 'cash_div_tax': None}),
    ('suspend_d', {'ts_code': '000609.SZ', 'trade_date': '20260423', 'suspend_type': 'S', 'suspend_timing': None})])
def test_native_identity_date_and_semantics_fail_closed(api, row):
    with pytest.raises(ValueError):
        convert(api, [row])


@pytest.mark.parametrize('change', ['missing_fields', 'short_row', 'duplicate_fields', 'noninteger_code', 'no_data', 'duplicate_json'])
def test_malformed_envelope_not_silently_normalized(change):
    p = json.loads(payload('daily', [price()]))
    if change == 'missing_fields':
        p['data']['fields'].pop(); p['data']['items'][0].pop()
    elif change == 'short_row':
        p['data']['items'][0].pop()
    elif change == 'duplicate_fields':
        p['data']['fields'][1] = p['data']['fields'][0]
    elif change == 'noninteger_code':
        p['code'] = False
    elif change == 'no_data':
        p['data'] = None
    raw = canonical(p).encode() if change != 'duplicate_json' else b'{"code":0,"code":1}'
    with pytest.raises(ValueError):
        source.convert_response(source.request_spec(CASE, 'daily'), raw)


def test_hithink_adjustment_and_action_exchange_identity():
    spec = source.request_spec(CASE, 'hithink_daily')
    spec['params']['adjust'] = 'forward'
    with pytest.raises(ValueError):
        source.convert_response(spec, payload('hithink_daily', []))
    p = json.loads(payload('hithink_actions', [])); p['data'].pop('thscode')
    with pytest.raises(ValueError):
        source.convert_response(source.request_spec(CASE, 'hithink_actions'), canonical(p).encode())


def test_suspension_scope_and_resumption_not_trade_proof():
    row = {'ts_code': '000609.SZ', 'trade_date': '20260422', 'suspend_type': 'S', 'suspend_timing': None}
    assert convert('suspend_d', [row])['claims'][0]['claim']['value']['coverage'] == 'full_session'
    assert convert('suspend_d', [{**row, 'suspend_timing': '09:30-10:00'}])['claims'][0]['claim']['value']['coverage'] == 'intraday'
    assert convert('suspend_d', [{**row, 'suspend_type': 'R'}])['claims'] == []
    assert convert('suspend_d', [])['claims'] == []


def test_cash_gross_not_net_and_bonus_is_not_split():
    r = convert('dividend', [{**dividend(), 'cash_div_tax': '0.2000', 'stk_div': .1}])
    c = r['claims'][0]['claim']
    assert c['value']['gross_cny_per_share'] == '0.2' and len(r['claims']) == 1
    assert 'not_instant_split' in r['notes'][0]['reason']
    h = convert('hithink_actions', [{'ticker': '000609', 'ex_date_ms': source.ms(CASE['date']), 'dividend_per_share': .2, 'per_share_bonus': 0}])
    assert h['claims'][0]['claim'] == c
    assert convert('dividend', [{**dividend(), 'div_proc': '预案'}])['claims'] == []


def test_duplicate_rows_and_conflicting_revision_preserved():
    r = convert('dividend', [dividend(), dividend(), {**dividend(), 'cash_div_tax': .3}])
    assert len(r['claims']) == 2 and r['notes'][0]['reason'] == 'duplicate_native_row'


def test_delisting_effective_date_not_future_backfill():
    row = {'ts_code': '000609.SZ', 'list_status': 'D', 'list_date': '20000101', 'delist_date': '20260401'}
    assert convert('stock_basic', [row])['claims'][0]['claim']['value']['status'] == 'delisted'
    assert convert('stock_basic', [{**row, 'delist_date': '20260430'}])['claims'] == []


@pytest.fixture
def audit_folder(tmp_path):
    folder = tmp_path/'audit'
    folder.mkdir()
    write_json(folder/'report.json', {'cases': [CASE]})
    seal(folder, {'fixture': True})
    return folder


def fake_fetch(spec, secret):
    rows = [price()] if spec['api'] == 'daily' else []
    return {'http_status': 200, 'transport_error': None, 'raw': payload(spec['api'], rows)}


def test_native_bridge_reproduction_and_candidate_adjudication(audit_folder, tmp_path):
    out = tmp_path/'native'
    r = run.collect(audit_folder, out, fetch=fake_fetch, secrets={'hithink_official': 'key-a', 'xiaodefa_tushare': 'key-b'}, clock=lambda: utc(NOW))
    assert r['transport_attempts'] == 4 and r['evidence_count'] == 1
    assert run.verify_collection(out) == r
    decision = json.loads((out/'adjudication/report.json').read_text())
    assert decision['classification_counts'] == {'raw_bar_candidate_not_authenticated': 1}
    assert not decision['portfolio_resumed']
    with pytest.raises(FileExistsError):
        run.collect(audit_folder, out, secrets={})


@pytest.mark.parametrize('mode', ['credential_echo', 'http_redirect', 'transport_error', 'business_error'])
def test_failures_not_empty_market_evidence(audit_folder, tmp_path, mode):
    def fetch(spec, secret):
        return {'http_status': 302 if mode == 'http_redirect' else 200,
                'transport_error': 'fixture_network_error' if mode == 'transport_error' else None,
                'raw': secret.encode() if mode == 'credential_echo' else b'{"code":5003,"data":null}'}
    out = tmp_path/mode
    r = run.collect(audit_folder, out, fetch=fetch, secrets={'hithink_official': 'secret-example', 'xiaodefa_tushare': 'secret-example'}, clock=lambda: utc(NOW))
    assert r['evidence_count'] == 0 and run.verify_collection(out) == r
    if mode == 'credential_echo':
        assert not list(out.rglob('native.json'))
    assert all(b'secret-example' not in p.read_bytes() for p in out.rglob('*') if p.is_file())


def test_no_credentials_or_wall_budget_does_not_fetch(audit_folder, tmp_path):
    def unexpected(*args):
        raise AssertionError('must not fetch')
    r = run.collect(audit_folder, tmp_path/'none', fetch=unexpected, secrets={}, clock=lambda: utc(NOW))
    assert r['transport_attempts'] == 0
    r = run.collect(audit_folder, tmp_path/'budget', fetch=unexpected, secrets={'hithink_official': 'x', 'xiaodefa_tushare': 'y'}, clock=lambda: utc(NOW), wall_seconds=1)
    assert r['transport_attempts'] == 0


def test_endpoint_circuit_breaker_prevents_repeated_permission_calls(audit_folder, tmp_path):
    data = {'cases': [CASE, {**CASE, 'case_id': 'case-two', 'instrument': 'SH.600193'}]}
    # New immutable fixture audit, do not overwrite the original manifest.
    p = tmp_path/'two'; p.mkdir(); write_json(p/'report.json', data); seal(p, {'fixture': True})
    def fetch(*args):
        return {'http_status': 200, 'transport_error': None, 'raw': b'{"code":5003}'}
    r = run.collect(p, tmp_path/'native', fetch=fetch, secrets={'hithink_official': 'abc', 'xiaodefa_tushare': 'abc'}, clock=lambda: utc(NOW))
    assert r['transport_attempts'] == 4 and r['status_counts']['endpoint_circuit_open'] == 4


def test_transport_secrets_only_stdin_no_redirects(monkeypatch):
    observed = {}
    def subprocess_run(args, **kwargs):
        observed.update(args=args, **kwargs)
        return SimpleNamespace(returncode=0, stdout=b'{"code":0}\n200', stderr=b'')
    monkeypatch.setattr(run.subprocess, 'run', subprocess_run)
    monkeypatch.setattr(run, 'reserve_rate_slot', lambda _: None)
    for api in ('daily', 'hithink_daily'):
        r = run.http_fetch(source.request_spec(CASE, api), 'secret-value')
        assert r['http_status'] == 200 and b'secret-value' in observed['input']
        assert 'secret-value' not in repr(observed['args'])
        assert '--location' not in observed['args'] and '-k' not in observed['args']
        assert observed['timeout'] == 18


def test_manifest_and_derived_mapping_tampering(audit_folder, tmp_path):
    out = tmp_path/'native'
    run.collect(audit_folder, out, fetch=fake_fetch, secrets={'hithink_official': 'a-key', 'xiaodefa_tushare': 'b-key'}, clock=lambda: utc(NOW))
    (out/'requests/01/structured.json').write_text('{}')
    with pytest.raises(ValueError, match='hash'):
        run.verify_collection(out)


def test_rate_limit_is_source_wide(audit_folder, tmp_path):
    def limited(*args):
        return {'http_status': 429, 'transport_error': None, 'raw': b'{"code":-1}'}
    r = run.collect(audit_folder, tmp_path/'limited', fetch=limited,
                    secrets={'hithink_official': 'key-a', 'xiaodefa_tushare': 'key-b'}, clock=lambda: utc(NOW))
    assert r['transport_attempts'] == 2 and r['status_counts']['source_circuit_open'] == 2


def test_unknown_instrument_does_not_disable_other_cases(tmp_path):
    a = tmp_path/'audit'; a.mkdir()
    write_json(a/'report.json', {'cases': [CASE, {**CASE, 'case_id': 'second', 'instrument': 'SH.600193'}]})
    seal(a, {})
    def unknown(*args):
        return {'http_status': 200, 'transport_error': None, 'raw': b'{"code":1002}'}
    r = run.collect(a, tmp_path/'native', fetch=unknown, secrets={'hithink_official': 'key-a'}, clock=lambda: utc(NOW))
    assert r['transport_attempts'] == 2


def test_single_bounded_continuation_preserves_old_evidence_and_no_repeat(audit_folder, tmp_path):
    keys = {'hithink_official': 'key-a', 'xiaodefa_tushare': 'key-b'}
    first = tmp_path/'first'
    run.collect(audit_folder, first, fetch=fake_fetch, secrets=keys, clock=lambda: utc(NOW))
    second = tmp_path/'second'
    def no_call(*args):
        raise AssertionError('existing candidate should not be fetched again')
    r = run.collect(audit_folder, second, fetch=no_call, secrets=keys, clock=lambda: utc(NOW), resume_from=first)
    assert r['transport_attempts'] == 0 and r['cumulative_transport_attempts'] == 4 and r['evidence_count'] == 1
    assert run.verify_collection(second) == r
    with pytest.raises(ValueError, match='one bounded'):
        run.collect(audit_folder, tmp_path/'third', secrets=keys, clock=lambda: utc(NOW), resume_from=second)


def test_rate_continuation_requires_minimum_cooldown(audit_folder, tmp_path):
    def limited(*args):
        return {'http_status': 429, 'transport_error': None, 'raw': b'{"code":-1}'}
    first = tmp_path/'first'
    keys = {'hithink_official': 'key-a', 'xiaodefa_tushare': 'key-b'}
    run.collect(audit_folder, first, fetch=limited, secrets=keys, clock=lambda: utc(NOW))
    r = run.collect(audit_folder, tmp_path/'second', fetch=limited, secrets=keys, clock=lambda: utc(NOW), resume_from=first)
    assert r['status_counts']['rate_cooldown_not_elapsed'] == 4 and r['transport_attempts'] == 0
