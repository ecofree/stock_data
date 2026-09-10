"""Read-only three-ledger reconciliation, not a claim of selection alpha."""
from decimal import Decimal
import json

from .domain import identity, utc
from .paper_storage import load_paper


def project_attribution(store, account_id):
    store.check_owner()
    if not store.con.execute('SELECT count(*) FROM v2_schema WHERE version=4').fetchone()[0]:
        raise ValueError('existing paper ledger required; attribution never initializes a ledger')
    book = load_paper(store,account_id)
    now = utc(store.clock())
    if utc(book.state['last_at'])>now:
        raise ValueError('cannot project future ledger knowledge as current evidence')
    orders,signals,missing = [],{},[]
    for order in book.state['orders'].values():
        fills = [f for f in book.state['fills'] if f['order_id']==order['order_id']]
        row = store.con.execute('SELECT payload FROM decision_certificate WHERE decision_id=?',[order['decision_ref']]).fetchone()
        certificate = json.loads(row[0]) if row else None
        if certificate is not None and (identity(certificate)!=order['decision_ref'] or
            certificate['account_id']!=account_id or certificate['instrument']!=order['instrument'] or
            certificate.get('side','buy')!=order['side'] or not certificate['hypothetical_ready']):
            raise ValueError('order decision reference checksum/account/side mismatch')
        if certificate and certificate.get('signal_id'):
            sigrow = store.con.execute('SELECT payload FROM signal_event WHERE signal_id=?',[certificate['signal_id']]).fetchone()
            if not sigrow or identity(json.loads(sigrow[0]))!=certificate['signal_id']:
                raise ValueError('opportunity evidence missing or corrupted')
            signals[certificate['signal_id']] = json.loads(sigrow[0])
        qty = sum(f['quantity'] for f in fills)
        gross = sum(f['quantity']*f['price_fen'] for f in fills)
        fees = sum(f['fee_fen'] for f in fills)
        if qty!=order['quantity']-order['remaining'] or gross!=order['notional_fen'] or fees!=order['fee_fen']:
            raise ValueError('fill/order attribution control total mismatch')
        benchmark = certificate['price_fen'] if certificate else None
        effect = ((benchmark*qty-gross) if order['side']=='buy' else (gross-benchmark*qty)) if benchmark is not None else None
        delay = None
        if certificate and qty:
            seconds = sum(Decimal(str((utc(f['at'])-utc(certificate['asof'])).total_seconds()))*f['quantity'] for f in fills)
            if seconds<0:
                raise ValueError('fill precedes confirmation evidence')
            delay = str(seconds/qty)
        if certificate is None:
            missing.append(order['order_id'])
        orders.append({'order_id':order['order_id'],'decision_id':order['decision_ref'],
            'side':order['side'],'instrument':order['instrument'],'status':order['status'],
            'quantity':order['quantity'],'filled_quantity':qty,'unfilled_quantity':order['remaining'],
            'gross_fen':gross,'fees_fen':fees,'confirmation_price_fen':benchmark,
            'execution_price_effect_fen':effect,'execution_effect_after_fees_fen':effect-fees if effect is not None else None,
            'weighted_confirmation_to_fill_seconds':delay,'exit_rationale':certificate.get('exit_policy') if certificate else None,
            'fill_event_ids':[f['quote_event_id'] for f in fills],
            'benchmark_status':'frozen_confirmation_quote' if certificate else 'unavailable_legacy_direct_order'})
    initial_unrealized = sum(l['quantity']*l['mark_price_fen']-l['cost_fen'] for l in book.config.get('initial_lots',[]))
    unrealized = sum(l['quantity']*book.state['marks'][l['instrument']]['price_fen']-l['cost_fen'] for l in book.state['lots'])
    components = {'realized_pnl_fen':book.state['realized_pnl_fen'],'unrealized_pnl_fen':unrealized,
                  'initial_unrealized_pnl_fen':initial_unrealized,'income_fen':book.state['income_fen']}
    explained = components['realized_pnl_fen']+unrealized-initial_unrealized+components['income_fen']
    profit = book.summary()['profit_ex_external_cash_fen']
    if explained!=profit or sum(o['fees_fen'] for o in orders)!=book.state['fees_fen']:
        raise ValueError('portfolio attribution control total mismatch')
    held = []
    if store.con.execute("SELECT count(*) FROM information_schema.tables WHERE table_name='exit_reservation'").fetchone()[0]:
        held = [dict(zip(('reservation_id','instrument','quantity','status'),r)) for r in store.con.execute(
            "SELECT reservation_id,instrument,quantity,status FROM exit_reservation WHERE account_id=? AND status IN ('held','unknown') ORDER BY reservation_id",[account_id]).fetchall()]
    confirmations, outcomes = [],[]
    for action_id,payload in store.con.execute('SELECT action_id,payload FROM operator_action WHERE account_id=? ORDER BY happened_at,action_id',[account_id]).fetchall():
        action = json.loads(payload)
        if action['request'].get('operation') in ('close_unsent','reconcile_paper_unsent'):
            result=action['result']
            if result.get('account_id')!=account_id or identity({k:v for k,v in result.items() if k!='closure_id'})!=result.get('closure_id'):
                raise ValueError('paper closure checksum/account mismatch')
            continue
        certificate = {k:v for k,v in action['result'].items() if k!='decision_id'}
        if identity(certificate)!=action['result']['decision_id'] or certificate['account_id']!=account_id:
            raise ValueError('confirmation evidence checksum/account mismatch')
        confirmations.append({'action_id':action_id,**action})
        linked = [o for o in orders if o['decision_id']==action['result']['decision_id']]
        table='exit_reservation' if certificate.get('side')=='sell' else 'reservation'
        reservation=None
        if certificate.get('reservation_id'):
            reservation=store.con.execute(f'SELECT status FROM {table} WHERE reservation_id=?',
                                          [certificate['reservation_id']]).fetchone()
        closure=reservation[0] if reservation and reservation[0] in ('cancelled','expired_not_sent','reconciled_not_sent') else None
        outcomes.append({'action_id':action_id,'side':certificate.get('side','buy'),'instrument':certificate['instrument'],
            'hypothetical_ready':certificate['hypothetical_ready'],'blockers':certificate['blockers'],
            'order_ids':[o['order_id'] for o in linked],
            'outcome':'paper_order_recorded' if linked else closure or ('not_delivered_or_unknown' if certificate['hypothetical_ready'] else 'confirmation_blocked')})
    result = {'schema_version':1,'account_id':account_id,'asof':book.state['last_at'],'generated_at':now.isoformat(),
        'scope':'paper_observed_capacity_proxy_not_actual_performance','execution_ready':False,
        'ledger_state_hash':identity(book.state),'ledger_config_hash':identity(book.config),
        'current_valuation_complete':book.complete_valuation(now),
        'opportunity_ledger':{'signals':signals,'selection_skill_status':'not_estimated',
                              'limitation':'no_predeclared_horizon_or_counterfactual; no win_rate_or_alpha_inferred'},
        'paper_portfolio_ledger':{'summary':book.summary(),'components':components,'explained_profit_fen':explained,
                                  'reconciliation_difference_fen':explained-profit,'orders':orders,
                                  'unlinked_order_ids':missing,'held_exit_reservations':held},
        'operator_decision_ledger':confirmations,
        'decision_outcomes':outcomes,
        'actual_operator_ledger':{'status':'not_imported_into_this_projection','trades':None,'return':None},
        'interpretation':['execution_effect_is_against_confirmation_quote_for_filled_quantity_only',
                          'execution_effect_is_not_additive_to_portfolio_pnl; fees_already_in_cost_basis',
                          'unfilled_opportunity_cost_not_estimated','delay_is_observed_not_causal_timing_attribution',
                          'no_real_account_or_broker_fill_acceptance']}
    result['projection_id'] = identity(result)
    return result


def render_attribution_markdown(data):
    """Portable review artifact; strings escaped to avoid table/link injection."""
    def cell(value):
        return str(value).replace('|','\\|').replace('\n',' ').replace('\r',' ').replace('<','&lt;').replace('>','&gt;')
    portfolio = data['paper_portfolio_ledger']
    lines = ['# 三账归因复盘（纸面代理）','',
        f"账本时点：{cell(data['asof'])}；当前估值完整：{data['current_valuation_complete']}。",
        '全部金额单位为分。合成输入时仅验证实现，不是策略表现。真实成交未导入，收益保持未知。','',
        '## 组合账核对','',
        '已实现 + 未实现 − 初始未实现 + 分红收入 = 剔除外部入出金后的观测损益。','',
        *[f'- {k}: {v}' for k,v in portfolio['components'].items()],
        f"- 解释损益：{portfolio['explained_profit_fen']}；核对差额：{portfolio['reconciliation_difference_fen']}",'',
        '## 执行与成本','',
        '| 订单 | 方向 | 状态 | 成交/委托 | 价格偏差贡献 | 费用 | 确认至成交加权秒数 |',
        '|---|---|---|---:|---:|---:|---:|']
    for o in portfolio['orders']:
        values = (o['order_id'],o['side'],o['status'],f"{o['filled_quantity']}/{o['quantity']}",
                  o['execution_price_effect_fen'],o['fees_fen'],o['weighted_confirmation_to_fill_seconds'])
        lines.append('| '+' | '.join(cell(v) if v is not None else '未识别' for v in values)+' |')
    lines += ['','价格偏差以冻结确认价为基准，仅计已成交数量，正值有利；不是选股 alpha。',
              '费用已计入组合成本，不可把这张执行归因表再加到组合损益。未成交机会成本与因果择时贡献未估计。','',
              '## 机会与真实人工账','',
              f"关联 {len(data['opportunity_ledger']['signals'])} 条原始信号，未预登记评价期限/反事实，不推断选股胜率。",
              '纸面确认和真实成交分开；真实账 trades=null、return=null，不冒充零成交或零收益。','',
              f"投影 ID：{data['projection_id']}",f"账本哈希：{data['ledger_state_hash']}"]
    return '\n'.join(lines)+'\n'
