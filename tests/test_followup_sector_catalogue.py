"""A partial provider catalogue must not shrink the downstream collection scope."""
from contextlib import closing
from types import SimpleNamespace

import pytest

from collectors.collect_sector import collect_sector_plates
from trade_system.data_store import DuckDBStore
from trade_system.schema import init_schema


@pytest.fixture
def catalogue(tmp_path):
    with closing(DuckDBStore(tmp_path / 'catalogue.duckdb')) as store:
        init_schema(store.conn)
        store.conn.execute("INSERT INTO sector_plates(sector_code,sector_name) VALUES ('A','original A'),('B','original B')")
        yield store


@pytest.mark.parametrize('bad', [None, {}, [], {'plates': None}, {'plates': {}},
    {'plates': [{'code': 'A', 'name': 'updated'}, {'name': 'missing code'}]},
    *({'plates': [{'code': 'A', 'name': 'updated'}, {'code': code, 'name': 'bad'}]}
      for code in (None, '', ' ', True, [], {}, 1.5, ' A ')),
    {'plates': [{'code': 'A', 'name': []}]}, {'plates': [['A']]},
])
def test_invalid_batch_preserves_every_original_row(catalogue, bad):
    before = catalogue.fetchall('SELECT * FROM sector_plates ORDER BY sector_code')
    client = SimpleNamespace(get=lambda *a: bad)
    if bad is None:
        assert collect_sector_plates(client, catalogue, '2026-10-12') == [('A','original A'),('B','original B')]
    else:
        with pytest.raises(ValueError, match='sector plates'):
            collect_sector_plates(client, catalogue, '2026-10-12')
    assert catalogue.fetchall('SELECT * FROM sector_plates ORDER BY sector_code') == before
    assert catalogue.fetchall('SELECT count(*) FROM _collect_log')[0][0] == 0


@pytest.mark.parametrize('plates', [[], [{'code': 'A', 'name': 'updated'}], [['A','updated'], ['C','new',1]]])
def test_absence_even_in_claimed_complete_response_never_authorizes_removal(catalogue, plates):
    # A provider Boolean is not a qualified completeness/retirement contract.
    client = SimpleNamespace(get=lambda *a: {'plates': plates, 'complete': True})
    rows = collect_sector_plates(client, catalogue, '2026-10-12')
    assert ('B', 'original B') in rows
    assert len(rows) == (3 if len(plates) == 2 else 2)
    assert collect_sector_plates(client, catalogue, '2026-10-12') == rows


def test_downstream_uses_retained_catalogue(catalogue, monkeypatch):
    from collectors import collect_sector as module
    client = SimpleNamespace(get=lambda endpoint,*a,**k: {'plates': [{'code': 'A', 'name': 'updated'}]} if endpoint=='/sector/plates' else None)
    monkeypatch.setattr(module, 'collect_sector_ranking', lambda *a: 0)
    observed = []
    def consume(client, store, day, codes):
        observed.append(list(codes))
        return 0
    for name in ('stocks','all_stocks','strength','capital','boom_reason','son_plates','sub_concepts',
                 'parent','plate_info_qj','bk_fenshi_zhibo','strength_batch','strength_ndays','strength_dataframe'):
        monkeypatch.setattr(module, 'collect_sector_' + name, consume)
    module.collect_all_sector(client, catalogue, '2026-10-12')
    assert observed and all(codes == ['A','B'] for codes in observed)


@pytest.mark.parametrize('route',['ranking','fallback'])
@pytest.mark.parametrize('damage',['partial','malformed','stock_write_failure'])
def test_ranking_routes_cannot_drop_retained_sector_scope(catalogue, monkeypatch, route, damage):
    from collectors.collect_sector import collect_sector_ranking, _collect_sector_ranking_fallback
    day='2026-10-12'
    catalogue.conn.execute("INSERT INTO sector_ranking(date,sector_code,sector_name) VALUES (?,'A','old A'),(?,'B','old B')",[day,day])
    catalogue.conn.execute("INSERT INTO sector_stocks(date,sector_code,stock_code) VALUES (?,'B','000002')",[day])
    tables=('sector_ranking','sector_stocks','sector_strength')
    before={t:catalogue.fetchall('SELECT * FROM '+t+' ORDER BY 1,2') for t in tables}
    def get(endpoint,*a,**k):
        if endpoint=='/sector/plates':
            return {'plates':[{'code':'A','name':'new A'}, *([{'name':'missing B'}] if damage=='malformed' else [])]}
        if endpoint=='/sector/ranking':
            return {'sectors':[{'sector_code':'A','sector_name':'new A','stocks':[{'stock_code':'000001'}]}, *([{'name':'missing B'}] if damage=='malformed' else [])]}
        if endpoint=='/sector/strength':return {'date':day,'strength':10} if a[0]['code']=='A' else None
        if endpoint=='/sector/stocks':return [{'stock_code':'000001','stock_name':'one'}] if a[0]['code']=='A' else None
        raise AssertionError(endpoint)
    original=catalogue.insert_rows
    def insert(table,*a,**k):
        if table=='sector_stocks':raise OSError('injected stock write failure')
        return original(table,*a,**k)
    if damage=='stock_write_failure':monkeypatch.setattr(catalogue,'insert_rows',insert)
    call=collect_sector_ranking if route=='ranking' else _collect_sector_ranking_fallback
    if damage=='partial':
        for _ in range(2):call(SimpleNamespace(get=get),catalogue,day)
        assert catalogue.fetchall('SELECT sector_code FROM sector_ranking ORDER BY sector_code')==[('A',),('B',)]
        assert catalogue.fetchall('SELECT sector_code,stock_code FROM sector_stocks ORDER BY sector_code')==[('A','000001'),('B','000002')]
    else:
        with pytest.raises(ValueError if damage=='malformed' else OSError):call(SimpleNamespace(get=get),catalogue,day)
        assert {t:catalogue.fetchall('SELECT * FROM '+t+' ORDER BY 1,2') for t in tables}==before


def test_malformed_catalogue_is_rejected_before_ranking_starts(catalogue, monkeypatch):
    from collectors import collect_sector as module
    client=SimpleNamespace(get=lambda *a: {'plates':[{'code':'A'}, {'name':'missing B'}]})
    monkeypatch.setattr(module,'collect_sector_ranking',lambda *a: pytest.fail('ranking ran before catalogue validation'))
    with pytest.raises(ValueError):module.collect_all_sector(client,catalogue,'2026-10-12')


@pytest.mark.parametrize('route',['catalogue','ranking','fallback'])
def test_merge_refreshes_only_received_keys_and_does_not_refresh_old_quotes(catalogue, route):
    from datetime import datetime
    from collectors.collect_sector import collect_sector_ranking, _collect_sector_ranking_fallback
    old=datetime(2026,1,1)
    catalogue.conn.execute('UPDATE sector_plates SET fetched_at=?',[old])
    for code,stock in [('A','000001'),('B','000002')]:
        catalogue.conn.execute('INSERT INTO sector_ranking(date,sector_code,sector_name,fetched_at) VALUES (?,?,?,?)', ['2026-10-12',code,'old',old])
        catalogue.conn.execute('INSERT INTO sector_stocks(date,sector_code,stock_code,change_pct,fetched_at) VALUES (?,?,?,?,?)', ['2026-10-12',code,stock,77,old])
    def get(endpoint,*a,**k):
        if endpoint=='/sector/plates':return {'plates':[{'code':'A','name':'new'}]}
        if endpoint=='/sector/ranking':return {'sectors':[{'sector_code':'A','stocks':[{'stock_code':'000001'}]}]}
        if endpoint=='/sector/strength':return {'date':'2026-10-12','strength':10} if a[0]['code']=='A' else None
        if endpoint=='/sector/stocks':return [{'stock_code':'000001'}] if a[0]['code']=='A' else None
        raise AssertionError(endpoint)
    call={'catalogue':collect_sector_plates,'ranking':collect_sector_ranking,'fallback':_collect_sector_ranking_fallback}[route]
    call(SimpleNamespace(get=get),catalogue,'2026-10-12')
    table='sector_plates' if route=='catalogue' else 'sector_ranking'
    clocks=dict(catalogue.fetchall('SELECT sector_code,fetched_at FROM '+table))
    assert clocks['A']>old and clocks['B']==old
    if route!='catalogue':
        quotes=catalogue.fetchall('SELECT sector_code,change_pct,fetched_at FROM sector_stocks ORDER BY sector_code')
        assert quotes[0][0:2]==('A',None) and quotes[0][2]>old
        assert quotes[1]==('B',77,old)
