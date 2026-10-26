"""Self-contained, read-only rehearsal evidence page; never an operational desk."""
from html import escape

from . import recovery_bundle, cold_archive, feedback_ledger
from .domain import canonical
from .isolated_workspace import workspace, child, read_json

BLOCKERS = {
    'statement_completeness_not_declared': '尚未声明材料完整',
    'fees_incomplete': '费用材料不完整',
    'negative_inventory_requires_account_rules': '持仓为负，需要账户规则核验',
    'closing_controls_differ': '期末现金或持仓不一致',
    'retained_active_events_omitted_from_statement': '声明遗漏账本中的有效事件',
    'declared_active_events_not_applied': '声明中的有效事件未进入本次核对',
    'declared_event_revision_not_current': '声明引用了已经被修订的事件版本',
    'retained_event_heads_omitted_from_statement': '声明未包含完整的当前事件状态',
}


def render(evidence):
    recovery = evidence['recovery']
    archives = evidence['archives']
    feedback = evidence['feedback']
    coverage = evidence.get('coverage')
    coverage_text = ('已按独立恢复基准核验覆盖：' + str(coverage['rows']) + ' 行；仅对应约定快照和日期范围。'
                     if coverage else '归档覆盖尚未核验：分区存在和行数合计不能证明查询结果完整。')
    records = []
    for row in feedback['records']:
        status = ('旧版回执待重新核对' if row.get('requires_revalidation') else '已被修订替代' if row['superseded']
                  else '算术核对一致' if row['arithmetic_reconciled'] else '存在差异或缺少证据')
        records.append('<tr><td>' + escape(row['account_id']) + '</td><td>' + escape(row['source_kind']) +
                       '</td><td>' + status + '</td><td>' + str(row['cash_difference_fen']) + '</td><td>' +
                       ('已确认' if row['human_acknowledged'] else '未确认') + '</td><td>' +
                       escape('；'.join(BLOCKERS.get(b, '存在未知阻断，请检查原始证据') for b in row['blockers']) or '—') + '</td></tr>')
    encoded = canonical(evidence).replace('&', '\\u0026').replace('<', '\\u003c').replace('>', '\\u003e')
    return '''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>StockData 后续能力隔离验收</title><style>
*{box-sizing:border-box}body{margin:0;background:#f4f6f8;color:#213644;font:16px/1.7 "Microsoft YaHei",sans-serif}
main{max-width:1060px;margin:auto;padding:24px}section{background:white;padding:24px;margin:20px 0;border:1px solid #d8e1e7}
h1{font-size:28px}h2{font-size:22px}.pending{padding:14px;border-left:4px solid #ae742d;background:#fff4e2}
.scroll{overflow:auto}table{border-collapse:collapse;width:100%;min-width:880px}td,th{text-align:left;padding:10px;border-bottom:1px solid #ddd}
th{white-space:nowrap}td:nth-child(3){min-width:140px}td:last-child{min-width:260px}.scroll-hint{display:none}
pre{white-space:pre-wrap;overflow-wrap:anywhere;font-size:12px}summary{cursor:pointer}code{overflow-wrap:anywhere}
@media(max-width:600px){main{padding:12px}section{padding:14px}h1{font-size:24px}.scroll-hint{display:block}}
</style><main><h1>后续能力隔离验收</h1>
<p>处理顺序：C1 完整恢复 → C2 容量治理 → C3 真实反馈。</p>
<p class="pending">当前是本机隔离演练。异地恢复、约定 RPO/RTO 和真实对账单验收尚未完成。生产、计划任务与退役入口未获启用资格。</p>
<section id="recovery"><h2>C1 · 恢复包与恢复核验</h2><p>已核对恢复文件：''' + str(len(recovery['files'])) + ''' 个；本次恢复步骤耗时：''' + f"{recovery['elapsed_seconds']:.3f}" + ''' 秒。</p>
<p>包含声明的代码、环境、工作区、原始事实和数据库。恢复耗时不等于完整异地 RTO；RPO、RPO/RTO 目标保持未知。配置重新绑定与异地验收待完成。</p></section>
<section id="archive"><h2>C2 · 复制归档与联合读取</h2><p>已核验分区：''' + str(len(archives)) + '''；分区行数合计：''' + str(sum(r['rows'] for r in archives)) + '''（分区可能重叠，不代表去重后的业务总数）。</p>
<p>完整产品键区分来源和复权；相同重叠只读一次，冲突拒绝合并。源数据删除数：0。保留时长与实际冷存储容量尚待确定。</p><p id="coverage-status">''' + coverage_text + '''</p></section>
<section id="feedback"><h2>C3 · 独立反馈账本</h2><p>原始文件：''' + str(feedback['counts']['original']) + '''；保留事件版本：''' + str(feedback['counts']['event']) + '''。</p>
<p class="scroll-hint">窄屏可左右滑动查看核对表格。</p>
<div class="scroll"><table><thead><tr><th>账户标识</th><th>声明来源</th><th>核对状态</th><th>现金差额 / 分</th><th>人工确认</th><th>阻断依据</th></tr></thead><tbody>''' + ''.join(records) + '''</tbody></table></div>
<p>模拟数据不会成为真实业绩。人工确认与算术核对独立；原始券商资料真实性仍需另外核验。</p></section>
<details><summary>展开本次验收证据</summary><pre>''' + escape(canonical(evidence)) + '''</pre></details>
<script type="application/json" id="evidence">''' + encoded + '</script></main></html>'


def export(root, restored, partitions, destination, *, coverage=None, source=None):
    root = workspace(root)
    if bool(coverage) != bool(source):
        raise ValueError('coverage report requires both the plan and actual hot source')
    evidence = {'recovery': recovery_bundle.verified_restore(root, restored),
                'archives': [cold_archive.verify(root, p) for p in partitions],
                'feedback': feedback_ledger.inspect(root), 'execution_ready': False}
    if coverage:
        plan = read_json(child(root, coverage))
        result = cold_archive.joint_read(root, source, partitions, plan['start'], plan['end_exclusive'], coverage=coverage)
        evidence['coverage'] = {k:v for k,v in result.items() if k != 'rows'}
        evidence['coverage']['rows'] = len(result['rows'])
    target = child(root, destination)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open('x', encoding='utf-8') as stream:
        stream.write(render(evidence))
    return {'page': destination, 'execution_ready': False, 'real_acceptance': False}
