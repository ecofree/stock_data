from copy import deepcopy
import json

import pytest

from tools.v2.probe_bounded_hot import config, events
from tools.v2.run_event_replay import Clock
from trade_system.v2.domain import canonical, identity
from trade_system.v2.paper_ledger import PaperBook
from trade_system.v2.paper_storage import open_paper,load_paper,_append,apply_paper_event,paper_history
from trade_system.v2.storage import Store


def seed(store,n=140):
    open_paper(store,config())
    for event in events(n):
        store.clock.set(event['at'])
        _append(store,load_paper(store,config()['account_id'],writer_session=True),event)
    return load_paper(store,config()['account_id'],writer_session=True)


def test_mixed_events_equal_legacy_and_full_restart(tmp_path):
    c=config(); c.pop('state_format'); c.pop('hot_limits')
    reference=PaperBook(c)
    clock=Clock()
    with Store(tmp_path/'p.db',clock=clock) as s:
        open_paper(s,config())
        for e in events(140):
            clock.set(e['at'])
            book=load_paper(s,c['account_id'],writer_session=True)
            assert _append(s,book,e)==reference.apply(e)
            assert len(canonical(book.state))<4000
            assert not book.seen.pending
        orders,fills=paper_history(s,book)
        assert orders==reference.state['orders'] and fills==reference.state['fills']
        digest=identity(book.state)
        assert s.con.execute('SELECT max(length(payload)) FROM paper_checkpoint').fetchone()[0]<4000
    with Store(tmp_path/'p.db',clock=clock) as s:
        restored=load_paper(s,c['account_id'],full_replay=True)
        assert identity(restored.state)==digest and restored.summary()==reference.summary()


@pytest.mark.parametrize('target',['journal','cold','index','checkpoint','archive','head','event_id','receipt','extra_identity'])
def test_full_audit_rejects_corruption(tmp_path,target):
    with Store(tmp_path/'p.db',clock=Clock()) as s:
        b=seed(s,70)
        if target=='journal': s.con.execute('DELETE FROM paper_ledger_event WHERE seq=1')
        if target=='cold': s.con.execute('DELETE FROM paper_history WHERE seq=1')
        if target=='index': s.con.execute('DELETE FROM paper_identity WHERE seq=2')
        if target=='checkpoint': s.con.execute('DELETE FROM paper_checkpoint WHERE seq=64')
        if target=='head': s.con.execute('UPDATE paper_account SET last_seq=last_seq-1')
        if target=='event_id': s.con.execute("UPDATE paper_ledger_event SET event_id='corrupt-id' WHERE seq=1")
        if target=='receipt': s.con.execute("UPDATE paper_ledger_event SET known_at=known_at+INTERVAL 1 SECOND WHERE seq=1")
        if target=='extra_identity': s.con.execute("INSERT INTO paper_identity VALUES (?,'order','extra',2)",[b.config['account_id']])
        if target=='archive':
            raw=s.con.execute('SELECT raw_hash FROM paper_ledger_event WHERE seq=1').fetchone()[0]
            (s.path.parent/(s.path.name+'.raw')/raw).write_bytes(b'corrupt')
        with pytest.raises(ValueError): load_paper(s,b.config['account_id'])


def test_durable_ids_and_failed_commit_invalidate_cache(tmp_path,monkeypatch):
    with Store(tmp_path/'p.db',clock=Clock()) as s:
        b=seed(s,28); before=identity(b.state)
        original=list(events(28))[0]; original.pop('at')
        assert apply_paper_event(s,b.config['account_id'],original)==b.summary()
        bad=deepcopy(original); bad['payload']['last_fen']=999
        with pytest.raises(ValueError,match='idempotency'): apply_paper_event(s,b.config['account_id'],bad)
        retry=deepcopy(list(events(2))[1]); retry['at']=b.state['last_at']; retry['event_id']='reuse-order'
        with pytest.raises(ValueError,match='identity already used'): _append(s,b,retry)
        b=load_paper(s,b.config['account_id'],writer_session=True)
        e={'event_id':'rollback','kind':'cash_transfer','payload':{'amount_fen':1,'evidence_id':'test'},'at':b.state['last_at']}
        real=s.archive
        def fail(payload):
            if 'nav' in json.loads(payload): raise OSError('injected cold archive failure')
            return real(payload)
        monkeypatch.setattr(s,'archive',fail)
        with pytest.raises(OSError): _append(s,b,e)
        assert not s.paper_hot_cache
        monkeypatch.setattr(s,'archive',real)
        assert identity(load_paper(s,b.config['account_id']).state)==before
        _append(s,load_paper(s,b.config['account_id'],writer_session=True),e)
        assert load_paper(s,b.config['account_id']).journal_seq==29


def test_bounded_real_service_confirmation_and_exit_path(tmp_path):
    from tools.v2.run_event_replay import run_replay
    result=run_replay(tmp_path/'case',bounded=True)
    assert result['fills']==3 and result['summary']['realized_pnl_fen']==9000


def test_bounds_fail_without_discarding_unknown_exposure(tmp_path):
    c=config();c['hot_limits']['orders']=1
    with Store(tmp_path/'p.db',clock=Clock()) as s:
        open_paper(s,c)
        for event in list(events(6)):
            s.clock.set(event['at']);_append(s,load_paper(s,c['account_id'],writer_session=True),event)
        before=load_paper(s,c['account_id']).state
        order=next(iter(before['orders'].values()))
        assert order['status']=='unknown' and order['remaining']==100
        extra=deepcopy(list(events(2))[1]);extra.update(event_id='excess',at=before['last_at'])
        extra['payload']['order_id']='another'
        with pytest.raises(ValueError,match='hot limit'):
            _append(s,load_paper(s,c['account_id'],writer_session=True),extra)
        assert load_paper(s,c['account_id']).state==before


@pytest.mark.parametrize('when',['before_commit','after_commit'])
def test_process_crash_and_idempotent_recovery(tmp_path,when):
    import subprocess
    import sys
    from pathlib import Path
    clock=Clock()
    path=tmp_path/'p.db'
    with Store(path,clock=clock) as s:
        seed(s,63)
    script='''
import os,sys
from tools.v2.probe_bounded_hot import config,events
from tools.v2.run_event_replay import Clock
from trade_system.v2.storage import Store
from trade_system.v2 import paper_storage as p
event=list(events(64))[-1]
clock=Clock(event['at'])
with Store(sys.argv[1],clock=clock) as s:
    b=p.load_paper(s,config()['account_id'],writer_session=True)
    def die(*a,**kw): os._exit(23)
    if sys.argv[2]=='before_commit':p._projection=die
    else:p._bind_history=die
    p._append(s,b,event)
'''
    proc=subprocess.run([sys.executable,'-c',script,str(path),when],cwd=Path(__file__).resolve().parents[1],timeout=30)
    assert proc.returncode==23
    event=list(events(64))[-1];clock.set(event.pop('at'))
    with Store(path,clock=clock) as s:
        before=load_paper(s,config()['account_id'])
        assert before.journal_seq==(63 if when=='before_commit' else 64)
        apply_paper_event(s,config()['account_id'],event)
        apply_paper_event(s,config()['account_id'],event)
        assert load_paper(s,config()['account_id']).journal_seq==64
    assert path.with_suffix('.db.owner.guard').exists()


def test_replay_deadline_and_failed_audit_invalidate_warm_cache(tmp_path,monkeypatch):
    import time
    from trade_system.v2 import paper_storage as storage
    with Store(tmp_path/'p.db',clock=Clock()) as s:
        b=seed(s,70);account=b.config['account_id'];expected=identity(b.state)
        original=storage._archive_body
        def expire(*args):
            original(*args)
            s.command_deadline=time.monotonic()-1
        monkeypatch.setattr(storage,'_archive_body',expire)
        with pytest.raises(TimeoutError):load_paper(s,account,full_replay=True)
        assert account not in s.paper_hot_cache
        s.command_deadline=None
        monkeypatch.setattr(storage,'_archive_body',original)
        assert identity(load_paper(s,account,writer_session=True).state)==expected
        s.con.execute('DELETE FROM paper_history WHERE seq=2')
        with pytest.raises(ValueError):load_paper(s,account,full_replay=True)
        assert account not in s.paper_hot_cache
        with pytest.raises(ValueError):load_paper(s,account,writer_session=True)


def test_ten_accounts_cache_eviction_and_multiple_active_instruments(tmp_path):
    codes=['SZ.000002','SH.600000','SZ.000001']
    configs=[];hashes={}
    with Store(tmp_path/'multi.db',clock=Clock()) as s:
        for n in range(10):
            c=config();c['account_id']=f'fixture-multi-{n}'
            c['instruments']={code:deepcopy(next(iter(c['instruments'].values()))) for code in codes}
            c['initial_lots']=[{**c['initial_lots'][0],'instrument':code} for code in codes]
            configs.append(c);open_paper(s,c)
            book=load_paper(s,c['account_id'],writer_session=True)
            # Same order identity in different accounts is allowed, never merged.
            for i,code in enumerate(codes):
                event=deepcopy(list(events(2))[1]);event['at']=c['opened_at']
                event['event_id']=f'submit-{i}';event['payload'].update(order_id=f'order-{i}',instrument=code)
                _append(s,book,event)
                _append(s,book,{'event_id':f'unknown-{i}','at':c['opened_at'],'kind':'unknown',
                    'payload':{'order_id':f'order-{i}','evidence_id':'fixture-unknown'}})
            assert len(book.state['orders'])==3 and len(book.state['lots'])==3
            hashes[c['account_id']]=identity(book.state)
            assert len(s.paper_hot_cache)<=8
        assert configs[0]['account_id'] not in s.paper_hot_cache
        for c in configs:
            restored=load_paper(s,c['account_id'],writer_session=True)
            assert identity(restored.state)==hashes[c['account_id']]
            assert all(o['status']=='unknown' for o in restored.state['orders'].values())
            assert len(s.paper_hot_cache)<=8
    with Store(tmp_path/'multi.db',clock=Clock()) as s:
        for c in configs:
            assert identity(load_paper(s,c['account_id'],full_replay=True).state)==hashes[c['account_id']]
