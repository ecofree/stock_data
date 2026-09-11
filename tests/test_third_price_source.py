import json

import pytest

from tools.v2 import probe_third_price_source as p
from tests.test_price_conflict_probe import source
from trade_system.v2.daily_session import seal
from trade_system.v2.domain import canonical
from trade_system.v2.gap_evidence import read_json


def raw(code='000001', lines=None):
    return canonical({'rc': 0, 'data': {'code': code, 'market': 0, 'klines': lines if lines is not None else
        [d + ',10,11,12,9,100,100000' for d in p.DAYS]}})


class Client:
    def query(self, request):
        return raw(request['code'][:6])


def real_fixture(folder):
    path = folder / 'registration.json'; reg = read_json(path)[0]
    reg['origin'] = 'eastmoney_public'; path.write_text(canonical(reg), encoding='utf-8')
    (folder / 'completed.json').unlink(); seal(folder)


def test_third_agreement_never_repairs(tmp_path, monkeypatch):
    monkeypatch.setattr(p.time, 'sleep', lambda _: None)
    baseline = source(tmp_path); receipts = tmp_path / 'receipts'
    p.capture(baseline, receipts, client=Client())
    with pytest.raises(ValueError, match='real capture'):
        p.analyze(baseline, receipts, tmp_path / 'invalid')
    real_fixture(receipts)
    report = p.analyze(baseline, receipts, tmp_path / 'analysis')
    assert report['cases'] == 2 and report['canonical_replacements'] == 0
    assert not report['independent_upstream_verified'] and not report['research_ready']
    case = read_json(tmp_path / 'analysis/000001.SZ.json')[0]['cases'][0]
    assert case['comparisons']['xiaodefa_relay']['turnover_within_0_50_cny_under_interpretation']
    assert not case['canonical_replacement_authorized']
    with pytest.raises(ValueError, match='new output'):
        p.capture(baseline, receipts, client=Client())


@pytest.mark.parametrize('bad', ['code', 'market', 'rc', 'width', 'duplicate_day', 'date', 'nan', 'negative', 'bounds', 'too_many', 'duplicate_key'])
def test_invalid_response_rejected(bad):
    data = json.loads(raw()); item = data['data']
    if bad == 'code': item['code'] = '000002'
    if bad == 'market': item['market'] = False
    if bad == 'rc': data['rc'] = False
    if bad == 'width': item['klines'][0] += ',0'
    if bad == 'duplicate_day': item['klines'][1] = item['klines'][0]
    if bad == 'date': item['klines'][0] = '2025-11-26,10,11,12,9,100,100000'
    if bad == 'nan': item['klines'][0] = p.DAYS[0] + ',NaN,11,12,9,100,100000'
    if bad == 'negative': item['klines'][0] = p.DAYS[0] + ',10,11,12,9,-1,100000'
    if bad == 'bounds': item['klines'][0] = p.DAYS[0] + ',10,11,9,12,100,100000'
    if bad == 'too_many': item['klines'] *= 2
    text = canonical(data) if bad != 'duplicate_key' else '{"rc":0,"rc":1}'
    with pytest.raises(ValueError): p.normalize(p.request_for('000001.SZ'), text)


def test_original_precision_and_no_implicit_missing_zero():
    rows = p.normalize(p.request_for('000001.SZ'), raw(lines=[p.DAYS[0] + ',10,11,12,9,1037323,1205480322.84']))
    assert rows[p.DAYS[0]]['raw_cells'][-1] == '1205480322.84'
    assert rows[p.DAYS[0]]['values']['volume_shares'] == '103732300'
    assert p.normalize(p.request_for('000001.SZ'), raw(lines=[])) == {}
    assert p.compare([{'date': p.DAYS[0], 'parent_record_id': 'test', 'evidence': [
        {'provider': x, 'values': {}} for x in ('hithink_native', 'xiaodefa_relay')]}], {})[0]['comparisons'] == {}


@pytest.mark.parametrize('bad', ['time', 'request', 'rows', 'extra'])
def test_resealed_bad_binding_rejected(tmp_path, monkeypatch, bad):
    monkeypatch.setattr(p.time, 'sleep', lambda _: None)
    baseline = source(tmp_path); receipts = tmp_path / 'receipts'
    p.capture(baseline, receipts, client=Client()); real_fixture(receipts)
    path = receipts / ('status-00.json' if bad == 'rows' else 'receipt-00.json')
    data = read_json(path)[0]
    if bad == 'time': data['received_at'] = '2099-01-01T00:00:00+00:00'
    if bad == 'request': data['request']['params']['fqt'] = '1'
    if bad == 'rows': data['rows'] = True
    if bad == 'extra': (receipts / 'extra.json').write_text('{}')
    path.write_text(canonical(data), encoding='utf-8')
    (receipts / 'completed.json').unlink(); seal(receipts)
    with pytest.raises(ValueError): p.analyze(baseline, receipts, tmp_path / 'invalid')


def test_failure_circuit_and_secret_redaction(tmp_path, monkeypatch):
    monkeypatch.setattr(p.time, 'sleep', lambda _: None)
    baseline = source(tmp_path, codes=tuple(f'{i:06d}.SZ' for i in range(1, 5)))
    client = Client(); calls = []
    def fail(request):
        calls.append(request); raise RuntimeError('credential-do-not-store')
    client.query = fail
    p.capture(baseline, tmp_path / 'receipts', client=client)
    assert len(calls) == 3
    for path in (tmp_path / 'receipts').glob('*.json'):
        assert 'credential-do-not-store' not in path.read_text(encoding='utf-8')


def test_public_transport_is_bounded_and_never_disables_tls(monkeypatch):
    from types import SimpleNamespace
    seen = []
    monkeypatch.setattr(p.shutil, 'which', lambda name: 'curl')
    def run(argv, **kwargs):
        seen.append((argv, kwargs)); return SimpleNamespace(returncode=0, stdout=raw().encode())
    monkeypatch.setattr(p.subprocess, 'run', run)
    assert p.Client().query(p.request_for('000001.SZ')) == raw()
    argv, kwargs = seen[0]
    assert '--disable' == argv[1] and '--max-filesize' in argv and '--max-time' in argv
    assert '--insecure' not in argv and '-k' not in argv and '--retry' not in argv
    assert '--location' not in argv and not kwargs.get('shell') and kwargs['timeout'] == 20


@pytest.mark.parametrize('mode', ['failed', 'oversize'])
def test_transport_error_not_exposed(monkeypatch, mode):
    from types import SimpleNamespace
    monkeypatch.setattr(p.shutil, 'which', lambda name: 'curl')
    monkeypatch.setattr(p.subprocess, 'run', lambda *a, **k: SimpleNamespace(
        returncode=1 if mode == 'failed' else 0,
        stdout=b'x' * (p.MAX_BYTES + 1), stderr=b'private-environment-information'))
    with pytest.raises(ValueError) as error: p.Client().query(p.request_for('000001.SZ'))
    assert 'private-environment' not in str(error.value)
