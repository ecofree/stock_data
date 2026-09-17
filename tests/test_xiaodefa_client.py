import urllib.error
import threading

import pytest

from trade_system.xiaodefa_source import XiaodefaClient, XiaodefaError


def envelope(rows, fields=('ts_code', 'trade_date', 'close')):
    return {'code': 0, 'data': {'fields': list(fields), 'items': rows}}


@pytest.mark.parametrize('url', [
    'https://fastapic.stockai888.top', 'http://t.xiaodefa.top/',
    'https://t.xiaodefa.top.evil.test/', 'https://user@t.xiaodefa.top/',
    'https://t.xiaodefa.top/?redirect=other', 'https://t.xiaodefa.top:444/',
])
def test_unapproved_destination_refused_before_transport(url):
    calls = []
    with pytest.raises(XiaodefaError, match='endpoint'):
        XiaodefaClient(token='fixture', url=url, runner=lambda *a: calls.append(a))
    assert not calls


def test_missing_retained_credential_never_opens_database(tmp_path, monkeypatch):
    from trade_system import xiaodefa_source as source
    from trade_system.tushare_history import TushareHistoryCollector
    monkeypatch.setattr(source, 'SETTINGS', {'TUSHARE_FAST_RELAY_TOKEN': 'old-token'})
    monkeypatch.setenv('TUSHARE_PRIMARY_PROVIDER', 'fast')
    database = tmp_path / 'must-not-exist.duckdb'
    with pytest.raises(XiaodefaError, match='missing token'):
        TushareHistoryCollector(database)
    assert not database.exists()


@pytest.mark.parametrize('payload', [
    {'code': 401, 'data': {}}, {'code': 0},
    envelope([['000001.SZ', '20260916']]),
    envelope([['000001.SZ', '20260916', float('nan')]]),
    envelope([['000001.SZ', '20260916', float('inf')]]),
    envelope([['000001.SZ', '20260916', 12]], ('ts_code', 'ts_code', 'close')),
    envelope([['000001.SZ', '20260916', 12]], ('ts_code', 'date', 'close')),
])
def test_invalid_envelope_has_one_attempt_and_no_fallback(payload):
    calls = []
    def request(*args):
        calls.append(args)
        return payload
    client = XiaodefaClient(token='fixture', runner=request)
    with pytest.raises(XiaodefaError):
        client.query_rows('daily', fields='ts_code,trade_date,close')
    assert len(calls) == 1


@pytest.mark.parametrize('code, attempts', [(301, 1), (403, 1), (429, 3), (503, 3)])
def test_http_retry_budget_and_redirect_refusal(code, attempts, monkeypatch):
    from trade_system import xiaodefa_source as source
    monkeypatch.setattr(source.time, 'sleep', lambda _: None)
    calls = []
    def request(*args):
        calls.append(args)
        raise urllib.error.HTTPError('https://t.xiaodefa.top/', code, 'fixture', {}, None)
    client = XiaodefaClient(token='fixture', runner=request)
    with pytest.raises(XiaodefaError, match='HTTP'):
        client.query('daily')
    assert len(calls) == attempts


def test_page_budget_is_not_a_completeness_claim():
    client = XiaodefaClient(token='fixture', runner=lambda body, _: envelope(
        [[str(body['params']['offset']), '20260916', 12]]))
    with pytest.raises(XiaodefaError, match='budget exhausted'):
        client.query_all('daily', page_size=1, max_rows=2)


def test_repeated_page_cannot_be_published_as_complete():
    client = XiaodefaClient(token='fixture', runner=lambda *_: envelope([['000001.SZ', '20260916', 12]]))
    with pytest.raises(XiaodefaError, match='repeated page'):
        client.query_all('daily', page_size=1, max_rows=5)


def test_explicit_terminal_page_completes_request():
    client = XiaodefaClient(token='fixture', runner=lambda body, _: envelope(
        [['000001.SZ', '20260916', 12]] if body['params']['offset'] == 0 else []))
    assert client.query_all('daily', page_size=1, max_rows=5) == [
        {'ts_code': '000001.SZ', 'trade_date': '20260916', 'close': 12}]


def test_fixed_case_collector_cannot_reactivate_retired_network():
    from trade_system.v2.native_gap_run import http_fetch
    with pytest.raises(ValueError, match='retired'):
        http_fetch({'url': 'https://fastapic.stockai888.top'}, 'old-token')


def test_receipt_consumers_use_the_same_transport(monkeypatch):
    from trade_system.v2.research_campaign import Client
    calls = []
    monkeypatch.setattr(XiaodefaClient, 'query_data', lambda self, *a: calls.append(a) or {'fields': [], 'items': []})
    client = Client.__new__(Client)
    client.relay = XiaodefaClient(token='fixture')
    result = client.query({'provider': 'xiaodefa_relay', 'api': 'trade_cal', 'params': {'exchange': 'SSE'}})
    assert len(calls) == 1 and result == {'fields': [], 'items': []}


@pytest.fixture
def acquisition(tmp_path, monkeypatch):
    from trade_system import resilient_sources as source
    class Cache:
        values = {}
        def get(self, key):
            return self.values.get(key, (None, 0))
        def put(self, key, value):
            self.values[key] = (value, value['received_at'])
    monkeypatch.setattr(source, 'CACHE_DIR', str(tmp_path))
    monkeypatch.setattr(source, 'cache', Cache())
    monkeypatch.setattr(source.health, 'is_cooldown', lambda *a: False)
    monkeypatch.setattr(source.health, 'record', lambda *a: None)
    monkeypatch.setattr(source.rate, 'acquire', lambda *a: None)
    monkeypatch.setattr(source.rate, 'release', lambda *a: None)
    return source


def test_two_consumers_reuse_receipt_without_refreshing_time(acquisition, monkeypatch):
    calls = []
    monkeypatch.setitem(acquisition.SOURCE_PLAN, 'fixture', [('fixture', lambda: calls.append(1) or [{'value': 7}])])
    first, receipt = acquisition.get('fixture', run_id='first')
    second, reused = acquisition.get('fixture', run_id='second')
    assert first == second and calls == [1]
    assert reused['status'] == 'fresh' and reused['source'] == 'fixture'
    assert reused['received_at'] == receipt['received_at']


def test_timed_out_worker_keeps_acquisition_owned(acquisition, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    calls = []
    def fetch():
        calls.append(1)
        entered.set()
        release.wait(5)
        return [{'value': 7}]
    monkeypatch.setitem(acquisition.SOURCE_PLAN, 'fixture', [('fixture', fetch)])
    try:
        assert acquisition.get('fixture', timeout_per=.01)[1]['status'] == 'in_progress'
        assert entered.wait(1)
        assert acquisition.get('fixture', timeout_per=.01)[1]['status'] == 'in_progress'
        assert calls == [1]
    finally:
        release.set()


def test_cache_reuse_does_not_append_or_rewrite_stored_facts(tmp_path):
    from trade_system.multi_source_store import MultiSourceStore
    database = tmp_path/'facts.duckdb'
    rows = [{'date': '2026-09-16', 'main_net': 12}]
    with MultiSourceStore(database, fetcher=lambda *a, **k: None) as store:
        store.store('stock_flow', '000001', rows, {'source': 'fixture', 'status': 'live'})
        before = store.con.execute('SELECT observed_at FROM multi_source_observation').fetchall()
        result = store.store('stock_flow', '000001', rows, {'source': 'fixture', 'status': 'fresh', 'cache_hit': True})
        assert result['receipt_reused'] and result['rows_written'] == 0
        assert store.con.execute('SELECT observed_at FROM multi_source_observation').fetchall() == before


def test_implicit_and_explicit_session_share_inflight_owner(acquisition, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    calls = []
    def fetch(code, **kwargs):
        def run():
            calls.append(kwargs['date'])
            entered.set()
            release.wait(5)
            return [{'value': 7}]
        return [('fixture', run)]
    monkeypatch.setitem(acquisition.SOURCE_PLAN, 'zt_pool', fetch)
    try:
        assert acquisition.get('zt_pool', timeout_per=.01)[1]['status'] == 'in_progress'
        assert entered.wait(1)
        day = acquisition.datetime.date.today().strftime('%Y%m%d')
        assert acquisition.get('zt_pool', date=day, timeout_per=.01)[1]['status'] == 'in_progress'
        assert calls == [day]
    finally:
        release.set()


def test_kline_receipts_do_not_mix_additional_request_semantics(acquisition, monkeypatch):
    calls = []
    def plan(code, **kwargs):
        def fetch():
            calls.append(kwargs['market_scope'])
            return [{'date': '2026-09-16', 'close': len(calls), 'adjustment': 'qfq',
                     'volume_unit': 'shares', 'amount_unit': 'yuan'}]
        return [('fixture', fetch)]
    monkeypatch.setitem(acquisition.SOURCE_PLAN, 'kline', plan)
    args = {'start': '20260916', 'end': '20260916'}
    acquisition.get('kline', '000001', market_scope='first', **args)
    acquisition.get('kline', '000001', market_scope='second', **args)
    assert calls == ['first', 'second']
