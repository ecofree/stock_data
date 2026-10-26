"""Self-contained current-scope recovery and capacity evidence."""
from html import escape
from .domain import canonical
from .acceptance_scope import SCOPE


def render(evidence):
    if evidence.get('acceptance_scope') != SCOPE or evidence.get('execution_ready') is not False:
        raise ValueError('explicit non-executing observation scope required')
    recovery, capacity = evidence['recovery'], evidence['capacity']
    if recovery['offsite_accepted'] is not False or not capacity['complete_multiset_verified']:
        raise ValueError('local report cannot certify offsite recovery or incomplete coverage')
    rows = ''.join('<tr><td>' + escape(r['table']) + '</td><td>' + str(r['rows']) + '</td><td>' +
                   escape('、'.join(r['products']) or '未映射到当前产品') + '</td><td>保留原库</td></tr>' for r in evidence['inventory']['tables'])
    return '''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>行情、研究与人工观察 · 离线验收</title><style>
*{box-sizing:border-box}body{margin:0;background:#f2f5f6;color:#203744;font:16px/1.7 "Microsoft YaHei",sans-serif}
main{max-width:1120px;margin:auto;padding:24px}section{padding:22px;margin:18px 0;background:white;border:1px solid #d6e0e5}
h1{font-size:28px}h2{font-size:22px}.pending{background:#fff3d9;padding:15px;border-left:4px solid #ae742d}
.scroll{overflow:auto}table{border-collapse:collapse;width:100%;min-width:620px}td,th{padding:10px;text-align:left;border-bottom:1px solid #ddd}
input{font:inherit;padding:8px;max-width:100%}pre{white-space:pre-wrap;overflow-wrap:anywhere;font-size:12px}summary{cursor:pointer}
@media(max-width:600px){main{padding:12px}section{padding:14px}h1{font-size:23px}}
</style><main><h1>行情、研究与人工观察 · 离线验收</h1>
<p>本阶段不纳入真实账户。行情事实、研究有效性与人工观察分别判断；当前材料为历史版本。</p>
<p class="pending">本机隔离恢复已经核验。整机独立性、异地取回和灾难恢复全程仍需离机实测；没有恢复运营。</p>
<section><h2>C1 · 历史材料恢复</h2><p>恢复文件：''' + str(len(recovery['files'])) + ''' 个。文件身份、数据库结构与行数、业务引用和原发布页均重新核对。</p>
<p>环境重建、搬运和完整恢复总耗时见本次外部运行回执；此处不把复制耗时当作 RTO，也不推算持续运营 RPO。</p>
<p><a href="workbench.html">查看恢复后的只读观察工作台</a></p></section>
<section><h2>C2 · 历史表容量验收</h2><p id="capacity-summary">已核验 ''' + str(capacity['rows']) + ''' 行、''' + str(capacity['batches']) + ''' 批；每批与独立恢复基线比较字段类型、业务值及重复次数，总体覆盖完整。</p>
<p>归档数据文件：''' + f"{capacity['archive_bytes']/1024**2:.2f}" + ''' MiB。源库未删除，当前未回收生产空间。</p>
<p>这是历史原始表保全。缺来源、复权或其他资格的记录仍保持未知，归档通过不授予行情分析资格。</p></section>
<section><h2>C3 · 真实账户对账</h2><p id="account-scope">本阶段不适用。未导入真实账户，不要求券商对账单，不计算真实收益、可用仓位或交易资格。</p>
<p>旧反馈账本及历史记录保留，重新纳入真实账户范围时必须单独验收。</p></section>
<section><h2>全库保留清单</h2><p>只对本次选择的历史表完成分批归档；其他表保留原库。未验证可重建的材料不删除。</p>
<label>筛选表名 <input id="table-search" maxlength="100"></label><p id="table-count"></p>
<div class="scroll"><table><thead><tr><th>表名</th><th>行数</th><th>当前产品映射</th><th>保留决定</th></tr></thead><tbody id="tables">''' + rows + '''</tbody></table></div></section>
<details><summary>展开完整验收证据</summary><pre>''' + escape(canonical(evidence)) + '''</pre></details>
<script>const rows=[...document.querySelectorAll('#tables tr')],input=document.getElementById('table-search');
function filter(){let n=0;for(const r of rows){r.hidden=!r.cells[0].textContent.toLowerCase().includes(input.value.trim().toLowerCase());if(!r.hidden)n++}document.getElementById('table-count').textContent='显示 '+n+' / '+rows.length+' 张表'}input.addEventListener('input',filter);filter();</script></main></html>'''
