"""Declared business edges, not supplier entitlement or observed consumption.

Source priority is owned by source_authority; qualification is owned by the
named readers. This catalogue never executes collectors or grants readiness.
"""
from dataclasses import asdict, dataclass
import hashlib
import json

from .source_authority import SOURCE_POLICIES


VERSION = 'manual-research-business-v1'


@dataclass(frozen=True)
class Product:
    policy: str | None
    raw: tuple[str, ...]
    projection: tuple[str, ...]
    consumers: tuple[str, ...]
    qualification: str
    retention_reason: str


PRODUCTS = {
    'market_context': Product('market_state', ('daily_summary',), ('v_market_daily',),
        ('trade_system.v2.market_workspace.project', 'trade_system.daily_review.build_daily_review_context'),
        'market_contract.market_daily_status_sql: five dated observed counts with provenance', '市场背景与复盘；部分统计不代表全市场认证'),
    'daily_price': Product('kline', ('tushare_daily', 'multi_source_kline'), ('v_kline_daily',),
        ('trade_system.v2.market_workspace.project', 'trade_system.readiness.market_view_capability'),
        'readiness.market_view_capability: security/date/provider/adjustment/units/clock', '价格事实、广度和同口径历史比较'),
    'concept_membership': Product('ths_concept', ('ths_concept_daily', 'ths_concept_stock_history'),
        ('v_default_concept_daily', 'v_default_concept_stock_history'),
        ('trade_system.v2.market_workspace.theme_facts', 'trade_system.concept_flow.qualified_concept_review'),
        'ths_quality: dated qualified view, declared member denominator and bounded snapshot age', '题材成员与资金聚合；保留快照不证明历史当时可得'),
    'limit_pool': Product('limit_pool', ('official_limit_pool', 'xdf_limit_pool', 'l2_realtime_all_boards'), ('v_limit_pool',),
        ('trade_system.v2.market_workspace.limit_facts', 'trade_system.review_queries._concept_limit_up_review'),
        'dated native boards; empty only with qualified complete receipt', '涨停池、连板高度与复盘；不是可执行候选'),
    'stock_flow': Product('stock_flow', ('multi_source_stock_flow',), ('multi_source_stock_flow',),
        ('trade_system.readiness.flow_observation_capability', 'trade_system.concept_flow.qualified_concept_review'),
        'flow_contract + readiness: original origin, definition, units, dated scope and TTL', '单产品资金观察；独立确认另验'),
    'sector_flow': Product('sector_flow', ('multi_source_sector_flow',), ('multi_source_sector_flow',),
        ('trade_system.readiness.flow_observation_capability', 'trade_system.capital_flow_health.assess_capital_flow_health'),
        'flow_contract: taxonomy and denominator cannot cross products', '板块资金观察与覆盖核验'),
    'observation_quote': Product('observation_quote', ('executable_quote_snapshot', 'multi_source_quote'),
        ('retained_source_quote_observation_not_order_or_account_quote',),
        ('trade_system.v2.observation_workspace.project',),
        'observation_workspace.qualify: event time, receipt time and TTL before source rank', '观察条件跟踪；旧表名不授予执行资格'),
    'auction_observation': Product('auction', ('auction_tick', 'auction_quote_snapshot'), ('v_auction_evidence',),
        ('trade_system.readiness.assess_trade_date_readiness',),
        'auction_evidence: bounded dated tick/order-book products remain distinct', '竞价观察窗口核验'),
    'auction_match': Product('auction_match', ('multi_source_observation', 'auction_evidence_snapshot'), ('v_auction_evidence',),
        ('trade_system.auction_evidence.build_auction_evidence_status',),
        'dated final matched result; never pre-open tick evidence', '盘后竞价复盘'),
    'index': Product('index', ('index_kline',), ('index_kline',),
        ('trade_system.readiness.assess_trade_date_readiness',), 'requested session equals source session', '指数背景与数据资格核验'),
    'lhb': Product('lhb', ('lhb_list',), ('lhb_list',),
        ('trade_system.daily_review.build_daily_review_context',), 'disclosure date and provider retained', '龙虎榜复盘补充，不阻断基础市场发布'),
    'valuation': Product(None, ('tushare_daily_basic', 'valuation_intake'), ('valuation_capabilities',),
        ('trade_system.review_queries._valuation_capability_review',),
        'valuation_capabilities: security/field/session qualification; unknown stays unknown', '分证券估值用途，不承诺全市场完整'),
    'adjustment': Product('kline', ('tushare_adj_factor',), ('qualified_price_window',),
        ('trade_system.tushare_history.TushareHistoryCollector',),
        'positive dated factors; raw and adjusted identities remain separate', '历史价格核对与隔离研究，不触发活动预测'),
    'chips': Product('chips', ('xdf_cyq_chips',), (), (), 'dated provider receipt, bounded scope',
        '有界历史补充；当前工作台未证明消费，不据此自动扩采'),
    'margin': Product('margin', ('xdf_margin_summary',), (), (), 'source publication date, not requested date',
        '有界披露补充；当前工作台未证明消费'),
    'disclosures': Product(None, ('xdf_share_float', 'xdf_hk_hold', 'xdf_stk_premarket', 'xdf_kpl_list'), (), (),
        'per-product source date, disclosure availability and explicit scope', '已有有界采集保留；消费未证实，不能将入库量当发布量'),
    'review_supplement': Product(None, ('collector:scripts/collect_review_supplement.py',), ('review_context',),
        ('trade_system.daily_review.build_daily_review_context',), 'dated qualified optional observations', '历史预览复盘补充'),
    'l2_observation': Product(None, ('l2_stock_intraday',), (), (), 'dated bounded price curves, no full-market claim',
        '已有盘中有界采集保留；当前工作台消费尚未证实'),
    'northbound': Product(None, ('multi_source_observation',), (), (), 'provider definition and entitlement unverified',
        '历史及显式有界诊断；未证实用途，不宣称真实北向流量'),
    'financial_history': Product(None, ('collector:scripts/collect_multisource.py',), (), (),
        'announcement time and source date; bounded explicit history only', '历史恢复材料；不构成当时可得或完整估值'),
    'quality': Product(None, ('qualified_source_facts',), ('operational_capabilities', 'strict_financial_certification'),
        ('trade_system.collection_profiles.operational_readiness', 'trade_system.p0_observation.build_financial_certification'),
        'separate factual uses, strict certification and publication', '分用途判断及问题清单，不产生执行权限'),
    'normalization': Product(None, ('source_separated_facts',), ('canonical_read_only_views',),
        ('trade_system.v2.market_workspace.project',), 'source/adjustment/units preserved', '只读投影，避免重复物理拷贝'),
    'stock_reference': Product(None, ('tushare_stock_basic', 'multi_source_observation'), ('dated_security_scope',),
        ('trade_system.readiness.qualified_stock_flow_scope',), 'ths_quality.qualified_stock_reference', '证券身份及覆盖分母'),
}

# One task may produce several products. Task names remain transport identities;
# no row count is multiplied across these declared business edges.
TASK_PRODUCTS = {
    'build_auction_evidence': ('auction_observation',),
    'collect_market_context': ('market_context',),
    'collect_realtime_limit_pool': ('limit_pool',),
    'collect_auction_evidence': ('auction_observation',),
    'collect_intraday_stock_flow_market': ('stock_flow',),
    'collect_executable_quotes': ('observation_quote',),
    'collect_l2_focus': ('l2_observation',),
    'collect_intraday_sector_flow_full': ('sector_flow',),
    'sync_tushare_close': ('daily_price', 'valuation', 'adjustment', 'stock_flow', 'sector_flow'),
    'prepare_valuation_session': ('valuation',),
    'collect_hithink_limit_pool_daily': ('limit_pool',),
    'collect_kpl_stock_flow_focus': ('stock_flow',),
    'collect_review_supplement': ('review_supplement',),
    'collect_auction_market_daily': ('auction_match',),
    'collect_lhb_daily': ('lhb',),
    'collect_index_kline_daily': ('index',),
    'collect_xiaodefa_critical': ('chips', 'margin'),
    'backfill_2026_tushare': ('daily_price', 'valuation', 'adjustment', 'stock_flow', 'sector_flow'),
    'backfill_2026_ths_concepts': ('concept_membership',),
    'collect_history_supplement': ('financial_history', 'margin', 'lhb', 'northbound'),
    'collect_ths_concepts_api': ('concept_membership',),
    'collect_advanced_lhb_daily': ('lhb',),
    'collect_northbound_daily': ('northbound',),
    'collect_xiaodefa': ('chips', 'margin', 'disclosures'),
    'derive_market_context': ('market_context',),
    'build_normalized_views': ('normalization',),
    'reconcile_independent_stock_flow': ('quality',),
    'audit_multisource_readiness': ('quality',),
    'check_capital_flow_health': ('quality',),
    'check_data_readiness': ('quality',),
    'build_data_catalog': ('quality',),
    'audit_data_quality': ('quality',),
    'prepare_stock_reference': ('stock_reference',),
}


def task_contract(name):
    ids = TASK_PRODUCTS[name]  # No generic consumer fallback for a new task.
    return {'version': VERSION, 'task_id': name, 'data_product_ids': list(ids),
            'consumers': sorted({c for key in ids for c in PRODUCTS[key].consumers}),
            'products': {key: asdict(PRODUCTS[key]) for key in ids},
            'evidence_scope': 'declared_edges_not_observed_consumption'}


def catalogue():
    for product in PRODUCTS.values():
        if product.policy is not None and product.policy not in SOURCE_POLICIES:
            raise ValueError('unknown source authority policy')
        if not product.consumers and not product.retention_reason:
            raise ValueError('product requires a consumer or explicit retention reason')
    return {'version': VERSION, 'scope': 'declared_not_deployed_or_entitled',
            'tasks': [task_contract(n) for n in sorted(TASK_PRODUCTS)],
            'products': {k: asdict(v) for k, v in PRODUCTS.items()}, 'execution_ready': False}


def contract_hash():
    return hashlib.sha256(json.dumps(catalogue(), sort_keys=True, ensure_ascii=False,
                                    separators=(',', ':')).encode()).hexdigest()


def market_consumption(market):
    """Measure fields actually projected into this bundle, not acquired rows.

    Hashes bind displayed values and qualification together. They do not certify
    which HTTP attempt supplied a row; that link stays explicitly unverified.
    """
    groups = {
        'daily_price': ('stocks', 'breadth', 'matched_previous', 'turnover'),
        'concept_membership': ('themes', 'membership_contract'),
        'limit_pool': ('limit_pool', 'previous_limit_queue'),
        'market_context': ('market_totals',),
        'stock_flow': ('concept_flow',),
        'valuation': ('valuation_capabilities',),
        'quality': ('operational_capabilities', 'collection_diagnostics'),
    }
    receipts = []
    for product_id, fields in groups.items():
        actual = {key: market[key] for key in fields if key in market}
        if not actual:
            continue
        raw = json.dumps(actual, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
        receipts.append({'product_id': product_id, 'consumer': 'trade_system.v2.market_workspace.project',
                         'fields': sorted(actual), 'value_sha256': hashlib.sha256(raw.encode()).hexdigest(),
                         'scope': 'projected_values_including_explicit_unknowns_not_qualified_source_row_count',
                         'source_request_link': 'unverified', 'source_rows_published': None})
    return {'schema': 'market_consumption_v1', 'contract_sha256': contract_hash(),
            'session': market['trade_date'], 'as_of': market['as_of'], 'receipts': receipts,
            'execution_ready': False}


def read_publication_consumption(root):
    """Read only the verified current bundle; never scan stale run folders."""
    from .v2.publisher import read_current
    manifest, files = read_current(root)
    data = json.loads(files['desk.json'])
    market = data.get('market') or {}
    receipt = market.get('product_consumption')
    base = {'publication_run_id': manifest['run_id'], 'generation': manifest['generation'],
            'session': market.get('trade_date'), 'artifact_sha256': manifest['artifacts']['desk.json']}
    if receipt is None:
        return dict(base, status='unmeasured_historical_bundle', receipts=[])
    expected = market_consumption(market)
    # A historical declaration hash is not rewritten to today's catalogue.
    expected['contract_sha256'] = receipt.get('contract_sha256')
    if receipt != expected or manifest.get('product_consumption') != receipt:
        raise ValueError('publication consumption differs from its verified inline values')
    return dict(base, status='verified_inline_consumption',
                current_contract=receipt['contract_sha256'] == contract_hash(), **receipt)


def render_markdown():
    lines = ['# 采集产品与消费合同', '',
             '由 `trade_system.business_contract` 生成。声明关系不等于实际调用、供应商授权、完整数据或生产部署。',
             '来源顺序只由 `source_authority` 管理；没有消费证据的产品保留未知，禁止根据名称自动扩采或删除。', '',
             '| 任务 | 数据产品 | 具体消费端 |', '|---|---|---|']
    for task in catalogue()['tasks']:
        lines.append('| ' + task['task_id'] + ' | ' + ', '.join(task['data_product_ids']) + ' | '
                     + ('<br>'.join(task['consumers']) or '未证实；见产品保留理由') + ' |')
    lines += ['', '| 产品 | 原始关系/材料 | 规范投影 | 资格与边界 | 保留理由 |', '|---|---|---|---|---|']
    for key, p in PRODUCTS.items():
        lines.append(f"| {key} | {', '.join(p.raw)} | {', '.join(p.projection) or '无当前发布投影'} | {p.qualification} | {p.retention_reason} |")
    return '\n'.join(lines) + '\n'
