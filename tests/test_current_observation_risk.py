"""Evaluated plan risks survive queue and page projection without account authority."""
from copy import deepcopy
import json
import re

import pytest

from trade_system.v2 import acceptance_scope, daily_workspace, operator_workflow as workflow
from trade_system.v2 import research_product_view as view


AT='2026-10-28T02:00:00+00:00'


def plan(instrument='000001', **changes):
    value={'plan_id':instrument,'note_id':'synthetic-note','instrument':instrument,
        'is_latest':True,'condition':'manual','threshold':None,
        'received_at':'2026-10-28T01:00:00+00:00','valid_until':'2026-10-29T01:00:00+00:00',
        'invalidation':'原先填写的失效条件，不是已经发生的原因','execution_ready':False}
    value.update(changes)
    return value


def data_for(rows):
    data=daily_workspace.empty_projection()
    data.update(acceptance_scope=acceptance_scope.SCOPE,historical_rehearsal=True,
        plans={'account':acceptance_scope.account_not_applicable(),'rows':rows})
    data['action_queue']=daily_workspace.action_queue(data)
    return data


def evaluated(value, quote=None):
    return dict(value,evaluation=workflow.evaluate_observation_plan(
        value,AT,quote=quote,account=acceptance_scope.account_not_applicable()))


def test_old_account_binding_risk_is_prioritized_without_exporting_identifiers():
    current=evaluated(plan())
    old=evaluated(plan('000002',account_id='DO-NOT-EXPORT-ACCOUNT',
        account_snapshot_id='DO-NOT-EXPORT-SNAPSHOT'))
    original=deepcopy(old)
    data=data_for([current,old]);risk=data['action_queue'][0]
    assert risk['instrument']=='000002' and risk['priority']==1
    assert [r['code'] for r in risk['risk']['blockers']]==['account_scope_conflict','account_version_changed']
    assert risk['reason']=='pending_human_condition'  # Condition and risk remain separate facts.
    assert risk['risk']['summary_label']=='存在风险阻断 · 待人工核对观察条件'
    assert data['action_queue'][1]['priority']==2
    exported=view.export_projection(data)
    rendered=view.render(data)
    assert '历史账户绑定不适用于当前观察范围' in rendered
    assert '历史账户版本与当前资料不匹配' in rendered
    assert all(secret not in json.dumps(exported)+rendered for secret in ('DO-NOT-EXPORT-ACCOUNT','DO-NOT-EXPORT-SNAPSHOT'))
    assert old==original and old['account_id']=='DO-NOT-EXPORT-ACCOUNT'
    assert acceptance_scope.observation_only(exported['plans']['account'])
    assert all(not p['evaluation']['execution_ready'] for p in exported['plans']['rows'])


@pytest.mark.parametrize('reason,context,label',[
    ('plan_superseded',{'plans':{'000002'},'notes':set(),'invalid_notes':set()},'计划已被新版本替代'),
    ('judgement_revised_requires_new_plan',{'plans':set(),'notes':{'synthetic-note'},'invalid_notes':set()},'原判断已修订'),
    ('human_reported_invalidation',{'plans':set(),'notes':set(),'invalid_notes':{'synthetic-note'}},'人工已记录失效条件触发'),
])
def test_actual_invalidation_reason_survives_projection_and_precedes_pending(reason,context,label):
    invalid=evaluated(plan('000002'))
    actual=workflow.plan_invalidation(None,invalid,context=context)
    assert actual==reason
    invalid['evaluation'].update(state='invalid',invalidation=actual)
    data=data_for([evaluated(plan()),invalid])
    row=data['action_queue'][0]
    assert row['instrument']=='000002' and row['reason']=='invalid' and row['priority']==1
    assert row['risk']['invalidation']['code']==reason
    assert label in row['risk']['invalidation']['message']
    page=view.render(data)
    assert label in page and '实际失效原因：' in page and '预设失效条件：' in page
    invalid['is_latest']=False
    assert all(r['instrument']!='000002' for r in daily_workspace.action_queue(data))
    assert label in view.render(data)  # Historical version remains inspectable, never actionable.


@pytest.mark.parametrize('changes,quote,state,label,priority',[
    ({},None,'pending_human_condition','待人工核对观察条件',2),
    ({'condition':'price_above','threshold':10},
        {'instrument':'000001','price':9,'state':'current_observation_not_executable','valid_until':'2026-10-28T03:00:00+00:00'},
        'pending','观察条件尚未触发',2),
    ({'condition':'price_above','threshold':10},
        {'instrument':'000001','price':11,'state':'current_observation_not_executable','valid_until':'2026-10-28T03:00:00+00:00'},
        'triggered','观察条件已触发（不代表可执行）',1),
    ({'condition':'price_above','threshold':10},None,'data_insufficient','缺少有效报价或时点资料',2),
    ({'valid_until':AT},None,'expired','计划已过期',1),
])
def test_condition_states_remain_distinct_and_never_create_execution_authority(changes,quote,state,label,priority):
    data=data_for([evaluated(plan(**changes),quote)])
    row=data['action_queue'][0]
    assert row['reason']==state and row['priority']==priority
    assert row['risk']['state_label']==label
    assert not row['risk']['blockers'] and row['risk']['invalidation'] is None
    assert row['risk']['execution_ready'] is False
    assert label in view.render(data)


def test_unknown_risk_display_is_generic_without_rewriting_original_evaluation():
    evaluation={'state':'private-account-unknown','account_blockers':['private-account-reason'],
        'invalidation':'private-actual-reason','execution_ready':True}
    risk=workflow.observation_plan_risk(evaluation)
    assert risk['state']=='unknown' and risk['priority']==1 and risk['execution_ready'] is False
    assert 'private-' not in json.dumps(risk)
    row=plan(evaluation=evaluation)
    page=view.render(data_for([row]))
    inline=json.loads(re.search(r'<script type="application/json" id="data">(.*?)</script>',page,re.S)[1])
    assert inline['plans']['rows'][0]['evaluation']==evaluation  # Do not rewrite the original evaluation.
    assert '未识别的账户阻断' in page and '具体原因需核对' in page


@pytest.mark.parametrize('event,expected',[
    ('judgement','judgement_revised_requires_new_plan'),
    ('human','human_reported_invalidation'),
    ('plan','plan_superseded'),
])
def test_journal_revisions_keep_original_and_display_the_actual_cause(tmp_path,event,expected):
    from tests.test_observation_plans import make_plan
    from tests.test_model_independent_workspace import command
    from trade_system.v2 import research_product as product, research_journal as journal
    from trade_system.v2.domain import now_utc
    (tmp_path/'workspace-config.json').write_text(json.dumps({'acceptance_scope':acceptance_scope.SCOPE}))
    original=make_plan(tmp_path)
    original_path=tmp_path/'notes/plans'/(original['plan_id']+'.json')
    before=original_path.read_bytes()
    if event=='judgement':
        product.save_note(tmp_path,dict(command(),request_id='b'*32,
            supersedes=original['note_id'],hypothesis='追加修订，原计划必须失效'))
    elif event=='human':
        journal.save_review(tmp_path,{'note_id':original['note_id'],'request_id':'b'*32,
            'reviewer':'SYNTHETIC TEST ONLY','evidence':'人工核对原条件已经触发','conclusion':'triggered'})
    else:
        workflow.save_observation_plan(tmp_path,{'note_id':original['note_id'],'operator':original['operator'],
            'request_id':'b'*32,'condition':'manual','valid_until':original['valid_until'],'supersedes':original['plan_id']})
    projected=workflow.observation_plans(tmp_path,now_utc().isoformat())
    old=next(r for r in projected['rows'] if r['plan_id']==original['plan_id'])
    assert old['evaluation']['state']=='invalid' and old['evaluation']['invalidation']==expected
    data=data_for(projected['rows']);rendered=view.render(data)
    risk=workflow.observation_plan_risk(old['evaluation'])
    assert risk['invalidation']['message'] in rendered
    assert original_path.read_bytes()==before
    if event=='plan':assert original['plan_id'] not in {r.get('plan_id') for r in data['action_queue']}
    else:assert data['action_queue'][0]['reason']=='invalid'
    assert all(not r['evaluation']['execution_ready'] for r in projected['rows'])
