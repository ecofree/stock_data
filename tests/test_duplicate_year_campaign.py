from datetime import datetime

import duckdb
import pytest

from tools.v2 import duplicate_year_campaign as campaign
from tools.v2.normalize_price_units import FIELDS
from tools.v2.probe_identity_sources import API_FIELDS,PRICES
from trade_system.v2.daily_session import CST,seal
from trade_system.v2.domain import canonical,file_hash
from trade_system.v2.gap_evidence import read_json


def source(tmp_path,*,codes=('000001',)):
    db=tmp_path/'source.db'
    with duckdb.connect(str(db)) as con:
        con.execute('CREATE TABLE tushare_daily ('+','.join(k+' VARCHAR' for k in FIELDS)+')')
        for code in codes:
            for alias,volume,amount in [(code+'.SZ','100','100'),('SZ.'+code,'1','10')]:
                con.execute('INSERT INTO tushare_daily VALUES ('+','.join('?' for _ in FIELDS)+')',
                    [alias,code,'2025-01-02','10','12','9','11',volume,amount,'hands','thousand_yuan','none','legacy','2026-08-12'])
    return db


class Fixture:
    def query(self,r):
        inside=r['start']<='2025-01-02'<=r['end']
        if r['api']==PRICES:return {'item':[{'date_ms':int(datetime(2025,1,2,tzinfo=CST).timestamp()*1000),
            'open_price':10,'high_price':12,'low_price':9,'close_price':11,'volume':10000,'turnover':100000}] if inside else []}
        return {'fields':API_FIELDS['daily'],'items':[[r['code'],'20250102',10,12,9,11,100,100]] if inside else []}


def declare_test_native(folder):
    for path in [folder/'registration.json',folder/'batch-01/registration.json']:
        reg=read_json(path)[0];reg['origin']='native_and_relay';path.write_text(canonical(reg))
    (folder/'batch-01/completed.json').unlink();seal(folder/'batch-01')


def test_full_duplicate_scope_and_raw_preservation(tmp_path,monkeypatch):
    monkeypatch.setattr(campaign.time,'sleep',lambda _:None)
    db=source(tmp_path);before=file_hash(db);folder=tmp_path/'raw'
    campaign.capture(db,folder,client=Fixture())
    with pytest.raises(ValueError,match='real source'):campaign.analyze(db,folder,tmp_path/'no')
    declare_test_native(folder)
    result=campaign.analyze(db,folder,tmp_path/'out')
    assert result['raw_source_rows']==2 and result['qualified_raw_rows']==2
    assert result['canonical_duplicate_keys']==1 and result['quarantined_duplicate_keys']==0
    assert result['raw_records_deleted']==0 and not result['research_ready'] and file_hash(db)==before


def test_year_windows_nonoverlap_and_bounded_requests():
    from datetime import date,timedelta
    windows=campaign.WINDOWS
    assert windows[0][0]=='2025-01-01' and windows[-1][1]=='2025-12-31'
    for a,b in zip(windows,windows[1:]):assert date.fromisoformat(a[1])+timedelta(days=1)==date.fromisoformat(b[0])
    assert len(campaign.plan(['000001.SZ']*1))==10
    with pytest.raises(ValueError):campaign.plan(['000001.SZ']*7)
    with pytest.raises(ValueError):campaign.plan(['000001.SZ','000001.SZ'])


def test_resume_does_not_repeat_requests(tmp_path,monkeypatch):
    monkeypatch.setattr(campaign.time,'sleep',lambda _:None);db=source(tmp_path);folder=tmp_path/'raw'
    campaign.capture(db,folder,client=Fixture())
    class NoCalls:
        def query(self,*args):raise AssertionError('sealed batch must be replayed without network')
    campaign.capture(db,folder,client=NoCalls())


def test_provider_failure_budget_and_no_secret_retention(tmp_path,monkeypatch):
    monkeypatch.setattr(campaign.time,'sleep',lambda _:None);db=source(tmp_path,codes=('000001','000002'));folder=tmp_path/'raw';calls=[]
    class Failed:
        def query(self,r):calls.append(r);raise RuntimeError('secret-not-safe-to-print')
    campaign.capture(db,folder,client=Failed())
    assert len(calls)==10
    statuses=[read_json(p)[0] for p in sorted((folder/'batch-01').glob('status-*.json'))]
    assert sum(s['status']=='skipped' for s in statuses)==10
    assert 'secret-not-safe-to-print' not in canonical(statuses)
    declare_test_native(folder);result=campaign.analyze(db,folder,tmp_path/'out')
    assert result['canonical_duplicate_keys']==0 and result['quarantined_duplicate_keys']==2


@pytest.mark.parametrize('bad',['identity','time','status','membership'])
def test_resealed_receipt_mutations_still_rejected(tmp_path,monkeypatch,bad):
    monkeypatch.setattr(campaign.time,'sleep',lambda _:None);db=source(tmp_path);folder=tmp_path/'raw'
    campaign.capture(db,folder,client=Fixture());declare_test_native(folder)
    batch=folder/'batch-01'
    if bad=='membership':(batch/'extra.json').write_text('{}')
    elif bad=='status':
        p=batch/'status-00.json';obj=read_json(p)[0];obj['rows']=20;p.write_text(canonical(obj))
    else:
        p=batch/'receipt-00.json';obj=read_json(p)[0]
        if bad=='identity':obj['request']['code']='000002.SZ'
        else:obj['received_at']='2099-01-01T00:00:00+00:00'
        p.write_text(canonical(obj))
    (batch/'completed.json').unlink();seal(batch)
    with pytest.raises(ValueError):campaign.analyze(db,folder,tmp_path/'out')


def rate_limited_parent(tmp_path,monkeypatch):
    from tools.v2 import repair_year_campaign as repair
    from datetime import timedelta
    from trade_system.v2.domain import now_utc
    monkeypatch.setattr(campaign.time,'sleep',lambda _:None);db=source(tmp_path);folder=tmp_path/'raw'
    campaign.capture(db,folder,client=Fixture());declare_test_native(folder)
    batch=folder/'batch-01'
    (batch/'receipt-00.json').unlink()
    (batch/'status-00.json').write_text(canonical({'index':0,'status':'failed','error_type':'HTTPError','http_status':429}))
    (batch/'completed.json').unlink();seal(batch)
    later=now_utc()+timedelta(seconds=61);monkeypatch.setattr(repair,'now_utc',lambda:later)
    return db,folder,repair


def test_explicit_rate_limit_repair_preserves_source_and_qualifies_only_missing(tmp_path,monkeypatch):
    db,folder,repair=rate_limited_parent(tmp_path,monkeypatch)
    before={p.relative_to(folder).as_posix():file_hash(p) for p in folder.rglob('*') if p.is_file()}
    repair.capture(folder,tmp_path/'repair',client=Fixture())
    with pytest.raises(ValueError):repair.replay(folder,tmp_path/'repair')
    p=tmp_path/'repair/registration.json';reg=read_json(p)[0];reg['origin']='native_and_relay';p.write_text(canonical(reg))
    (tmp_path/'repair/completed.json').unlink();seal(tmp_path/'repair')
    result=campaign.analyze(db,folder,tmp_path/'out',repair=tmp_path/'repair')
    assert result['canonical_duplicate_keys']==1 and result['repair_counts']['observed']==1
    assert before=={p.relative_to(folder).as_posix():file_hash(p) for p in folder.rglob('*') if p.is_file()}


def test_rate_limit_repair_requires_cooldown_and_separate_output(tmp_path,monkeypatch):
    db,folder,repair=rate_limited_parent(tmp_path,monkeypatch)
    with pytest.raises(ValueError,match='separate'):repair.capture(folder,folder/'repair',client=Fixture())
    _,_,last=repair.parent(folder);monkeypatch.setattr(repair,'now_utc',lambda:last)
    with pytest.raises(ValueError,match='60 seconds'):repair.capture(folder,tmp_path/'out',client=Fixture())
    assert not (tmp_path/'out').exists()
