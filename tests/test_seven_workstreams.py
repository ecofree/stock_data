from pathlib import Path

import pytest

from tests.test_native_enrichment import Client
from tests.test_v2_daily_session import report,item,moment
from tools.v2 import finish_daily_receipts as recovery,resume_durable_probe as resume
from trade_system.v2 import native_enrichment as native
from trade_system.v2.domain import canonical,file_hash,identity
from trade_system.v2.gap_evidence import read_json,write_json


def partial_fixture(tmp_path,monkeypatch):
    parent=report(items=[item('000002'),item('000001')]);following=report('2026-09-11')
    native.capture(parent,'2026-09-11',tmp_path/'complete',client=Client(),clock=lambda:moment('2026-09-11'))
    reg=read_json(tmp_path/'complete/registration.json')[0];reg['origin']='hithink_native'
    partial=tmp_path/'partial';partial.mkdir()
    write_json(partial/'registration.json',reg)
    write_json(partial/'failed.json',{'error_type':'HTTPError','retained_responses':1})
    (partial/'response-00.json').write_bytes((tmp_path/'complete/response-00.json').read_bytes())
    monkeypatch.setattr(recovery.daily,'verify',lambda path:parent if str(path)=='parent' else following)
    monkeypatch.setattr(recovery,'now_utc',lambda:moment('2026-09-11','16:01:00'))
    monkeypatch.setattr(recovery.time,'sleep',lambda _:None)
    return partial


def test_native_prefix_recovery_calls_only_missing_and_preserves_bytes(tmp_path,monkeypatch):
    partial=partial_fixture(tmp_path,monkeypatch);calls=[]
    from trade_system import hithink_client
    class Counted(Client):
        def __init__(self,**kwargs):assert kwargs['single_attempt'] is True
        def _get(self,path,params):calls.append((path,params));return super()._get(path,params)
    monkeypatch.setattr(hithink_client,'HiThinkClient',Counted)
    before={p.name:file_hash(p) for p in partial.iterdir()}
    result=recovery.run(partial,'parent','following',tmp_path/'out')
    assert len(calls)==1 and result['observed_prices']==2 and result['human_judgements']==0
    assert not result['human_loop_complete'] and not result['execution_ready']
    assert before=={p.name:file_hash(p) for p in partial.iterdir()}
    assert file_hash(partial/'response-00.json')==file_hash(tmp_path/'out/enrichment/response-00.json')
    assert native.verify(tmp_path/'out/enrichment')['origin']=='hithink_native'
    rendered=recovery.verify_and_publish(tmp_path/'out','parent','following',tmp_path/'published')
    assert rendered['native_requests']==0 and rendered['raw_input_replay_equal'] and len(calls)==1
    with pytest.raises(ValueError,match='immutable inputs'):
        recovery.verify_and_publish(tmp_path/'out','parent','following',tmp_path/'out/child')


@pytest.mark.parametrize('bad',['origin','cohort','count','extra','prefix_request','parent'])
def test_native_prefix_recovery_refuses_bad_inputs_before_network(tmp_path,monkeypatch,bad):
    partial=partial_fixture(tmp_path,monkeypatch)
    reg=read_json(partial/'registration.json')[0]
    if bad=='origin':reg['origin']='synthetic_fixture'
    if bad=='cohort':reg['instruments'].reverse()
    if bad=='parent':reg['parent_report_id']='bad'
    if bad=='count':(partial/'failed.json').write_text(canonical({'retained_responses':True}))
    if bad=='extra':write_json(partial/'extra.json',{})
    if bad=='prefix_request':
        rec=read_json(partial/'response-00.json')[0];rec['params']['adjust']='qfq';(partial/'response-00.json').write_text(canonical(rec))
    (partial/'registration.json').write_text(canonical(reg))
    with pytest.raises(ValueError):recovery.run(partial,'parent','following',tmp_path/'out')
    assert not (tmp_path/'out').exists()


@pytest.mark.parametrize('phase',['initialization','request'])
def test_native_recovery_failure_is_safe_and_unpublished(tmp_path,monkeypatch,phase):
    partial=partial_fixture(tmp_path,monkeypatch)
    from trade_system import hithink_client
    class Failed:
        def __init__(self,**kwargs):
            if phase=='initialization':raise RuntimeError('secret-in-exception')
        def _get(self,*args):raise RuntimeError('secret-in-exception')
    monkeypatch.setattr(hithink_client,'HiThinkClient',Failed)
    with pytest.raises(RuntimeError):recovery.run(partial,'parent','following',tmp_path/'out')
    data=(tmp_path/'out/failed.json').read_text()
    assert 'secret-in-exception' not in data and not (tmp_path/'out/publication').exists()
    assert read_json(tmp_path/'out/failed.json')[0]['retained_responses']==1


def resume_registration(folder):
    folder.mkdir()
    write_json(folder/'started.json',{'scope':'synthetic_private_append_load_public_authority_tested_separately',
        'events':100000,'config_sha256':identity(resume.config()),'source_files':{'tools/v2/probe_bounded_hot.py':file_hash(Path(resume.__file__).with_name('probe_bounded_hot.py'))}})


@pytest.mark.parametrize('bad',['batch_zero','batch_large','batch_bool','source','config','scope','count'])
def test_resume_rejects_changed_registration_before_database(tmp_path,bad):
    folder=tmp_path/'source';resume_registration(folder);reg=read_json(folder/'started.json')[0];batch=10
    if bad=='source':reg['source_files']['tools/v2/probe_bounded_hot.py']='wrong'
    if bad=='config':reg['config_sha256']='wrong'
    if bad=='scope':reg['scope']='live'
    if bad=='count':reg['events']=10
    if bad.startswith('batch'):batch={'batch_zero':0,'batch_large':10001,'batch_bool':True}[bad]
    (folder/'started.json').write_text(canonical(reg))
    with pytest.raises(ValueError):resume.run(folder,tmp_path/'out',batch_size=batch)
    assert not (tmp_path/'out').exists()


def test_resume_continues_exact_sequence_and_reopens(tmp_path):
    from tools.v2.run_event_replay import Clock
    from trade_system.v2.storage import Store
    from trade_system.v2.paper_storage import open_paper,load_paper,_append
    folder=tmp_path/'source';resume_registration(folder);clock=Clock()
    with Store(folder/'paper.duckdb',clock=clock) as store:
        open_paper(store,resume.config())
        for event in resume.events(14):
            clock.set(event['at']);_append(store,load_paper(store,resume.config()['account_id'],writer_session=True),event)
    result=resume.run(folder,tmp_path/'out',batch_size=2)
    assert (result['start_seq'],result['end_seq'],result['events_appended'])==(14,16,2)
    assert result['pre_and_post_full_replay_equal'] and not result['target_reached']
    assert read_json(tmp_path/'out/checkpoint-000016.json')[0]['seq']==16


def test_multi_account_small_probe_and_input_bounds(tmp_path):
    from tools.v2.probe_multi_account import run
    with pytest.raises(ValueError):run(tmp_path/'bad',accounts=11)
    result=run(tmp_path/'out',accounts=2,rounds=1)
    assert result['peak_active_orders_per_account']==9 and result['peak_unknown_per_account']==9
    assert result['reopen_full_audit_equal'] and len(result['reopen_full_audit_seconds'])==3
    assert not result['execution_ready']
