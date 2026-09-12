"""Synthetic quote qualification; no fixture is real intraday acceptance."""
from datetime import datetime
import json

import duckdb
import pytest

from trade_system.v2 import observation_workspace as watch, research_product as product
from trade_system.file_lock import FileLock,FileLockBusy


@pytest.fixture
def con():
    c=duckdb.connect(':memory:')
    c.execute('''CREATE TABLE multi_source_quote(source_date DATE,asset_type VARCHAR,asset_code VARCHAR,
        price DOUBLE,change_pct DOUBLE,provider VARCHAR,fetched_at TIMESTAMP,is_stale BOOLEAN,source_event_time TIMESTAMP)''')
    yield c
    c.close()


def add(c,provider='hithink',price=10,event='2026-09-11 10:00:00',received='2026-09-11 10:00:01',stale=False):
    c.execute("INSERT INTO multi_source_quote VALUES ('2026-09-11','stock','000001.SZ',?,1,?,?,?,?)",[price,provider,received,stale,event])


def test_priority_only_after_source_time_and_quality(con):
    add(con);add(con,'tencent',11,received='2026-09-11 10:00:04')
    value=watch.project(con,['000001','600001'],'2026-09-11T10:01:00+08:00')
    assert value['qualified']==1 and value['rows'][0]['provider']=='hithink'
    assert value['rows'][1]['price'] is None and value['execution_ready'] is False
    con.execute("UPDATE multi_source_quote SET is_stale=true WHERE provider='hithink'")
    assert watch.project(con,['000001'],'2026-09-11T10:01:00+08:00')['rows'][0]['provider']=='tencent'


@pytest.mark.parametrize('event,received,reason',[
    (None,'2026-09-11 10:00:01','missing_source_time'),
    ('2026-09-11 09:00:00','2026-09-11 10:00:01','expired'),
    ('2026-09-11 10:00:02','2026-09-11 10:00:01','invalid_time_order'),
    ('2026-09-10 10:00:00','2026-09-11 10:00:01','source_date_mismatch')])
def test_receipt_recency_is_not_quote_recency(con,event,received,reason):
    add(con,event=event,received=received)
    row=watch.project(con,['000001'],'2026-09-11T10:01:00+08:00')['rows'][0]
    assert row['price'] is None and reason in row['rejected_reasons']


def test_quote_expires_during_view_without_republishing(con):
    add(con)
    original=watch.project(con,['000001'],'2026-09-11T10:01:00+08:00')
    before=json.dumps(original)
    shown=watch.present(original,'2026-09-11T10:06:00+08:00')
    assert shown['qualified']==0 and shown['rows'][0]['price'] is None
    assert json.dumps(original)==before
    original['rows'][0]['price']=999
    with pytest.raises(ValueError,match='changed'):watch.present(original,'2026-09-11T10:06:00+08:00')


def test_conflicting_primary_quote_is_not_silently_selected(con):
    add(con);add(con,price=20)
    row=watch.project(con,['000001'],'2026-09-11T10:01:00+08:00')['rows'][0]
    assert row['state']=='conflicting_same_priority_quotes' and row['price'] is None


def test_legacy_missing_event_column_never_promotes_receipt_time(con):
    add(con);con.execute('ALTER TABLE multi_source_quote DROP COLUMN source_event_time')
    row=watch.project(con,['000001'],'2026-09-11T10:01:00+08:00')['rows'][0]
    assert row['price'] is None and row['rejected_reasons']==['missing_source_time']


def test_observation_shares_daily_update_lock_before_any_reads(tmp_path):
    with FileLock(tmp_path/'update.guard'):
        with pytest.raises(FileLockBusy):product.observe(tmp_path)
    assert not (tmp_path/'observation-publication').exists()


def test_bad_optional_quote_snapshot_does_not_take_down_judgements(tmp_path,monkeypatch):
    folder=tmp_path/'observation-publication';folder.mkdir()
    (folder/'current.json').write_text('broken synthetic pointer')
    def fail(*args):raise ValueError('synthetic corrupt quote artifact')
    monkeypatch.setattr(product,'read_current',fail)
    result=product.journal_projection(tmp_path,{'reviews':[]})
    assert result['notes']==[] and result['observation']['error']=='observation_snapshot_unavailable'
    assert result['observation']['qualified']==0


def test_bounds_and_aware_clock(con):
    with pytest.raises(ValueError):watch.project(con,['../a'],'2026-09-11T10:01:00+08:00')
    with pytest.raises(ValueError):watch.project(con,['000001'],'2026-09-11T10:01:00')
    with pytest.raises(ValueError):watch.project(con,[str(n).zfill(6) for n in range(201)],'2026-09-11T10:01:00+08:00')
    assert watch.local_clock('2026-09-11T02:00:00+00:00')==datetime(2026,9,11,10)


def test_quote_store_preserves_source_time_identity_and_zero_change():
    from zoneinfo import ZoneInfo
    from trade_system.multi_source_store import MultiSourceStore
    now=datetime.now(ZoneInfo('Asia/Shanghai'))
    with MultiSourceStore(':memory:') as store:
        data={'code':'000001','price':10,'change_pct':0,'source_event_time':now.isoformat()}
        store.store('valuation','000001',data,{'status':'live','source':'hithink'},asset_type='stock')
        row=store.con.execute('SELECT source_date,source_event_time,collected_at,fetched_at,change_pct FROM multi_source_quote').fetchone()
        assert row[0]==now.date() and row[1]==now.replace(tzinfo=None)
        assert row[2]==row[3] and row[3]>=row[1] and row[4]==0
        observed=watch.project(store.con,['000001'],datetime.now(ZoneInfo('Asia/Shanghai')).isoformat())
        assert observed['qualified']==1
        with pytest.raises(ValueError,match='identity mismatch'):
            store.store('valuation','000001',dict(data,code='600001'),{'status':'live','source':'hithink'},asset_type='stock')
        assert store.con.execute('SELECT count(*) FROM multi_source_quote').fetchone()[0]==1


def test_quote_time_never_falls_back_to_fetch_or_naive_iso():
    from trade_system.units import quote_source_event_time
    assert quote_source_event_time({'fetched_at':'2026-09-11T10:00:00+08:00'},'hithink') is None
    assert quote_source_event_time({'source_event_time':'2026-09-11T10:00:00'},'hithink') is None
    assert quote_source_event_time({'time':'20260911100000'},'tencent')==datetime(2026,9,11,10)
    assert quote_source_event_time({'time':'20260911100000'},'unknown') is None
    assert quote_source_event_time({'source_event_time':'2026-09-11T02:00:00+00:00'},'hithink')==datetime(2026,9,11,10)
