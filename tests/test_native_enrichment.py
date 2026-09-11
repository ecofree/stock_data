from copy import deepcopy
from datetime import datetime

import pytest

from tests.test_v2_daily_session import report,moment
from trade_system.v2 import native_enrichment as native
from trade_system.v2.daily_session import next_review,CST


class Client:
    def _get(self,path,params):
        if path==native.MEMBERS:
            return {'timestamp':int(moment('2026-09-11').timestamp()*1000),
                    'item':[{'thscode':'000002.SZ','ticker':'000002','name':'fixture'}]}
        return {'timestamp':int(moment('2026-09-11').timestamp()*1000),'item':[{
            'date_ms':int(datetime(2026,9,11,tzinfo=CST).timestamp()*1000),
            'open_price':10,'high_price':12,'low_price':9,'close_price':11,'volume':1000,'turnover':11000}]}


def test_native_receipts_consume_exact_cohort_but_fixtures_not_native(tmp_path):
    parent=report();following=report('2026-09-11')
    result=native.capture(parent,'2026-09-11',tmp_path/'r',themes=['886042.TI'],
        client=Client(),clock=lambda:moment('2026-09-11'))
    assert native.verify(tmp_path/'r')==result
    assert result['origin']=='synthetic_fixture'
    assert result['topics']['886042.TI']['cohort_members']==['SZ.000002']
    from trade_system.v2.daily_session_view import render_enrichment
    assert '10 / 12 / 9 / 11' in render_enrichment(result)
    review=next_review(parent,following,[],created=moment('2026-09-11'),observations=result['observations'],enrichment=result)
    assert float(review['rows'][0]['next_session_open_close_pct'])==10
    assert review['rows'][0]['actual_operator_return'] is None
    from trade_system.v2.daily_session_view import render_followup
    html=render_followup(review)
    assert '886042.TI' in html and '不是历史成分' in html
    with pytest.raises(ValueError,match='provenance'):
        next_review(parent,following,[],created=moment('2026-09-11'),observations=result['observations'])
    changed=deepcopy(result);changed['parent_report_id']='wrong'
    with pytest.raises(ValueError,match='bind'):
        next_review(parent,following,[],created=moment('2026-09-11'),observations=result['observations'],enrichment=changed)


@pytest.mark.parametrize('bad',['wrong_day','future','duplicate','bad_ohlc'])
def test_rejects_bad_price_evidence(tmp_path,bad):
    class Bad(Client):
        def _get(self,path,params):
            r=super()._get(path,params)
            if bad=='wrong_day': r['item'][0]['date_ms']-=86400000
            if bad=='future': r['timestamp']+=86400000
            if bad=='duplicate': r['item']*=2
            if bad=='bad_ohlc': r['item'][0]['high_price']=9
            return r
    with pytest.raises(ValueError):
        native.capture(report(),'2026-09-11',tmp_path/'r',client=Bad(),clock=lambda:moment('2026-09-11'))
    assert not (tmp_path/'r'/'completed.json').exists()


def test_preclose_no_network_or_artifact(tmp_path):
    with pytest.raises(ValueError,match='close'):
        native.capture(report(),'2026-09-11',tmp_path/'r',client=Client(),clock=lambda:moment('2026-09-11','11:00:00'))
    assert not (tmp_path/'r').exists()


def test_client_initialization_failure_retains_safe_diagnostic(tmp_path,monkeypatch):
    from trade_system import hithink_client
    import json
    def fail(**kwargs):raise RuntimeError('secret must not be retained')
    monkeypatch.setattr(hithink_client,'HiThinkClient',fail)
    with pytest.raises(RuntimeError):
        native.capture(report(),'2026-09-11',tmp_path/'r',clock=lambda:moment('2026-09-11'))
    failed=(tmp_path/'r'/'failed.json').read_text(encoding='utf-8')
    assert json.loads(failed)=={'error_type':'RuntimeError','retained_responses':0}
    assert 'secret' not in failed and not (tmp_path/'r'/'completed.json').exists()
