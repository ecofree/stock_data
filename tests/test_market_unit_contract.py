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


def test_kline_rejects_wrong_adjustment_before_selecting_next_source(monkeypatch):
    wrong = [{'date': '2026-09-11', 'adjustment': 'none'}]
    correct = [{'date': '2026-09-11', 'adjustment': 'qfq'}]
    monkeypatch.setattr(kline_sources, '_KLINE_SOURCES', [
        ('wrong', lambda *a: wrong), ('right', lambda *a: correct)])
    assert kline_sources.get_kline('000001', fq='qfq') == correct
    monkeypatch.setattr(kline_sources, '_KLINE_SOURCES', [('wrong', lambda *a: wrong)])
    assert kline_sources.get_kline('000001', fq='qfq') == []


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
    monkeypatch.setattr(sources, 'TUSHARE_TOKEN', 'fixture')
    raw = {'total_mv': 20000, 'circ_mv': 10000, 'turnover_rate': None}
    monkeypatch.setattr(sources, '_tushare_query', lambda *a, **k: [raw])
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
