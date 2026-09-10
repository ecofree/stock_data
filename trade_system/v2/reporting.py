"""Read-only projection and self-contained five-zone decision review page."""
import html
import json

from .accounts import latest_account
from .domain import utc


def project_review(store, account_id, context_id=None):
    store.check_owner()
    account = latest_account(store, account_id)
    signals = [json.loads(r[0]) for r in store.con.execute('''SELECT payload FROM signal_event
        QUALIFY row_number() OVER (PARTITION BY instrument,strategy_version,
          coalesce(json_extract_string(payload,'$.instance_id'),'legacy_unscoped') ORDER BY asof_time DESC,rowid DESC)=1
        ORDER BY asof_time DESC''').fetchall()]
    decisions = [json.loads(r[0]) for r in store.con.execute('''SELECT payload FROM decision_certificate
        WHERE account_id=? ORDER BY rowid DESC LIMIT 100''', [account_id]).fetchall()]
    products = [dict(zip(('dataset','unit','semantics','consumer','origin'), r))
                for r in store.con.execute('SELECT * FROM data_product ORDER BY dataset').fetchall()]
    held = store.con.execute("SELECT coalesce(sum(amount_fen),0) FROM reservation WHERE account_id=? AND status IN ('held','unknown')", [account_id]).fetchone()[0]
    result = {'generated_at': utc(store.clock()).isoformat(), 'scope':'paper_only',
            'account': account['payload'] if account else None, 'account_reconciled': account['reconciled'] if account else False,
            'reserved_fen': held, 'signals': signals, 'decisions': decisions, 'products': products,
            'execution_ready':False, 'broker_routing':'disabled',
            'market_dimensions': None, 'theme_roles': None,
            'fact_count':store.con.execute('SELECT count(*) FROM fact').fetchone()[0],
            'operator_actions':store.con.execute('SELECT count(*) FROM operator_action WHERE account_id=?', [account_id]).fetchone()[0]}
    if context_id is not None:
        from .context_storage import get_context
        from .market_context import holding_risks
        context = get_context(store, context_id)
        result.update(context=context, market_dimensions=context['market_dimensions'], theme_roles=context['theme_roles'],
                      holding_risks=holding_risks(context, result['account']))
    if store.con.execute("SELECT count(*) FROM information_schema.tables WHERE table_name='paper_account'").fetchone()[0]:
        if store.con.execute('SELECT count(*) FROM paper_account WHERE account_id=?',[account_id]).fetchone()[0]:
            from .attribution import project_attribution
            result['attribution'] = project_attribution(store,account_id)
    return result


def render_review(data):
    e = lambda value: html.escape(str(value), quote=True)
    account = data.get('account')
    account_title = '纸面账户' if account and account['mode']=='paper' else '真实账户待核验'
    cash = e(account['cash']) if account else '—'
    equity = e(account['equity']) if account else '—'
    position_rows = ''.join(f"<tr><td>{e(p['instrument'])}</td><td>{e(p['quantity'])}</td><td>{e(p['sellable'])}</td><td>{e(p['mark_price'])}</td></tr>"
                             for p in (account or {}).get('positions', []))
    if not position_rows:
        position_rows = '<tr><td colspan="4" class="empty">没有经过核验的持仓输入；不能据此推断空仓。</td></tr>'
    risk_html = ''.join(f'<p>{e(r["instrument"])} · {e(", ".join(r["reasons"]))} · 仅观察，不是卖出指令</p>' for r in data.get('holding_risks', []))
    plans = []
    for index, decision in enumerate(data.get('decisions', [])):
        reasons = ''.join(f'<li>{e(reason)}</li>' for reason in decision['blockers']) or '<li>签发时通过纸面检查，不等于当前可执行。</li>'
        plans.append(f'''<article class="plan" data-search="{e(decision['instrument'])}">
          <div class="plan-head"><h3>{e(decision['instrument'])}</h3><span class="tag">历史纸面证书</span></div>
          <dl><div><dt>数量上限</dt><dd>{e(decision['max_quantity'])}</dd></div>
          <div><dt>有效期</dt><dd class="small">{e(decision['expires_at'])}</dd></div></dl>
          <details><summary>依据与阻塞原因</summary><ul>{reasons}</ul>
          <p class="mono">账户版本 {e(decision['snapshot_id'][:16])}</p>
          <p class="mono">输入版本 {e(decision['input_manifest'][:16])}</p></details>
          <button disabled title="静态页不能确认；须通过本地服务重新校验">静态快照 · 不提供确认</button></article>''')
    if not plans:
        plans = ['<div class="empty">尚无决策证书。数据、策略或账户前提不足时，不生成可执行建议。</div>']
    context = data.get('context')
    market_html = '<div class="empty">广度、梯队晋级、失败样本与轮动尚未接入本投影，不从测试参数推断当前行情。</div>'
    themes_html = '<div class="empty">题材归属、当日催化和核心角色须有同一时点的证据。当前不生成未经核验的“龙头”标签。</div>'
    research_notice = ''
    if context:
        market = context['market_dimensions']
        def metric(value, percent=False):
            if value is None:
                return '缺证据'
            return f'{value*100:.2f}%' if percent else e(value)
        metrics = [('观测覆盖 / 声明股票池',f"{market['observed_quotes']} / {market['expected_universe']}"),
                   ('上涨 / 下跌 / 平盘',f"{market['advance']} / {market['decline']} / {market['flat']}"),
                   ('观测晋级 / 前日涨停样本',f"{market['promoted_observed']} / {market['prior_limit_cohort']}"),
                   ('未晋级样本平均当日涨幅',f"{market['nonpromoted_mean_return_pct']:.2f}%" if market['nonpromoted_mean_return_pct'] is not None else '缺证据'),
                   ('题材集中度 HHI / 分摊归属',metric(round(market['fractional_theme_hhi'],4) if market['fractional_theme_hhi'] is not None else None)),
                   ('前五题材集合变化率',metric(market['top5_rotation_jaccard'],True))]
        market_html = '<div class="cols">'+''.join(f'<div class="ledger"><p>{e(label)}</p><strong>{value}</strong></div>' for label,value in metrics)+'</div>'
        market_html += f'<p class="sub">观测前一开市日 {e(market["previous_open"])}；认证日历 {e(market["calendar_authority_verified"])}。晋级、失败与轮动均限于有记录的样本，不是全市场成交验证。</p>'
        theme_rows = []
        for theme in context['theme_roles'][:30]:
            label = theme['name'] if '\ufffd' not in theme['name'] else '名称编码损坏 · 以题材代码定位'
            theme_rows.append(f'<tr><td>{e(theme["theme_id"])}<br>{e(label)}</td><td>{theme["observed_members"]}/{theme["member_count"]}</td>'
                f'<td>{metric(theme["advancing_fraction"],True)}</td><td>{theme["observed_limit_count"]}</td>'
                f'<td>高度 {e(", ".join(theme["height_roles"]) or "无观测")}<br>承载 {e(", ".join(theme["capacity_roles"]))}</td>'
                f'<td>{e(", ".join(theme["against"]) or "通过实验筛选；未校准")}<br>催化：未核实</td></tr>')
        themes_html = '<p class="sub">按实验筛选条件展示前 30 组；高度角色可并列，成交额承载角色不是唯一龙头。归属不等于催化，全部分组及证据保存在不可变报告包。</p><div class="scroll"><table><thead><tr><th>题材代码 / 名称</th><th>报价覆盖</th><th>上涨占比</th><th>观测涨停数</th><th>不同角色</th><th>反对 / 缺口</th></tr></thead><tbody>'+''.join(theme_rows)+'</tbody></table></div>'
        candidates = context['conditional_candidates']
        condition_names = {'received_final_auction':'竞价最终结果实际收到','fresh_session_quote':'当前交易阶段的新鲜报价',
                           'theme_recheck':'题材依据重新核验','funds_evidence':'同口径资金证据','account_risk_recheck':'账户与风险重新核验'}
        candidate_rows = ''.join(f'<tr data-search="{e(c["instrument"])}"><td>{e(c["instrument"])}</td><td>watch · 条件观察</td><td>{e(", ".join(c["theme_ids"]))}</td><td>{e("、".join(condition_names.get(k,k) for k in c["required_next"]))}</td></tr>' for c in candidates)
        plans.insert(0, '<div class="empty">历史条件观察不是开仓批准。竞价、资金、账户条件尚未满足。</div>')
        plans.append('<div class="scroll"><table><thead><tr><th>证券</th><th>状态</th><th>归属证据</th><th>下一步必需条件</th></tr></thead><tbody>'+candidate_rows+'</tbody></table></div>' if candidates else '<div class="empty">没有通过当前实验条件的候选，保留空结果，不降低门槛凑数量。</div>')
        research_notice = f'<div class="notice"><strong>{"历史数据研究" if context["mode"]=="historical_research" else "系统时点研究"} · {e(context["trade_date"])} · 不是当前行情</strong><p>输入 {e(context["input_hash"][:20])}</p><details><summary>查看研究边界与数据缺口</summary><p>{e(", ".join(context["blockers"]))}</p></details></div>'
    sources = ''.join(f"<tr><td>{e(p['dataset'])}</td><td>{e(p['origin'])}</td><td>{e(p['unit'])}</td><td>{e(p['semantics'])}</td><td>{e(p['consumer'])}</td></tr>"
                      for p in data.get('products', [])) or '<tr><td colspan="5">尚未登记数据产品</td></tr>'
    signal_rows = ''.join(f"<tr><td>{e(s['instrument'])}</td><td>{e(s['state'])}</td><td>{e(s['strategy_version'])}</td><td>{e(', '.join(s['reasons']))}</td></tr>"
                          for s in data.get('signals', [])) or '<tr><td colspan="4">尚无状态事件</td></tr>'
    return f'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
    <meta name="viewport" content="width=device-width,initial-scale=1"><title>Stock Data · 决策研究台</title>
    <meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; img-src data:; connect-src 'none'; form-action 'none'; base-uri 'none'">
    <style>
    :root{{--ink:#152d32;--paper:#f5f3eb;--muted:#647271;--rule:#cbd0c6;--accent:#b86827;--pale:#f0e3d0}}
    *{{box-sizing:border-box}}body{{margin:0;background:var(--paper);color:var(--ink);font-family:'DengXian','Microsoft YaHei',sans-serif;font-size:15px;line-height:1.6}}
    .shell{{max-width:1440px;margin:auto;display:grid;grid-template-columns:215px 1fr;min-height:100vh}}
    aside{{background:var(--ink);color:#eff0df;padding:36px 25px;position:sticky;top:0;height:100vh}}
    .brand{{font:25px Georgia,serif;letter-spacing:1px}}.edition{{font-size:11px;letter-spacing:3px;color:#a9b6ad;margin-top:8px}}
    nav{{margin-top:64px}}nav a{{display:block;padding:13px 0;color:#d0d8cf;text-decoration:none;border-bottom:1px solid #385054}}nav b{{color:#c99159;margin-right:12px;font-family:Georgia,serif}}
    .footnote{{font-size:12px;margin-top:48px;color:#a9b6ad}}main{{padding:40px 48px 70px;min-width:0}}
    .eyebrow{{font-size:11px;letter-spacing:3px;text-transform:uppercase;color:var(--accent)}}h1{{font:40px 'STSong',Georgia,serif;margin:12px 0 6px}}h2{{font:25px 'STSong',Georgia,serif;margin:0}}h3{{margin:0;font-size:20px}}p{{margin:8px 0}}.sub{{color:var(--muted);font-size:13px}}
    .notice{{border-left:4px solid var(--accent);background:var(--pale);padding:15px 20px;margin:24px 0 32px}}
    section{{padding:28px 0;border-top:1px solid var(--rule);scroll-margin-top:20px}}.section-head{{display:flex;justify-content:space-between;align-items:center;gap:16px;margin-bottom:22px}}
    .tag{{font-size:11px;border:1px solid #baaa8f;padding:4px 9px;border-radius:2px;color:#815124;white-space:nowrap}}
    .metrics{{display:grid;grid-template-columns:repeat(3,1fr);gap:25px;margin-bottom:24px}}.metric{{border-right:1px solid var(--rule)}}.metric:last-child{{border:none}}.metric label,dt{{display:block;font-size:12px;color:var(--muted)}}.metric strong{{font:33px Georgia,serif;display:block;margin-top:5px}}
    table{{width:100%;border-collapse:collapse;text-align:left;font-size:13px}}th{{font-weight:normal;color:var(--muted);padding:10px 12px;background:#eaece3}}td{{padding:13px 12px;border-bottom:1px solid #dcded4;overflow-wrap:anywhere}}.scroll{{overflow-x:auto}}
    .empty{{padding:23px;background:#ebece4;color:var(--muted);font-size:14px}}.cols{{display:grid;grid-template-columns:1fr 1fr;gap:20px}}.plan{{border:1px solid var(--rule);padding:23px;background:#fbfaf5}}.plan-head{{display:flex;justify-content:space-between;gap:8px}}dl{{display:flex;justify-content:space-between;gap:15px;margin:24px 0}}dd{{margin:6px 0;font:26px Georgia,serif}}dd.small{{font:12px 'Cascadia Code',monospace;max-width:165px;overflow-wrap:anywhere}}
    details{{font-size:13px}}summary{{cursor:pointer;color:var(--accent)}}.mono{{font:11px 'Cascadia Code',Consolas,monospace;overflow-wrap:anywhere}}button{{margin-top:20px;width:100%;padding:10px;color:#73807a;background:#eceee7;border:1px solid var(--rule);cursor:not-allowed}}input{{padding:9px 12px;border:1px solid var(--rule);background:#fffef8;max-width:210px;color:var(--ink)}}input:focus{{outline:2px solid var(--accent)}}.ledger{{padding:20px;background:#e6e9de}}.ledger strong{{font:24px Georgia,serif}}footer{{border-top:1px solid var(--rule);padding-top:24px;margin-top:24px;color:var(--muted);font-size:12px}}
    @media(max-width:900px){{.shell{{grid-template-columns:1fr}}aside{{height:auto;position:static;padding:18px 24px}}nav{{margin-top:12px;display:flex;gap:15px;overflow:auto}}nav a{{font-size:12px;white-space:nowrap}}.edition,.footnote{{display:none}}main{{padding:28px 24px}}h1{{font-size:32px}}.cols{{grid-template-columns:1fr}}.metric strong{{font-size:26px}}}}
    aside{{min-width:0}}.metrics,.cols{{min-width:0}}.plan{{min-width:0}}.cols>.scroll{{grid-column:1/-1}}
    #themes table{{min-width:940px}}#themes td:first-child{{min-width:145px}}#themes td:nth-child(2),#themes td:nth-child(3),#themes td:nth-child(4){{white-space:nowrap}}#themes td:nth-child(5){{min-width:260px}}#themes td:last-child{{min-width:160px}}
    @media(max-width:900px){{.shell{{grid-template-columns:minmax(0,1fr)}}}}
    @media(max-width:480px){{.section-head{{align-items:flex-start;flex-wrap:wrap}}.metrics{{grid-template-columns:1fr;gap:15px}}.metric{{border-right:0;border-bottom:1px solid var(--rule);padding-bottom:12px}}.plan-head{{flex-wrap:wrap}}dl{{flex-wrap:wrap}}}}
    @media(prefers-reduced-motion:no-preference){{html{{scroll-behavior:smooth}}}}
    </style></head><body><div class="shell"><aside><div class="brand">STOCK / DATA</div><div class="edition">RESEARCH DESK · V2</div>
    <nav><a href="#account"><b>01</b>账户风险</a><a href="#market"><b>02</b>市场模式</a><a href="#themes"><b>03</b>题材机会</a><a href="#plans"><b>04</b>个股计划</a><a href="#evidence"><b>05</b>数据证据</a></nav>
    <p class="footnote">证据先于判断。<br>研究、模拟与实际操作<br>分别记录。</p></aside><main>
    <div class="eyebrow">Evidence-led decision review</div><h1>决策研究台</h1><p class="sub">生成时点 {e(data['generated_at'])} · 自包含只读快照</p>
    <div class="notice"><strong>真实执行关闭{' · 合成测试场景，非当前行情' if data.get('fixture_only') else ''}</strong><br>当前仅供研究和纸面复核。这里的历史证书不是此刻交易许可；账户、报价和风险必须在服务内重新校验。</div>{research_notice}
    <section id="account"><div class="section-head"><h2>01 / 账户风险</h2><span class="tag">{account_title}</span></div>
    <div class="metrics"><div class="metric"><label>账户权益 / 元</label><strong>{equity}</strong></div><div class="metric"><label>账户现金 / 元</label><strong>{cash}</strong></div><div class="metric"><label>纸面预占 / 元</label><strong>{data.get('reserved_fen',0)/100:,.2f}</strong></div></div>
    <div class="scroll"><table><thead><tr><th>证券身份</th><th>持仓数量</th><th>可卖数量</th><th>快照估值价</th></tr></thead><tbody>{position_rows}</tbody></table></div>{risk_html}</section>
    <section id="market"><div class="section-head"><h2>02 / 市场模式</h2><span class="tag">{'历史观测已接入' if context else '市场特征链待接入'}</span></div>{market_html}</section>
    <section id="themes"><div class="section-head"><h2>03 / 题材机会</h2><span class="tag">{'归属、反应、催化分开' if context else '角色证据待接入'}</span></div>{themes_html}</section>
    <section id="plans"><div class="section-head"><h2>04 / 个股计划</h2><input id="filter" aria-label="筛选证券代码" placeholder="筛选证券代码"></div><div class="cols">{''.join(plans)}</div>
    <details style="margin-top:24px"><summary>查看策略状态事件</summary><div class="scroll"><table><thead><tr><th>证券</th><th>状态</th><th>版本</th><th>原因</th></tr></thead><tbody>{signal_rows}</tbody></table></div></details></section>
    <section id="evidence"><div class="section-head"><h2>05 / 数据证据</h2><span class="tag">{e(data['fact_count'])} 条事实版本</span></div><div class="scroll"><table><thead><tr><th>产品版本</th><th>原始来源族</th><th>单位</th><th>语义</th><th>消费者</th></tr></thead><tbody>{sources}</tbody></table></div>
    <div class="cols" style="margin-top:24px"><div class="ledger"><p>纸面操作事件</p><strong>{e(data['operator_actions'])}</strong><p class="sub">不等同于券商成交</p></div><div class="ledger"><p>真实成交复盘</p><strong>待接入</strong><p class="sub">未导入真实执行资料，不计算真实收益</p></div></div></section>
    <footer>范围：paper_only · 券商路由 disabled · 所有交互数据已内联，无运行时外部数据依赖。</footer>
    </main></div><script>document.getElementById('filter').addEventListener('input',function(){{let q=this.value.trim().toLowerCase();document.querySelectorAll('[data-search]').forEach(x=>x.hidden=!x.dataset.search.toLowerCase().includes(q));}});</script></body></html>'''
