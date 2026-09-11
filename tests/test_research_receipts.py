from copy import deepcopy
from datetime import datetime,timezone

import duckdb
import pytest

from scripts.export_qlib_features import _query,apply_calendar_overlay,apply_research_overlay
from tests.test_next_work_packages import calendar_rows
from tools.v2.probe_native_gaps import calendar_overlay
from trade_system.v2 import research_receipts as rr
from trade_system.v2.daily_session import seal
from trade_system.v2.domain import identity,file_hash
from trade_system.v2.gap_evidence import write_json

NOW=datetime(2026,9,11,tzinfo=timezone.utc)


def calendar(tmp_path):
    folder=tmp_path/'calendar';folder.mkdir()
    receipt={'provider':'xiaodefa_relay','api':'trade_cal','rows':calendar_rows(),
             'params':{'exchange':'SSE','start_date':'20240101','end_date':'20241231'},'received_at':NOW.isoformat()}
    write_json(folder/'calendar-relay-receipt.json',receipt)
    write_json(folder/'calendar-overlay.json',{**calendar_overlay(receipt['rows']),'receipt_sha256':identity(receipt)})
    seal(folder);return folder


class Fixture:
    def query(self,api,day):
        values=['000001.SZ',day.replace('-','')]
        values += [2] if api=='adj_factor' else [10,0,2,3,1]
        return {'fields':rr.FIELDS[api],'items':[values]}


def test_sealed_receipts_recompute_units_and_never_claim_native(tmp_path):
    c=calendar(tmp_path);out=tmp_path/'out'
    rr.capture(c,'2024-01-02','2024-01-05',out,client=Fixture(),clock=lambda:NOW)
    reg,data,meta=rr.verify(out,clock=lambda:NOW)
    assert reg['origin']=='synthetic_fixture' and not meta['historical_PIT_qualified']
    assert len(data['adj_factor'])==6 and data['moneyflow'][0]['net_mf_amount']=='100000'
    assert data['moneyflow'][0]['buy_lg_amount']=='0'
    (out/'moneyflow-2024-01-02.json').write_text('{}')
    with pytest.raises(ValueError,match='changed'):rr.verify(out,clock=lambda:NOW)


@pytest.mark.parametrize('bad',['duplicate','wrong_day','zero_factor','infinite','negative_gross','boolean','truncated','missing_field'])
def test_reject_bad_or_truncated_source(bad):
    api='adj_factor' if bad in ('zero_factor','infinite','boolean') else 'moneyflow'
    data=deepcopy(Fixture().query(api,'2024-01-02'))
    if bad=='duplicate':data['items']*=2
    if bad=='wrong_day':data['items'][0][1]='20240103'
    if bad=='zero_factor':data['items'][0][2]=0
    if bad=='infinite':data['items'][0][2]=float('inf')
    if bad=='negative_gross':data['items'][0][3]=-1
    if bad=='boolean':data['items'][0][2]=True
    if bad=='truncated':data['items']*=6000
    if bad=='missing_field':data['fields']=data['fields'][:-1]
    with pytest.raises(ValueError):rr.normalize(api,'2024-01-02',data)


def test_missing_net_flow_is_null_not_zero():
    data=Fixture().query('moneyflow','2024-01-02');data['items'][0][2]=None
    assert rr.normalize('moneyflow','2024-01-02',data)[0]['net_mf_amount'] is None


def test_partial_failure_is_not_sealed_or_secret_leaking(tmp_path):
    class Bad(Fixture):
        def query(self,*args):raise RuntimeError('secret transport string')
    c=calendar(tmp_path)
    with pytest.raises(RuntimeError):rr.capture(c,'2024-01-02','2024-01-05',tmp_path/'failed',client=Bad(),clock=lambda:NOW)
    assert not (tmp_path/'failed'/'completed.json').exists()
    assert 'secret' not in (tmp_path/'failed'/'failed.json').read_text()


@pytest.mark.parametrize('gap',[False,True])
def test_overlay_is_read_only_no_legacy_factor_or_unit_mix(tmp_path,monkeypatch,gap):
    c=calendar(tmp_path);out=tmp_path/'out'
    class Missing(Fixture):
        def query(self,api,day):
            data=super().query(api,day)
            if gap and api=='moneyflow' and day=='2024-01-03':data['items']=[]
            return data
    rr.capture(c,'2024-01-02','2024-01-05',out,client=Missing(),clock=lambda:NOW)
    reg,data,meta=rr.verify(out,clock=lambda:NOW)
    path=tmp_path/'source.db'
    with duckdb.connect(str(path)) as con:
        con.execute('CREATE TABLE tushare_daily(date DATE,stock_code VARCHAR,open DOUBLE,high DOUBLE,low DOUBLE,close DOUBLE,volume DOUBLE,turnover DOUBLE)')
        con.execute('CREATE TABLE tushare_daily_basic(date DATE,stock_code VARCHAR,turnover_rate DOUBLE,volume_ratio DOUBLE,pe DOUBLE,pb DOUBLE,total_mv DOUBLE,circ_mv DOUBLE)')
        con.execute('CREATE TABLE tushare_adj_factor(date DATE,stock_code VARCHAR,adj_factor DOUBLE)')
        con.execute('CREATE TABLE tushare_moneyflow(date DATE,stock_code VARCHAR,net_mf_amount DOUBLE,buy_lg_amount DOUBLE,sell_lg_amount DOUBLE,buy_elg_amount DOUBLE,sell_elg_amount DOUBLE)')
        con.execute('CREATE TABLE tushare_trade_cal(cal_date DATE,is_open BOOLEAN)')
        for day in reg['days']:
            con.execute("INSERT INTO tushare_daily VALUES (?,'000001',10,12,9,11,100,1000)",[day])
            con.execute('INSERT INTO tushare_trade_cal VALUES (?,true)',[day])
            con.execute("INSERT INTO tushare_adj_factor VALUES (?,'000001',999)",[day])
        con.execute("INSERT INTO tushare_moneyflow VALUES ('2024-01-03','000001',999999,999,999,999,999)")
    before=file_hash(path)
    with duckdb.connect(str(path),read_only=True) as con:
        cal=apply_calendar_overlay(con,c)
        with pytest.raises(ValueError,match='fixtures'):apply_research_overlay(con,out,cal,'2024-01-02','2024-01-05')
        # Positive consumer test explicitly injects source status, not live acceptance.
        reg['origin']='xiaodefa_relay'
        monkeypatch.setattr(rr,'verify',lambda p:(reg,data,meta))
        apply_research_overlay(con,out,cal,'2024-01-02','2024-01-05')
        frame=con.execute(_query('2024-01-02','2024-01-09',include_calendar=True,include_adjustment=True,
            calendar_relation='verified_calendar_overlay',adjustment_relation='verified_adjustment',money_relation='verified_moneyflow')).fetchdf()
        assert frame.iloc[0]['open']==20 and frame.iloc[0]['large_net_mf']==-20000
        assert abs(frame.iloc[0]['label_next_ret']-10)<1e-9
        assert frame.iloc[:4]['moneyflow_5d'].isna().all()
        assert frame.iloc[-1]['moneyflow_5d']==500000 if not gap else frame['moneyflow_5d'].isna().all()
        assert con.execute('SELECT min(adj_factor) FROM tushare_adj_factor').fetchone()[0]==999
    assert file_hash(path)==before


def test_transport_uses_bounded_single_attempt_opener(monkeypatch):
    from trade_system import http_transport
    class Response:
        def __enter__(self):return self
        def __exit__(self,*a):pass
        def read(self,n):
            assert n==8_000_001
            return b'x'*n
    monkeypatch.setattr(http_transport,'open_verified_once',lambda *a,**kw:Response())
    relay=rr.Relay.__new__(rr.Relay);relay.token='test';relay.url='https://example.test/';relay.last=0
    with pytest.raises(ValueError,match='byte budget'):relay.query('adj_factor','2024-01-02')
