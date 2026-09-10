from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys

import pytest

from tools.v2.run_event_replay import Clock,paper_config,run_replay
from trade_system.v2.domain import canonical,file_hash,identity
from trade_system.v2.storage import Store
from trade_system.v2.paper_storage import open_paper,apply_paper_event
from trade_system.v2.recovery import backup,restore,check_manifest
from trade_system.v2 import daily_workflow,prospective_registry
from trade_system.v2.publisher import publish,read_current
from trade_system.pipeline_runtime import LatestReportTransaction
from tests.test_v2_daily_session import Fixture,moment,report

ROOT=Path(__file__).resolve().parents[1]


def fixture_db(path):
    with Store(path,clock=Clock()) as store:
        open_paper(store,paper_config())
        apply_paper_event(store,'fixture-event-paper',{'event_id':'one','kind':'cash_transfer',
            'payload':{'amount_fen':1,'evidence_id':'synthetic'}})
        store.con.execute("CREATE SEQUENCE fixture_seq START 3")
        store.con.execute("CREATE TABLE fixture_parent(id INTEGER PRIMARY KEY)")
        store.con.execute("INSERT INTO fixture_parent VALUES (1)")
        store.con.execute("CREATE TABLE fixture_child(id INTEGER REFERENCES fixture_parent(id))")
        store.con.execute("CREATE INDEX fixture_index ON fixture_child(id)")
        store.con.execute("CREATE VIEW fixture_view AS SELECT * FROM fixture_parent")


def test_full_backup_restore_preserves_structure_raw_and_source(tmp_path):
    db=tmp_path/'source.duckdb';fixture_db(db);original=file_hash(db)
    receipt=backup(db,tmp_path/'backup')
    result=restore(tmp_path/'backup',tmp_path/'restored')
    assert result['catalog_hash']==receipt['catalog_hash']
    assert result['paper_replay_hashes'].keys()=={'fixture-event-paper'}
    assert file_hash(db)==original
    assert not result['execution_ready']
    check_manifest(tmp_path/'backup')
    with pytest.raises(ValueError):
        restore(tmp_path/'backup',tmp_path/'restored')


def test_restore_partial_fill_journal(tmp_path):
    run_replay(tmp_path/'replay')
    backup(tmp_path/'replay'/'paper.duckdb',tmp_path/'backup')
    assert restore(tmp_path/'backup',tmp_path/'restored')['paper_replay_hashes']


def test_backup_refuses_live_owner_or_source_raw_output(tmp_path):
    db=tmp_path/'source.duckdb'
    with Store(db):
        with pytest.raises(Exception):
            backup(db,tmp_path/'backup')
    assert not (tmp_path/'backup').exists()
    with pytest.raises(ValueError):
        backup(db,Path(str(db)+'.raw')/'nested')


def test_corrupt_backup_and_copy_failure_leave_source_unchanged(tmp_path,monkeypatch):
    db=tmp_path/'source.duckdb';fixture_db(db);original=file_hash(db)
    backup(db,tmp_path/'backup')
    with (tmp_path/'backup'/'database.duckdb').open('ab') as stream:
        stream.write(b'corrupt')
    with pytest.raises(ValueError):
        restore(tmp_path/'backup',tmp_path/'bad_restore')
    assert not (tmp_path/'bad_restore').exists()
    def fail(*args):
        raise OSError('fixture disk failure')
    monkeypatch.setattr('trade_system.v2.recovery.shutil.copyfile',fail)
    with pytest.raises(OSError):
        backup(db,tmp_path/'failed')
    assert not (tmp_path/'failed'/'completed.json').exists()
    assert file_hash(db)==original


def test_old_runner_requires_copy_before_any_live_work(tmp_path):
    db=tmp_path/'nonexistent.duckdb'
    result=subprocess.run([sys.executable,str(ROOT/'scripts/run_integrated_daily.py'),
        '--db',str(db),'--reports-dir',str(tmp_path/'reports')],cwd=ROOT,capture_output=True)
    assert result.returncode==2
    assert b'verified disposable copy' in result.stderr
    assert not db.exists() and not (tmp_path/'reports').exists()


def test_v2_publication_rejects_legacy_transaction(tmp_path):
    publish(tmp_path/'pub','first',{'index.html':b'ok'},generation=1)
    with pytest.raises(ValueError,match='namespace'):
        LatestReportTransaction(tmp_path/'pub','legacy')
    assert read_current(tmp_path/'pub')[0]['run_id']=='first'


def test_preclose_workflow_no_network_no_completed_marker(tmp_path):
    class Never:
        def _get(self,*args):
            pytest.fail('preclose must not call a provider')
    result=daily_workflow.run(tmp_path/'run',clock=lambda:moment(time='09:00:00'),client=Never())
    assert result['native_requests']==0 and not result['daily_close_complete']
    assert not (tmp_path/'run'/'completed.json').exists()


def test_synthetic_workflow_cannot_publish_as_current_native(tmp_path):
    result=daily_workflow.run(tmp_path/'run',clock=lambda:moment(),client=Fixture(),
                              publish_root=tmp_path/'pub',generation=1)
    assert result['status']=='source_check_required'
    assert not (tmp_path/'pub').exists()


def test_native_workflow_builds_pending_human_queue_and_keeps_previous_on_failure(tmp_path,monkeypatch):
    # Unit fixture injects both trusted package functions; never accepted as provider proof.
    monkeypatch.setattr(daily_workflow.daily,'capture',lambda *a,**k:report())
    monkeypatch.setattr(daily_workflow.daily,'verify',lambda *a:report())
    r=daily_workflow.run(tmp_path/'run',clock=lambda:moment(),publish_root=tmp_path/'pub',generation=1)
    assert r['candidate_count']==1 and r['judgements_received']==0 and not r['human_loop_complete']
    pointer=file_hash(tmp_path/'pub'/'current.json')
    def fail(*a,**k):
        raise ConnectionError('simulated provider failure')
    monkeypatch.setattr(daily_workflow.daily,'capture',fail)
    with pytest.raises(ConnectionError):
        daily_workflow.run(tmp_path/'failed',clock=lambda:moment(),publish_root=tmp_path/'pub',generation=2)
    assert file_hash(tmp_path/'pub'/'current.json')==pointer
    assert (tmp_path/'failed'/'failed.json').exists()


def protocol():
    historical=json.loads((ROOT/'docs/v2/rolling_ablation_20260910.json').read_text(encoding='utf-8'))
    return {'schema':1,'scope':'prospective_research_only','start_date':'2026-09-14','end_date':'2026-12-31',
        'minimum_paired_sessions':20,'max_fits':16,'historical_design_sha256':identity(historical),
        'variants':historical['variants'],'label_definition':historical['label_definition'],
        'selection_policy':'Same frozen PIT universe and common qualified cohort across all four groups; no forced fills.',
        'round_trip_cost_bps':[0,10,30,50],'seed':20260911,'num_boost_round':20,'num_threads':2,
        'acceptance':{'automatic_promotion':False,'negative_results_retained':True,'paired_common_cohort':True,
                      'train_only_preprocessing':True,'actual_receipt_time_required':True,'untouched_holdout_required':True}}


def test_prospective_registration_never_auto_qualifies_with_time(tmp_path):
    source=tmp_path/'plan.json';source.write_text(canonical(protocol()),encoding='utf-8')
    registered=prospective_registry.register(source,tmp_path/'reg',clock=lambda:moment())
    later=prospective_registry.inspect(tmp_path/'reg',clock=lambda:moment('2026-12-31'))
    assert not later['research_ready'] and later['fits_performed']==0
    assert later['registration_id']==registered['registration_id']
    assert len(later['blockers'])==6
    with pytest.raises(ValueError):
        prospective_registry.register(source,tmp_path/'late',clock=lambda:moment('2026-09-14'))


@pytest.mark.parametrize('change',[{'start_date':'2026-09-10'},{'max_fits':100},
    {'round_trip_cost_bps':[0]},{'scope':'production'},{'minimum_paired_sessions':2}])
def test_prospective_plan_rejects_weakened_boundaries(tmp_path,change):
    plan=deepcopy(protocol());plan.update(change)
    source=tmp_path/'plan.json';source.write_text(canonical(plan),encoding='utf-8')
    with pytest.raises(ValueError):
        prospective_registry.register(source,tmp_path/'reg',clock=lambda:moment())
    assert not (tmp_path/'reg').exists()
