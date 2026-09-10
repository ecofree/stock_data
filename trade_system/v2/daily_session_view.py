"""Self-contained editorial daily observation and honest follow-up views."""
from html import escape
import json

from .domain import identity, utc

STYLE = '''<style>
:root{--ink:#183e35;--paper:#f5f2e9;--line:#ccd5c9;--muted:#57685f;--accent:#a54027}
*{box-sizing:border-box}body{margin:0;background:var(--paper);color:var(--ink);font:16px/1.65 "Microsoft YaHei",sans-serif}main{max-width:1250px;margin:auto;padding:40px 5vw}header{border-top:6px solid var(--ink);padding:25px 0}h1{font:600 clamp(30px,4vw,50px)/1.25 Georgia,"Songti SC",serif;margin:10px 0}h2{font:600 25px Georgia,"Songti SC",serif}.eyebrow{letter-spacing:.17em;font-size:12px}.muted{color:var(--muted);font-size:14px}.warning{padding:15px 20px;border-left:4px solid var(--accent);background:#eee3d8}.stats{display:flex;gap:28px;flex-wrap:wrap;margin:22px 0}.stat b{font:36px Georgia,serif;display:block}.layout{display:grid;grid-template-columns:minmax(0,1.8fr) minmax(250px,1fr);gap:30px}.card,aside{border-top:1px solid var(--line);padding:20px 0}.card h3{margin:0;font-size:22px}.tag{font-size:12px;color:var(--accent)}label{display:block;margin:12px 0}input,textarea,select,button{font:inherit;max-width:100%;padding:9px;border:1px solid var(--line);background:#fffdf7;color:var(--ink)}input,textarea,select{width:100%}textarea{min-height:90px}button{background:var(--ink);color:white;cursor:pointer}.mono{font:12px/1.7 Consolas,monospace;overflow-wrap:anywhere}.row{display:flex;justify-content:space-between;gap:16px}.tablewrap{overflow:auto}table{border-collapse:collapse;width:100%;font-size:14px}td,th{padding:12px;text-align:left;border-bottom:1px solid var(--line)}summary{cursor:pointer}li,p,td{overflow-wrap:anywhere}footer{margin-top:40px;border-top:1px solid var(--line);padding-top:14px;font-size:12px}@media(max-width:750px){main{padding:20px}.layout{grid-template-columns:1fr}.row{display:block}.stats{gap:18px}td,th{padding:8px}}
</style>'''


def shell(title, content, script=''):
    return ('<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; style-src \'unsafe-inline\'; script-src \'unsafe-inline\'; connect-src \'none\'; img-src data:; base-uri \'none\'; form-action \'none\'">'
        '<title>'+escape(title)+'</title>'+STYLE+'<main>'+content+
        '<footer>LOCAL / SELF-CONTAINED · 研究判断不生成委托 · 数据缺失不填零 · 真实执行关闭</footer></main>'+script+'</html>')


RISKS = {'sealed_limit_price_not_executable_quote':'封板价不是可成交报价',
    'next_session_gap_and_nonfill_risk':'次日跳空、封板及无法成交风险',
    'daily_pool_cannot_establish_buy_or_sell_permission':'日线池不能证明买卖资格',
    'special_status_unknown':'特殊证券状态未知', 'provider_reports_ST':'来源标记 ST',
    'provider_reports_new_listing':'来源标记新股', 'multi_board_extension_risk':'多连板后的波动与延续风险'}


def render(report):
    from .daily_session import validate_report, CST
    validate_report(report)
    cards = []
    for c in report['candidates']:
        risks = '；'.join(RISKS.get(r,r) for r in c['risks'])
        cards.append('<article class="card"><div class="row"><h3>'+escape(c['name'])+
            ' <small class="mono">'+escape(c['instrument'])+'</small></h3><span class="tag">观察，不是买入建议</span></div>'+
            '<p>来源观察：'+str(c['observed_height'])+' 连板 · 最新来源价 '+escape(c['close_cny'])+
            ' 元 · 来源涨跌幅 '+escape(c['provider_change_pct'])+'%</p><p>来源归因：'+escape(c['provider_reason'] or '未提供')+
            '</p><p class="muted">归因未经独立公告核验，不代表已证实催化。</p><p class="warning">'+escape(risks)+
            '</p><details><summary>证据身份</summary><p class="mono">候选 '+c['candidate_id']+'<br>原始行 '+c['source_row_sha256']+'</p></details></article>')
    count = len(report['candidates'])
    content = '<header><div class="eyebrow">DAILY OBSERVATION / '+escape(report['trade_date'])+'</div><h1>今日机会，明日求证。</h1>'
    content += '<p>官方涨停池 → 候选依据与风险 → 人工判断 → 下一交易日观察复盘</p><p class="muted">冻结采集时点：'+utc(report['generated_at']).astimezone(CST).strftime('%Y-%m-%d %H:%M:%S')+' 北京时间。本页不会自动刷新；跨日打开仍是原日期快照。</p><nav><a href="#observations">查看候选</a> · <a href="#judgement">跳转人工判断</a> · <a href="#followup">次日复盘边界</a></nav></header>'
    banner = '合成测试数据，不是真实市场。' if report['origin']=='synthetic_fixture' else '真实原生响应；范围仅为涨停观察池，不代表全市场优选。'
    status = {'current_native_observation':'采集时点为当日原生观察','source_check_required':'来源待进一步核查'}.get(report['status'],report['status'])
    content += '<div class="warning">'+banner+' 状态：'+escape(status)+'。无账户、无交易授权。</div>'
    content += '<section class="stats">'+''.join('<div class="stat"><span>'+k+'</span><b>'+str(v)+'</b></div>' for k,v in [('原始池记录',report['pool_total']),('固定观察候选',count),('待补证项',len(report['gaps']))])+'</section>'
    content += '<div class="layout"><section id="observations"><h2>01 / 解释与反对证据</h2><p class="muted">按观察连板高度、证券身份排序；不是收益预测或 QLib 排名。超过展示上限的证券仍保留在来源包。</p>'+''.join(cards)+'</section><aside id="judgement"><h2>02 / 记录你的判断</h2><p>导出后必须通过本地 judge 命令归档。只下载文件不算已提交。</p>'
    content += '<form id="journal"><label>候选证券<select id="candidate">'+''.join('<option value="'+c['candidate_id']+'">'+escape(c['instrument']+' '+c['name'])+'</option>' for c in report['candidates'])+'</select></label>'
    content += '<label>操作者声明<input id="operator" required maxlength="200"></label><label>研究选择<select id="intent"><option value="observe">继续观察</option><option value="reject">不采用</option><option value="paper_hypothesis">纸面假设（不下单）</option></select></label><label>判断依据<textarea id="hypothesis" required maxlength="4000"></textarea></label><label>失效条件<textarea id="invalidation" required maxlength="4000"></textarea></label><button type="submit"'+(' disabled' if not count else '')+'>导出人工判断 JSON</button></form><p id="feedback" role="status"></p>'
    content += '<h2 id="followup">03 / 次日复盘边界</h2><p>必须等待来源日历确认下一交易日，并采集该日数据。保留每个原候选，包括未采用、没有判断和缺行情的证券。迟交判断不会算作事前判断。</p><details><summary>查看缺口与来源时点</summary><ul>'+''.join('<li>'+escape(g)+'</li>' for g in report['gaps'])+'</ul><p class="mono">'+escape(report['generated_at'])+'<br>'+report['report_id']+'</p><p>原生响应时间不是每条行情的首次事件时间；人工姓名不是登录认证。</p></details></aside></div>'
    payload = json.dumps(report,ensure_ascii=False,allow_nan=False).replace('<','\\u003c').replace('>','\\u003e').replace('&','\\u0026')
    script = '<script type="application/json" id="data">'+payload+'</script>'+'''<script>
'use strict';const D=JSON.parse(document.getElementById('data').textContent),$=id=>document.getElementById(id);
$('journal').addEventListener('submit',e=>{e.preventDefault();try{const n={schema:1,scope:'daily_judgement_no_execution',report_id:D.report_id,candidate_id:$('candidate').value,note_id:crypto.randomUUID(),created_at:new Date().toISOString(),operator:$('operator').value.trim(),intent:$('intent').value,hypothesis:$('hypothesis').value.trim(),invalidation:$('invalidation').value.trim()};if(!n.operator||!n.hypothesis||!n.invalidation||!D.candidates.some(c=>c.candidate_id===n.candidate_id))throw Error('请完整填写候选、操作者、依据和失效条件');const u=URL.createObjectURL(new Blob([JSON.stringify(n,null,2)],{type:'application/json'}));const a=document.createElement('a');a.href=u;a.download='daily-judgement-'+n.note_id+'.json';a.click();setTimeout(()=>URL.revokeObjectURL(u),1000);$('feedback').textContent='已导出，尚未归档；未产生任何委托。'}catch(err){$('feedback').textContent=err.message}});
</script>'''
    return shell('每日候选观察与人工判断',content,script)


def render_followup(review):
    if 'review_id' in review and review['review_id'] != identity({k:v for k,v in review.items() if k!='review_id'}):
        raise ValueError('follow-up fingerprint changed')
    pending = review['scope']=='pending_next_session_not_completed_review'
    content = '<header><div class="eyebrow">FOLLOW-UP / 原候选完整保留</div><h1>'+('等待下一交易日。' if pending else '判断之后，核对证据。')+'</h1></header>'
    content += '<p class="warning">'+('尚未取得下一交易日数据，不能算完成次日复盘。' if pending else '这里只复盘行情观察和人工判断；没有券商成交证据，不展示真实收益。')+'</p>'
    content += '<p>固定候选数：'+str(review['cohort_size'])+' · 日期：'+escape(str(review.get('trade_date') or '待来源日历确认'))+'</p><div class="tablewrap"><table><thead><tr><th>证券</th><th>判断与时点</th><th>次日行情</th><th>来源边界</th></tr></thead><tbody>'
    for row in review['rows']:
        decisions = row.get('judgements',[])
        label = '；'.join(n['note']['intent']+' / '+n['timing'] for n in decisions) or '未取得人工判断'
        bar = row.get('observation')
        observed = '缺失 / 不填零' if not bar else '开 '+bar['open']+' / 收 '+bar['close']+' / 来源涨跌 '+bar['provider_change_pct']+'%'
        source = '等待数据' if not bar else bar['source_status']
        content += '<tr>'+''.join('<td>'+escape(v)+'</td>' for v in [row['instrument']+' '+row['name'],label,observed,source])+'</tr>'
    content += '</tbody></table></div><p>次日未出现在涨停池不等于停牌、失败或负收益；日内价格变化不等于符合 T+1 的可实现收益。</p>'
    return shell('下一交易日观察复盘',content)
