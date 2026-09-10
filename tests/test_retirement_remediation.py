"""Behavioral counterexamples from the f8067b1 audit, using only temporary DBs."""
from datetime import timedelta
import subprocess
import sys

import duckdb
import pytest

from trade_system.v2.domain import utc
from trade_system.v2.storage import Store
from trade_system.v2.event_bridge import KINDS, event_signal
from tools.v2.run_event_replay import Clock, policies


class AdvancingClock:
    def __init__(self):
        self.at = utc('2026-09-10T09:31:00+08:00')

    def __call__(self):
        self.at += timedelta(microseconds=1)
        return self.at


def test_legacy_direct_connection_rejects_stopped_v2_without_writes(tmp_path):
    from trade_system.db_utils import legacy_connect
    path=tmp_path/'paper.duckdb'
    with Store(path):
        pass
    before=path.read_bytes()
    with pytest.raises(ValueError,match='refuses V2'):
        legacy_connect(path)
    assert path.read_bytes()==before
    with legacy_connect(path,read_only=True) as con:
        assert con.execute('SELECT count(*) FROM v2_schema').fetchone()[0]==1
    with legacy_connect(tmp_path/'legacy.duckdb') as con:
        con.execute('CREATE TABLE legacy_ok(x INTEGER)')


def test_expired_running_transaction_rolls_back_without_killing_writer(tmp_path,monkeypatch):
    import threading
    from trade_system.v2.service import Service
    dispatch=Service._dispatch
    entered=threading.Event()
    release=threading.Event()
    def slow(store,command,data):
        if command=='slow_transaction':
            with store.transaction():
                store.con.execute("INSERT INTO data_product VALUES ('expired','CNY','point','fixture','fixture')")
                entered.set()
                release.wait(.08)
            return True
        return dispatch(store,command,data)
    monkeypatch.setattr(Service,'_dispatch',staticmethod(slow))
    with Service(tmp_path/'paper.duckdb') as service:
        future=service.submit('slow_transaction',budget_seconds=.05)
        assert entered.wait(1)
        assert service.health()['active']['command']=='slow_transaction'
        with pytest.raises(TimeoutError,match='safe boundary'):
            future.result(2)
        assert service.health()['writer_alive']
        # Same name can now register normally: timed-out transaction rolled back.
        service.submit('product',dataset='expired',unit='CNY',semantics='point',consumer='different',origin='fixture').result(2)


@pytest.mark.parametrize('delivered',[False,True])
def test_unknown_requires_full_paper_journal_reconciliation(tmp_path,delivered):
    from tests.test_v2_event_replay import confirm
    from trade_system.v2.decisions import DecisionService,RiskPolicy
    from trade_system.v2.paper_storage import submit_confirmed_buy
    from trade_system.v2.operator_workflow import review_desk
    with Store(tmp_path/'paper.duckdb',clock=Clock()) as store:
        for kind,(unit,semantics) in KINDS.items():
            store.register_product('fixture.'+kind,unit,semantics,'event:'+kind,'synthetic_fixture')
        confirmed=confirm(store)
        authority=DecisionService(store,RiskPolicy(**confirmed['policy']))
        if delivered:
            submit_confirmed_buy(store,'fixture-event-paper','confirm','order')
            with pytest.raises(ValueError,match='delivered'):
                authority.reconcile_paper_unsent('confirm',operator='fixture',request_id='reconcile')
        else:
            authority.mark_unknown(confirmed['reservation_id'])
            result=authority.reconcile_paper_unsent('confirm',operator='fixture',request_id='reconcile')
            assert result['status']=='reconciled_not_sent'
            assert result==authority.reconcile_paper_unsent('confirm',operator='fixture',request_id='reconcile')
            assert review_desk(store,'fixture-event-paper')['review']['attribution']['decision_outcomes'][0]['outcome']=='reconciled_not_sent'


def test_no_unguarded_legacy_duckdb_writer_calls():
    import ast
    from pathlib import Path
    root=Path(__file__).resolve().parents[1]
    files=subprocess.check_output(['git','ls-files','*.py'],cwd=root,text=True).splitlines()
    offenders=[]
    for relative in files:
        if relative.startswith(('tests/','tools/v2/','trade_system/v2/','trade_system/vendor/')) or relative=='trade_system/db_utils.py':
            continue
        tree=ast.parse((root/relative).read_text(encoding='utf-8-sig'))
        for node in ast.walk(tree):
            if isinstance(node,ast.ImportFrom) and node.module=='duckdb' and any(a.name=='connect' for a in node.names):
                offenders.append((relative,node.lineno,'unreviewed connection alias'))
            if isinstance(node,ast.Call) and isinstance(node.func,ast.Attribute) and isinstance(node.func.value,ast.Name) and (node.func.value.id,node.func.attr)==('duckdb','connect'):
                readonly=any(k.arg=='read_only' and isinstance(k.value,ast.Constant) and k.value.value is True for k in node.keywords)
                if not readonly:
                    offenders.append((relative,node.lineno,'unguarded writer'))
    assert not offenders,offenders


@pytest.mark.parametrize('clock', [Clock(), AdvancingClock(), None])
def test_event_signal_captures_one_cutoff(tmp_path, clock):
    with Store(tmp_path/'v2.duckdb', **({'clock': clock} if clock else {})) as store:
        for kind, (unit, semantics) in KINDS.items():
            store.register_product('fixture.'+kind, unit, semantics, 'event:'+kind, 'synthetic_fixture')
        key = event_signal(store, 'SZ.000002', *policies())
        assert store.con.execute('SELECT count(*) FROM signal_event WHERE signal_id=?', [key]).fetchone()[0] == 1


@pytest.mark.parametrize('effective', ['2026-09-12T00:00:00Z', 'not-a-date'])
def test_fact_retry_must_not_hide_conflict_or_invalid_effective_time(tmp_path, effective):
    with Store(tmp_path/'v2.duckdb', clock=Clock()) as store:
        store.register_product('notice', 'CNY', 'announced_event', 'audit', 'synthetic_fixture')
        args = ('notice', 'SZ.000002', '2026-09-09T00:00:00Z', 1, 'r1', b'original')
        first = store.ingest(*args, effective_at='2026-09-11T00:00:00Z')
        assert store.ingest(*args, effective_at='2026-09-11T00:00:00Z')['deduplicated'] == 1
        with pytest.raises(ValueError):
            store.ingest(*args, effective_at=effective)
        assert store.con.execute('SELECT fact_id FROM fact').fetchall() == [(first['fact_id'],)]


def test_fact_revision_cannot_change_raw_content(tmp_path):
    with Store(tmp_path/'v2.duckdb', clock=Clock()) as store:
        store.register_product('quote', 'CNY', 'point', 'audit', 'synthetic_fixture')
        args = ('quote', 'SZ.000002', '2026-09-09T00:00:00Z', 1, 'r1')
        store.ingest(*args, b'original')
        with pytest.raises(ValueError, match='conflict'):
            store.ingest(*args, b'changed')


@pytest.mark.parametrize('method', ['ensure_paper_tables', 'submit_paper_order', 'simulate_fill', 'release_t1_sellable'])
def test_retired_paper_module_never_opens_database(tmp_path, method):
    from trade_system import paper_execution
    db = tmp_path/'must-not-exist.duckdb'
    with pytest.raises(RuntimeError, match='retired'):
        getattr(paper_execution, method)(db)
    assert not db.exists()


def test_retired_paper_cannot_write_stopped_v2(tmp_path):
    from trade_system.paper_execution import ensure_paper_tables
    db = tmp_path/'v2.duckdb'
    with Store(db):
        pass
    before = db.read_bytes()
    with pytest.raises(RuntimeError, match='retired'):
        ensure_paper_tables(db)
    assert db.read_bytes() == before


def test_retired_operator_preserves_manual_records(tmp_path):
    from trade_system.daily_loop import run_daily_operator_loop
    db = tmp_path/'manual.duckdb'
    with duckdb.connect(str(db)) as con:
        con.execute("CREATE TABLE watchlist(note VARCHAR); INSERT INTO watchlist VALUES ('manual')")
        con.execute("CREATE TABLE trade_plan(note VARCHAR); INSERT INTO trade_plan VALUES ('manual condition')")
    before = db.read_bytes()
    with pytest.raises(RuntimeError, match='retired'):
        run_daily_operator_loop(db, '2026-09-10')
    assert db.read_bytes() == before


def test_retired_cli_rejects_before_argument_parsing(tmp_path):
    from pathlib import Path
    script = Path(__file__).resolve().parents[1]/'scripts/paper_order.py'
    result = subprocess.run([sys.executable, str(script), '--approve'], cwd=tmp_path, capture_output=True)
    assert result.returncode == 2 and b'Retired' in result.stderr
    assert list(tmp_path.iterdir()) == []


def test_opportunity_instances_do_not_inherit_yesterday_terminal_state(tmp_path):
    from trade_system.v2.strategies import record_signal, StrategyPolicy, signal, latest_instance_signal
    clock = Clock()
    with Store(tmp_path/'v2.duckdb', clock=clock) as store:
        inputs = dict(price='7',auction_received_at='2026-09-10T09:25:00+08:00',
                      auction_confirmed=True,theme_supported=True,funds_supported=True)
        policy = StrategyPolicy('algorithm-v1','9','11','8','2026-09-10T10:00:00+08:00')
        first = record_signal(store,'SZ.000002',store.freeze(clock()),policy,at=clock(),**inputs)
        assert signal(store,first)['state']=='invalidated'
        clock.set('2026-09-11T09:31:00+08:00')
        policy = StrategyPolicy('algorithm-v1','9','11','8','2026-09-11T10:00:00+08:00')
        inputs['price']='10'
        inputs['auction_received_at']='2026-09-11T09:25:00+08:00'
        second = record_signal(store,'SZ.000002',store.freeze(clock()),policy,at=clock(),**inputs)
        assert signal(store,second)['state']=='triggered'
        assert latest_instance_signal(store,signal(store,first))==(first,'invalidated')
        assert signal(store,first)['parameter_set_id']==signal(store,second)['parameter_set_id']


def test_fixed_funds_window_is_not_changed_by_intermediate_sample(tmp_path):
    from trade_system.v2.event_bridge import derive_inputs, EventPolicy, ingest_event
    clock=Clock()
    with Store(tmp_path/'v2.duckdb',clock=clock) as store:
        for kind,(unit,semantics) in KINDS.items():
            store.register_product('fixture.'+kind,unit,semantics,'event:'+kind,'synthetic_fixture')
        def add(key,at,value):
            ingest_event(store,'fixture.funds_cumulative','SZ.000002','funds_cumulative',at,
                         {'net_cny':str(value),'metric_version':'fixture-net-v1','counter_epoch':'synthetic-fixture-counter'},key)
        add('start','2026-09-10T09:30:00+08:00',100)
        add('end','2026-09-10T09:31:00+08:00',400)
        policy=EventPolicy(**policies()[1])
        assert derive_inputs(store,'SZ.000002',policy)[1]['fund_delta_cny']=='300'
        add('middle','2026-09-10T09:30:50+08:00',350)
        evidence=derive_inputs(store,'SZ.000002',policy)[1]
        assert evidence['fund_delta_cny']=='300' and evidence['fund_observed_span_seconds']==60


def test_unsent_confirmation_expiry_and_unknown_protection(tmp_path):
    from tests.test_v2_event_replay import confirm
    from trade_system.v2.decisions import DecisionService,RiskPolicy
    clock=Clock()
    with Store(tmp_path/'v2.duckdb',clock=clock) as store:
        for kind,(unit,semantics) in KINDS.items():
            store.register_product('fixture.'+kind,unit,semantics,'event:'+kind,'synthetic_fixture')
        confirmed=confirm(store)
        service=DecisionService(store,RiskPolicy(**confirmed['policy']))
        with pytest.raises(ValueError,match='not expired'):
            service.close_unsent('confirm',operator='fixture',request_id='expire',reason='expired_not_sent')
        clock.set('2026-09-10T09:32:00+08:00')
        service.mark_unknown(confirmed['reservation_id'])
        with pytest.raises(ValueError,match='unknown'):
            service.close_unsent('confirm',operator='fixture',request_id='expire',reason='expired_not_sent')


def test_unsent_cancel_is_idempotent_and_prevents_delivery(tmp_path):
    from tests.test_v2_event_replay import confirm
    from trade_system.v2.decisions import DecisionService,RiskPolicy
    from trade_system.v2.paper_storage import submit_confirmed_buy
    with Store(tmp_path/'v2.duckdb',clock=Clock()) as store:
        for kind,(unit,semantics) in KINDS.items():
            store.register_product('fixture.'+kind,unit,semantics,'event:'+kind,'synthetic_fixture')
        confirmed=confirm(store)
        service=DecisionService(store,RiskPolicy(**confirmed['policy']))
        result=service.close_unsent('confirm',operator='fixture',request_id='cancel')
        assert result==service.close_unsent('confirm',operator='fixture',request_id='cancel')
        from trade_system.v2.operator_workflow import review_desk,render_desk
        review=review_desk(store,'fixture-event-paper')
        assert len(review['reservation_closures'])==1
        assert len(review['confirmations'])==1
        assert review['review']['attribution']['decision_outcomes'][0]['outcome']=='cancelled'
        assert '<html' in render_desk(review)
        with pytest.raises(ValueError,match='held'):
            submit_confirmed_buy(store,'fixture-event-paper','confirm','order')


def test_critical_admission_survives_normal_queue_saturation(tmp_path, monkeypatch):
    import threading
    from trade_system.v2.service import Service, ServiceBusy
    entered, release=threading.Event(),threading.Event()
    dispatch=Service._dispatch
    def paused(store,command,data):
        if command=='hold_fixture':
            entered.set()
            if not release.wait(5):
                raise TimeoutError('fixture release missing')
            return 'released'
        return dispatch(store,command,data)
    monkeypatch.setattr(Service,'_dispatch',staticmethod(paused))
    service=Service(tmp_path/'v2.duckdb',capacity=1,critical_reserve=1)
    try:
        active=service.submit('hold_fixture')
        assert entered.wait(2)
        normal=service.submit('status')
        with pytest.raises(ServiceBusy):
            service.submit('status')
        critical=service.submit('account',account_id='missing')
        release.set()
        assert active.result(5)=='released' and critical.result(5) is None
        assert normal.result(5)['execution_ready'] is False
    finally:
        release.set()
        service.close()
        service.close()
    assert not service.thread.is_alive()


def test_legacy_connector_refuses_stopped_v2_without_writes(tmp_path):
    from trade_system.db_utils import refuse_v2_writes
    db=tmp_path/'v2.duckdb'
    with Store(db):
        pass
    before=db.read_bytes()
    with pytest.raises(ValueError,match='identity'):
        refuse_v2_writes(db)
    assert before==db.read_bytes()


def test_scoped_manifest_omits_unrelated_facts(tmp_path):
    with Store(tmp_path/'v2.duckdb',clock=Clock()) as store:
        store.register_product('quote','CNY','point','audit','fixture')
        for code in ('SZ.000001','SZ.000002'):
            store.ingest('quote',code,'2026-09-09T00:00:00Z',10,'r1',b'fixture')
        manifest=store.freeze(store.clock(),datasets=['quote'],codes=['SZ.000001'])
        assert len(store.facts(manifest,'quote','SZ.000001'))==1
        assert store.facts(manifest,'quote','SZ.000002')==[]


def test_archive_copies_without_source_deletion_or_overwrite(tmp_path):
    from scripts.archive_legacy_tables import copy_verified
    db=tmp_path/'source.duckdb'
    with duckdb.connect(str(db)) as con:
        con.execute('CREATE TABLE legacy_qds_demo(id INTEGER PRIMARY KEY, value DECIMAL(10,2))')
        con.execute('INSERT INTO legacy_qds_demo VALUES (1,1.25),(2,NULL)')
    original=db.read_bytes()
    result=copy_verified(db,['legacy_qds_demo'],tmp_path/'archive')
    assert result['tables']['legacy_qds_demo']['rows']==2
    assert result['source_deleted'] is False and result['schema_restore_verified'] is False
    assert db.read_bytes()==original
    copied=(tmp_path/'archive/legacy_qds_demo.parquet').read_bytes()
    with pytest.raises(ValueError,match='new archive'):
        copy_verified(db,['legacy_qds_demo'],tmp_path/'archive')
    assert (tmp_path/'archive/legacy_qds_demo.parquet').read_bytes()==copied


@pytest.mark.parametrize('corruption',[None,'journal','checkpoint','archive'])
def test_paper_checkpoint_matches_full_replay_and_rejects_corruption(tmp_path,corruption):
    from tools.v2.run_event_replay import paper_config
    from trade_system.v2.paper_storage import open_paper,apply_paper_event,load_paper
    db=tmp_path/'paper.duckdb'
    with Store(db,clock=Clock()) as store:
        open_paper(store,paper_config())
        for i in range(65):
            apply_paper_event(store,'fixture-event-paper',{'event_id':str(i),'kind':'cash_transfer',
                'payload':{'amount_fen':1,'evidence_id':'synthetic'}})
        assert store.con.execute('SELECT seq FROM paper_checkpoint').fetchall()==[(64,)]
        assert load_paper(store,'fixture-event-paper').summary()==load_paper(store,'fixture-event-paper',full_replay=True).summary()
    with Store(db,clock=Clock()) as store:
        expected=load_paper(store,'fixture-event-paper',full_replay=True).summary()
        assert load_paper(store,'fixture-event-paper').summary()==expected
        if corruption=='journal':
            store.con.execute("UPDATE paper_ledger_event SET raw_hash='invalid' WHERE seq=1")
        elif corruption=='checkpoint':
            store.con.execute("UPDATE paper_checkpoint SET payload='{}'")
        elif corruption=='archive':
            raw=store.con.execute('SELECT raw_hash FROM paper_checkpoint').fetchone()[0]
            (db.parent/(db.name+'.raw')/raw).write_bytes(b'invalid synthetic checkpoint')
        if corruption:
            with pytest.raises(ValueError,match='checksum'):
                load_paper(store,'fixture-event-paper')


def test_archive_failure_keeps_source_and_does_not_publish_completion(tmp_path,monkeypatch):
    from scripts import archive_legacy_tables as archive
    db=tmp_path/'source.duckdb'
    with duckdb.connect(str(db)) as con:
        con.execute('CREATE TABLE legacy_qds_demo(x INTEGER)')
        con.execute('INSERT INTO legacy_qds_demo VALUES (1)')
    before=db.read_bytes()
    def fail(*args,**kwargs):
        raise OSError('synthetic disk hash read failure')
    monkeypatch.setattr(archive.hashlib,'file_digest',fail)
    output=tmp_path/'archive'
    with pytest.raises(OSError,match='synthetic'):
        archive.copy_verified(db,['legacy_qds_demo'],output)
    assert db.read_bytes()==before
    assert (output/'FAILED.json').exists()
    assert not (output/'completed.json').exists()
    assert (output/'legacy_qds_demo.parquet').exists()


def test_old_paper_ledger_read_does_not_migrate_checkpoint_schema(tmp_path):
    from tools.v2.run_event_replay import paper_config
    from trade_system.v2.paper_storage import open_paper,load_paper,apply_paper_event
    with Store(tmp_path/'paper.duckdb',clock=Clock()) as store:
        open_paper(store,paper_config())
        store.con.execute('DROP TABLE paper_checkpoint')
        store.con.execute('DELETE FROM v2_schema WHERE version=6')
        load_paper(store,'fixture-event-paper')
        assert store.con.execute('SELECT count(*) FROM v2_schema WHERE version=6').fetchone()[0]==0
        apply_paper_event(store,'fixture-event-paper',{'event_id':'deposit','kind':'cash_transfer',
            'payload':{'amount_fen':1,'evidence_id':'synthetic'}})
        assert store.con.execute('SELECT count(*) FROM v2_schema WHERE version=6').fetchone()[0]==1
