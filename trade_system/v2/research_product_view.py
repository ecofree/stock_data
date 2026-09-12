"""Self-contained delivery desk. Current forecasts and historical experiments are separate."""
from html import escape
import json

STYLE = """<style>
:root{color-scheme:light;--ink:#172d39;--muted:#536876;--line:#d6e0e5;--accent:#126579}
*{box-sizing:border-box}body{margin:0;background:#f3f6f7;color:var(--ink);font:15px/1.65 "Microsoft YaHei","PingFang SC",sans-serif}
main{max-width:1480px;margin:auto;padding:24px}header{border-bottom:3px solid var(--accent);padding:0 0 18px}
h1{font-size:28px;margin:4px 0}h2{font-size:20px;margin:8px 0 16px}h3{font-size:16px}p{margin:10px 0}
.eyebrow{font-size:12px;letter-spacing:2px;color:var(--muted)}section{padding:20px;background:#fff;border:1px solid var(--line);margin:18px 0;min-width:0}
table{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums}td,th{padding:9px;text-align:left;border-bottom:1px solid var(--line);font-size:14px}th{background:#f2f6f8;position:sticky;top:0}
button{font:inherit;background:var(--accent);color:#fff;border:0;padding:8px 13px;border-radius:4px;cursor:pointer}button:disabled{opacity:.5;cursor:not-allowed}
label{display:block;margin:8px 0}input,select,textarea{font:inherit;max-width:100%;padding:8px;border:1px solid #aabcc6;border-radius:3px}textarea{width:100%;min-height:90px}
a{color:var(--accent)}pre{white-space:pre-wrap;overflow-wrap:anywhere}.warning{border-left:3px solid #b37823;padding:10px 14px;background:#fff8eb;font-size:13px}
.workspace{display:grid;grid-template-columns:minmax(0,1.55fr) minmax(330px,1fr);gap:18px}.workspace section{margin:0}
.workspace #journal{align-self:start;position:sticky;top:12px}.workspace #current{min-width:560px}
#current+tbody{font-size:14px}#today>.scroll{max-height:440px;overflow:auto}#notes{max-height:300px;overflow:auto}
#compare-panel{display:grid;grid-template-columns:1fr 1fr;gap:12px}#compare-panel article{padding:12px;border:1px solid var(--line);overflow-wrap:anywhere}
#compare-panel strong{display:block}tr[aria-selected=true]{background:#e5f0f4}#note-context{overflow-wrap:anywhere}
.statusbar{display:flex;gap:16px;flex-wrap:wrap;padding:10px;background:#e7eff3;font-size:13px}footer{color:var(--muted);font-size:13px;padding:20px 0}
@media(max-width:900px){main{padding:12px}.workspace{grid-template-columns:minmax(0,1fr)}.workspace #journal{position:static}.tabs{flex-wrap:wrap}section{padding:14px}h1{font-size:24px}}
@media(max-width:480px){#compare-panel{grid-template-columns:1fr}.toolbar{gap:8px}td,th{font-size:13px}}
#market{padding:12px 18px}#market .metrics{margin:8px 0;gap:12px}#market .metrics article{padding:8px 0}#market .metrics b{font-size:24px}#market h2{margin:0;font-size:18px}.tabs{padding:10px 0!important}summary{cursor:pointer}#compare-panel:empty{display:none}#today h2{margin:0}
</style>"""


def render(data):
    payload=json.dumps(data,ensure_ascii=False,allow_nan=False).replace('<','\\u003c').replace('>','\\u003e').replace('&','\\u0026')
    return '''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; connect-src 'none'; form-action 'self'; base-uri 'none'">
<title>Stock Data · 研究与每日复盘</title>'''+STYLE+'''
<style>.tabs{display:flex;gap:24px;border-bottom:1px solid #bbb;padding:15px 0}.tabs a{color:#28574b}.metrics{display:grid;grid-template-columns:repeat(4,1fr);gap:16px;margin:25px 0}.metrics article{border-top:2px solid #263b36;padding:14px 0}.metrics b{display:block;font:30px Georgia,serif}th{white-space:nowrap}.scroll{overflow:auto}button:focus-visible,input:focus-visible,textarea:focus-visible{outline:3px solid #b56f3b;outline-offset:2px}.small{font-size:13px;color:#5a645e}.toolbar{display:flex;align-items:center;gap:20px;flex-wrap:wrap}.badge{border:1px solid #6d8979;padding:4px 10px}#current tbody tr{cursor:pointer}#current tbody tr:hover{background:#e6eadf}#message{white-space:pre-wrap;color:#96502d}.note{border-left:3px solid #406052;padding:12px;margin:12px 0}summary{padding:10px 0}@media(max-width:650px){.metrics{grid-template-columns:repeat(2,1fr)}.tabs{gap:12px;font-size:14px}}</style>
<main><header><span class="eyebrow">STOCK DATA / LOCAL RESEARCH DESK</span><h1>每日观察与复盘</h1><div class="statusbar"><span>研究观察 · 不连接交易</span><span>账户：未核对</span><span>模型选股优势：未验证</span></div></header>
<nav class="tabs"><a href="#market">盘后复盘</a><a href="#observation">盘中观察</a><a href="#today">候选比较</a><a href="#experiment">QLib对照</a><a href="#journal">判断记录</a><a href="#followup">后续复盘</a></nav>
<p id="message" role="status" aria-live="polite"><!--SERVER_MESSAGE--></p>
<section id="observation"><div class="toolbar"><h2>盘中观察</h2><form method="post" action="/observe"><input type="hidden" name="csrf" value="__CSRF__"><button>核对本地最新报价</button></form><form method="post" action="/capture-quotes"><input type="hidden" name="csrf" value="__CSRF__"><button>联网获取备用报价</button></form></div><p class="small">核对本地记录不联网；备用获取仅对缺失证券请求腾讯（最多200只、4次请求、零重试，同范围60秒内复用）。尚无已验证的原生/中继实时接口；合格高优先级留存来源优先。采集保留原始响应，不训练、不写现网主库、不推导仓位。过期报价不以昨日收盘价回填。</p><p id="observation-state" role="status"></p><p id="observation-capture" class="small"></p><label><input type="checkbox" id="observation-all"> 同时查看未加入人工关注的研究证券</label><div class="scroll"><table><thead><tr><th>证券 / 原判断</th><th>下一观察条件</th><th>人工核对</th><th>合格当前价</th><th>来源与状态</th></tr></thead><tbody id="observation-rows"></tbody></table></div></section>
<section id="market"><h2>盘后复盘</h2><p id="market-state" class="small"></p><div id="market-metrics" class="metrics"></div>
<details id="theme-workspace"><summary>题材与同日涨停证券</summary><label>搜索题材 <input id="theme-search" maxlength="40"></label><p id="theme-count" class="small"></p><div class="scroll" style="max-height:320px"><table><thead><tr><th>题材</th><th>涨停 / 成员</th><th>最高连板</th></tr></thead><tbody id="theme-rows"></tbody></table></div><div id="theme-detail"></div></details></section><section id="today"><div class="toolbar"><h2>候选比较</h2><form method="post" action="/update"><input type="hidden" name="csrf" value="__CSRF__"><button>更新真实数据与预测</button></form></div>
<p id="session-date" class="small"></p><details class="small"><summary>数据与模型时点</summary><p id="freshness"></p></details><details class="warning"><summary>研究边界：价格目标不是收益或交易资格</summary><p>模型评分是下一交易日开盘至T+2收盘的价格变化估计，不是成交承诺或收益保证。当前历史对照未证明选股优势，以下是研究观察清单，不是买入推荐。缺失记录保留；当前模型预先指定为价格基线，不按测试赢家自动切换。</p></details>
<div class="toolbar"><label>筛选证券 <input id="search" placeholder="输入代码或名称，点击行查看依据" maxlength="30"></label><label>排序依据 <select id="sort"><option value="code">证券代码（默认，不代表推荐顺序）</option><option value="model">QLib价格模型</option><option value="rule">20日动量规则（不训练）</option></select></label></div>
<div class="scroll"><table id="current"><thead><tr><th>证券</th><th>模型估计 %</th><th>20日动量 %</th><th>数据状态</th></tr></thead><tbody></tbody></table></div><p id="empty"></p>
<p class="small">选两只证券加入对比；模型正向贡献只是数值解释，不是利好事实。</p><div id="compare-panel" aria-live="polite"></div><details id="details"><summary>选中候选的依据与风险</summary><div id="evidence"></div></details></section>
<section id="experiment"><section class="metrics" id="stats"></section><h2>历史研究：共同样本对照</h2><p id="scope" class="small"></p><div class="scroll"><table><thead><tr><th>模型组</th><th>样本 / 日期</th><th>MSE</th><th>日均Rank IC</th><th>Top5价格目标均值 %</th></tr></thead><tbody id="models"></tbody></table></div>
<p class="small">同一组样本、同一目标和预处理规则。资金与Alpha158的对照结果允许为负；Top5统计不是资金曲线。尾段不评分、不调参。历史可用性选择存在偏差，不能外推全市场。</p><div id="comparison"></div></section>
<section id="price-study" hidden><h2>独立21日价格研究</h2><p id="price-study-status"></p><p class="small">与上方61日Alpha158共同研究分开；新旧价格模型、动量和等权使用相同证券日。不足6只的日期保留，Top5不适用；汇总只在共同合格日期上计算，不是资金曲线。</p><div class="scroll"><table><thead><tr><th>对照</th><th>全期MSE</th><th>合格日Top5 / 等权目标均值 %</th></tr></thead><tbody id="price-study-models"></tbody></table></div><details><summary>全部测试日期与不足记录</summary><div class="scroll"><table><thead><tr><th>日期</th><th>证券数</th><th>Top5资格</th><th>新价格模型 %</th><th>旧模型 %</th><th>动量 %</th><th>等权 %</th></tr></thead><tbody id="price-study-days"></tbody></table></div></details></section>
<section id="journal"><h2>记录你的判断</h2><p class="small">署名为你自行声明；保存时记录本机接收时间，不生成委托。原记录不可改写。</p>
<p id="note-context" class="small"></p><form method="post" action="/note" id="note-form"><input type="hidden" name="csrf" value="__CSRF__"><input type="hidden" name="request_id" id="request-id"><input type="hidden" name="prediction_id" id="prediction-id"><input type="hidden" name="supersedes" id="supersedes">
<div class="layout"><div><label>证券 <select name="instrument" id="instrument"></select></label><label>署名 <input name="operator" required maxlength="200"></label><label>意向 <select name="intent"><option value="observe">继续观察</option><option value="reject">不采用</option><option value="paper_hypothesis">研究假设（不下单）</option></select></label></div><div><label>判断依据 <textarea name="hypothesis" required maxlength="4000"></textarea></label><label>失效条件 <textarea name="invalidation" required maxlength="4000"></textarea></label></div></div><button id="save">保存判断到本地</button> <button type="button" id="new-note">退出修订，记录当前判断</button></form><div id="notes"></div></section>
<section id="followup"><h2>冻结预测与后续结果</h2><p>只评价先冻结、后实际收到的价格。尚未成熟和精确会话缺价分别保留；不补零、不把新训练结果回填旧预测。</p><div id="reviews"></div></section>
<footer>离线显示数据全部内联。研究样本与真实账户分开；未验证选股优势，真实执行关闭。<!--RUNNING_REFRESH--></footer></main>
<script type="application/json" id="data">'''+payload+'''</script><script>
'use strict';const D=JSON.parse(document.getElementById('data').textContent),$=x=>document.getElementById(x),P=D.prediction;
const el=(tag,t)=>{const e=document.createElement(tag);e.textContent=t;return e},fmt=x=>x==null?'—':Number(x).toFixed(4);
const labels={price_baseline:'QLib · 价格',price_money:'QLib · 价格 + 资金',price_alpha158:'QLib · 价格 + Alpha158',constant:'常量预测（误差对照）'};
const featureNames={ret_1d:'1日涨跌幅 %',ret_5d:'5日涨跌幅 %',ret_20d:'20日涨跌幅 %',volatility_20d:'20日波动率',intraday_range:'当日振幅 / 收盘价',volume_ratio_20d:'成交量 / 20日均量',money_ratio:'当日净流入 / 成交额',money_ratio_5d:'5日净流入 / 成交额',base_value:'模型起始值'};
const gapNames={source_price_conflict:'双来源价格或量额冲突',missing_dual_source_price:'缺少双来源价格',daily_factor_missing:'缺复权因子',identity_snapshot_missing_or_outside_listing:'身份或上市期间不足',nonpositive_volume:'无有效成交量'};
const gaps=g=>Object.entries(g||{}).map(([k,v])=>(gapNames[k]||k)+' '+v+'日').join('；')||'无';
$('session-date').textContent=P?'行情日 '+P.date+' · '+P.predictions+' / '+P.rows.length+' 只可计算 · 保留缺失，不代表全市场':'尚无冻结预测';let draftDirty=false;const compared=new Set();const rows=P?P.rows:[];function table(target,items){target.replaceChildren(...items.map(values=>{const tr=el('tr','');values.forEach(v=>tr.append(el('td',v)));return tr}))}
[['历史研究证券',D.dataset.summary.universe_count],['滚动对照折数',D.research.folds.length],['当前非空预测',P?P.predictions:0],['判断记录',D.notes.length]].forEach(([a,b])=>{const s=el('article',a);s.append(el('b',b));$('stats').append(s)});
$('scope').textContent=D.dataset.dataset_config.start+' → '+D.dataset.dataset_config.end+'；'+D.dataset.rows+'行特征；事后取得的探索数据，不是原时点回放。';
$('freshness').textContent=P?'行情日期 '+P.date+' / 数据接收 '+(P.data_received_at||P.captured_at)+' / 本次冻结 '+P.captured_at+' / 模型训练截至 '+P.model_train_end+(P.receipt_replay?' / 原始回执重放':'')+(P.prospective_eligible===false?' / 已错过目标开盘，不计前瞻成绩':P.forecast_status==='pending_future_calendar'?' / 等待未来交易日日历核对，未计前瞻成绩':'')+(P.model_id!==D.model.model_id?' / 当前预测属于上一冻结模型，请更新':''):'尚未获取当前预测。下方历史实验已完成。';
function show(r){$('details').open=true;const evidence=$('evidence');evidence.replaceChildren(el('h3',r.instrument),el('p',r.prediction==null?'未进入模型观察清单：当前依赖窗口不足，不补值。':'进入观察清单的原因：21日价格依赖完整，冻结QLib模型可计算非空评分；不代表已验证买入机会。'),el('p','QLib价格目标估计 '+fmt(r.prediction)+'%；独立动量规则 '+fmt(r.baseline_momentum_20d)+'%。前者预测短期目标，后者只按过去20日涨跌排序。'),el('p','近21日缺口：'+gaps(r.active_window_gap_counts)+'。完整采集窗口缺口：'+gaps(r.window_gap_counts)),el('p','价格 / 资金 / Alpha158窗口：'+['price_eligible','money_eligible','alpha158_window_eligible'].map(k=>r.eligibility?.[k]?'足够':'不足').join(' / ')+'。Alpha158窗口足够不等于已产生当前Alpha158预测。'));
const t=el('table','');const body=el('tbody','');t.append(body);table(body,Object.entries(r.features).map(([k,v])=>[featureNames[k]||k,fmt(v)]));evidence.append(t,el('h3','评分的主要贡献'));
Object.entries(r.contributions||{}).sort((a,b)=>Math.abs(b[1])-Math.abs(a[1])).forEach(([k,v])=>evidence.append(el('p',(featureNames[k]||k)+'：'+(v>=0?'+':'')+fmt(v)+'个百分点')));
evidence.append(el('p','贡献项之和等于评分，不是因果解释。风险：模型老化、样本选择偏差、跳空及无法成交，价格目标不是组合收益。请自行填写失效条件，系统不会替你作出判断。'));
const d=el('details','');d.append(el('summary','来源回执'),el('p','同花顺原生价格；中继日线交叉核对、复权与资金。'),el('pre',JSON.stringify(r.receipt_files,null,2)));evidence.append(d);if(!$('supersedes').value&&!draftDirty)$('instrument').value=r.instrument}
function draw(){const key=$('sort').value==='code'?'instrument':$('sort').value==='rule'?'baseline_momentum_20d':'prediction';const filtered=rows.filter(r=>(r.instrument+' '+(r.name||'')).includes($('search').value.trim())).slice().sort((a,b)=>key==='instrument'?a.instrument.localeCompare(b.instrument):(a[key]==null)-(b[key]==null)||(b[key]||0)-(a[key]||0)||a.instrument.localeCompare(b.instrument));table($('current').tBodies[0],filtered.map(r=>[r.instrument+' '+(r.name||''),fmt(r.prediction),fmt(r.baseline_momentum_20d),r.prediction==null?'特征不足':'研究预测']));[...$('current').tBodies[0].rows].forEach((tr,i)=>{tr.tabIndex=0;tr.setAttribute('aria-selected',compared.has(filtered[i].instrument));const b=el('button',compared.has(filtered[i].instrument)?'移出对比':'加入对比');b.type='button';b.setAttribute('aria-label',b.textContent+' '+filtered[i].instrument);b.onclick=e=>{e.stopPropagation();const code=filtered[i].instrument;if(compared.has(code))compared.delete(code);else if(compared.size<2)compared.add(code);else{$('message').textContent='最多同时比较两只，请先移出一个。';return}drawCompare();draw()};tr.cells[0].append(el('br',''),b);tr.onclick=()=>show(filtered[i]);tr.onkeydown=e=>{if(e.key==='Enter')tr.click()}});$('empty').textContent=filtered.length?'':'当前无可展示候选'}draw();$('search').oninput=draw;$('sort').onchange=draw;
table($('models'),Object.entries(D.research.aggregate).map(([k,v])=>[labels[k]||k,v.samples+' / '+v.days,fmt(v.mse),fmt(v.mean_daily_rank_ic),fmt(v.top_k_mean_label_pct)]));
if(D.rule_baselines){const b=D.rule_baselines.summary;[['动量规则（无训练）',b.mean_daily_rank_ic,b.momentum_top5_target_pct],['共同证券等权观察',null,b.equal_weight_target_pct]].forEach(([name,ic,value])=>{const tr=el('tr','');[name,b.samples+' / '+b.days,'不适用',fmt(ic),fmt(value)].forEach(v=>tr.append(el('td',v)));$('models').append(tr)});$('comparison').append(el('p','规则对照复用每折完全相同的样本身份；动量分数不冒充收益预测，因此不计算其MSE。等权行使用全部共同证券，不是Top5。规则为事后明确的固定规则，不宣称未见样本。'))}
Object.entries(D.research.paired_comparisons).forEach(([k,v])=>$('comparison').append(el('p',(labels[k]||k)+' 相对价格基线日MSE差 '+fmt(v.mean_daily_mse_difference)+'；描述性区间 '+(v.moving_block_bootstrap_95pct||[]).map(fmt).join(' ～ ')+'（负值较好）')));
if(D.previous_model_comparison){const c=D.previous_model_comparison,v=c.old_frozen_model,tr=el('tr','');['旧冻结价格模型（训练截至 '+c.previous_train_end+'）',v.samples+' / '+v.days,fmt(v.mse),fmt(v.mean_daily_rank_ic),fmt(v.top_k_mean_label_pct)].forEach(x=>tr.append(el('td',x)));$('models').append(tr);$('comparison').append(el('p','旧模型复用新滚动实验的完全相同测试身份；这是近期历史对照，不是未来实盘成绩。近期候选模型另按固定60日训练、15日验证拟合，不使用最后20日留出标签，也不把训练内结果当成样本外表现。'));}
const candidate=D.candidate_model||D.model;
if(candidate.refit_boundaries)$('comparison').append(el('p','近期候选模型训练截至 '+candidate.train_end+'，验证截至 '+candidate.validation_end+'；独立留出从 '+candidate.refit_boundaries.holdout_start+' 开始，未参与最终拟合。'));
if(D.candidate_readiness&&!D.candidate_readiness.can_replace_current_model){const q=D.candidate_readiness;$('comparison').append(el('p','近期模型未替换当前模型：测试日每天仅 '+q.minimum_test_cross_section+'～'+q.maximum_test_cross_section+' 只，需要至少 '+q.required_minimum_cross_section+' 只才能检验Top5选择；新模型当前非空 '+q.current_nonempty+' 只，原版 '+q.previous_nonempty+' 只。上方继续展示旧模型预测，重训产物保留。'));if(q.minimum_test_cross_section<q.required_minimum_cross_section)[...$('models').rows].forEach(tr=>{if(!tr.cells[0].textContent.includes('等权'))tr.cells[4].textContent='不适用：共同候选不足'});}
if(P?.latest_session_certified===false)$('freshness').append(' / 按封存训练回执重算，未重新认证最新交易日；日期保持为 '+P.date);
if(D.price_study){const s=D.price_study,q=s.selection,m=q.selection_only_means;$('price-study').hidden=false;$('price-study-status').textContent='独立样本 '+q.all_samples+' 个证券日 / '+q.all_days+' 天；Top5合格 '+q.selection_days+' 天，不足 '+q.insufficient_days+' 天。指定证券 '+(s.recovery.security||'（历史补充记录）')+' 补充 '+s.recovery.supplied_factors+' 行复权，沿用原接收时间 '+s.recovery.received_at+'，新增请求 '+s.recovery.additional_provider_requests+' 次。补充数据下同一候选模型有 '+s.candidate_inference.nonempty+' 只非空预测；原批次和当前模型未替换。留出从 '+s.holdout[0]+' 开始，未评分。';table($('price-study-models'),[['独立价格QLib',fmt(s.aggregate.price_baseline.mse),fmt(m.model_top5)],['旧冻结价格模型',fmt(s.previous_model.mse),fmt(m.old_top5)],['常量预测（误差对照）',fmt(s.aggregate.constant.mse),'不适用'],['动量规则','不适用',fmt(m.momentum_top5)],['共同证券等权','不适用',fmt(m.equal_weight)]]);table($('price-study-days'),q.days.map(r=>[r.date,r.samples,r.selection_eligible?'可比较':'不足6只',fmt(r.model_top5),fmt(r.old_top5),fmt(r.momentum_top5),fmt(r.equal_weight)]));}
rows.forEach(r=>{const o=el('option',r.instrument);o.value=r.instrument;$('instrument').append(o)});$('prediction-id').value=P?P.prediction_id:'';$('save').disabled=!P;
const timingNames={before_entry:'事前判断',after_entry:'事后补记',timing_unverified:'时间资格待核对'};
function newRequest(){return [...crypto.getRandomValues(new Uint8Array(16))].map(x=>x.toString(16).padStart(2,'0')).join('')}
function resetNote(){draftDirty=false;$('note-form').reset();$('request-id').value=newRequest();$('prediction-id').value=P?P.prediction_id:'';$('supersedes').value='';$('note-context').textContent='绑定当前预测 '+(P?P.prediction_id.slice(0,12):'无')+'；以保存时刻区分事前与事后，不可回填时间。'}resetNote();$('new-note').onclick=()=>{if(!draftDirty||confirm('放弃尚未保存的草稿？'))resetNote()};
D.notes.forEach(n=>{const e=el('div','');e.className='note';e.append(el('strong',n.instrument+' · '+n.operator+' · '+n.received_at),el('p',(timingNames[n.timing]||'旧记录，时间待核对')+' / '+(n.is_latest?'当前记录':'历史版本')+' / '+n.prediction_id.slice(0,12)),el('p',n.hypothesis),el('p','失效：'+n.invalidation));if(n.is_latest){const b=el('button','追加修订，保留原文');b.type='button';b.onclick=()=>{const form=$('note-form');for(const k of ['operator','hypothesis','invalidation','intent'])form.elements[k].value=n[k];if(![...$('instrument').options].some(o=>o.value===n.instrument))$('instrument').append(new Option(n.instrument,n.instrument));$('instrument').value=n.instrument;$('prediction-id').value=n.prediction_id;$('supersedes').value=n.note_id;$('note-context').textContent='修订 '+n.note_id.slice(0,12)+'，仍绑定原预测；事后修订不替代事前判断。';$('journal').scrollIntoView()};e.append(b)}$('notes').append(e)});
const states={awaiting_review_refresh:'原判断已关联，等待数据更新评估；不是零收益',pending_exact_future_sessions:'等待后续交易日',mature:'价格目标已成熟',partial_missing_prices:'部分精确价格缺失',outside_current_receipt_window:'超出本次更新窗口',not_prospective:'非事前冻结，不计成绩'};
(D.reviews||[]).forEach(r=>{const e=el('details','');e.append(el('summary',r.prediction_date+' / '+r.prediction_id.slice(0,10)+' / '+(states[r.status]||r.status)+' / 配对 '+r.paired),el('p',r.count_in_summary?'同日同模型首个冻结批次，计入该组统计':'附加判断关联批次，不重复计入统计'));if(r.comparison)e.append(el('p','共同成熟证券 '+r.common_rule_model_samples+' 只；QLib Top5目标均值 '+fmt(r.comparison.model_top5_target_pct)+'%，动量Top5 '+fmt(r.comparison.momentum_top5_target_pct)+'%，等权 '+fmt(r.comparison.equal_weight_target_pct)+'%。不是投资组合收益。'));r.rows.forEach(row=>{const box=el('div','');box.className='note';box.append(el('strong',row.instrument+' / '+(row.entry_date||'等待实际交易日')+' → '+(row.exit_date||'未成熟')),el('p','冻结估计 '+fmt(row.prediction)+'%；实际价格目标 '+fmt(row.target_pct)+'%；误差 '+fmt(row.error_pct)+'个百分点'));(row.judgements||[]).forEach(n=>box.append(el('p',(timingNames[n.verified_timing]||'时间待核对')+' · '+n.operator+'：'+n.hypothesis+'；失效条件：'+n.invalidation)));if(!row.judgements?.length)box.append(el('p','未判断：原队列保留，不删除或补填。'));if(row.judgements?.length)box.append(el('p','失效条件是否触发须人工核对，不能仅凭收益正负自动判定。'));e.append(box)});$('reviews').append(e)});if(!(D.reviews||[]).length)$('reviews').textContent='没有成熟的前瞻结果；等待冻结预测之后的实际交易日。';
const market=D.market;
if(market){
$('market-state').textContent='交易日 '+market.trade_date+' / 评估时点 '+market.as_of+' / 市场状态 '+market.regime+'。历史复盘快照，不是实时行情。';
if(market.breadth_scope)$('market-state').append(' 广度仅覆盖可用标准价格记录，不冒充交易所全市场总数。');
if(market.matched_previous?.status==='matched'){const q=market.matched_previous;$('market-state').append(' 同股票、来源、复权与单位的 '+q.samples+' 只证券：上涨 '+q.previous.rise+' → '+q.current.rise+'，下跌 '+q.previous.fall+' → '+q.current.fall+'（对照 '+q.previous_date+'）。这是留存数据对照，不是历史到达时点认证。')}else $('market-state').append(' 同口径前日对照不可用，不推断升降。');
[['上涨 / 下跌',String(market.breadth.rise??'—')+' / '+String(market.breadth.fall??'—')],['涨停 / 跌停',String(market.breadth.limit_up??'—')+' / '+String(market.breadth.limit_down??'—')],['炸板率',market.breadth.blown_rate==null?'—':Number(market.breadth.blown_rate).toFixed(2)+'%'],['当前判断记录',D.notes.length]].forEach(([name,value])=>{const a=el('article',name);a.append(el('b',value));$('market-metrics').append(a)});
function themes(){
 const modern=!!market.stocks,all=market.themes.filter(t=>(t.concept_name||'').includes($('theme-search').value.trim()));
 if(modern){$('theme-workspace').querySelector('summary').textContent='题材完整成员与研究覆盖';const h=$('theme-workspace').querySelectorAll('th');h[1].textContent='研究覆盖 / 全成员';h[2].textContent='分类';}
 table($('theme-rows'),all.map(t=>[t.concept_name,modern?t.research_covered+' / '+t.member_count:(t.limit_up_count??'—')+' / '+t.member_count,modern?(t.broad_classification?'宽泛分类，非主线':'普通题材'):(t.max_board??'—')]));
 [...$('theme-rows').rows].forEach((tr,i)=>{tr.tabIndex=0;tr.onclick=()=>{
   const t=all[i],box=$('theme-detail'),members=modern?(t.member_codes||[]).map(c=>market.stocks[c]):t.limit_up_stocks||[];
   box.replaceChildren(el('h3',t.concept_name),el('p',modern?'成员快照 '+market.membership_date+'；保留全部声明成员，不代表全市场覆盖或研究推荐。':'仅同日涨停成员，不是完整成分。'));
   const search=document.createElement('input');search.placeholder='按证券代码或名称筛选';search.setAttribute('aria-label','筛选题材成员');box.append(search);
   const count=el('p',''),list=el('div',''),more=el('button','显示更多成员');more.type='button';box.append(count,list,more);let limit=50;
   function draw(){const filtered=members.filter(m=>((m.stock_name||'')+' '+m.stock_code).includes(search.value.trim()));list.replaceChildren();
     for(const member of filtered.slice(0,limit)){const p=el('p',(member.stock_name||'')+' '+member.stock_code+(modern?' / 涨跌 '+fmt(member.change_pct)+'%':' / '+member.board_level+'板'));const r=rows.find(r=>r.instrument===member.stock_code);
       if(r){const b=el('button','查看冻结研究证据');b.type='button';b.onclick=()=>{show(r);$('today').scrollIntoView()};p.append(b)}else p.append(el('span',' · 不在冻结研究池，不生成模型分数'));list.append(p)}
     count.textContent='显示 '+Math.min(limit,filtered.length)+' / '+filtered.length+'，全部成员 '+members.length+'；分页仅限制显示，不截断数据。';more.hidden=limit>=filtered.length;}
   more.onclick=()=>{limit+=50;draw()};search.oninput=()=>{limit=50;draw()};draw();
 };tr.onkeydown=e=>{if(e.key==='Enter')tr.click()}});
 $('theme-count').textContent='展示 '+all.length+' / '+market.themes.length+'；原目录 '+market.source_groups+'；未通过成员核对 '+market.excluded_broad_or_unknown_members+'。宽泛分类仅供检索，不提升为主线；目录顺序不代表投资优势。';
}
themes();$('theme-search').oninput=themes;
}else{$('market-state').textContent='未附同日市场复盘快照；当前仅有下方冻结研究范围，不用局部证券推断全市场。';$('theme-workspace').hidden=true}
function drawCompare(){
$('compare-panel').replaceChildren();$('today').querySelector('.scroll').before($('compare-panel'));
for(const code of compared){const r=rows.find(x=>x.instrument===code),box=el('article','');box.append(el('strong',r.instrument+' '+(r.name||'')),el('p','模型估计 '+fmt(r.prediction)+'% / 动量 '+fmt(r.baseline_momentum_20d)+'%'));
const contributions=Object.entries(r.contributions||{}).filter(([k])=>k!=='base_value');
const positive=contributions.filter(([,v])=>v>0).sort((a,b)=>b[1]-a[1])[0],negative=contributions.filter(([,v])=>v<0).sort((a,b)=>a[1]-b[1])[0];
box.append(el('p','最大正向模型贡献：'+(positive?(featureNames[positive[0]]||positive[0])+' +'+fmt(positive[1])+'个百分点':'无可用正向项')),el('p','最大反向模型贡献：'+(negative?(featureNames[negative[0]]||negative[0])+' '+fmt(negative[1])+'个百分点':'无可用反向项')),el('p','近21日已记录缺口：'+gaps(r.active_window_gap_counts)+'；不是风险评级'),el('p','下一步：核对来源与失效条件，再保存观察或拒绝理由；不形成交易资格。'));
$('compare-panel').append(box)}
}
const workspace=el('div','');workspace.className='workspace';$('today').before(workspace);workspace.append($('today'),$('journal'));$('experiment').before($('followup'));
const draftKey='stock-data-draft:'+location.pathname+':active',form=$('note-form');
function persistDraft(){draftDirty=true;const values={};for(const k of ['request_id','prediction_id','supersedes','instrument','operator','intent','hypothesis','invalidation'])values[k]=form.elements[k].value;try{sessionStorage.setItem(draftKey,JSON.stringify(values))}catch(_){$('message').textContent='浏览器不能保存草稿；离开前请先提交并取得回执。'}}
function editDraft(){form.elements.request_id.value=newRequest();persistDraft()}
form.addEventListener('input',editDraft);form.addEventListener('change',editDraft);
try{const raw=sessionStorage.getItem(draftKey);if(raw){const saved=JSON.parse(raw);const exists=D.notes.some(n=>n.request_id&&n.request_id===saved.request_id);if(exists)sessionStorage.removeItem(draftKey);else{if(saved.instrument&&![...$('instrument').options].some(o=>o.value===saved.instrument))$('instrument').append(new Option(saved.instrument,saved.instrument));for(const [k,v] of Object.entries(saved))if(form.elements[k])form.elements[k].value=v;if(!saved.request_id)$('request-id').value=newRequest();draftDirty=true;$('note-context').textContent='已恢复尚未保存的草稿，绑定预测 '+String(saved.prediction_id).slice(0,12)+(saved.prediction_id!==P?.prediction_id?'（历史批次，不会改绑今日预测）':'')+'。';}}}catch(_){}
window.addEventListener('beforeunload',e=>{if(draftDirty){e.preventDefault();e.returnValue=''}});
form.addEventListener('submit',()=>{persistDraft();draftDirty=false;$('save').disabled=true;$('save').textContent='提交中，等待独立保存回执…'});
window.addEventListener('pageshow',()=>{$('save').disabled=!P;$('save').textContent='保存判断到本地';if(form.elements.hypothesis.value)draftDirty=true});
$('new-note').addEventListener('click',()=>{if(!draftDirty)try{sessionStorage.removeItem(draftKey)}catch(_){}});
if(D.note_scope&&D.note_scope.total>D.note_scope.shown)$('notes').prepend(el('p','当前仅展示最近 '+D.note_scope.shown+' / '+D.note_scope.total+' 条；历史记录仍保留在本地。'));
const online=location.protocol==='http:'&&!form.elements.csrf.value.startsWith('__');
function staticMode(){if(!online){document.querySelectorAll('form button').forEach(b=>b.disabled=true);$('message').textContent='只读导出快照：这里不能更新或保存；填写/导出不代表已记录。请用 Start Research.cmd 打开在线工作台。'}}staticMode();window.addEventListener('pageshow',staticMode);
const conditionNames={pending:'仍待观察',triggered:'失效条件已触发',not_triggered:'截至记录时未触发',unclear:'证据不足，无法判断'};
const quoteStates={no_qualified_current_quote:'无合格当前报价',expired_after_publication:'已过期，不显示旧价格',conflicting_same_priority_quotes:'同优先级报价冲突',current_observation_not_executable:'时点合格，仅供观察'};
const obs=D.observation,watched=D.notes.filter(n=>n.is_latest&&n.intent==='observe');
$('observation-state').textContent='人工关注 '+watched.length+' 条；'+(obs?'当前报价合格 '+obs.qualified+' / '+obs.rows.length+' 只。最近核对 '+obs.as_of+'。':'尚未核对当前报价。')+' 账户未核对，不给可用仓位。';
if(obs?.capture)$('observation-capture').textContent='备用来源：腾讯；留存 '+obs.capture.received_rows+' 条，采集批次失败 '+obs.capture.failures+' 次；本次请求 '+obs.capture.provider_requests_this_run+' 次'+(obs.capture.reused?'（复用封存响应，不延长行情时效）':'')+'。响应完整性编号 '+obs.capture.manifest_id.slice(0,12)+'。取得响应不等于当前价合格。';
if(obs?.error)$('observation-state').textContent='报价快照校验失败或不可读取，当前报价关闭。日常工作台与判断记录仍可用；请核对来源后重新更新，不改用旧价。';
$('market').after($('observation'));
function drawObservation(){if(obs&&!obs.error){for(const q of obs.rows)if(q.price!=null){const deadline=Date.parse(q.valid_until+'+08:00');if(!Number.isFinite(deadline)||Date.now()>deadline){q.price=null;q.state='expired_after_publication'}}obs.qualified=obs.rows.filter(q=>q.price!=null).length;$('observation-state').textContent='人工关注 '+watched.length+' 条；当前报价合格 '+obs.qualified+' / '+obs.rows.length+' 只。最近核对 '+obs.as_of+'。账户未核对，不给可用仓位。'}const items=watched.map(n=>({code:n.instrument,note:n}));if($('observation-all').checked)for(const r of rows)if(!items.some(x=>x.code===r.instrument))items.push({code:r.instrument});
 table($('observation-rows'),items.map(item=>{const q=obs?.rows.find(r=>r.instrument===item.code),n=item.note,review=n?(D.human_reviews||[]).filter(r=>r.note_id===n.note_id).sort((a,b)=>b.received_at.localeCompare(a.received_at))[0]:null;return [item.code+' / '+(n?n.prediction_date:'未人工关注'),n?n.invalidation:'先记录观察理由与失效条件',review?conditionNames[review.conclusion]+' · '+review.received_at:'尚未人工核对',q?.price==null?'—':fmt(q.price),q?(quoteStates[q.state]||q.state)+(q.provider?' / '+q.provider+' / '+q.source_event_time:''):'没有报价快照']}));
 for(const [i,item] of items.entries())if(item.note){const a=el('a','查看原判断与复盘');a.href='#note-'+item.note.note_id;$('observation-rows').rows[i].cells[0].append(el('br',''),a)}
 if(!items.length){const row=el('tr',''),cell=el('td','暂无人工关注。先在候选比较区保存一条“继续观察”，不会自动把模型排序变成关注清单。');cell.colSpan=5;row.append(cell);$('observation-rows').append(row)}
}drawObservation();$('observation-all').onchange=drawObservation;setInterval(drawObservation,15000);window.addEventListener('pageshow',drawObservation);
for(const [i,n] of D.notes.entries()){const box=$('notes').querySelectorAll('.note')[i];if(!box)continue;box.id='note-'+n.note_id;const link=el('a','返回原预测复盘');link.href='#review-'+n.prediction_id;box.append(link);if(online){const receipt=el('a','查看保存回执');receipt.href='/receipt/'+n.note_id;box.append(el('br',''),receipt)}const revise=box.querySelector('button');if(revise)revise.addEventListener('click',editDraft);
 const reviewBox=el('details','');reviewBox.append(el('summary','记录这条判断的后续核对'));
 for(const r of (D.human_reviews||[]).filter(r=>r.note_id===n.note_id))reviewBox.append(el('p',r.received_at+' · '+r.reviewer+' · '+conditionNames[r.conclusion]+'：'+r.evidence));
 if(online){const f=document.createElement('form');f.method='post';f.action='/review';f.className='review-form';
  for(const [name,value] of Object.entries({csrf:form.elements.csrf.value,note_id:n.note_id,request_id:newRequest()})){const input=document.createElement('input');input.type='hidden';input.name=name;input.value=value;f.append(input)}
  const label=el('label','失效条件核对（人工声明，不代替未来价格）'),select=document.createElement('select');select.name='conclusion';for(const [k,v] of Object.entries(conditionNames))select.append(new Option(v,k));label.append(select);f.append(label);
  for(const [key,name] of [['reviewer','复盘署名'],['evidence','观察证据与时间范围']]){const label=el('label',name),input=document.createElement(key==='evidence'?'textarea':'input');input.name=key;input.required=true;input.maxLength=4000;label.append(input);f.append(label)}
  const submit=el('button','保存复盘记录');f.append(submit);const key='stock-data-review-draft:'+n.note_id;
  const keep=()=>{const value=Object.fromEntries(new FormData(f));delete value.csrf;try{sessionStorage.setItem(key,JSON.stringify(value))}catch(_){}};
  try{const saved=JSON.parse(sessionStorage.getItem(key)||'null');if(saved){for(const [k,v] of Object.entries(saved))if(f.elements[k])f.elements[k].value=v;draftDirty=true}}catch(_){}
  f.addEventListener('input',()=>{f.elements.request_id.value=newRequest();keep();draftDirty=true});f.addEventListener('change',()=>{f.elements.request_id.value=newRequest();keep();draftDirty=true});f.addEventListener('submit',()=>{keep();draftDirty=false;submit.disabled=true;submit.textContent='提交中…'});reviewBox.append(f);
 }else reviewBox.append(el('p','只读快照：不能在此保存复盘。'));
 box.append(reviewBox);
}
for(const [i,r] of (D.reviews||[]).entries())$('reviews').children[i].id='review-'+r.prediction_id;
function revealAnchor(){const id=location.hash.slice(1),target=$(id);if(target){if(target.tagName==='DETAILS')target.open=true;target.scrollIntoView()}}window.addEventListener('hashchange',revealAnchor);revealAnchor();
window.addEventListener('pageshow',()=>{if(online)document.querySelectorAll('.review-form button').forEach(b=>{b.disabled=false;b.textContent='保存复盘记录'})});
</script></html>'''


def receipt_page(note):
    """A durable acknowledgement does not depend on a healthy market renderer."""
    fields=[('回执编号',note['note_id']),('证券',note['instrument']),('声明署名',note['operator']),
            ('接收时间',note['received_at']),('原预测批次',note['prediction_id']),
            ('判断依据',note['hypothesis']),('失效条件',note['invalidation'])]
    body=''.join('<dt>'+escape(k)+'</dt><dd style="overflow-wrap:anywhere;white-space:pre-wrap">'+escape(str(v))+'</dd>' for k,v in fields)
    request=json.dumps(note.get('request_id'),ensure_ascii=True).replace('<','\\u003c')
    return ('<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; style-src \'unsafe-inline\'; script-src \'unsafe-inline\'; base-uri \'none\'; form-action \'none\'">'
            '<title>判断已保存 · Stock Data</title>'+STYLE+'<main><header><h1>判断已保存</h1>'
            '<p role="status">已可靠保存到本地。刷新此回执不会重复提交；不产生委托。</p></header><section><dl>'+body+'</dl>'
            '<p>署名由提交者自行声明，未经过账户身份认证。后续结果未成熟时继续等待，不补填结果。</p>'
            '<a href="/#note-'+escape(note['note_id'],quote=True)+'">返回这条判断</a> · '
            '<a href="/#review-'+escape(note['prediction_id'],quote=True)+'">查看原预测后续复盘</a></section></main>'
            '<script>try{const key="stock-data-draft:/:active",raw=sessionStorage.getItem(key);'
            'if(raw&&JSON.parse(raw).request_id==='+request+')sessionStorage.removeItem(key)}catch(_){}</script></html>')


def review_receipt_page(review):
    labels={'pending':'仍待观察','triggered':'失效条件已触发','not_triggered':'截至记录时未触发','unclear':'证据不足，无法判断'}
    text='<dl>'+''.join('<dt>'+escape(k)+'</dt><dd style="overflow-wrap:anywhere;white-space:pre-wrap">'+escape(str(v))+'</dd>' for k,v in [
        ('复盘回执',review['review_id']),('原判断',review['note_id']),('署名',review['reviewer']),
        ('记录时间',review['received_at']),('人工核对',labels[review['conclusion']]),('证据',review['evidence'])])+'</dl>'
    key=json.dumps('stock-data-review-draft:'+review['note_id'])
    request=json.dumps(review['request_id'])
    return ('<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; style-src \'unsafe-inline\'; script-src \'unsafe-inline\'; base-uri \'none\'; form-action \'none\'">'
        '<title>复盘已保存 · Stock Data</title>'+STYLE+'<main><header><h1>复盘已保存</h1></header><section>'+text+
        '<p>这是人工条件观察，不修改原判断、价格标签或账户状态，不代表模型优势验收。</p><a href="/#note-'+escape(review['note_id'])+'">返回原判断</a></section></main>'
        '<script>try{const key='+key+',raw=sessionStorage.getItem(key);if(raw&&JSON.parse(raw).request_id==='+request+')sessionStorage.removeItem(key)}catch(_){}</script></html>')


def server_page(html, token, message='', running=False):
    return html.replace('__CSRF__',escape(token,quote=True)).replace('<!--SERVER_MESSAGE-->',escape(message)).replace('__SERVER_MESSAGE__',escape(message)).replace(
        '<!--RUNNING_REFRESH-->','<script>setTimeout(()=>{if(!draftDirty&&!document.querySelector("form :focus"))location.reload();else document.getElementById("message").textContent+=" 有未保存草稿，已暂停自动刷新。"},10000)</script>' if running else '')
