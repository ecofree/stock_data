from copy import deepcopy
from datetime import date,datetime,timedelta

import pytest

from tools.v2 import research_campaign as campaign
from trade_system.v2.daily_session import CST,seal
from trade_system.v2.gap_evidence import read_json,write_json
from trade_system.v2.alpha158_research import compute


class Fixture:
    def query(self,r):
        if r['kind']=='native_calendar':return {'item':[{'date':'20260105'}]}
        if r['kind']=='identity_snapshot':
            code=r['params']['ts_code']
            return {'fields':campaign.FIELDS['stock_basic'],'items':[[code,code.split('.')[0],'fixture','SSE' if code.endswith('.SH') else 'SZSE','fixture','L','20100101',None]]}
        if r['kind']=='calendar':
            rows=[]
            for i in range(181):
                d=date(2025,1,1)+timedelta(days=i)
                rows.append([r['params']['exchange'],d.strftime('%Y%m%d'),int(d.weekday()<5),''])
            return {'fields':campaign.FIELDS['trade_cal'],'items':rows}
        start=date.fromisoformat(r['start']);end=date.fromisoformat(r['end'])
        days=[start+timedelta(days=i) for i in range((end-start).days+1) if (start+timedelta(days=i)).weekday()<5]
        if r['provider']=='hithink_native':return {'item':[{'date_ms':int(datetime.combine(d,datetime.min.time()).replace(tzinfo=CST).timestamp()*1000),
            'open_price':10,'high_price':12,'low_price':9,'close_price':11,'volume':10000,'turnover':100000} for d in days]}
        values=[10,12,9,11,100,100] if r['api']=='daily' else [2] if r['api']=='adj_factor' else [1,2,3,4,5]
        return {'fields':campaign.FIELDS[r['api']],'items':[[r['code'],d.strftime('%Y%m%d'),*values] for d in days]}


def package(tmp_path):
    path=tmp_path/'receipts';campaign.capture(path,client=Fixture())
    return path


def replace(path,value):
    path.unlink();write_json(path,value)


def reseal(path):
    (path/'completed.json').unlink();seal(path)


def declared_native(path):
    reg=read_json(path/'registration.json')[0];reg['origin']='native_and_relay'
    replace(path/'registration.json',reg);reseal(path)


def test_fixed_campaign_budget_and_two_exchange_calendars():
    plan=campaign.plan();assert len(plan)==55
    assert sum(r['provider']=='hithink_native' for r in plan)==13
    assert len({r['params']['exchange'] for r in plan if r['kind']=='calendar'})==2
    assert len(campaign.CODES)==4


def test_campaign_fixture_does_not_self_qualify(tmp_path):
    path=package(tmp_path)
    assert len(campaign.replay(path)[2])==55
    with pytest.raises(ValueError,match='synthetic'):campaign.derive(path)


def test_adjusted_inputs_money_units_and_default_labels_remain_blocked(tmp_path):
    path=package(tmp_path);declared_native(path)
    result=campaign.derive(path)
    assert result['coverage']['price_and_factor_rows']==516
    row=result['rows'][0]
    assert row['open']==20 and row['volume']==5000 and row['turnover']==100000 and row['net_mf_amount']==10000
    assert row['adjusted_price_target_ret']==pytest.approx(10)
    assert all(r['label_next_ret'] is None and r['label_date'] is None for r in result['rows'])
    assert not result['research_ready'] and result['formal_blockers']


@pytest.mark.parametrize('change',['calendar_missing','calendar_duplicate','calendar_exchange','identity_code','identity_exchange','identity_date'])
def test_calendar_and_identity_contracts_reject_invalid_rows(change):
    request=next(r for r in campaign.plan() if r['kind']==('calendar' if change.startswith('calendar') else 'identity_snapshot'))
    data=deepcopy(Fixture().query(request))
    if change=='calendar_missing':data['items'].pop()
    if change=='calendar_duplicate':data['items'][-1]=data['items'][0]
    if change=='calendar_exchange':data['items'][0][0]='OTHER'
    if change=='identity_code':data['items'][0][0]='000002.SZ'
    if change=='identity_exchange':data['items'][0][3]='SSE'
    if change=='identity_date':data['items'][0][6]='bad'
    with pytest.raises(ValueError):campaign.parse(request,data)


@pytest.mark.parametrize('change',['budget','future','wrong_code','extra_member'])
def test_sealed_campaign_cannot_hide_registration_or_receipt_changes(tmp_path,change):
    path=package(tmp_path);declared_native(path)
    if change=='budget':
        file=path/'registration.json';body=read_json(file)[0];body['max_requests']=100;replace(file,body)
    elif change=='extra_member':write_json(path/'extra.json',{})
    else:
        file=path/'receipt-13.json';body=read_json(file)[0]
        if change=='future':body['received_at']='2099-01-01T00:00:00+00:00'
        else:body['data']['items'][0][0]='000002.SZ'
        replace(file,body)
    reseal(path)
    with pytest.raises(ValueError):campaign.derive(path)


def test_price_conflict_retains_cohort_and_blocks_adjacent_proxy(tmp_path):
    path=package(tmp_path);declared_native(path)
    file=path/'receipt-13.json';body=read_json(file)[0];body['data']['items'][1][2]=10.1
    replace(file,body);reseal(path)
    result=campaign.derive(path)
    assert len(result['rows'])==516 and result['coverage']['gap_counts']=={'source_price_conflict':1}
    assert result['rows'][0]['adjusted_price_target_ret'] is None
    assert result['rows'][1]['close'] is None


def test_alpha158_refuses_unknown_units_before_computation(tmp_path):
    with pytest.raises(ValueError,match='input units'):compute(None,None,tmp_path/'not_created',input_units='guess')
    assert not (tmp_path/'not_created').exists()
