"""Synthetic event load only; never put test judgements in a real workspace."""
import json
import time

import pytest

from trade_system.v2 import journal_index as index, research_journal as journal
from trade_system.v2.domain import identity


def note(number):
    value={'instrument':'000001','prediction_id':None,'operator':'SYNTHETIC ONLY',
           'hypothesis':str(number),'invalidation':'synthetic', 'intent':'observe',
           'received_at':'2026-09-15T00:00:00+00:00','request_id':f'{number:032x}',
           'command_id':identity({'n':number}),'supersedes':None,'execution_ready':False}
    return dict(value,note_id=identity(value))


def test_index_retry_and_recovery_after_event_saved_before_cache_commit(tmp_path,monkeypatch):
    index.rebuild(tmp_path)
    value=note(1)
    with monkeypatch.context() as m:
        m.setattr(index,'appended',lambda *a:(_ for _ in ()).throw(OSError('synthetic cache crash')))
        with pytest.raises(OSError):journal.append_note(tmp_path,value)
    assert journal.read_note(tmp_path,value['note_id'])==value
    with pytest.raises(RuntimeError,match='stale'):index.recent(tmp_path,'note')
    index.ensure(tmp_path)
    assert index.lookup(tmp_path,'note','request',value['request_id'])==value
    assert index.recent(tmp_path,'note')[1]==1
    before=(tmp_path/'notes'/(value['note_id']+'.json')).read_bytes()
    journal.append_note(tmp_path,value)
    assert index.recent(tmp_path,'note')[1]==1
    assert before==(tmp_path/'notes'/(value['note_id']+'.json')).read_bytes()


def test_changed_selected_event_is_not_hidden_by_index(tmp_path):
    value=note(1);journal.append_note(tmp_path,value)
    path=tmp_path/'notes'/(value['note_id']+'.json')
    path.write_text(json.dumps(dict(value,hypothesis='tampered')),encoding='utf-8')
    with pytest.raises(ValueError,match='identity changed'):
        index.lookup(tmp_path,'note','request',value['request_id'])
    with pytest.raises(ValueError):index.rebuild(tmp_path)


def test_read_only_legacy_workspace_and_keyset_pagination(tmp_path):
    for i in range(13):
        value=note(i);journal.durable_event(tmp_path/'notes',value['note_id'],value)
    assert index.recent(tmp_path,'note')[1]==13
    assert not index.cache_path(tmp_path).exists()
    index.rebuild(tmp_path)
    seen=[];cursor=None
    while True:
        page=index.history(tmp_path,before=cursor,limit=5)
        seen.extend(n['note_id'] for n in page['events'])
        cursor=page['next_before']
        if cursor is None:break
    assert len(seen)==len(set(seen))==13
    with pytest.raises(ValueError):index.history(tmp_path,before='unknown')


def test_long_history_reads_do_not_enumerate_event_directories(tmp_path,monkeypatch,record_property):
    start=time.perf_counter()
    for i in range(10000):
        value=note(i);journal.durable_event(tmp_path/'notes',value['note_id'],value)
    index.rebuild(tmp_path)
    record_property('synthetic_event_count',10000)
    record_property('prepare_seconds',round(time.perf_counter()-start,3))
    reads=[];original=index.event
    def read(path,kind):reads.append(path);return original(path,kind)
    monkeypatch.setattr(index,'event',read)
    monkeypatch.setattr(index,'paths',lambda *a:pytest.fail('hot read enumerated history'))
    start=time.perf_counter()
    for _ in range(10):
        paths,total=index.recent(tmp_path,'note')
        assert len(paths)==100 and total==10000
        assert index.lookup(tmp_path,'note','request',f'{5000:032x}')['request_id']==f'{5000:032x}'
    assert len(reads)==10
    assert len(index.history(tmp_path)['events'])==100
    assert len(reads)==110
    record_property('ten_hot_reads_seconds',round(time.perf_counter()-start,3))
    # SQLite must search a bounded ordered index, not sort or scan all events.
    with index.reader(tmp_path) as con:
        plan=str(con.execute('EXPLAIN QUERY PLAN SELECT path FROM events WHERE kind=? ORDER BY received DESC,id DESC LIMIT 100',('note',)).fetchall())
    assert 'by_recent' in plan and 'TEMP B-TREE' not in plan


def test_old_review_still_invalidates_recent_plan_without_history_scan(tmp_path,monkeypatch):
    value=note(1);journal.append_note(tmp_path,value)
    review={'note_id':value['note_id'],'prediction_id':None,'instrument':'000001',
            'received_at':'2026-09-14T00:00:00+00:00','request_id':'b'*32,
            'conclusion':'triggered','command_id':'c'*64}
    review['review_id']=identity(review)
    journal.durable_event(tmp_path/'notes/reviews',review['review_id'],review)
    for i in range(200):
        other=dict(review,request_id=f'{i+100:032x}',conclusion='pending')
        other.pop('review_id');other['review_id']=identity(other)
        journal.durable_event(tmp_path/'notes/reviews',other['review_id'],other)
    index.rebuild(tmp_path)
    monkeypatch.setattr(index,'paths',lambda *a:pytest.fail('invalidations scanned history'))
    state=index.invalidations(tmp_path,[{'plan_id':'d'*64,'note_id':value['note_id']}])
    assert state['invalid_notes']=={value['note_id']}
