"""Producer-to-consumer counterexamples from the consolidated audit."""
import math

import duckdb
import pytest

from trade_system import normalize, units
from trade_system.adapters import kline_sources


@pytest.mark.parametrize('value', ['nan', 'inf', '-inf', 1e308, True])
def test_invalid_or_overflowing_conversion_is_unknown(value):
    assert units.normalize_amount(value, 'thousand_yuan') is None


@pytest.mark.parametrize('kind,value,unit,expected', [
    ('amount', 2, 'thousand_yuan', 2000),
    ('amount', 2, 'yuan', 2),
    ('amount', 2, 'CNY', 2),
    ('amount', 2, '10000_yuan', 20000),
    ('amount', 0, 'yuan', 0),
    ('amount', -2, 'yuan', -2),
    ('amount', 2, 'not_provided', None),
    ('amount', 'nan', 'yuan', None),
    ('amount', 1e308, 'thousand_yuan', None),
    ('volume', 2, 'hands', 200),
    ('volume', 2, 'shares', 2),
])
def test_sql_and_python_share_conversion_vectors(kind, value, unit, expected):
    function = units.normalize_amount if kind == 'amount' else units.normalize_volume
    assert function(value, unit) == expected
    with duckdb.connect(':memory:') as con:
        actual = con.execute('SELECT ' + units.normalization_sql('value', 'unit', kind)
            + ' FROM (SELECT ? AS value, ? AS unit)', [value, unit]).fetchone()[0]
    assert actual == expected


def test_actual_view_has_canonical_labels_and_is_idempotent():
    with duckdb.connect(':memory:') as con:
        con.execute('''CREATE TABLE tushare_daily(date VARCHAR,stock_code VARCHAR,
            open DOUBLE,high DOUBLE,low DOUBLE,close DOUBLE,volume DOUBLE,turnover DOUBLE,
            change_pct DOUBLE,fetched_at TIMESTAMP,volume_unit VARCHAR,amount_unit VARCHAR,
            adjustment VARCHAR,provider VARCHAR)''')
        con.execute("""INSERT INTO tushare_daily VALUES
            ('2026-09-11','000001',10,11,9,10,100,1000,0,'2026-09-11 18:00:00',
             'hands','thousand_yuan','none','tushare')""")
        normalize._create_kline_daily(con)
        volume, vu, amount, au = con.execute(
            'SELECT volume,volume_unit,turnover,amount_unit FROM v_kline_daily').fetchone()
    assert (volume, vu, amount, au) == (10000, 'shares', 1000000, 'yuan')
    assert units.normalize_amount(amount, au) == amount
    assert units.normalize_volume(volume, vu) == volume
    assert math.isfinite(amount)


def test_kline_rejects_wrong_adjustment_before_selecting_next_source(monkeypatch, tmp_path):
    from trade_system import resilient_sources as sources
    wrong = [{'date': '2026-09-11', 'adjustment': 'none', 'volume_unit':'hands', 'amount_unit':'yuan'}]
    correct = [{**wrong[0], 'adjustment':'qfq'}]
    monkeypatch.setattr(sources, 'CACHE_DIR', str(tmp_path))
    monkeypatch.setattr(sources.cache, 'get', lambda key: (None, 0))
    monkeypatch.setattr(sources.cache, 'put', lambda *args: None)
    monkeypatch.setattr(sources.health, 'is_cooldown', lambda *args: False)
    monkeypatch.setattr(sources.health, 'record', lambda *args: None)
    monkeypatch.setitem(sources.SOURCE_PLAN, 'kline', [('wrong', lambda: wrong), ('right', lambda: correct)])
    assert kline_sources.get_kline('000001', start='20260911', end='20260911', fq='qfq') == correct
    monkeypatch.setitem(sources.SOURCE_PLAN, 'kline', [('wrong', lambda: wrong)])
    assert kline_sources.get_kline('000001', start='20260911', end='20260911', fq='qfq') == []


def test_market_caps_preserve_total_float_and_unknown_semantics():
    a = units.market_caps({'total_mv': 2, 'total_mv_unit': '100m_yuan',
                          'circ_mv': 1, 'circ_mv_unit': '100m_yuan'})
    b = units.market_caps({'total_mv': 20000, 'total_mv_unit': '10000_yuan',
                          'circ_mv': 10000, 'circ_mv_unit': '10000_yuan'})
    assert a['total_market_cap_cny'] == b['total_market_cap_cny'] == 200000000
    assert a['float_market_cap_cny'] == b['float_market_cap_cny'] == 100000000
    unknown = units.market_caps({'market_cap': 200000000, 'total_mv': 2,
                                 'total_mv_unit': 'billion_yuan'})
    assert unknown['total_market_cap_cny'] is None
    assert unknown['float_market_cap_cny'] is None
    assert unknown['market_cap_quality']['total_market_cap_cny'] == 'unknown_unit'


def test_relay_valuation_exports_canonical_caps_and_raw(monkeypatch):
    from trade_system import resilient_sources as sources
    monkeypatch.setattr(sources, 'XIAODEFA_TOKEN', 'fixture')
    raw = {'total_mv': 20000, 'circ_mv': 10000, 'turnover_rate': None}
    monkeypatch.setattr(sources, '_xiaodefa_query', lambda *a, **k: [raw])
    result = sources._relay_daily_basic('000001', '20260911')
    assert result['total_market_cap_cny'] == 200000000
    assert result['float_market_cap_cny'] == 100000000
    assert result['raw'] == raw
    assert result['total_mv_unit'] == '100m_yuan'


def test_quote_persistence_keeps_raw_and_canonical_caps_without_schema_change():
    import json
    from trade_system.multi_source_store import MultiSourceStore
    from types import SimpleNamespace
    with duckdb.connect(':memory:') as con:
        con.execute('''CREATE TABLE multi_source_quote(source_date DATE,asset_type VARCHAR,
            asset_code VARCHAR,name VARCHAR,price DOUBLE,change_pct DOUBLE,pe_ttm DOUBLE,
            pb DOUBLE,total_mv DOUBLE,circ_mv DOUBLE,provider VARCHAR,total_mv_unit VARCHAR,
            circ_mv_unit VARCHAR,is_stale BOOLEAN,raw_json VARCHAR)''')
        row = {'total_mv': 2, 'total_mv_unit': '100m_yuan',
               'circ_mv': 1, 'circ_mv_unit': '100m_yuan', 'raw': {'receipt': 42}}
        MultiSourceStore._store_quote(SimpleNamespace(con=con), 'valuation', '000001',
                                     row, 'fixture', 'stock', False, '2026-09-11')
        raw = json.loads(con.execute('SELECT raw_json FROM multi_source_quote').fetchone()[0])
        assert raw['raw'] == {'receipt': 42}
        assert raw['total_market_cap_cny'] == 200000000
        assert raw['float_market_cap_cny'] == 100000000
        assert raw['market_cap_quality']['total_market_cap_cny'] is None
        assert row.get('total_market_cap_cny') is None  # caller receipt unchanged


@pytest.mark.parametrize('source,args', [('_from_baostock', ('000001','20260901','20260902','')),
    ('_from_pytdx', ('000001','20260901','20260902','')),
    ('_from_pytdx_minutes', ('000001','20260901'))])
def test_sdk_timeout_reaps_owned_child_before_return(monkeypatch, source, args):
    import subprocess
    import sys
    import time
    from trade_system.http_transport import request_budget, request_deadline
    real_run = subprocess.run
    real_popen = subprocess.Popen
    children = []
    def popen(*a, **kw):
        child = real_popen(*a, **kw)
        children.append(child)
        return child
    def run(command, **kw):
        assert 0 < kw['timeout'] <= 0.201
        # Exercise the actual process timeout/reap, with no SDK import or socket.
        return real_run([sys.executable, '-I', '-B', '-c', 'import time;time.sleep(30)'], **kw)
    monkeypatch.setattr(subprocess, 'Popen', popen)
    monkeypatch.setattr(subprocess, 'run', run)
    with request_budget(.2):
        with pytest.raises(TimeoutError, match='SDK deadline'):
            getattr(kline_sources, source)(*args)
    assert len(children) == 1 and children[0].poll() is not None
    token = request_deadline.set(time.monotonic()-1)
    try:
        with pytest.raises(TimeoutError):
            getattr(kline_sources, source)(*args)
        assert len(children) == 1
    finally:
        request_deadline.reset(token)


def test_sdk_raw_parser_retains_units_and_logs_out(monkeypatch):
    import sys
    from types import SimpleNamespace
    calls = []
    data = iter([True, False])
    result = SimpleNamespace(error_code='0', next=lambda: next(data),
        get_row_data=lambda: ['2026-09-01','10','12','9','11','100','1100'])
    monkeypatch.setitem(sys.modules, 'baostock', SimpleNamespace(
        login=lambda: SimpleNamespace(error_code='0'),
        logout=lambda: calls.append('logout'), query_history_k_data_plus=lambda *a, **kw: result))
    rows = kline_sources._baostock_rows('000001','20260901','20260901','')
    assert rows[0]['close'] == 11 and rows[0]['volume_unit'] == 'shares'
    assert rows[0]['amount_unit'] == 'yuan' and rows[0]['adjustment'] == 'none'
    assert calls == ['logout']


def test_sdk_worker_keeps_diagnostics_out_of_result(monkeypatch):
    import io
    import json
    import sys
    from types import SimpleNamespace
    output = io.BytesIO()
    monkeypatch.setattr(sys, 'stdin', SimpleNamespace(buffer=io.BytesIO(b'["pytdx", ["000001", "20260901", "20260901", ""]]')))
    monkeypatch.setattr(sys, 'stdout', SimpleNamespace(buffer=output))
    def rows(*args):
        print('SDK log must not enter response')
        return [{'close': 10, 'adjustment': 'none', 'volume_unit': 'hands'}]
    monkeypatch.setattr(kline_sources, '_pytdx_rows', rows)
    kline_sources._sdk_worker()
    assert json.loads(output.getvalue()) == [{'close': 10, 'adjustment': 'none', 'volume_unit': 'hands'}]


@pytest.mark.parametrize('source', ['_from_baostock', '_from_pytdx'])
@pytest.mark.parametrize('adjustment', ['qfq', 'hfq'])
def test_raw_only_sdk_never_requests_unsupported_adjustment(monkeypatch, source, adjustment):
    def forbidden(*a, **kw):
        pytest.fail('unsupported product must not consume SDK budget')
    monkeypatch.setattr(kline_sources, '_sdk_call', forbidden)
    assert getattr(kline_sources, source)('000001', '20260901', '20260902', adjustment) is None


@pytest.mark.parametrize('spent', [.2, 1.1])
def test_cninfo_signature_and_http_share_one_deadline(monkeypatch, tmp_path, spent):
    from types import SimpleNamespace
    from trade_system.adapters import cninfo_sources as cn
    from trade_system.http_transport import request_budget, request_deadline
    signature=tmp_path/'signature.js';signature.write_text('synthetic local fixture')
    monkeypatch.setenv('CNINFO_JS',str(signature))
    clock=[100.0];calls=[]
    monkeypatch.setattr(cn.time,'monotonic',lambda:clock[0])
    def run(command, **kw):
        assert command[-1]==str(signature) and kw['timeout']==pytest.approx(1)
        clock[0]+=spent
        return SimpleNamespace(returncode=0,stdout=b'synthetic-token')
    def read(request, **kw):
        remaining=request_deadline.get()-clock[0]
        if remaining<=0:raise TimeoutError('expired before request')
        calls.append(remaining)
        assert request.get_header('Accept-enckey')=='synthetic-token'
        return b'{"records":[{"F001V":"fixture"}]}'
    monkeypatch.setattr(cn.subprocess,'run',run)
    monkeypatch.setattr(cn,'read_verified_once',read)
    with request_budget(1):
        result=cn._cninfo_webapi('fixture',{})
    assert calls==pytest.approx([.8] if spent<1 else [])
    assert bool(result)==(spent<1)


def test_cninfo_signature_timeout_does_not_start_http(monkeypatch,tmp_path):
    import subprocess
    from trade_system.adapters import cninfo_sources as cn
    signature=tmp_path/'signature.js';signature.write_text('fixture')
    monkeypatch.setenv('CNINFO_JS',str(signature))
    def run(*a,**kw):raise subprocess.TimeoutExpired('node',kw['timeout'])
    def forbidden(*a,**kw):pytest.fail('HTTP after signature timeout')
    monkeypatch.setattr(cn.subprocess,'run',run);monkeypatch.setattr(cn,'read_verified_once',forbidden)
    with pytest.raises(TimeoutError):cn._cninfo_webapi('fixture',{})


def _cninfo_protocol_fixture(tmp_path, monkeypatch, *, total=32, change=None, circuit=None, http_status=200):
    import hashlib
    import json
    from datetime import datetime, timezone
    from trade_system.adapters import cninfo_sources as cn
    from trade_system import http_transport as transport
    plan = cn.cninfo_pagination_plan('000016.SZ', 'gssz0000016', '2025-01-01', '2026-09-29')
    path = tmp_path/'plan.json'
    path.write_text(json.dumps(plan, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    calls = []
    stamp = int(datetime(2026, 8, 20, tzinfo=timezone.utc).timestamp()*1000)
    def read(request, **kw):
        from urllib.parse import parse_qs
        transport._diagnostic_attempt(request)
        calls.append(request)
        assert kw['timeout'] <= 20 and kw['max_bytes'] == 4_000_000
        params = parse_qs(request.data.decode())
        number = int(params['pageNum'][0])
        start = (number-1)*30
        rows = [dict(secCode='000016', orgId='gssz0000016', announcementId=str(i),
            announcementTime=stamp) for i in range(start, min(start+30, total))]
        body = dict(totalAnnouncement=total, hasMore=total > number*30, announcements=rows)
        if change:
            change(number, body)
        raw = json.dumps(body, ensure_ascii=False, indent=1).encode()
        with transport.request_receipt(request) as receipt:
            receipt.update(status='response_received', http_status=http_status,
                response_headers=[('Content-Type', 'application/json')],
                response_headers_sha256='f'*64, response_sha256=hashlib.sha256(raw).hexdigest(),
                response_bytes=len(raw))
        return raw
    monkeypatch.setattr(cn, 'read_verified_once', read)
    monkeypatch.setattr(cn, '_cninfo_orgid', lambda *a: pytest.fail('hidden issuer lookup'))
    result = cn.diagnose_cninfo_pagination(path, hashlib.sha256(path.read_bytes()).hexdigest(),
                                         tmp_path/'evidence', circuit=circuit)
    return result, calls, plan


def test_cninfo_two_pages_keep_exact_seven_fields_and_raw_evidence(tmp_path, monkeypatch):
    import hashlib
    import json
    from pathlib import Path
    from trade_system.adapters import cninfo_sources as cn
    circuit = cn.CninfoPaginationCircuit()
    result, calls, plan = _cninfo_protocol_fixture(tmp_path, monkeypatch, circuit=circuit)
    assert len(calls) == result['actual_requests'] == 2
    assert result['protocol_verified'] and result['catalogue_complete']
    assert not result['financial_qualification'] and result['production_writes'] == 0
    assert result['unique_announcements'] == result['total_announcements'] == 32
    assert circuit.stopped == 'round_closed'
    fixed = {'stock','tabName','column','pageSize','pageNum','seDate','isHLtitle'}
    for i, (call, entry, record) in enumerate(zip(calls, plan['requests'], result['requests']), 1):
        assert set(entry['params']) == fixed and entry['params']['column'] == 'szse'
        assert call.data == Path(record['request_body_path']).read_bytes()
        assert hashlib.sha256(call.data).hexdigest() == entry['body_sha256'] == record['body_sha256']
        assert hashlib.sha256(Path(record['response_path']).read_bytes()).hexdigest() == record['response_sha256']
        assert record['received_at'] and not record['reused'] and record['http_status'] == 200
        assert result['source_pages'][i-1]['params'] == entry['params']
        assert json.loads(Path(record['request_path']).read_bytes())['params'] == entry['params']
    a, b = [e['params'].copy() for e in plan['requests']]
    a.pop('pageNum'); b.pop('pageNum')
    assert a == b


def test_cninfo_protocol_success_is_not_full_catalogue(tmp_path, monkeypatch):
    result, calls, _ = _cninfo_protocol_fixture(tmp_path, monkeypatch, total=95)
    assert len(calls) == 2 and result['protocol_verified']
    assert result['unique_announcements'] == 60
    assert not result['catalogue_complete'] and not result['financial_qualification']


def test_cninfo_duplicate_page_closes_round_across_later_security(tmp_path, monkeypatch):
    import hashlib
    import json
    from trade_system.adapters import cninfo_sources as cn
    circuit = cn.CninfoPaginationCircuit()
    def duplicate(number, body):
        if number == 2:
            for i, row in enumerate(body['announcements']): row['announcementId'] = str(i)
    result, calls, _ = _cninfo_protocol_fixture(tmp_path, monkeypatch, total=95, change=duplicate, circuit=circuit)
    assert result['stopped'] == circuit.stopped == 'duplicate_announcement_page'
    assert result['actual_requests'] == len(calls) == 2 and not result['protocol_verified']
    raw = json.dumps(cn.cninfo_pagination_plan('600000.SH', 'retained-org', '2025-01-01', '2026-09-29')).encode()
    path = tmp_path/'other-plan.json'; path.write_bytes(raw)
    later = cn.diagnose_cninfo_pagination(path, hashlib.sha256(raw).hexdigest(), tmp_path/'later', circuit=circuit)
    assert later['actual_requests'] == 0 and len(calls) == 2
    assert not later['protocol_verified'] and not later['catalogue_complete']


@pytest.mark.parametrize('failure', ['business', 'identity', 'date', 'total', 'terminal', 'duplicate'])
def test_cninfo_first_bad_page_never_sends_second(tmp_path, monkeypatch, failure):
    def change(number, body):
        if failure == 'business': body.update(code=429, message='rate limited')
        elif failure == 'identity': body['announcements'][0]['orgId'] = 'other-issuer'
        elif failure == 'date': body['announcements'][0]['announcementTime'] = 0
        elif failure == 'total': body['totalAnnouncement'] = True
        elif failure == 'terminal': body['hasMore'] = False
        elif failure == 'duplicate': body['announcements'][1]['announcementId'] = body['announcements'][0]['announcementId']
    result, calls, _ = _cninfo_protocol_fixture(tmp_path, monkeypatch, change=change)
    assert len(calls) == result['actual_requests'] == 1
    assert result['status'] == 'stopped' and not result['protocol_verified']
    assert result['requests'][0]['response_body_available']  # HTTP 200 error bytes retained.


def test_cninfo_single_page_does_not_prove_pagination(tmp_path, monkeypatch):
    result, calls, _ = _cninfo_protocol_fixture(tmp_path, monkeypatch, total=9)
    assert len(calls) == result['actual_requests'] == 1
    assert result['catalogue_complete'] and not result['protocol_verified']
    assert result['status'] == 'single_page_no_pagination_proof'


def test_cninfo_latest_never_guesses_issuer_after_lookup_failure(monkeypatch):
    from trade_system.adapters import cninfo_sources as cn
    calls=[]
    monkeypatch.setattr(cn, '_CNINFO_ORGID_MAP', {})
    def read(request, **kw):
        calls.append(request)
        assert request.full_url.endswith('/szse_stock.json')
        raise TimeoutError('retained issuer lookup failed')
    monkeypatch.setattr(cn, 'read_verified_once', read)
    assert cn._from_cninfo_announcements('002731') is None
    assert len(calls) == 1 and calls[0].get_method() == 'GET'


@pytest.mark.parametrize('foreign', ['valid', 'code', 'org'])
def test_cninfo_latest_rejects_foreign_announcement(monkeypatch, foreign):
    import json
    from trade_system.adapters import cninfo_sources as cn
    monkeypatch.setattr(cn, '_CNINFO_ORGID_MAP', {'002731':'9900022974'})
    row = dict(secCode='002731',orgId='9900022974',announcementId='retained-id',
               announcementTitle='retained title',announcementTime=1780000000000)
    if foreign != 'valid':
        row['secCode' if foreign=='code' else 'orgId']='foreign'
    def read(request, **kw):
        assert request.get_method() == 'POST'
        assert b'002731%2C9900022974' in request.data
        return json.dumps(dict(announcements=[row])).encode()
    monkeypatch.setattr(cn, 'read_verified_once', read)
    result = cn._from_cninfo_announcements('002731')
    if foreign == 'valid':
        assert result[0]['id']=='retained-id' and result[0]['title']=='retained title'
    else:
        assert result is None


def test_cninfo_plan_changed_scope_rejected_before_transport(tmp_path, monkeypatch):
    import hashlib
    import json
    from trade_system.adapters import cninfo_sources as cn
    plan = cn.cninfo_pagination_plan('000016.SZ','retained-org','2025-01-01','2026-09-29')
    plan['requests'][0]['params']['column'] = ''
    path=tmp_path/'plan.json';path.write_text(json.dumps(plan))
    monkeypatch.setattr(cn, 'read_verified_once', lambda *a, **k: pytest.fail('bad plan caused a request'))
    with pytest.raises(ValueError, match='differs from fixed'):
        cn.diagnose_cninfo_pagination(path, hashlib.sha256(path.read_bytes()).hexdigest(), tmp_path/'out')
    assert not (tmp_path/'out').exists()


@pytest.mark.parametrize('status', [None,201])
def test_cninfo_missing_or_non_200_actual_status_cannot_be_invented(tmp_path, monkeypatch, status):
    result, calls, _ = _cninfo_protocol_fixture(tmp_path, monkeypatch, http_status=status)
    assert result['actual_requests'] == len(calls) == 1
    assert result['stopped'] == 'actual_http_200_evidence_missing'
    assert result['requests'][0]['http_status'] is status
    assert not result['catalogue_complete'] and not result['protocol_verified']


def test_cninfo_outer_send_budget_cannot_be_reset_by_protocol(tmp_path, monkeypatch):
    from trade_system.http_transport import wire_request_budget
    with wire_request_budget(1) as outer:
        result, calls, _ = _cninfo_protocol_fixture(tmp_path, monkeypatch)
    assert outer['attempts'] == result['actual_requests'] == len(calls) == 1
    assert result['status'] == 'stopped' and not result['protocol_verified']


@pytest.mark.parametrize('body_unavailable', [False, True])
def test_cninfo_http_auth_failure_retains_body_and_stops_all_later_sends(tmp_path, monkeypatch, body_unavailable):
    import hashlib
    import json
    from pathlib import Path
    from types import SimpleNamespace
    from trade_system.adapters import cninfo_sources as cn
    import subprocess
    plan = tmp_path/'plan.json'
    plan.write_text(json.dumps(cn.cninfo_pagination_plan('000016.SZ','retained-org','2025-01-01','2026-09-29')))
    calls=[]
    raw=b'' if body_unavailable else b'{"code":403,"message":"permission denied"}'
    header=json.dumps(dict(ok=False,http_error=True,http_status=403,response_headers=[['Content-Type','application/json']],
                           response_headers_sha256='b'*64,response_body_unavailable=body_unavailable)).encode()
    def run(*a, **kw):
        calls.append(a)
        return SimpleNamespace(returncode=0,stdout=header+b'\n'+raw)
    monkeypatch.setattr(subprocess,'run',run)
    result=cn.diagnose_cninfo_pagination(plan,hashlib.sha256(plan.read_bytes()).hexdigest(),tmp_path/'out')
    assert len(calls) == result['actual_requests'] == 1 and result['stopped']=='http_403'
    assert result['failure_category']=='permission_denied'
    record=result['requests'][0]
    assert record['http_status']==403
    if body_unavailable:
        assert not Path(record['response_path']).exists() and not record['response_body_available']
        assert 'response_sha256' not in record and 'response_bytes' not in record
        transport_receipt = record['transport_receipts'][0]
        assert transport_receipt['response_body_unavailable'] is True
        assert 'response_sha256' not in transport_receipt and 'response_bytes' not in transport_receipt
    else:
        assert Path(record['response_path']).read_bytes()==raw
        assert record['response_sha256']==hashlib.sha256(raw).hexdigest()
    assert record['response_headers']==[['Content-Type','application/json']]


@pytest.mark.parametrize('with_metadata', [True,False])
def test_verified_worker_keeps_actual_http_status_and_redacts_headers(monkeypatch, with_metadata):
    import hashlib
    import io
    import json
    import sys
    from email.message import Message
    from types import SimpleNamespace
    from trade_system import http_transport as transport
    headers = Message()
    for name, value in [('Content-Type','application/json'),('Set-Cookie','private-cookie'),
                        ('Authorization','private-auth'),('X-Token','private-token')]:
        headers[name] = value
    class Response(io.BytesIO):
        def getcode(self): return 201
    response = Response(b' {"raw": 1} ') if with_metadata else io.BytesIO(b' {"raw": 1} ')
    if with_metadata: response.headers=headers
    monkeypatch.setattr(transport, 'open_verified_once', lambda *a, **k: response)
    output=io.BytesIO()
    data=dict(url='https://www.cninfo.com.cn/new/hisAnnouncement/query',body='pageNum=1',
              headers={},method='POST',timeout=1,max_bytes=100)
    monkeypatch.setattr(sys,'stdin',SimpleNamespace(buffer=io.BytesIO(json.dumps(data).encode())))
    monkeypatch.setattr(sys,'stdout',SimpleNamespace(buffer=output))
    transport._response_worker()
    status, raw = output.getvalue().split(b'\n',1)
    metadata=json.loads(status)
    assert raw == b' {"raw": 1} ' and metadata['http_status'] == (201 if with_metadata else None)
    assert metadata['response_headers'] == ([['Content-Type','application/json']] if with_metadata else [])
    assert metadata['response_headers_sha256'] == (hashlib.sha256(json.dumps(list(headers.items()),
        ensure_ascii=True,separators=(',',':')).encode('ascii')).hexdigest() if with_metadata else None)
    assert not any(v in status for v in [b'private-cookie', b'private-auth', b'private-token'])


@pytest.mark.parametrize('status', [200,201,403])
def test_verified_transport_actual_status_never_fabricates_200(monkeypatch,status):
    import json
    import urllib.error
    import urllib.request
    from types import SimpleNamespace
    from trade_system import http_transport as transport
    raw = b'{"retained":"original"}'
    payload=json.dumps(dict(ok=status<400,http_error=status>=400,http_status=status,response_headers=[['Content-Type','application/json']],
                           response_headers_sha256='a'*64)).encode()+b'\n'+raw
    monkeypatch.setattr(transport.subprocess if hasattr(transport,'subprocess') else __import__('subprocess'),
                        'run',lambda *a,**k:SimpleNamespace(returncode=0,stdout=payload))
    with transport.measure_requests() as receipts:
        if status<400:
            assert transport.read_verified_once(urllib.request.Request('https://www.cninfo.com.cn/'),
                                                timeout=1,max_bytes=1000) == raw
        else:
            with pytest.raises(urllib.error.HTTPError) as failure:
                transport.read_verified_once(urllib.request.Request('https://www.cninfo.com.cn/'),timeout=1,max_bytes=1000)
            assert failure.value.read() == raw
    assert receipts[0]['http_status'] == status
    summary=transport.summarize_requests(receipts)
    assert summary['products']['unattributed']['errors'] == (1 if status>=400 else 0)


@pytest.mark.parametrize('body_case', ['empty', 'unreadable', 'oversized', 'fp_none', 'marked_unavailable'])
def test_http_error_unknown_body_is_not_a_retained_empty_body(monkeypatch, body_case):
    import hashlib
    import io
    import json
    import sys
    import urllib.error
    import urllib.request
    from email.message import Message
    from types import SimpleNamespace
    from trade_system import http_transport as transport

    class UnreadableBody(io.BytesIO):
        def read(self, *args):
            raise OSError('body read failed')

    body = UnreadableBody() if body_case == 'unreadable' else io.BytesIO(
        b'x' * 101 if body_case == 'oversized' else b'')
    headers = Message()
    headers['Content-Type'] = 'application/json'
    error = urllib.error.HTTPError('https://www.cninfo.com.cn/', 403, 'denied', headers, body)
    if body_case == 'fp_none':
        error.fp = None
    elif body_case == 'marked_unavailable':
        error.response_body_unavailable = True

    def denied(*args, **kwargs):
        raise error

    monkeypatch.setattr(transport, 'open_verified_once', denied)
    data = dict(url=error.url, body=None, headers={}, method='GET', timeout=1, max_bytes=100)
    output = io.BytesIO()
    with monkeypatch.context() as child:
        child.setattr(sys, 'stdin', SimpleNamespace(buffer=io.BytesIO(json.dumps(data).encode())))
        child.setattr(sys, 'stdout', SimpleNamespace(buffer=output))
        transport._response_worker()
    payload = output.getvalue()
    monkeypatch.setattr(__import__('subprocess'), 'run',
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=payload))
    with transport.measure_requests() as receipts:
        with pytest.raises(urllib.error.HTTPError) as failure:
            transport.read_verified_once(urllib.request.Request(error.url), timeout=1, max_bytes=100)
    receipt = receipts[0]
    assert receipt['http_status'] == 403
    assert receipt['response_headers'] == [['Content-Type', 'application/json']]
    if body_case == 'empty':
        assert failure.value.fp is not None and failure.value.read() == b''
        assert receipt['response_sha256'] == hashlib.sha256(b'').hexdigest()
        assert receipt['response_bytes'] == 0
        assert not receipt.get('response_body_unavailable')
    else:
        assert failure.value.fp is None and receipt['response_body_unavailable'] is True
        assert 'response_sha256' not in receipt and 'response_bytes' not in receipt
        with pytest.raises(OSError, match='response body unavailable'):
            failure.value.read()


@pytest.mark.parametrize('http_status', [200, 403])
def test_unreadable_headers_keep_actual_status_and_body(monkeypatch, http_status):
    import io
    import json
    import sys
    import urllib.error
    import urllib.request
    from types import SimpleNamespace
    from trade_system import http_transport as transport

    class BrokenHeaders:
        def items(self):
            raise ValueError('private-header-value')

    class Response(io.BytesIO):
        headers = BrokenHeaders()

        def getcode(self):
            return http_status

    raw = b'{"retained":"original"}'

    def open_response(*args, **kwargs):
        if http_status >= 400:
            raise urllib.error.HTTPError('https://www.cninfo.com.cn/', http_status, 'denied',
                                         BrokenHeaders(), io.BytesIO(raw))
        return Response(raw)

    monkeypatch.setattr(transport, 'open_verified_once', open_response)
    output = io.BytesIO()
    data = dict(url='https://www.cninfo.com.cn/', body=None, headers={}, method='GET', timeout=1, max_bytes=100)
    with monkeypatch.context() as child:
        child.setattr(sys, 'stdin', SimpleNamespace(buffer=io.BytesIO(json.dumps(data).encode())))
        child.setattr(sys, 'stdout', SimpleNamespace(buffer=output))
        transport._response_worker()
    payload = output.getvalue()
    status, actual_raw = payload.split(b'\n', 1)
    assert actual_raw == raw and b'private-header-value' not in status
    monkeypatch.setattr(__import__('subprocess'), 'run',
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=payload))
    with transport.measure_requests() as receipts:
        if http_status < 400:
            assert transport.read_verified_once(urllib.request.Request(data['url']), timeout=1, max_bytes=100) == raw
        else:
            with pytest.raises(urllib.error.HTTPError) as failure:
                transport.read_verified_once(urllib.request.Request(data['url']), timeout=1, max_bytes=100)
            assert failure.value.read() == raw
    assert receipts[0]['http_status'] == http_status
    assert receipts[0]['response_headers_available'] is False
    assert receipts[0]['response_headers_sha256'] is None
    assert receipts[0]['response_headers_error_type'] == 'ValueError'


@pytest.mark.parametrize('body_failure', ['interrupted', 'timeout', 'oversized'])
def test_worker_body_failure_preserves_received_http_metadata_and_transport_category(monkeypatch, body_failure):
    import io
    import json
    import sys
    import urllib.error
    import urllib.request
    from types import SimpleNamespace
    from trade_system import http_transport as transport

    class Response(io.BytesIO):
        headers = {'Content-Type': 'application/json'}

        def getcode(self):
            return 200

        def read(self, *_args):
            if body_failure == 'interrupted':
                raise ConnectionResetError('body interrupted')
            if body_failure == 'timeout':
                raise TimeoutError('body timed out')
            return b'x' * 101

    monkeypatch.setattr(transport, 'open_verified_once', lambda *args, **kwargs: Response())
    output = io.BytesIO()
    data = dict(url='https://www.cninfo.com.cn/', body=None, headers={}, method='GET', timeout=1, max_bytes=100)
    with monkeypatch.context() as child:
        child.setattr(sys, 'stdin', SimpleNamespace(buffer=io.BytesIO(json.dumps(data).encode())))
        child.setattr(sys, 'stdout', SimpleNamespace(buffer=output))
        transport._response_worker()
    payload = output.getvalue()
    status, raw = payload.split(b'\n', 1)
    status = json.loads(status)
    assert raw == b'' and status['http_status'] == 200
    assert status['response_body_unavailable'] is True
    assert not status.get('http_error')
    monkeypatch.setattr(__import__('subprocess'), 'run',
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=payload))
    error_type = ValueError if body_failure == 'oversized' else urllib.error.URLError
    with transport.measure_requests() as receipts:
        with pytest.raises(error_type) as failure:
            transport.read_verified_once(urllib.request.Request(data['url']), timeout=1, max_bytes=100)
    assert not isinstance(failure.value, urllib.error.HTTPError)
    receipt = receipts[0]
    assert receipt['http_status'] == 200
    assert receipt['response_headers'] == [['Content-Type', 'application/json']]
    assert receipt['response_body_unavailable'] is True
    assert 'response_sha256' not in receipt and 'response_bytes' not in receipt
    if body_failure == 'oversized':
        assert str(failure.value) == 'response byte budget exceeded'
    else:
        assert receipt['error_category'] == {
            'interrupted': 'connection_interrupted', 'timeout': 'network_timeout',
        }[body_failure]


@pytest.mark.parametrize('capture_failure', ['read', 'write'])
def test_cninfo_failure_body_capture_cannot_leave_shared_round_open(tmp_path, monkeypatch, capture_failure):
    import hashlib
    import io
    import json
    import urllib.error
    from pathlib import Path
    from trade_system.adapters import cninfo_sources as cn
    from trade_system import http_transport as transport

    circuit = cn.CninfoPaginationCircuit()
    path = tmp_path/'plan.json'
    path.write_text(json.dumps(cn.cninfo_pagination_plan('000016.SZ', 'retained-org', '2025-01-01', '2026-09-29')))
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    calls = []

    class UnreadableBody(io.BytesIO):
        def read(self, *_args):
            assert circuit.stopped == 'http_403'
            raise OSError('failure body read interrupted')

    def denied(request, **kwargs):
        transport._diagnostic_attempt(request)
        calls.append(1)
        transport.stop_diagnostic('permission_denied')
        with transport.request_receipt(request) as receipt:
            receipt.update(http_status=403, response_headers=[['Content-Type', 'application/json']])
            body = UnreadableBody() if capture_failure == 'read' else io.BytesIO(b'{"code":403}')
            raise urllib.error.HTTPError(request.full_url, 403, 'denied', {}, body)

    original_write = Path.write_bytes

    def write_bytes(file, data):
        if file.name == 'page-1-response.json' and capture_failure == 'write':
            assert circuit.stopped == 'http_403'
            raise OSError('failure body persistence interrupted')
        return original_write(file, data)

    monkeypatch.setattr(cn, 'read_verified_once', denied)
    monkeypatch.setattr(Path, 'write_bytes', write_bytes)
    result = cn.diagnose_cninfo_pagination(path, digest, tmp_path/'out', circuit=circuit)
    assert calls == [1] and result['actual_requests'] == 1
    assert result['stopped'] == circuit.stopped == 'http_403'
    assert not result['protocol_verified']
    record = result['requests'][0]
    assert record['status'] == 'failed' and record['http_status'] == 403
    assert record['response_body_capture_error_type'] == 'OSError'
    assert not record['response_body_available'] and not Path(record['response_path']).exists()
    assert 'response_sha256' not in record and 'response_bytes' not in record
    assert json.loads((tmp_path/'out/page-1-receipt.json').read_text()) == record
    later = cn.diagnose_cninfo_pagination(path, digest, tmp_path/'later', circuit=circuit)
    assert calls == [1] and later['actual_requests'] == 0


def test_cninfo_body_interruption_keeps_actual_http_status_in_page_receipt(tmp_path, monkeypatch):
    import hashlib
    import json
    from types import SimpleNamespace
    from trade_system.adapters import cninfo_sources as cn

    path = tmp_path/'plan.json'
    path.write_text(json.dumps(cn.cninfo_pagination_plan('000016.SZ', 'retained-org', '2025-01-01', '2026-09-29')))
    payload = json.dumps(dict(ok=False, http_status=200, error='connection_interrupted',
        cause_type='ConnectionResetError', response_body_unavailable=True,
        response_headers=[['Content-Type', 'application/json']])).encode()+b'\n'
    calls = []

    def run(*args, **kwargs):
        calls.append(1)
        return SimpleNamespace(returncode=0, stdout=payload)

    monkeypatch.setattr(__import__('subprocess'), 'run', run)
    result = cn.diagnose_cninfo_pagination(path, hashlib.sha256(path.read_bytes()).hexdigest(), tmp_path/'out')
    assert calls == [1] and result['actual_requests'] == 1
    record = result['requests'][0]
    assert record['http_status'] == 200 and record['status'] == 'failed'
    assert record['response_headers'] == [['Content-Type', 'application/json']]
    assert record['transport_receipts'][0]['error_category'] == 'connection_interrupted'
    assert not record['response_body_available']
    assert 'response_sha256' not in record and 'response_bytes' not in record
