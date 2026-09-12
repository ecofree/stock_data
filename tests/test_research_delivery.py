import http.client
from http.server import ThreadingHTTPServer
import threading
import re
from urllib.parse import urlencode

import numpy as np
import pandas as pd
import pytest

from tools.v2 import research_campaign as campaign
from trade_system.v2 import research_dataset as dataset, research_product as product
from trade_system.v2 import research_followup as followup, research_product_server as server
from trade_system.v2.domain import identity


def configuration():
    return {'schema':1,'scope':'retrospective_availability_selected_exploration','universe':['000001','600001'],
        'start':'2025-01-01','end':'2025-12-31','warmup_sessions':61,'max_rows':20000,
        'split':{'train':40,'valid':15,'test':20,'holdout':20}}


def prices(n=90):
    days=pd.bdate_range('2025-01-01',periods=n).strftime('%Y-%m-%d').tolist()
    rows=[{'datetime':d,'instrument':c,'open':10+i*.1,'close':10.05+i*.1,
        'high':11+i*.1,'low':9+i*.1,'volume':1000+i,'turnover':12000+i,'net_mf_amount':i-40.}
        for c in ('000001','600001') for i,d in enumerate(days)]
    return pd.DataFrame(rows),days


def prediction(day='2025-01-01'):
    p={'date':day,'captured_at':day+'T17:00:00+08:00','model_frozen_at':day+'T16:30:00+08:00',
        'rows':[{'instrument':'000001','prediction':1.}], 'predictions':1}
    return dict(p,prediction_id=identity(p))


def test_preflight_rejects_the_old_short_dataset_before_fit():
    with pytest.raises(ValueError,match='insufficient'): dataset.preflight(configuration(),prices(117)[1])
    assert dataset.preflight(configuration(),prices(218)[1])['minimum_sessions']==157


def test_no_duplicate_calendar_or_universe():
    c=configuration(); days=prices(218)[1]
    with pytest.raises(ValueError,match='unique'):dataset.preflight(c,days+[days[-1]])
    c['universe']=['000001','000001']
    with pytest.raises(ValueError,match='universe'):dataset.preflight(c,days)


def test_feature_prefix_invariance_and_no_gap_bridge():
    frame,days=prices(); full=dataset.features(frame,days)
    prefix=dataset.features(frame[frame.datetime<=days[69]],days[:70])
    pd.testing.assert_frame_equal(full[full.datetime<=pd.Timestamp(days[69])].reset_index(drop=True),prefix)
    assert full.feature_eligible.sum()==60
    frame.loc[(frame.instrument=='000001')&(frame.datetime==days[50]),'close']=np.nan
    broken=dataset.features(frame,days)
    assert not broken[broken.instrument=='000001'].feature_eligible.any()
    with pytest.raises(ValueError,match='duplicate'):dataset.features(pd.concat([frame,frame.iloc[:1]]),days)


@pytest.mark.parametrize('start,end,windows',[('2026-06-14','2026-09-11',1),('2025-01-01','2025-06-30',3)])
def test_generic_capture_same_entry_ranges(start,end,windows):
    c={'codes':['000001.SZ','600001.SH'],'start':start,'end':end,'window_days':90,'max_requests':50,'selection_scope':'test'}
    requests=campaign.plan(c)
    assert len(requests)==3+2*(4*windows+1)
    c['max_requests']=1
    with pytest.raises(ValueError,match='before network'):campaign.plan(c)


def test_64_valid_calendar_rows_do_not_trip_legacy_probe_limit():
    from tools.v2.probe_identity_sources import rows_for
    dates=pd.bdate_range('2026-06-15',periods=64)
    r={'provider':'xiaodefa_relay','api':'adj_factor','code':'000001.SZ','start':'2026-06-14','end':'2026-09-11','kind':'security_history'}
    d={'fields':campaign.FIELDS['adj_factor'],'items':[['000001.SZ',day.strftime('%Y%m%d'),2] for day in dates]}
    assert len(campaign.parse(r,d))==64
    with pytest.raises(ValueError,match='bounded response'):rows_for(r,d)


def test_capture_circuit_stops_calls_without_claiming_success(tmp_path):
    class Broken:
        calls=0
        def query(self,r):self.calls+=1;raise ValueError('unavailable')
    client=Broken();campaign.capture(tmp_path/'capture',client=client)
    _,_,_,statuses=campaign.replay(tmp_path/'capture')
    assert client.calls==6 and sum(s['status']=='skipped' for s in statuses)==49


def test_followup_pending_then_exact_real_price_target():
    frame,days=prices(3);p=prediction()
    assert followup.evaluate(p,frame,days,'2025-01-02T16:00:00+08:00')['paired']==0
    result=followup.evaluate(p,frame,days,'2025-01-03T16:00:00+08:00')
    assert result['status']=='mature' and result['paired']==1
    assert result['rows'][0]['target_pct']==pytest.approx((10.25/10.1-1)*100)
    frame=frame[frame.datetime!=days[1]]
    result=followup.evaluate(p,frame,days,'2025-01-03T16:00:00+08:00')
    assert result['paired']==0 and result['rows'][0]['target_pct'] is None


def test_followup_rejects_post_outcome_prediction_and_modified_archive():
    frame,days=prices(3);p=prediction();p['rows'][0]['prediction']=7
    with pytest.raises(ValueError,match='changed'):followup.evaluate(p,frame,days,'2025-01-03T16:00:00+08:00')
    p=prediction();p['captured_at']='2025-01-03T16:00:00+08:00';p['prediction_id']=identity({k:v for k,v in p.items() if k!='prediction_id'})
    assert followup.evaluate(p,frame,days,'2025-01-03T16:00:00+08:00')['status']=='not_prospective'


def test_note_requires_explicit_human_inputs_and_retains_versions(tmp_path,monkeypatch):
    monkeypatch.setattr(product,'read_prediction',lambda _:prediction())
    monkeypatch.setattr(product,'publish_desk',lambda _:None)
    p=prediction();values={'prediction_id':p['prediction_id'],'instrument':'000001','intent':'observe',
        'operator':'SYNTHETIC TEST ONLY','hypothesis':'test hypothesis','invalidation':'test invalidation'}
    first=product.save_note(tmp_path,values);values['hypothesis']='changed test hypothesis'
    second=product.save_note(tmp_path,values)
    assert first!=second and len(list((tmp_path/'notes').glob('*.json')))==2
    values['intent']='buy'
    with pytest.raises(ValueError,match='non-executable'):product.save_note(tmp_path,values)
    values['intent']='observe';values['operator']=''
    with pytest.raises(ValueError,match='human'):product.save_note(tmp_path,values)


def test_http_refuses_foreign_host_origin_csrf_and_paths(tmp_path,monkeypatch):
    monkeypatch.setattr(server,'read_current',lambda _:({}, {'index.html':b'<input name="csrf" value="__CSRF__">'}))
    monkeypatch.setattr(product,'read_prediction',lambda _:prediction())
    monkeypatch.setattr(product,'publish_desk',lambda _:None)
    service=ThreadingHTTPServer(('127.0.0.1',0),server.handler(tmp_path,tmp_path,0))
    port=service.server_port;service.RequestHandlerClass=server.handler(tmp_path,tmp_path,port)
    thread=threading.Thread(target=service.serve_forever,daemon=True);thread.start()
    try:
        def request(path='/',method='GET',headers=None,body=None):
            conn=http.client.HTTPConnection('127.0.0.1',port,timeout=3)
            conn.request(method,path,body=body,headers=headers or {});response=conn.getresponse()
            status=response.status;response.read();conn.close();return status
        assert request(headers={'Host':'evil.invalid'})==403
        assert request('/arbitrary-file')==404
        assert request('/note','POST',{},'csrf=bad')==403
        headers={'Origin':f'http://127.0.0.1:{port}','Content-Type':'application/x-www-form-urlencoded'}
        assert request('/note','POST',headers,'csrf=bad')==403
        assert request('/note','POST',headers,'x='+'a'*33000)==413
        conn=http.client.HTTPConnection('127.0.0.1',port,timeout=3);conn.request('GET','/')
        response=conn.getresponse();html=response.read().decode();conn.close()
        token=re.search(r'value="([^"]+)"',html).group(1)
        form={'csrf':token,'request_id':'1'*32,'prediction_id':prediction()['prediction_id'],'instrument':'000001','intent':'observe',
            'operator':'SYNTHETIC TEST ONLY','hypothesis':'test','invalidation':'test'}
        assert request('/note','POST',headers,urlencode(form))==303
        assert len(list((tmp_path/'notes').glob('*.json')))==1
    finally:service.shutdown();service.server_close();thread.join()


def test_prediction_and_contributions_use_frozen_feature_order(tmp_path):
    lgb=pytest.importorskip('lightgbm',reason='optional QLib model runtime; exercised in local research environment')
    from trade_system.v2.gap_evidence import write_json
    frame,days=prices();calculated=dataset.features(frame,days)
    fit=calculated[calculated.feature_eligible]
    model=lgb.train({'objective':'regression','verbosity':-1,'num_threads':1,'min_data_in_leaf':3},
        lgb.Dataset(fit[dataset.BASE],label=fit.ret_5d),num_boost_round=3)
    model.save_model(str(tmp_path/'model.txt'));write_json(tmp_path/'prep.json',{'used_features':dataset.BASE})
    result=product.predict(frame,days,{'model_path':str(tmp_path/'model.txt'),'preprocessing_path':str(tmp_path/'prep.json')})
    assert len(result)==2 and result.prediction.notna().all()
    for row in result.to_dict('records'):assert sum(row['contributions'].values())==pytest.approx(row['prediction'])
    frame.loc[frame.datetime==days[-1],'close']=np.nan
    assert product.predict(frame,days,{'model_path':str(tmp_path/'model.txt'),'preprocessing_path':str(tmp_path/'prep.json')}).prediction.isna().all()
