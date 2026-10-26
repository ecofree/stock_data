from datetime import datetime
from pathlib import Path
import hashlib
import json

import duckdb
import pytest

from trade_system.normalize import _create_market_daily, _create_limit_pool
from trade_system.multi_source_store import MultiSourceStore
from trade_system.quality import DEFAULT_DUPLICATE_KEYS, dedupe_table


@pytest.mark.parametrize('kind,asset', [('kline','stock'),('index_kline','index'),('etf_kline','etf'),('cb_kline','cb')])
def test_distinct_adjustment_products_survive_retries_and_repair(tmp_path,kind,asset):
    db=tmp_path/'products.duckdb'
    with MultiSourceStore(db) as store:
        for adjustment, price in [('none',10),('qfq',5),('hfq',20),('qfq',6)]:
            store.store(kind,'000001',[{'date':'2026-10-12','close':price,'adjustment':adjustment}],
                        {'source':'fixture','status':'live'})
        assert store.con.execute('SELECT adjustment,close FROM multi_source_kline ORDER BY adjustment').fetchall()==[('hfq',20),('none',10),('qfq',6)]
        assert store.con.execute('SELECT DISTINCT asset_type FROM multi_source_kline').fetchone()==(asset,)
        from trade_system.normalize import _multisource_bar_sql
        raw=store.con.execute(_multisource_bar_sql(store.con,asset)).description
        names=[r[0] for r in raw]
        rows=store.con.execute(_multisource_bar_sql(store.con,asset)).fetchall()
        assert [(r[names.index('adjustment')],r[names.index('close')]) for r in rows]==[('none',10)]
    result=dedupe_table(db,'multi_source_kline',DEFAULT_DUPLICATE_KEYS['multi_source_kline'])
    assert result['removed_rows']==0
    with duckdb.connect(str(db),read_only=True) as con:
        assert con.execute('SELECT count(*) FROM multi_source_kline').fetchone()[0]==3


def test_cache_hit_cannot_certify_a_different_materialized_product(tmp_path):
    data=[{'date':'2026-10-12','close':5,'adjustment':'qfq'}]
    with MultiSourceStore(tmp_path/'cache.duckdb') as store:
        store.store('kline','000001',data,{'source':'fixture','status':'live'})
        assert store.store('kline','000001',data,{'source':'fixture','status':'fresh'})['status']=='fresh'
        store.con.execute("UPDATE multi_source_kline SET adjustment='hfq'")
        assert store.store('kline','000001',data,{'source':'fixture','status':'fresh'})['status']=='cache_unmaterialized'


def test_existing_key_is_migrated_transactionally_not_silently_replaced(tmp_path):
    from trade_system.migrations import apply_pending
    db=tmp_path/'old.duckdb'
    with MultiSourceStore(db) as store:
        store.con.execute('DROP INDEX uq_multi_source_kline_product')
        store.con.execute('CREATE UNIQUE INDEX uq_multi_source_kline ON multi_source_kline(source_date,asset_type,asset_code,provider)')
        store.con.execute("INSERT INTO multi_source_kline(source_date,asset_type,asset_code,provider,adjustment,close) VALUES ('2026-10-12','stock','000001','fixture','none',10)")
    with pytest.raises(ValueError,match='0016'):
        MultiSourceStore(db)
    migration=Path(__file__).resolve().parents[1]/'migrations/0016_kline_product_identity.sql'
    folder=tmp_path/'migrations';folder.mkdir();(folder/migration.name).write_bytes(migration.read_bytes())
    with duckdb.connect(str(db)) as con:
        before=con.execute('SELECT * FROM multi_source_kline').fetchall()
        assert apply_pending(con,folder)==[16]
        assert apply_pending(con,folder)==[]
        assert con.execute('SELECT * FROM multi_source_kline').fetchall()==before
        con.execute("INSERT INTO multi_source_kline(source_date,asset_type,asset_code,provider,adjustment,close) VALUES ('2026-10-12','stock','000001','fixture','qfq',5)")
        assert con.execute('SELECT count(*) FROM multi_source_kline').fetchone()[0]==2


@pytest.mark.parametrize('old_shape',[False,True])
def test_product_key_migration_handles_absent_optional_bar_table(old_shape):
    migration=Path(__file__).resolve().parents[1]/'migrations/0016_kline_product_identity.sql'
    with duckdb.connect(':memory:') as con:
        if old_shape:
            con.execute('CREATE TABLE multi_source_kline(source_date DATE,asset_type VARCHAR,asset_code VARCHAR,provider VARCHAR,close DOUBLE)')
            con.execute("INSERT INTO multi_source_kline VALUES ('2026-10-12','stock','000001','unknown',10)")
        con.execute(migration.read_text())
        con.execute(migration.read_text())
        assert con.execute('SELECT count(*) FROM multi_source_kline').fetchone()[0]==int(old_shape)
        assert 'adjustment' in con.execute("SELECT expressions FROM duckdb_indexes() WHERE index_name='uq_multi_source_kline_product'").fetchone()[0]
        if old_shape:
            assert con.execute('SELECT close,adjustment FROM multi_source_kline').fetchone()==(10,None)


def test_duplicate_product_migration_aborts_without_repairing_or_losing_rows(tmp_path):
    from trade_system.migrations import apply_pending
    migration=Path(__file__).resolve().parents[1]/'migrations/0016_kline_product_identity.sql'
    folder=tmp_path/'migrations';folder.mkdir();(folder/migration.name).write_bytes(migration.read_bytes())
    with duckdb.connect(':memory:') as con:
        con.execute('CREATE TABLE multi_source_kline(source_date DATE,asset_type VARCHAR,asset_code VARCHAR,provider VARCHAR,adjustment VARCHAR,close DOUBLE)')
        con.execute("INSERT INTO multi_source_kline VALUES ('2026-10-12','stock','000001','fixture','qfq',5),('2026-10-12','stock','000001','fixture','qfq',6)")
        before=con.execute('SELECT * FROM multi_source_kline').fetchall()
        with pytest.raises(RuntimeError,match='migration 16') as error:
            apply_pending(con,folder)
        assert isinstance(error.value.__cause__,duckdb.ConstraintException)
        assert con.execute('SELECT * FROM multi_source_kline').fetchall()==before
        assert con.execute('SELECT count(*) FROM schema_migration WHERE version=16').fetchone()[0]==0


@pytest.mark.parametrize('case,expected', [('no_pool',None),('missing_day',None),('partial',None),('complete',2),('complete_empty',0)])
def test_market_total_requires_complete_dated_receipt(case,expected):
    with duckdb.connect(':memory:') as con:
        con.execute('CREATE TABLE tushare_daily(date DATE,stock_code VARCHAR,change_pct DOUBLE,fetched_at TIMESTAMP)')
        con.execute("INSERT INTO tushare_daily VALUES ('2026-10-12','000001',10,'2026-10-12 16:00:00')")
        if case != 'no_pool':
            con.execute('CREATE TABLE eastmoney_limit_up_pool(date DATE,stock_code VARCHAR,board_level INTEGER)')
        if case == 'partial':
            con.execute("INSERT INTO eastmoney_limit_up_pool VALUES ('2026-10-12','000001',2)")
        if case.startswith('complete'):
            from zoneinfo import ZoneInfo
            n=0 if case=='complete_empty' else 2
            con.execute('CREATE TABLE official_limit_pool(trade_date DATE,stock_code VARCHAR,source VARCHAR,continue_day_cnt INTEGER)')
            con.execute('CREATE TABLE history_fetch_checkpoint(dataset VARCHAR,trade_date DATE,page_no INTEGER,status VARCHAR,rows_written INTEGER,last_error VARCHAR,updated_at TIMESTAMP)')
            if n:
                con.execute("INSERT INTO official_limit_pool VALUES ('2026-10-12','000001','hithink',2),('2026-10-12','000002','hithink',1)")
            receipt={'path':'/api/a-share/special-data/limit-up-pool','params':{'date_ms':int(datetime(2026,10,12,tzinfo=ZoneInfo('Asia/Shanghai')).timestamp()*1000)},
                     'total':n,'pages':1,'items_sha256':hashlib.sha256(b'[]' if not n else b'fixture').hexdigest()}
            con.execute("INSERT INTO history_fetch_checkpoint VALUES ('hithink_limit_pool','2026-10-12',0,'success',?,?,'2026-10-12 17:00:00')",[n,json.dumps(receipt)])
        _create_market_daily(con)
        up,status,source=con.execute('SELECT limit_up_count,market_daily_status,market_daily_source FROM v_market_daily').fetchone()
        assert up==expected
        assert status==('partial' if expected is None else 'estimated')
        assert ('missing' in source)==(expected is None)
        if case=='complete':
            con.execute("DELETE FROM official_limit_pool WHERE stock_code='000002'")
            assert con.execute('SELECT limit_up_count,market_daily_status FROM v_market_daily').fetchone()==(None,'partial')


def test_limit_source_priority_only_compares_native_board_facts():
    with duckdb.connect(':memory:') as con:
        con.execute('CREATE TABLE l2_realtime_all_boards(date DATE,stock_code VARCHAR,stock_name VARCHAR,board_level INTEGER,limit_up_time VARCHAR,fetched_at TIMESTAMP)')
        con.execute("INSERT INTO l2_realtime_all_boards VALUES ('2026-10-12','000001','fixture',2,'09:30','2026-10-12 16:00')")
        con.execute('CREATE TABLE xdf_limit_pool(trade_date DATE,ts_code VARCHAR,name VARCHAR,board_level INTEGER,limit_times DOUBLE,fetched_at TIMESTAMP)')
        con.execute("INSERT INTO xdf_limit_pool VALUES ('2026-10-12','000001.SZ','fixture',3,NULL,'2026-10-12 17:00')")
        _create_limit_pool(con)
        assert con.execute('SELECT board_level,source FROM v_limit_pool').fetchone()==(2,'kpl_l2')
        con.execute('UPDATE xdf_limit_pool SET limit_times=3')
        assert con.execute('SELECT board_level,source FROM v_limit_pool').fetchone()==(3,'xiaodefa')
        con.execute('CREATE TABLE official_limit_pool(trade_date DATE,continue_day_cnt INTEGER,stock_code VARCHAR,stock_name VARCHAR,limit_up_time VARCHAR,fetched_at TIMESTAMP,source VARCHAR)')
        con.execute("INSERT INTO official_limit_pool VALUES ('2026-10-12',4,'000001','fixture','09:30','2026-10-12 15:00','hithink')")
        _create_limit_pool(con)
        assert con.execute('SELECT board_level,source FROM v_limit_pool').fetchone()==(4,'hithink')
