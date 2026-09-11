from copy import deepcopy

from tests.test_native_enrichment import Client
from tests.test_v2_daily_session import report,moment,item,packet
from trade_system.v2.daily_session import next_review
from trade_system.v2.daily_session_view import render_followup
from trade_system.v2.domain import identity
from trade_system.v2 import native_enrichment


def fixture_review(tmp_path,notes=(),*,missing=False):
    parent=report(items=[item('000002'),item('000001')]);following=report('2026-09-11')
    class Mixed(Client):
        def _get(self,path,params):
            result=super()._get(path,params)
            if missing and params.get('thscode')=='000001.SZ':result['item']=[]
            return result
    enriched=native_enrichment.capture(parent,'2026-09-11',tmp_path/'e',client=Mixed(),clock=lambda:moment('2026-09-11'))
    return next_review(parent,following,notes,created=moment('2026-09-11'),observations=enriched['observations'],enrichment=enriched)


def test_followup_exposes_groups_range_missing_and_no_real_return(tmp_path):
    result=fixture_review(tmp_path,missing=True);html=render_followup(result)
    assert '判断分组与观察覆盖' in html and '原依据与风险' in html
    assert '无事前判断' in html and '缺失 / 不填零' in html
    assert '开→低 -10.00%' in html and '开→高 +20.00%' in html
    assert '不是命中率、策略收益或真人绩效' in html
    assert '<script' not in html and 'connect-src' in html
    assert html.count('<summary>原依据与风险</summary>')==2


def test_followup_preserves_late_notes_and_escapes_operator_text(tmp_path):
    parent=report(items=[item('000002'),item('000001')])
    notes=[]
    for index,time in enumerate(('08:00:00','10:00:00')):
        note=packet(parent,note_id=str(index),operator='<script>operator</script>',hypothesis='<img src=x onerror=alert(1)>',
                    invalidation='<b>original threshold</b>',created_at=moment('2026-09-11',time).isoformat())
        body={'note':note,'received_at':note['created_at']};notes.append({**body,'receipt_id':identity(body)})
    result=fixture_review(tmp_path,notes);html=render_followup(result)
    assert '迟交 / 不算事前判断' in html and '下一开盘前已接收' in html
    assert '查看原判断及失效条件' in html and '&lt;script&gt;operator' in html
    assert '<img src=x' not in html and '&lt;b&gt;original threshold' in html
    assert result['learning']['groups']['observe']['cohort_count']==1


def test_pending_view_has_no_completed_group_metrics():
    html=render_followup({'scope':'pending_next_session_not_completed_review','cohort_size':0,'rows':[]})
    assert '尚未取得下一交易日数据' in html and 'id="review-groups"' not in html


def test_review_changed_fingerprint_is_rejected(tmp_path):
    import pytest
    result=deepcopy(fixture_review(tmp_path));result['learning']['groups']['observe']['cohort_count']=9
    with pytest.raises(ValueError,match='fingerprint'):render_followup(result)
