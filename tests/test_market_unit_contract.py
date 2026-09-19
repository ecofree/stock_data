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
