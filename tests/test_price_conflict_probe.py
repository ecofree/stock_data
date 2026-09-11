from copy import deepcopy
from datetime import datetime

import pytest

from tools.v2 import probe_price_conflicts as p
from tools.v2.canonical_price_research import resolve
from trade_system.v2.daily_session import CST,seal
from trade_system.v2.domain import canonical,file_hash,identity
from trade_system.v2.gap_evidence import read_json,write_json


def source(tmp_path,codes=('000001.SZ',)):
    folder=tmp_path/'parent';folder.mkdir();variants=[]
    for code in codes:
        for day in p.DAYS:
            original={'stock_code':code[:6],'ts_code':code,'date':day}
            evidence=[{'date':day,'request_code':code,'provider':provider,'values':{'open':'10','high':'12','low':'9','close':'11','volume_shares':volume,'turnover_cny':amount}}
                for provider,volume,amount in [('hithink_native','100','1000'),('xiaodefa_relay','10000','100000')]]
            variants.append({'original':original,'original_sha256':identity(original),'status':'unit_unqualified','evidence':evidence})
    write_json(folder/'result.json',{'records':resolve(variants),'quarantined_duplicate_keys':len(variants),'database_sha256':'frozen-fixture',
        'source_unchanged':True,'execution_ready':False,'production_cutover':False})
    seal(folder);return folder


class Client:
    def __init__(self,mode='persistent'):self.calls=[];self.mode=mode
    def query(self,r):
        self.calls.append(r)
        days=[d for d in p.DAYS if r['start']<=d<=r['end']]
        if r['role']=='relay':return {'fields':p.probe.API_FIELDS['daily'],'items':[[r['code'],d.replace('-',''),10,12,9,11,100,100] for d in days]}
        correct=self.mode=='revised' or (self.mode=='window' and r['role']!='wide')
        return {'thscode':r['code'],'interval':'1d','adjust':'none','item':[{'date_ms':int(datetime.fromisoformat(d).replace(tzinfo=CST).timestamp()*1000),
            'open_price':10,'high_price':12,'low_price':9,'close_price':11,'volume':10000 if correct else 100,'turnover':100000 if correct else 1000} for d in days]}


def native_fixture(folder):
    reg=read_json(folder/'registration.json')[0];reg['origin']='native_and_relay'
    (folder/'registration.json').write_text(canonical(reg),encoding='utf-8')
    manifests={}
    for code in reg['codes']:
        batch=folder/code;r=read_json(batch/'registration.json')[0];r['origin']='native_and_relay'
        (batch/'registration.json').write_text(canonical(r),encoding='utf-8');(batch/'completed.json').unlink();seal(batch)
        manifests[code]=identity(p.sealed(batch))
    (folder/'completed.json').write_text(canonical({'registration_sha256':file_hash(folder/'registration.json'),'batch_manifest_ids':manifests}),encoding='utf-8')


@pytest.mark.parametrize('mode,expected',[('persistent','persistent_cross_source_conflict'),('window','native_window_inconsistent'),('revised','dual_source_now_agrees_revision_review_required')])
def test_window_classification_never_grants_repair(tmp_path,monkeypatch,mode,expected):
    monkeypatch.setattr(p.time,'sleep',lambda _:None);parent=source(tmp_path);client=Client(mode);raw=tmp_path/'raw'
    p.capture(parent,raw,client=client);assert len(client.calls)==5
    with pytest.raises(ValueError,match='real capture'):p.analyze(parent,raw,tmp_path/'rejected')
    native_fixture(raw);r=p.analyze(parent,raw,tmp_path/'out')
    assert r['case_statuses']=={expected:2} and r['canonical_replacements']==0 and not r['research_ready']
    assert p.sealed(tmp_path/'out')


@pytest.mark.parametrize('field,value',[('thscode','000002.SZ'),('interval','1w'),('adjust','qfq')])
def test_native_envelope_refuses_wrong_identity_or_basis(field,value):
    request=p.plan('000001.SZ')[0];data=Client().query(request);data[field]=value
    with pytest.raises(ValueError,match='identity/interval/adjustment'):p.normalize(request,data)


def test_completed_resume_needs_no_client_or_network(tmp_path,monkeypatch):
    monkeypatch.setattr(p.time,'sleep',lambda _:None);parent=source(tmp_path);raw=tmp_path/'raw';client=Client()
    p.capture(parent,raw,client=client)
    before={str(x):file_hash(x) for x in raw.rglob('*') if x.is_file()}
    p.capture(parent,raw,client=client);assert len(client.calls)==5
    assert before=={str(x):file_hash(x) for x in raw.rglob('*') if x.is_file()}


def test_failure_circuit_preserves_status_and_hides_exception(tmp_path,monkeypatch):
    monkeypatch.setattr(p.time,'sleep',lambda _:None);parent=source(tmp_path,('000001.SZ','000002.SZ','000003.SZ','000004.SZ'));client=Client()
    def fail(r):client.calls.append(r);raise RuntimeError('secret-in-provider-exception')
    client.query=fail;p.capture(parent,tmp_path/'raw',client=client)
    assert len(client.calls)==6
    for f in (tmp_path/'raw').rglob('*.json'):assert 'secret-in-provider-exception' not in f.read_text(encoding='utf-8')


@pytest.mark.parametrize('bad',['time','request','rows','extra'])
def test_resealed_mutation_rejected(tmp_path,monkeypatch,bad):
    monkeypatch.setattr(p.time,'sleep',lambda _:None);parent=source(tmp_path);raw=tmp_path/'raw';p.capture(parent,raw,client=Client())
    batch=raw/'000001.SZ'
    if bad=='extra':write_json(batch/'extra.json',{})
    elif bad=='rows':
        path=batch/'status-0.json';r=read_json(path)[0];r['rows']=True;path.write_text(canonical(r),encoding='utf-8')
    else:
        path=batch/'receipt-0.json';r=read_json(path)[0]
        if bad=='time':r['received_at']='2099-01-01T00:00:00+00:00'
        else:r['request']['params']['adjust']='qfq'
        path.write_text(canonical(r),encoding='utf-8')
    (batch/'completed.json').unlink();seal(batch)
    with pytest.raises(ValueError):p.capture(parent,raw,client=Client())


def test_missing_day_is_not_zero_or_agreement():
    case={'date':p.DAYS[0],'parent_record_id':'test','evidence':[{'provider':'hithink_native','values':{}}]}
    row=p.compare([case],{})[0]
    assert row['status']=='incomplete_observations' and not row['dual_source_now_agrees']
    assert row['native_changed_since_parent'] is None


def test_parent_seal_and_input_namespace(tmp_path,monkeypatch):
    monkeypatch.setattr(p.time,'sleep',lambda _:None);parent=source(tmp_path)
    with pytest.raises(ValueError,match='namespace'):p.capture(parent,parent/'nested',client=Client())
    obj=read_json(parent/'result.json')[0];obj=deepcopy(obj);obj['records'][0]['date']='2025-12-01'
    (parent/'result.json').write_text(canonical(obj),encoding='utf-8');(parent/'completed.json').unlink();seal(parent)
    with pytest.raises(ValueError):p.parent(parent)
