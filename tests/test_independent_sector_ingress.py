"""Real retained entrypoints: validate all responses before atomic keyed writes."""
from contextlib import closing
from types import SimpleNamespace
import shutil

import pytest

from collectors import collect_sector as module
from trade_system.data_store import DuckDBStore
from trade_system.schema import init_schema
from trade_system.source_validation import validate_kpl

DAY='2026-10-12'
TABLES=('sector_plates','sector_ranking','sector_strength','sector_stocks')


@pytest.fixture(scope='module')
def sector_template(tmp_path_factory):
    path=tmp_path_factory.mktemp('sector-template')/'seed.duckdb'
    with closing(DuckDBStore(path)) as db:
        init_schema(db.conn)
        db.conn.execute("INSERT INTO sector_plates(sector_code,sector_name) VALUES ('A','old A'),('B','old B')")
        db.conn.execute("INSERT INTO sector_strength(date,sector_code,strength_value,fetched_at) VALUES (?,'A',9,'2026-10-12 12:00')",[DAY])
        db.conn.execute("INSERT INTO sector_stocks(date,sector_code,stock_code,stock_name,change_pct,fetched_at) VALUES (?,'A','000001','old',7,'2026-10-12 12:00')",[DAY])
    return path


@pytest.fixture
def store(tmp_path,sector_template):
    path=tmp_path/'sectors.duckdb'; shutil.copy2(sector_template,path)
    with closing(DuckDBStore(path)) as db:yield db


def snapshot(store):
    return {table:store.fetchall('SELECT * FROM '+table+' ORDER BY 1,2,3') for table in TABLES}


def supplier(*, shape='dict', damage=None, fallback=True):
    def get(endpoint,params=None,**kwargs):
        code=(params or {}).get('code','A')
        if endpoint=='/sector/plates':return {'plates':[{'code':'A','name':'new A'},{'code':'B','name':'new B'}]}
        if endpoint=='/sector/ranking':
            return {'date':DAY,'sectors':[] if fallback else [{'sector_code':'A','stock_count':1,'stocks':[{'stock_code':'000001'}]}]}
        if endpoint=='/sector/strength':
            return {'date':DAY,'sector_code':'WRONG' if damage=='identity' and code=='B' else code,'strength':1,'zhangting':2}
        if endpoint=='/sector/stocks':
            rows=[{'stock_code':'000001','stock_name':'new','change_pct':3,'date':DAY}]
            if code=='B':
                if damage=='duplicate':rows.append(dict(rows[0],stock_name='conflict'))
                if damage in ('null','blank','boolean'):rows[0]['stock_code']={'null':None,'blank':' ','boolean':True}[damage]
                if damage=='wrong_date':rows[0]['date']='2026-10-11'
                if damage=='nan':rows[0]['change_pct']=float('nan')
                if damage=='negative_amount':rows[0]['turnover']=-1
                if damage=='fractional_count':return {'stocks':{},'date':DAY}
            return rows if shape=='list' else {'date':DAY,'sector_code':code,'stocks':rows}
        return None
    return SimpleNamespace(get=get)


@pytest.mark.parametrize('entry',['fallback','members','all'])
@pytest.mark.parametrize('damage',['duplicate','null','blank','boolean','wrong_date','nan','negative_amount','fractional_count'])
def test_last_bad_sector_cannot_publish_earlier_good_sector(store,entry,damage):
    before=snapshot(store)
    call={'fallback':lambda:module._collect_sector_ranking_fallback(supplier(damage=damage),store,DAY),
          'members':lambda:module.collect_sector_stocks(supplier(damage=damage),store,DAY,['A','B']),
          'all':lambda:module.collect_all_sector(supplier(damage=damage),store,DAY)}[entry]
    with pytest.raises(ValueError):call()
    assert snapshot(store)==before


@pytest.mark.parametrize('entry',['fallback','all','strength'])
def test_wrong_sector_that_gateway_accepts_is_rejected_before_any_projection(store,entry):
    client=supplier(damage='identity')
    response=client.get('/sector/strength',{'code':'B','date':DAY})
    assert validate_kpl('/sector/strength', {'code':'B','date':DAY}, response).ok
    before=snapshot(store)
    with pytest.raises(ValueError,match='identity'):
        if entry=='fallback':module._collect_sector_ranking_fallback(client,store,DAY)
        elif entry=='all':module.collect_all_sector(client,store,DAY)
        else:module.collect_sector_strength(client,store,DAY,['A','B'])
    assert snapshot(store)==before


@pytest.mark.parametrize('entry',['fallback','all','strength'])
def test_undated_strength_cannot_overwrite_or_refresh_a_dated_fact(store,entry):
    original=supplier().get
    def get(endpoint,params=None,**kwargs):
        response=original(endpoint,params,**kwargs)
        if endpoint=='/sector/strength':response.pop('date')
        return response
    client=SimpleNamespace(get=get)
    before=snapshot(store)
    with pytest.raises(ValueError,match='explicit strength date'):
        if entry=='fallback':module._collect_sector_ranking_fallback(client,store,DAY)
        elif entry=='all':module.collect_all_sector(client,store,DAY)
        else:module.collect_sector_strength(client,store,DAY,['A','B'])
    assert snapshot(store)==before


@pytest.mark.parametrize('shape',['dict','list'])
@pytest.mark.parametrize('fallback',[True,False])
def test_complete_retained_entry_twice_is_idempotent_on_all_core_keys(store,shape,fallback):
    client=supplier(shape=shape,fallback=fallback)
    for _ in range(2):module.collect_all_sector(client,store,DAY)
    for table,keys in [('sector_plates','sector_code'),('sector_ranking','date,sector_code'),('sector_strength','date,sector_code'),('sector_stocks','date,sector_code,stock_code')]:
        assert store.fetchall(f'SELECT count(*) FROM (SELECT {keys} FROM {table} GROUP BY {keys} HAVING count(*)>1)')==[(0,)]
    assert store.fetchall('SELECT stock_name,change_pct FROM sector_stocks ORDER BY sector_code')==[('new',3),('new',3)]


def test_core_publish_failure_rolls_back_catalogue_and_all_projections(store,monkeypatch):
    before=snapshot(store); original=store.insert_rows
    def fail(table,*args,**kwargs):
        if table=='sector_stocks':raise OSError('injected publish failure')
        return original(table,*args,**kwargs)
    monkeypatch.setattr(store,'insert_rows',fail)
    with pytest.raises(OSError):module.collect_all_sector(supplier(),store,DAY)
    assert snapshot(store)==before


def test_undated_members_do_not_refresh_old_quote_and_absent_keys_are_retained(store):
    client=SimpleNamespace(get=lambda *a,**k:[['000001','new',88],['000002','second',None]])
    module.collect_sector_stocks(client,store,DAY,['A'])
    assert store.fetchall('SELECT stock_code,change_pct FROM sector_stocks ORDER BY stock_code')==[('000001',None),('000002',None)]
    client=SimpleNamespace(get=lambda *a,**k:{'stocks':[],'date':DAY})
    before=snapshot(store)
    module.collect_sector_stocks(client,store,DAY,['A'])
    assert snapshot(store)==before


@pytest.mark.parametrize('value', [-1,True,1.5,float('inf')])
def test_ranking_invalid_stock_count_cannot_publish(store,value):
    data={'sectors':[{'sector_code':'A','stock_count':value,'stocks':[]}]}
    before=snapshot(store)
    with pytest.raises(ValueError):module.collect_sector_ranking(SimpleNamespace(get=lambda *a,**k:data),store,DAY)
    assert snapshot(store)==before


def test_fallback_partial_members_cannot_manufacture_ranking_stock_count(store):
    store.conn.execute("INSERT INTO sector_ranking(date,sector_code,stock_count) VALUES (?,'A',100)",[DAY])
    module._collect_sector_ranking_fallback(supplier(),store,DAY)
    assert store.fetchall('SELECT sector_code,stock_count FROM sector_ranking ORDER BY sector_code')==[('A',None),('B',None)]
