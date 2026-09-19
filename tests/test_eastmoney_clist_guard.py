from __future__ import annotations

import pytest

from trade_system.eastmoney_clist_guard import EastmoneyClistGuard, EastmoneyClistUnavailable


def test_clist_guard_cools_down_and_success_resets(tmp_path):
    guard = EastmoneyClistGuard(tmp_path / "guard.json")
    state = guard.record_failure("RemoteDisconnected", endpoint="https://82.push2.eastmoney.com/api/qt/clist/get")
    assert state["failures"] == 1
    assert state["last_endpoint"].startswith("https://82.")
    with pytest.raises(EastmoneyClistUnavailable):
        guard.assert_available()
    state = guard.record_success("https://push2.eastmoney.com/api/qt/clist/get")
    assert state["failures"] == 0
    guard.assert_available()


@pytest.mark.parametrize('post', [False, True])
def test_datacenter_preserves_request_and_null_values(monkeypatch, post):
    from urllib.parse import parse_qs, urlsplit
    from trade_system.adapters import eastmoney_dc as em
    calls = []
    def read(request, **limits):
        calls.append((request, limits))
        return b'{"data":[{"value":null,"zero":0}]}'
    monkeypatch.setattr(em, 'read_verified_once', read)
    result = em._em_get_json('https://push2.eastmoney.com/api/test', params={'code':'000001'},
                            data={'code':'000001'}, post=post, timeout=3)
    assert result == {'data':[{'value':None, 'zero':0}]}
    assert len(calls) == 1
    request, limits = calls[0]
    assert limits == {'timeout':3, 'max_bytes':8_000_000}
    assert request.get_method() == ('POST' if post else 'GET')
    assert urlsplit(request.full_url).netloc == 'push2.eastmoney.com'
    assert parse_qs(request.data.decode() if post else urlsplit(request.full_url).query) == {'code':['000001']}


@pytest.mark.parametrize('failure', [TimeoutError('expired'), ValueError('invalid response')])
def test_datacenter_failure_does_not_retry_other_hosts_or_http(monkeypatch, failure):
    from trade_system.adapters import eastmoney_dc as em
    calls = []
    def read(request, **limits):
        calls.append(request.full_url)
        raise failure
    def forbidden(*args, **kwargs):
        pytest.fail('adapter entered retired transport fallback')
    monkeypatch.setattr(em, 'read_verified_once', read)
    monkeypatch.setattr(em._u, 'urlopen', forbidden)
    import requests
    monkeypatch.setattr(requests, 'Session', forbidden)
    with pytest.raises(type(failure), match=str(failure)):
        em._em_get_json('https://push2.eastmoney.com/api/test')
    assert calls == ['https://push2.eastmoney.com/api/test']


@pytest.mark.parametrize('spent,expected_calls', [(0.6, 2), (1.1, 1)])
def test_clist_routes_share_budget_and_keep_source_identity(monkeypatch, spent, expected_calls):
    from types import SimpleNamespace
    from trade_system.adapters import eastmoney_dc as em
    clock = [100.0]; calls = []; failures = []; successes = []
    monkeypatch.setattr(em.time, 'monotonic', lambda: clock[0])
    for name in ('DEFAULT_CLIST_GUARD', 'DELAY_CLIST_GUARD'):
        monkeypatch.setattr(em, name, SimpleNamespace(assert_available=lambda: None,
            record_failure=lambda error, endpoint: failures.append(endpoint),
            record_success=lambda endpoint: successes.append(endpoint)))
    def request(url, params, *, timeout):
        calls.append((url, timeout))
        if len(calls) == 1:
            clock[0] += spent
            raise TimeoutError('primary failed')
        return {'data':{'diff':[{'f12':'000001','f2':None}]}}
    monkeypatch.setattr(em, '_em_get_json', request)
    if expected_calls == 1:
        with pytest.raises(RuntimeError):em._em_get_clist_json(timeout=1)
        assert not successes
    else:
        result = em._em_get_clist_json(timeout=1)
        assert result['_clist_source'] == 'eastmoney_delay'
        assert result['data']['diff'][0]['f2'] is None
        assert calls[1][1] == pytest.approx(0.4)
        assert successes == [calls[1][0]]
    assert len(calls) == expected_calls
    assert failures == [calls[0][0]]


@pytest.mark.parametrize('failure', [TimeoutError('expired'), ValueError('invalid')])
def test_finance_report_does_not_multiply_failed_requests(monkeypatch, failure):
    from trade_system import eastmoney_finance as em
    calls = []
    def read(*a, **kw):
        calls.append(a)
        raise failure
    monkeypatch.setattr(em, 'read_verified_once', read)
    with pytest.raises(type(failure)):
        em.get_income_statement('000001')
    assert len(calls) == 1


def test_finance_fallback_and_pagination_share_original_budget(monkeypatch, tmp_path):
    import time
    from trade_system import eastmoney_finance as em
    from trade_system.http_transport import request_deadline, request_budget
    for name in ('DEFAULT_CLIST_GUARD', 'DELAY_CLIST_GUARD'):
        monkeypatch.setattr(em, name, EastmoneyClistGuard(tmp_path / (name+'.json')))
    monkeypatch.setattr(em.shared_host_limiter, 'acquire', lambda *a, **kw: None)
    deadlines = []
    def read(url, **kw):
        deadlines.append(request_deadline.get())
        if 'delay' not in url:
            raise ValueError('primary failed')
        return {'data': {'total': 21, 'diff': [{'f12': '000001'}]}}
    monkeypatch.setattr(em, '_read_json', read)
    assert request_deadline.get() is None
    with request_budget(5):
        original = request_deadline.get()
        _, meta = em.get_fund_flow_market_realtime('2026-07-15', page_size=20, max_pages=2, pause_seconds=0)
        assert deadlines == [original, original, original]
        assert meta['source'] == 'eastmoney_intraday_clist_delay'
        assert request_deadline.get() == original
    assert request_deadline.get() is None
    token = request_deadline.set(time.monotonic()-1)
    try:
        with pytest.raises(TimeoutError):
            em.get_fund_flow_market_realtime('2026-07-15')
        assert len(deadlines) == 3
    finally:
        request_deadline.reset(token)
