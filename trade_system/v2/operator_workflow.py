"""Offline paper-plan handoff. Exported JSON is data, never command authority."""
import json
import html

from .accounts import latest_account
from .decisions import DecisionService, RiskPolicy
from .domain import canonical, identity, utc
from .reporting import project_review, render_review


SCOPE = 'offline_paper_confirmation_only'
ACK = 'I_UNDERSTAND_PAPER_ONLY_NO_REAL_ORDER'


def read_observation_plan(output, plan_id):
    import re
    from pathlib import Path
    from .gap_evidence import read_json
    from .research_journal import read_note
    if not isinstance(plan_id,str) or not re.fullmatch('[a-f0-9]{64}',plan_id):raise ValueError('valid plan identity required')
    plan=read_json(Path(output)/'notes/plans'/(plan_id+'.json'))[0]
    if plan_id!=identity({k:v for k,v in plan.items() if k!='plan_id'}):raise ValueError('plan changed')
    note=read_note(output,plan['note_id'])
    if note['instrument']!=plan['instrument']:raise ValueError('plan parent differs')
    return plan


def save_observation_plan(output, values):
    """Append an explicit non-executable plan; the existing account core owns risk."""
    from pathlib import Path
    from datetime import timedelta
    import re
    from trade_system.file_lock import FileLock
    from .domain import now_utc, number
    from .gap_evidence import read_json
    from .research_journal import read_note, durable_event
    output=Path(output)
    command={k:values.get(k,'') for k in ('note_id','operator','condition','threshold','valid_until','supersedes')}
    request_id=values.get('request_id')
    if not isinstance(request_id,str) or not re.fullmatch('[a-f0-9]{32}',request_id):raise ValueError('valid plan request id required')
    with FileLock(output/'judgement.guard'):
        for path in (output/'notes/plans').glob('*.json'):
            old=read_observation_plan(output,path.stem)
            if old['request_id']==request_id:
                if old['command_id']!=identity(command):raise ValueError('plan request reused with different content')
                return old['plan_id']
        note=read_note(output,command['note_id'])
        if command['operator']!=note['operator']:raise ValueError('plan must preserve declared judgement author')
        if command['condition'] not in ('manual','price_above','price_below'):raise ValueError('explicit supported condition required')
        if command['condition']!='manual' and number(command['threshold'])<=0:raise ValueError('positive unadjusted CNY threshold required')
        at=now_utc();until=utc(command['valid_until'])
        if not at<until<=at+timedelta(days=30):raise ValueError('plan expiry must be in the next 30 days')
        if command['supersedes']:
            parent=read_observation_plan(output,command['supersedes'])
            if any(parent[k]!=command[k] for k in ('note_id','operator')):raise ValueError('plan revision parent differs')
            if any(read_json(p)[0].get('supersedes')==command['supersedes'] for p in (output/'notes/plans').glob('*.json')):
                raise ValueError('plan already revised')
        risk=configured_account_risk(output,at.isoformat())
        plan=dict(command,request_id=request_id,command_id=identity(command),instrument=note['instrument'],
            evidence_id=note.get('evidence_id') or note.get('prediction_id'),received_at=at.isoformat(),
            account_id=risk.get('account_id'),account_snapshot_id=risk.get('snapshot_id'),
            invalidation=note['invalidation'],scope='observation_plan_not_order',price_unit='unadjusted_CNY',
            quantity=None,execution_ready=False,account_status=risk['status'])
        plan['plan_id']=identity(plan);durable_event(output/'notes/plans',plan['plan_id'],plan)
        return plan['plan_id']


def configured_account_risk(output, as_of):
    from pathlib import Path
    from .gap_evidence import read_json
    from .accounts import risk_snapshot
    path=Path(output)/'workspace-config.json'
    config=read_json(path)[0] if path.exists() else {}
    if not config.get('account_database') or not config.get('account_id'):
        return {'status':'account_unknown','execution_ready':False,'positions':[],'open_orders':[]}
    import duckdb
    try:return risk_snapshot(config['account_database'],config['account_id'],as_of)
    except (duckdb.Error,OSError,ValueError) as exc:
        return {'status':'account_source_unavailable','error':str(exc)[:160],'execution_ready':False,'positions':[],'open_orders':[]}


def evaluate_observation_plan(plan, as_of, *, quote=None, account=None):
    from .domain import number
    from datetime import datetime
    from zoneinfo import ZoneInfo
    at=utc(as_of)
    deadline=None
    if quote and quote.get('valid_until'):
        deadline=datetime.fromisoformat(quote['valid_until'])
        if deadline.tzinfo is None:deadline=deadline.replace(tzinfo=ZoneInfo('Asia/Shanghai'))
    blockers=[]
    if not account or account.get('status')!='account_snapshot_available_not_execution':blockers.append('account_not_verified')
    if plan.get('account_snapshot_id') and (account or {}).get('snapshot_id')!=plan['account_snapshot_id']:blockers.append('account_version_changed')
    if at<utc(plan['received_at']):state='data_insufficient'
    elif at>=utc(plan['valid_until']):state='expired'
    elif plan['condition']=='manual':state='pending_human_condition'
    elif (not quote or quote.get('instrument')!=plan['instrument'] or quote.get('price') is None
          or quote.get('state')!='current_observation_not_executable'
          or deadline is None or at>=utc(deadline)):
        state='data_insufficient'
    else:
        price=number(quote['price']);threshold=number(plan['threshold'])
        triggered=price>=threshold if plan['condition']=='price_above' else price<=threshold
        state='triggered' if triggered else 'pending'
    return {'state':state,'account_blockers':blockers,'execution_ready':False,
            'condition_scope':'observation_only_not_fill_or_order_permission'}


def observation_plans(output, as_of, quotes=()):
    from pathlib import Path
    import heapq
    paths=heapq.nlargest(100,(Path(output)/'notes/plans').glob('*.json'),key=lambda p:p.stat().st_mtime_ns)
    plans=[read_observation_plan(output,p.stem) for p in paths]
    superseded={p.get('supersedes') for p in plans};by_code={q['instrument']:q for q in quotes}
    account=configured_account_risk(output,as_of)
    invalidations=plan_invalidation_context(output)
    rows=[]
    for plan in plans:
        evaluation=evaluate_observation_plan(plan,as_of,quote=by_code.get(plan['instrument']),account=account)
        invalidation=plan_invalidation(output,plan,context=invalidations)
        if invalidation:evaluation.update(state='invalid',invalidation=invalidation)
        rows.append(dict(plan,is_latest=plan['plan_id'] not in superseded,evaluation=evaluation))
    return {'account':account,'rows':rows}


def plan_invalidation_context(output):
    from pathlib import Path
    from .gap_evidence import read_json
    from .research_journal import read_review,note_paths,review_paths
    plans={read_json(p)[0].get('supersedes') for p in (Path(output)/'notes/plans').glob('*.json')}
    notes={read_json(p)[0].get('supersedes') for p in note_paths(output)}
    reviews=[read_review(output,p.stem) for p in review_paths(output)]
    return {'plans':plans,'notes':notes,'invalid_notes':{r['note_id'] for r in reviews if r['conclusion']=='triggered'}}


def plan_invalidation(output, plan, *, context=None):
    state=context if context is not None else plan_invalidation_context(output)
    if plan['plan_id'] in state['plans']:return 'plan_superseded'
    if plan['note_id'] in state['notes']:return 'judgement_revised_requires_new_plan'
    # Once invalidated, a new plan version is required; a later note cannot revive it.
    if plan['note_id'] in state['invalid_notes']:return 'human_reported_invalidation'
    return None


def link_observation_paper_plan(store, output, plan_id, decision_id):
    """A plan can link only to a real existing core certificate, never manufacture one."""
    plan=read_observation_plan(output,plan_id);packet=export_plan(store,decision_id)
    cert=packet['certificate'];code=cert['instrument'].split('.')[-1]
    if code!=plan['instrument'] or utc(store.clock())>=utc(plan['valid_until']):raise ValueError('paper certificate does not match a current observation plan')
    if plan.get('account_id') and cert['account_id']!=plan['account_id']:raise ValueError('paper account differs')
    if plan.get('account_snapshot_id') and cert['snapshot_id']!=plan['account_snapshot_id']:raise ValueError('account snapshot changed')
    return {'plan_id':plan_id,'note_id':plan['note_id'],'paper_packet':packet,'execution_ready':False}


def confirm_linked_observation(store, output, linked, *, condition_confirmed=False, **confirmation):
    plan=read_observation_plan(output,linked['plan_id'])
    cert=validate_packet(store,linked['paper_packet'])
    if linked['note_id']!=plan['note_id'] or cert['instrument'].split('.')[-1]!=plan['instrument']:
        raise ValueError('linked observation parent differs')
    confirmation=dict(confirmation)
    confirmation['request_id']=identity([plan['plan_id'],confirmation['request_id']])
    action_id='desk:'+identity([linked['paper_packet']['packet_id'],confirmation['request_id']])
    existing=store.con.execute('SELECT 1 FROM operator_action WHERE action_id=?',[action_id]).fetchone()
    if not existing:
        if plan_invalidation(output,plan):raise ValueError('observation plan invalidated or superseded')
        if utc(store.clock())>=utc(plan['valid_until']):raise ValueError('observation plan expired')
        if plan['condition']=='manual' and condition_confirmed is not True:raise ValueError('explicit manual condition confirmation required')
        current=DecisionService(store,RiskPolicy(**cert['policy'])).evaluate(
            cert['account_id'],cert['signal_id'],confirmation['quote_manifest'],cert['quote_dataset'])
        if plan.get('account_snapshot_id') and current['snapshot_id']!=plan['account_snapshot_id']:
            raise ValueError('observation account version changed')
        if plan['condition']!='manual':
            state=evaluate_observation_plan(plan,store.clock().isoformat(),quote={
                'instrument':plan['instrument'],'price':current['price_fen']/100,
                'state':'current_observation_not_executable' if not current['blockers'] else 'unavailable',
                'valid_until':current['expires_at']})
            if state['state']!='triggered':raise ValueError('observation condition is not currently triggered')
    # Retries delegate to the durable core even after expiry; new requests recheck everything.
    return dict(confirm_plan(store,linked['paper_packet'],**confirmation),plan_id=linked['plan_id'],note_id=linked['note_id'])


def certificate(store, decision_id):
    store.check_owner()
    row = store.con.execute('SELECT payload FROM decision_certificate WHERE decision_id=?',[decision_id]).fetchone()
    if row is None:
        raise ValueError('unknown local decision certificate')
    data = json.loads(row[0])
    if identity(data) != decision_id:
        raise ValueError('local decision certificate fingerprint mismatch')
    if data['scope'] != 'paper_only' or data['execution_ready'] is not False or 'paper_confirm' not in data['allowed_actions']:
        raise ValueError('only original confirmable paper certificates can be exported')
    account = latest_account(store,data['account_id'])
    if account is None or account['mode'] != 'paper':
        raise ValueError('existing paper account required')
    return data


def export_plan(store, decision_id):
    data = certificate(store,decision_id)
    if not utc(data['asof']) <= utc(store.clock()) < utc(data['expires_at']):
        raise ValueError('cannot export future or expired paper plan; propose a fresh decision')
    if latest_account(store,data['account_id'])['snapshot_id'] != data['snapshot_id']:
        raise ValueError('account snapshot changed; propose a fresh decision')
    packet = {'schema':1,'scope':SCOPE,'decision_id':decision_id,'certificate':data,
              'execution_ready':False}
    return {**packet,'packet_id':identity(packet)}


def validate_packet(store, packet):
    if not isinstance(packet,dict) or set(packet) != {'schema','scope','decision_id','certificate','execution_ready','packet_id'}:
        raise ValueError('strict paper-plan packet required; arbitrary commands are forbidden')
    if packet['schema'] != 1 or packet['scope'] != SCOPE or packet['execution_ready'] is not False:
        raise ValueError('paper-only scope required')
    if identity({k:v for k,v in packet.items() if k!='packet_id'}) != packet['packet_id']:
        raise ValueError('paper packet fingerprint mismatch')
    original = certificate(store,packet['decision_id'])
    if original != packet['certificate']:
        raise ValueError('exported packet differs from authoritative local certificate')
    return original


def confirm_plan(store, packet, *, quantity_requested, operator, request_id, quote_manifest, acknowledgement):
    if acknowledgement != ACK:
        raise ValueError('explicit paper-only acknowledgement required')
    for name,value in [('operator',operator),('request_id',request_id)]:
        if not isinstance(value,str) or not value.strip() or len(value)>200:
            raise ValueError('bounded explicit '+name+' required')
    original = validate_packet(store,packet)
    # A caller chooses the intent ID, but cannot supply policy, account, side,
    # signal or price overrides. Current rechecks and reservations remain in the
    # single decision authority. Same request retry returns its durable result.
    action_id = 'desk:'+identity([packet['packet_id'],request_id])
    result = DecisionService(store,RiskPolicy(**original['policy'])).confirm(
        packet['decision_id'],quantity_requested=quantity_requested,operator=operator,
        request_id=action_id,quote_manifest=quote_manifest)
    return {'scope':SCOPE,'packet_id':packet['packet_id'],'request_id':action_id,
            'confirmation':result,'execution_ready':False,'paper_order_created':False,
            'operator_identity':'caller_declared_not_authenticated'}


def review_desk(store, account_id):
    """Same-account audit projection, including blocked/undelivered confirmations."""
    review = project_review(store,account_id)
    review['fixture_only'] = bool(review['products']) and all(
        p['origin'] in ('synthetic_fixture','fixture') for p in review['products'])
    actions,closures = [],[]
    rows = store.con.execute('SELECT action_id,payload FROM operator_action WHERE account_id=? ORDER BY happened_at,action_id',[account_id]).fetchall()
    for action_id,raw in rows:
        action = json.loads(raw)
        result = action['result']
        if action['request'].get('operation') in ('close_unsent','reconcile_paper_unsent'):
            if result.get('account_id')!=account_id or identity({k:v for k,v in result.items() if k!='closure_id'})!=result.get('closure_id'):
                raise ValueError('paper closure fingerprint/account mismatch')
            closures.append({'request_id':action_id,**action})
            continue
        if result['account_id'] != account_id or identity({k:v for k,v in result.items() if k!='decision_id'}) != result['decision_id']:
            raise ValueError('operator confirmation fingerprint or account mismatch')
        actions.append({'request_id':action_id,'request':action['request'],'confirmation':result,
            'delivery_status':'see_linked_paper_attribution' if review.get('attribution') else 'not_verified_no_paper_ledger'})
    body = {'schema':1,'scope':'paper_workflow_review_not_actual_operator_performance',
            'generated_at':utc(store.clock()).isoformat(),'account_id':account_id,
            'review':review,'confirmations':actions,'reservation_closures':closures,
            'execution_ready':False,'actual_operator_return':None}
    # Datetimes in the read-only account projection do not enter this payload;
    # project_review exposes the original JSON account declaration.
    canonical(body)
    return {**body,'report_id':identity(body)}


def render_desk(data):
    if data.get('report_id') != identity({k:v for k,v in data.items() if k!='report_id'}):
        raise ValueError('paper review fingerprint changed')
    e=lambda value:html.escape(str(value),quote=True)
    attr=data['review'].get('attribution') or {}
    outcomes={r['action_id']:r for r in attr.get('decision_outcomes',[])}
    orders={o['order_id']:o for o in attr.get('paper_portfolio_ledger',{}).get('orders',[])}
    rows=[]
    names={'confirmation_blocked':'确认被阻止','not_delivered_or_unknown':'未送达或待核对',
           'paper_order_recorded':'已有纸面订单','cancelled':'已撤销未送达预占',
           'expired_not_sent':'已关闭到期未送达预占','reconciled_not_sent':'纸面全账核对确认未送达'}
    for item in data['confirmations']:
        c=item['confirmation']; outcome=outcomes.get(item['request_id'],{})
        linked=[orders[k] for k in outcome.get('order_ids',[]) if k in orders]
        filled=sum(o['filled_quantity'] for o in linked)
        reasons='、'.join(c['blockers']) or '确认时通过；不代表当前许可'
        cells=[c['instrument'],'卖出' if c.get('side')=='sell' else '买入',item['request']['quantity'],
               names.get(outcome.get('outcome'),'无纸面账，不推断送达'),filled if attr else '未知',
               sum(o['fees_fen'] for o in linked) if attr else '未知']
        rows.append('<tr>'+''.join('<td>'+e(v)+'</td>' for v in cells)+'</tr><tr><td colspan="6"><details><summary>证书、请求和阻塞依据</summary><p>'+e(reasons)+
            '</p><p class="mono">'+e(item['request_id'])+'<br>'+e(c['decision_id'])+'</p><p>操作者声明：'+e(item['request']['operator'])+'；未作身份认证</p></details></td></tr>')
    if not rows:rows=['<tr><td colspan="6">尚无确认记录；不推断已经执行或空仓。</td></tr>']
    residual=attr.get('paper_portfolio_ledger',{}).get('reconciliation_difference_fen')
    section=('<section id="workflow"><div class="section-head"><h2>06 / 确认与成交复盘</h2><span class="tag">三种记录分别核对</span></div><p class="sub">确认不是成交。下面仅关联本账户的纸面证书、预占、订单和假设成交。</p><div class="scroll"><table><thead><tr><th>证券</th><th>方向</th><th>请求数量</th><th>送达状态</th><th>假设成交数量</th><th>费用 / 分</th></tr></thead><tbody>'+''.join(rows)+
        '</tbody></table></div><div class="cols" style="margin-top:24px"><div class="ledger"><p>纸面归因核对差额 / 分</p><strong>'+e(residual if residual is not None else '未知')+'</strong></div><div class="ledger"><p>真实操作者收益</p><strong>未导入</strong><p class="sub">不由纸面收益或模型评分代替</p></div></div><p class="mono">复盘版本 '+e(data['report_id'])+'</p></section>')
    page=render_review(data['review'])
    return page.replace('</nav>','<a href="#workflow"><b>06</b>确认复盘</a></nav>',1).replace('<footer>',section+'<footer>',1)
