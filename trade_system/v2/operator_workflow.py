"""Offline paper-plan handoff. Exported JSON is data, never command authority."""
import json
import html

from .accounts import latest_account
from .decisions import DecisionService, RiskPolicy
from .domain import canonical, identity, utc
from .reporting import project_review, render_review


SCOPE = 'offline_paper_confirmation_only'
ACK = 'I_UNDERSTAND_PAPER_ONLY_NO_REAL_ORDER'


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
    actions = []
    rows = store.con.execute('SELECT action_id,payload FROM operator_action WHERE account_id=? ORDER BY happened_at,action_id',[account_id]).fetchall()
    for action_id,raw in rows:
        action = json.loads(raw)
        result = action['result']
        if result['account_id'] != account_id or identity({k:v for k,v in result.items() if k!='decision_id'}) != result['decision_id']:
            raise ValueError('operator confirmation fingerprint or account mismatch')
        actions.append({'request_id':action_id,'request':action['request'],'confirmation':result,
            'delivery_status':'see_linked_paper_attribution' if review.get('attribution') else 'not_verified_no_paper_ledger'})
    body = {'schema':1,'scope':'paper_workflow_review_not_actual_operator_performance',
            'generated_at':utc(store.clock()).isoformat(),'account_id':account_id,
            'review':review,'confirmations':actions,'execution_ready':False,'actual_operator_return':None}
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
           'paper_order_recorded':'已有纸面订单'}
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
