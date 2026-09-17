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
