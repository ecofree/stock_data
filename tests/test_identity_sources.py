from copy import deepcopy

import duckdb
import pytest

from tools.v2.probe_identity_sources import plan,rows_for,capture,analyze,API_FIELDS
from trade_system.v2.domain import file_hash


def test_registered_plan_is_bounded_and_native_first():
    p=plan();assert len(p)==16
    assert all(r['provider']=='hithink_native' for r in p[:4])
    assert all(r['params']['adjust']=='none' and r['params']['offset']==0 for r in p[:4])


@pytest.mark.parametrize('bad',['code','date','duplicate','cap','width','negative','nan'])
def test_reject_wrong_code_dates_unbounded_or_invalid_prices(bad):
    request=plan()[4]
    data={'fields':API_FIELDS['daily'],'items':[['300114.SZ','20240102',10,12,9,11,100,1000]]}
    if bad=='code':data['items'][0][0]='302132.SZ'
    if bad=='date':data['items'][0][1]='20250102'
    if bad=='duplicate':data['items']*=2
    if bad=='cap':data['items']*=64
    if bad=='width':data['items'][0].pop()
    if bad=='negative':data['items'][0][-2]=-1
    if bad=='nan':data['items'][0][2]=float('nan')
    with pytest.raises(ValueError):rows_for(request,data)


def test_declared_price_units_converted_but_money_units_stay_explicit():
    row=rows_for(plan()[4],{'fields':API_FIELDS['daily'],'items':[['300114.SZ','20240102',10,12,9,11,100,1000]]})['2024-01-02']
    assert row['volume_shares']=='10000' and row['turnover_cny']=='1000000'
    request=deepcopy(plan()[6])
    data={'fields':API_FIELDS['moneyflow'],'items':[['300114.SZ','20240102',None,1,2,3,4]]}
    assert rows_for(request,data)['2024-01-02']['net_mf_amount'] is None


def test_failure_receipts_retained_safe_and_analysis_readonly(tmp_path):
    class Fixture:
        def query(self,r):
            if r['provider']=='hithink_native':raise ValueError('secret value')
            fields=API_FIELDS[r['api']]
            if r['api']=='daily':values=[10,12,9,11,100,1000]
            elif r['api']=='adj_factor':values=[2]
            else:values=[10,1,2,3,4]
            return {'fields':fields,'items':[[r['code'],r['start'].replace('-',''),*values]]}
    folder=tmp_path/'capture';capture(folder,Fixture())
    assert all('secret' not in p.read_text() for p in folder.glob('*.json'))
    db=tmp_path/'source.db'
    with duckdb.connect(str(db)) as c:
        c.execute('CREATE TABLE tushare_daily(date DATE,stock_code VARCHAR,open DOUBLE,high DOUBLE,low DOUBLE,close DOUBLE,volume DOUBLE,turnover DOUBLE,volume_unit VARCHAR,amount_unit VARCHAR)')
        c.execute("INSERT INTO tushare_daily VALUES ('2024-01-02','302132',10,12,9,11,1,100,'hands','thousand_cny')")
    before=file_hash(db)
    result=analyze(folder,db,tmp_path/'analysis')
    assert result['observed_requests']==12 and result['price_comparisons']==2
    assert not result['merge_authorized'] and file_hash(db)==before
