"""Self-contained historical research review and non-executable human notes."""
import json
from pathlib import Path

from .domain import canonical, identity, instrument, utc
from .gap_evidence import read_json, write_json
from .program_supplement import verify
from .rolling_research import file_hash


def build_data(program, rolling_results):
    results = verify(program)
    old = read_json(rolling_results)[0]
    if old['execution_ready'] is not False or old['signal_impact'] != 'disabled':
        raise ValueError('historical diagnostic only')
    variants = {}
    for name,r in results.items():
        f = r['final']
        variants[name] = {k:r[k] for k in ('complete_requested_period','portfolio_return','blocked_new_risk_since',
            'potential_windows','selected_instruments','daily','data_requests','skips','fills')}
        variants[name].update({'holdings':{c:q for c,q in f['holdings'].items() if q},
            'cash_fen':f['cash_fen'],'held_cost_fen':f['held_cost_fen'],
            'cost_residual_fen':f['cost_reconciliation_residual_fen'],
            'cash_residual_fen':f['cash_reconciliation_residual_fen']})
    data = {'schema':1,'scope':'historical_research_review_not_live_decision',
        'variants':variants,'metrics':old['aggregate'],'paired_comparisons':old['paired_comparisons'],
        'sources':{'program_manifest_sha256':file_hash(Path(program)/'run/completed.json'),
                   'rolling_results_sha256':file_hash(rolling_results)},
        'execution_ready':False,'actual_operator_return':None,
        'boundaries':['历史修复假设，不是当时系统可知回放','QLib 未证明选股优势，禁止升级 champion',
            '持仓未结清的全区间收益留空，不删除停牌证券','人工笔记不创建委托、冻结资金或交易证书',
            '真实账户未提供，20 交易日稳定性与生产切换尚未验收']}
    return {**data,'report_id':identity(data)}


def validate_note(note, report):
    if report.get('report_id') != identity({k:v for k,v in report.items() if k != 'report_id'}):
        raise ValueError('report content fingerprint changed')
    expected = {'schema','report_id','note_id','created_at','variant','instrument','intent',
                'hypothesis','invalidation','reflection','scope'}
    if not isinstance(note,dict) or set(note) != expected:
        raise ValueError('strict journal fields required')
    if note['schema'] != 1 or note['report_id'] != report['report_id'] or note['scope'] != 'research_note_no_execution':
        raise ValueError('journal must bind this report and cannot authorize execution')
    if note['variant'] not in report['variants'] or note['intent'] not in ('observe','reject','paper_hypothesis'):
        raise ValueError('unknown variant or executable intent')
    instrument(note['instrument'])
    variant = report['variants'][note['variant']]
    known = {x['instrument'] for key in ('fills','data_requests','skips') for x in variant[key]}
    if note['instrument'] not in known:
        raise ValueError('instrument not in this historical report')
    utc(note['created_at'])
    for key in ('note_id','hypothesis','invalidation','reflection'):
        if not isinstance(note[key],str) or len(note[key]) > 4000 or (key != 'reflection' and not note[key].strip()):
            raise ValueError('bounded explicit journal text required')
    return {'note_id':note['note_id'],'content_sha256':identity(note),'execution_ready':False,
            'scope':'human_research_annotation_not_actual_operator_outcome'}


def import_note(raw, report, folder):
    if len(raw) > 64000:
        raise ValueError('journal exceeds size limit')
    note = json.loads(raw)
    receipt = validate_note(note,report)
    folder = Path(folder)
    folder.mkdir(parents=True,exist_ok=True)
    target = folder/(identity([note['report_id'],note['note_id']])+'.json')
    document = canonical({'note':note,'receipt':receipt})
    try:
        with target.open('x',encoding='utf-8') as stream:
            stream.write(document)
    except FileExistsError:
        if target.read_text(encoding='utf-8') != document:
            raise ValueError('journal id already exists with different content') from None
    return receipt


def render(data):
    payload = json.dumps(data,ensure_ascii=False,allow_nan=False).replace('<','\\u003c').replace('>','\\u003e').replace('&','\\u0026')
    return HTML.replace('__PAYLOAD__',payload)


def publish(program, rolling_results, folder):
    folder = Path(folder)
    folder.mkdir(parents=True,exist_ok=False)
    data = build_data(program,rolling_results)
    write_json(folder/'review.json',data)
    with (folder/'index.html').open('x',encoding='utf-8') as stream:
        stream.write(render(data))
    return {'report_id':data['report_id'],'html_sha256':file_hash(folder/'index.html')}


HTML = r'''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'none'; img-src 'none'; form-action 'none'; base-uri 'none'">
<title>研究与复盘工作台 · Stock Data</title>
<style>
:root{--ink:#202a28;--paper:#f3f0e8;--line:#cccfc5;--green:#28574b;--warn:#963f27}*{box-sizing:border-box}body{margin:0;background:var(--paper);color:var(--ink);font:15px/1.65 'Microsoft YaHei',sans-serif}header{background:var(--ink);color:var(--paper);padding:25px 5vw;display:flex;justify-content:space-between;gap:20px;align-items:center}small,.mono{font-family:Consolas,monospace;letter-spacing:.08em}header strong{display:block;font-size:20px}header small{color:#a8b9af}main{max-width:1440px;margin:auto;padding:38px 5vw}h1{font:42px/1.25 'SimSun',serif;margin:0 0 15px}h2{font-size:21px;margin:0 0 18px}h3{font-size:16px}.lead{max-width:800px;color:#59615a;margin-bottom:30px}.warning{border-left:4px solid var(--warn);padding:12px 20px;background:#e9dfd2;margin:24px 0}.warning strong{color:var(--warn)}.controls{display:flex;gap:18px;align-items:center;flex-wrap:wrap;margin:20px 0}button,select,input,textarea{font:inherit;padding:9px 12px;border:1px solid #9aa89e;border-radius:0;background:#faf9f4;color:var(--ink)}button{cursor:pointer;background:var(--green);color:white;border-color:var(--green)}button:hover{background:#193e33}button:focus-visible,input:focus-visible,select:focus-visible,textarea:focus-visible{outline:3px solid #b87339;outline-offset:2px}.stats{display:grid;grid-template-columns:repeat(4,1fr);border-top:2px solid var(--ink);border-bottom:1px solid var(--line);margin:25px 0}.stat{padding:18px 18px 18px 0}.stat span{display:block;font-size:12px;color:#606d63}.stat b{font:30px Consolas,monospace;display:block;margin-top:10px}.columns{display:grid;grid-template-columns:1.6fr 1fr;gap:40px}.panel{padding:25px 0;border-bottom:1px solid var(--line)}.tablewrap{overflow:auto}table{width:100%;border-collapse:collapse;font-size:13px}th{text-align:left;font-weight:normal;color:#536458;background:#e6e8dd;white-space:nowrap}td,th{padding:11px 12px;border-bottom:1px solid var(--line)}td{font-variant-numeric:tabular-nums}label{display:block;font-size:13px}form label{margin:12px 0}form input,textarea,form select{display:block;width:100%;margin-top:5px}textarea{min-height:78px;resize:vertical}#feedback{min-height:30px;color:var(--green);overflow-wrap:anywhere}#reportid{overflow-wrap:anywhere;font-size:11px}.badge{border:1px solid #71897b;padding:6px 13px;color:#d8e5d7;white-space:nowrap}details{margin:18px 0}summary{cursor:pointer}footer{border-top:2px solid var(--ink);padding:25px 0;margin-top:30px;color:#637064;font-size:12px}.muted{color:#606d63}.note{border-left:2px solid var(--green);padding-left:12px;margin:12px 0;white-space:pre-wrap}.empty{padding:20px;color:#606d63}svg{width:100%;height:140px}svg text{font:11px Consolas;fill:#687569}@media(max-width:850px){.columns{grid-template-columns:1fr;gap:10px}h1{font-size:32px}.stats{grid-template-columns:repeat(2,1fr)}header{align-items:start}.badge{font-size:11px}main{padding-top:25px}}@media(prefers-reduced-motion:no-preference){main{animation:enter .35s ease-out}@keyframes enter{from{opacity:0;transform:translateY(8px)}to{opacity:1;transform:none}}}
</style>
<style>.columns>*{min-width:0}.tablewrap{max-width:100%}.panel p{overflow-wrap:anywhere}@media(max-width:500px){header{flex-direction:column;align-items:flex-start;gap:12px}}</style>
<header><div><small>STOCK DATA / RESEARCH DESK</small><strong>研究与复盘工作台</strong></div><div class="badge">RESEARCH ONLY · 不连接交易</div></header>
<main><small class="muted">01 / FROZEN EVIDENCE, OPEN QUESTIONS</small><h1>先看证据，再做判断。</h1>
<p class="lead">把模型标签诊断、历史组合假设和人的判断分开记录。同一个实验，同一组证券；缺失数据不变成零收益，也不被删掉。</p>
<div class="warning"><strong>未达到正式选股与执行验收</strong><br>本页不是当日荐股。历史已暴露；QLib 尚无经验证优势。真实账户未提供，所有笔记均不产生委托。</div>
<div class="controls"><label>组合变体 <select id="variant"></select></label><span id="window" class="mono muted"></span></div>
<section class="stats" id="stats" aria-live="polite"></section>
<div class="columns"><div>
<section class="panel"><h2>历史账户轨迹</h2><p class="muted">仅显示现金余额，不是净值曲线；未定价持仓仍在账。</p><div id="trace"></div><p id="balance"></p></section>
<section class="panel"><h2>QLib：标签层面的证据</h2><div class="tablewrap"><table><thead><tr><th>模型</th><th>日均 Rank IC</th><th>Top-K 标签均值</th></tr></thead><tbody id="metrics"></tbody></table></div><p class="muted">资金特征的 MSE 配对区间跨过 0；不能声称稳定改善，更不能等同独立数据源价值。标签收益不等于组合或操作者收益。</p></section>
<section class="panel"><h2>待补证 · 精确到证券与日期</h2><label>筛选证券代码 <input id="search" placeholder="例如 000609" autocomplete="off"></label><p id="gapcount" class="muted"></p><div class="tablewrap"><table><thead><tr><th>日期</th><th>证券</th><th>缺口字段</th><th>状态</th></tr></thead><tbody id="gaps"></tbody></table></div></section>
<details><summary>查看历史假设成交（非券商回报）</summary><div class="tablewrap"><table><thead><tr><th>证券</th><th>方向</th><th>数量</th><th>原价 / 分</th></tr></thead><tbody id="fills"></tbody></table></div><p class="muted">页面仅展示当前筛选的前 100 条；完整证据已内联。</p></details>
</div><aside>
<section class="panel"><h2>人工判断与复盘笔记</h2><p class="muted">先填写假设和失效条件，导出后由本地命令校验归档。这里不是交易确认入口。</p>
<form id="journal"><label>证券 <input id="code" placeholder="SZ.000609" required maxlength="9"></label>
<label>研究意向 <select id="intent"><option value="observe">继续观察</option><option value="reject">不采用</option><option value="paper_hypothesis">纸面假设（不下单）</option></select></label>
<label>判断依据 <textarea id="hypothesis" required maxlength="4000"></textarea></label>
<label>失效条件 <textarea id="invalidation" required maxlength="4000"></textarea></label>
<label>事后复盘 / 反证 <textarea id="reflection" maxlength="4000"></textarea></label>
<button type="submit">导出研究笔记 JSON</button></form><p id="feedback" role="status"></p>
<label>预览已有笔记（不会自动归档）<input id="noteFile" type="file" accept="application/json,.json"></label><div id="notes"></div></section>
<section class="panel"><h2>验收边界</h2><ul id="boundaries"></ul><details><summary>报告身份 / 来源指纹</summary><p id="reportid" class="mono"></p></details></section>
</aside></div><footer>LOCAL / SELF-CONTAINED　·　无外部数据请求　·　刷新页面不保存未导出的输入　·　人工笔记不作为真实账户收益证据</footer></main>
<script type="application/json" id="data">__PAYLOAD__</script><script>
'use strict';const D=JSON.parse(document.getElementById('data').textContent),$=id=>document.getElementById(id);
const labels={price_baseline:'价格基线',price_plus_funds:'价格 + 资金',identity_hash_baseline:'身份哈希基线'};
const text=(tag,t)=>{const e=document.createElement(tag);e.textContent=t;return e};
const table=(id,rows)=>{$(id).replaceChildren(...rows.map(r=>{const tr=document.createElement('tr');r.forEach(c=>tr.append(text('td',c)));return tr}))};
const fmt=x=>x==null?'未具备条件':Number(x).toLocaleString('zh-CN',{maximumFractionDigits:2});
Object.keys(D.variants).forEach(v=>{const o=text('option',labels[v]||v);o.value=v;$('variant').append(o)});
function update(){const v=D.variants[$('variant').value],q=$('search').value.trim();$('stats').replaceChildren();
[['已评估交易日',v.daily.length],['假设成交笔数',v.fills.length],['保留持仓数',Object.keys(v.holdings).length],['完整组合假设收益',v.portfolio_return==null?'—':(100*Number(v.portfolio_return)).toFixed(2)+'%']].forEach(([k,n])=>{const e=text('div','');e.className='stat';e.append(text('span',k),text('b',n));$('stats').append(e)});
$('window').textContent=v.daily[0].date+' — '+v.daily.at(-1).date;
$('balance').textContent='现金 ¥'+fmt(v.cash_fen/100)+' · 保留成本 ¥'+fmt(v.held_cost_fen/100)+' · 新增风险停止于 '+(v.blocked_new_risk_since||'无')+'。现金/成本核对差额 '+v.cash_residual_fen+'/'+v.cost_residual_fen+' 分。';
const ns='http://www.w3.org/2000/svg',svg=document.createElementNS(ns,'svg');svg.setAttribute('viewBox','0 0 700 140');svg.setAttribute('role','img');svg.setAttribute('aria-label','历史现金余额，不是净值');const ys=v.daily.map(d=>d.cash_fen),lo=Math.min(...ys),range=Math.max(...ys)-lo||1;
const line=document.createElementNS(ns,'polyline');line.setAttribute('points',ys.map((y,i)=>(10+i*680/Math.max(ys.length-1,1))+','+(120-(y-lo)*100/range)).join(' '));line.setAttribute('fill','none');line.setAttribute('stroke','#28574b');line.setAttribute('stroke-width','2');svg.append(line);$('trace').replaceChildren(svg);
const gaps=v.data_requests.filter(g=>g.instrument.includes(q));$('gapcount').textContent='匹配 '+gaps.length+' 条；显示前 100 条。优先官方 API，其次 TuShare 中继；不插值。';table('gaps',gaps.slice(0,100).map(g=>[g.date,g.instrument,g.field,g.held?'持仓缺口 / '+g.reason:g.reason]));
table('fills',v.fills.filter(f=>f.instrument.includes(q)).slice(0,100).map(f=>[f.instrument,f.side,f.quantity,f.price_fen]));}
$('variant').addEventListener('change',update);$('search').addEventListener('input',update);
table('metrics',Object.entries(D.metrics).map(([k,v])=>[labels[k]||k,v.mean_daily_rank_ic==null?'无定义':v.mean_daily_rank_ic.toFixed(4),v.top_k_mean_label_pct.toFixed(3)+'%']));
$('boundaries').replaceChildren(...D.boundaries.map(b=>text('li',b)));$('reportid').textContent=D.report_id+'\n'+JSON.stringify(D.sources);update();
function validate(n){if(n.scope!=='research_note_no_execution'||n.report_id!==D.report_id||n.schema!==1)throw Error('笔记不属于本报告，或含不允许的执行范围');if(!D.variants[n.variant]||!['observe','reject','paper_hypothesis'].includes(n.intent))throw Error('无效研究意向');const v=D.variants[n.variant];if(![...v.fills,...v.skips,...v.data_requests].some(x=>x.instrument===n.instrument))throw Error('证券不在当前变体证据中');for(const k of ['note_id','hypothesis','invalidation','reflection'])if(typeof n[k]!=='string'||n[k].length>4000||(k!=='reflection'&&!n[k].trim()))throw Error('请完整填写判断依据及失效条件');}
$('journal').addEventListener('submit',e=>{e.preventDefault();try{const n={schema:1,report_id:D.report_id,note_id:crypto.randomUUID(),created_at:new Date().toISOString(),variant:$('variant').value,instrument:$('code').value.trim(),intent:$('intent').value,hypothesis:$('hypothesis').value,invalidation:$('invalidation').value,reflection:$('reflection').value,scope:'research_note_no_execution'};validate(n);const url=URL.createObjectURL(new Blob([JSON.stringify(n,null,2)],{type:'application/json'}));const a=document.createElement('a');a.href=url;a.download='research-note-'+n.note_id+'.json';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);$('feedback').textContent='已导出研究笔记；未创建委托。请用本地 import-note 命令校验归档。'}catch(err){$('feedback').textContent=err.message}});
$('noteFile').addEventListener('change',async()=>{try{const f=$('noteFile').files[0];if(!f)return;if(f.size>64000)throw Error('笔记过大');const n=JSON.parse(await f.text());validate(n);const e=text('div',n.instrument+' / '+n.intent+'\n依据：'+n.hypothesis+'\n失效：'+n.invalidation+'\n复盘：'+n.reflection);e.className='note';$('notes').replaceChildren(e);$('feedback').textContent='仅预览，尚未归档；未产生账户动作。'}catch(err){$('feedback').textContent=err.message}});
</script></html>'''
