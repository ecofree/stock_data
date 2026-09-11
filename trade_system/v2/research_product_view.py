"""Self-contained delivery desk. Current forecasts and historical experiments are separate."""
from html import escape
import json

from .daily_session_view import STYLE


def render(data):
    payload=json.dumps(data,ensure_ascii=False,allow_nan=False).replace('<','\\u003c').replace('>','\\u003e').replace('&','\\u0026')
    return '''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; connect-src 'none'; form-action 'self'; base-uri 'none'">
<title>Stock Data · 研究与每日复盘</title>'''+STYLE+'''
<style>.tabs{display:flex;gap:24px;border-bottom:1px solid #bbb;padding:15px 0}.tabs a{color:#28574b}.metrics{display:grid;grid-template-columns:repeat(4,1fr);gap:16px;margin:25px 0}.metrics article{border-top:2px solid #263b36;padding:14px 0}.metrics b{display:block;font:30px Georgia,serif}th{white-space:nowrap}.scroll{overflow:auto}button:focus-visible,input:focus-visible,textarea:focus-visible{outline:3px solid #b56f3b;outline-offset:2px}.small{font-size:13px;color:#5a645e}.toolbar{display:flex;align-items:center;gap:20px;flex-wrap:wrap}.badge{border:1px solid #6d8979;padding:4px 10px}#current tbody tr{cursor:pointer}#current tbody tr:hover{background:#e6eadf}#message{white-space:pre-wrap;color:#96502d}.note{border-left:3px solid #406052;padding:12px;margin:12px 0}summary{padding:10px 0}@media(max-width:650px){.metrics{grid-template-columns:repeat(2,1fr)}.tabs{gap:12px;font-size:14px}}</style>
<main><header><span class="eyebrow">STOCK DATA / LOCAL RESEARCH DESK</span><h1>研究有结果，判断有记录。</h1><p>当前预测 · 来源与风险 · 人工判断 · 后续复盘</p><span class="badge">本地研究版 / 不连接交易</span></header>
<nav class="tabs"><a href="#today">今日观察</a><a href="#experiment">QLib对照</a><a href="#journal">判断记录</a><a href="#followup">后续复盘</a></nav>
<p id="message">__SERVER_MESSAGE__</p><section class="metrics" id="stats"></section>
<section id="today"><div class="toolbar"><h2>当前研究候选</h2><form method="post" action="/update"><input type="hidden" name="csrf" value="__CSRF__"><button>更新真实数据与预测</button></form></div>
<p id="freshness" class="small"></p><p class="warning">模型评分是下一交易日开盘至T+2收盘的价格变化估计，不是成交承诺或收益保证。当前历史对照未证明选股优势，以下是研究观察清单，不是买入推荐。缺失记录保留；当前模型预先指定为价格基线，不按测试赢家自动切换。</p>
<label>筛选证券 <input id="search" placeholder="输入代码，点击行查看依据" maxlength="6"></label>
<div class="scroll"><table id="current"><thead><tr><th>证券</th><th>模型估计 %</th><th>20日动量 %</th><th>数据状态</th></tr></thead><tbody></tbody></table></div><p id="empty"></p>
<details id="details"><summary>选中候选的依据与风险</summary><div id="evidence"></div></details></section>
<section id="experiment"><h2>历史研究：共同样本对照</h2><p id="scope" class="small"></p><div class="scroll"><table><thead><tr><th>模型组</th><th>样本 / 日期</th><th>MSE</th><th>日均Rank IC</th><th>Top5价格目标均值 %</th></tr></thead><tbody id="models"></tbody></table></div>
<p class="small">同一组样本、同一目标和预处理规则。资金与Alpha158的对照结果允许为负；Top5统计不是资金曲线。尾段不评分、不调参。历史可用性选择存在偏差，不能外推全市场。</p><div id="comparison"></div></section>
<section id="journal"><h2>记录你的判断</h2><p class="small">署名为你自行声明；保存时记录本机接收时间，不生成委托。原记录不可改写。</p>
<form method="post" action="/note" id="note-form"><input type="hidden" name="csrf" value="__CSRF__"><input type="hidden" name="prediction_id" id="prediction-id">
<div class="layout"><div><label>证券 <select name="instrument" id="instrument"></select></label><label>署名 <input name="operator" required maxlength="200"></label><label>意向 <select name="intent"><option value="observe">继续观察</option><option value="reject">不采用</option><option value="paper_hypothesis">研究假设（不下单）</option></select></label></div><div><label>判断依据 <textarea name="hypothesis" required maxlength="4000"></textarea></label><label>失效条件 <textarea name="invalidation" required maxlength="4000"></textarea></label></div></div><button id="save">保存判断到本地</button></form><div id="notes"></div></section>
<section id="followup"><h2>冻结预测与后续结果</h2><p>只评价先冻结、后实际收到的价格。尚未成熟和精确会话缺价分别保留；不补零、不把新训练结果回填旧预测。</p><div id="reviews"></div></section>
<footer>离线显示数据全部内联。研究样本与真实账户分开；未验证选股优势，真实执行关闭。__RUNNING_REFRESH__</footer></main>
<script type="application/json" id="data">'''+payload+'''</script><script>
'use strict';const D=JSON.parse(document.getElementById('data').textContent),$=x=>document.getElementById(x),P=D.prediction;
const el=(tag,t)=>{const e=document.createElement(tag);e.textContent=t;return e},fmt=x=>x==null?'—':Number(x).toFixed(4);
const labels={price_baseline:'价格基线',price_money:'价格 + 资金',price_alpha158:'价格 + Alpha158',constant:'常量对照'};
const rows=P?P.rows:[];function table(target,items){target.replaceChildren(...items.map(values=>{const tr=el('tr','');values.forEach(v=>tr.append(el('td',v)));return tr}))}
[['历史研究证券',D.dataset.summary.universe_count],['滚动对照折数',D.research.folds.length],['当前非空预测',P?P.predictions:0],['判断记录',D.notes.length]].forEach(([a,b])=>{const s=el('article',a);s.append(el('b',b));$('stats').append(s)});
$('scope').textContent=D.dataset.dataset_config.start+' → '+D.dataset.dataset_config.end+'；'+D.dataset.rows+'行特征；事后取得的探索数据，不是原时点回放。';
$('freshness').textContent=P?'数据日期 '+P.date+' / 实际接收 '+P.captured_at+' / 模型训练截至 '+P.model_train_end+(P.model_id!==D.model.model_id?' / 模型已重建，当前预测仍属于上一冻结模型，请更新':''):'尚未获取当前预测。请在收盘后更新；下方历史实验已完成。';
function show(r){$('details').open=true;$('evidence').replaceChildren(el('h3',r.instrument),el('p','模型估计 '+fmt(r.prediction)+'%；20日动量 '+fmt(r.baseline_momentum_20d)+'%。两者不是同一评分。'),el('p','风险：历史样本选择偏差、模型未验证优势、模型老化与市场变化、价格目标不等于可成交收益。'),el('p','来源缺口：'+(r.source_gaps.join('；')||'当日未记录价格来源缺口；完整热身资格另见数据状态')),el('p','来源：同花顺原生价格 / 中继价格交叉核对、每日复权与资金。'),el('pre',JSON.stringify(r.features,null,2)),el('p','模型贡献（各项与基准值相加等于模型评分；不是因果解释）：'),el('pre',JSON.stringify(r.contributions||{},null,2)),el('p','原始回执文件：'),el('pre',JSON.stringify(r.receipt_files,null,2)));$('instrument').value=r.instrument}
function draw(){const filtered=rows.filter(r=>r.instrument.includes($('search').value.trim()));table($('current').tBodies[0],filtered.map(r=>[r.instrument,fmt(r.prediction),fmt(r.baseline_momentum_20d),r.prediction==null?'特征不足':'研究预测']));[...$('current').tBodies[0].rows].forEach((tr,i)=>{tr.tabIndex=0;tr.onclick=()=>{show(filtered[i]);$('evidence').append(el('p','完整热身窗口缺口计数：'+JSON.stringify(filtered[i].window_gap_counts||{})))};tr.onkeydown=e=>{if(e.key==='Enter')tr.click()}});$('empty').textContent=filtered.length?'':'当前无可展示候选'}draw();$('search').oninput=draw;
table($('models'),Object.entries(D.research.aggregate).map(([k,v])=>[labels[k]||k,v.samples+' / '+v.days,fmt(v.mse),fmt(v.mean_daily_rank_ic),fmt(v.top_k_mean_label_pct)]));
Object.entries(D.research.paired_comparisons).forEach(([k,v])=>$('comparison').append(el('p',(labels[k]||k)+' 相对价格基线日MSE差 '+fmt(v.mean_daily_mse_difference)+'；描述性区间 '+(v.moving_block_bootstrap_95pct||[]).map(fmt).join(' ～ ')+'（负值较好）')));
rows.forEach(r=>{const o=el('option',r.instrument);o.value=r.instrument;$('instrument').append(o)});$('prediction-id').value=P?P.prediction_id:'';$('save').disabled=!P;
D.notes.forEach(n=>{const e=el('div','');e.className='note';e.append(el('strong',n.instrument+' · '+n.operator+' · '+n.received_at),el('p',n.hypothesis),el('p','失效：'+n.invalidation));$('notes').append(e)});
const states={pending_exact_future_sessions:'等待后续交易日',mature:'价格目标已成熟',partial_missing_prices:'部分精确价格缺失',outside_current_receipt_window:'超出本次更新窗口'};
(D.reviews||[]).forEach(r=>{const e=el('details','');e.append(el('summary',r.prediction_date+' / '+(states[r.status]||r.status)+' / 配对 '+r.paired));const box=el('pre',JSON.stringify(r.rows,null,2));e.append(box);$('reviews').append(e)});if(!(D.reviews||[]).length)$('reviews').textContent='没有成熟的前瞻结果；等待冻结预测之后的实际交易日。';
</script></html>'''


def server_page(html, token, message='', running=False):
    return html.replace('__CSRF__',escape(token,quote=True)).replace('__SERVER_MESSAGE__',escape(message)).replace(
        '__RUNNING_REFRESH__','<script>setTimeout(()=>location.reload(),10000)</script>' if running else '')
