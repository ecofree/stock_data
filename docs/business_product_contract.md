# 采集产品与消费合同

由 `trade_system.business_contract` 生成。声明关系不等于实际调用、供应商授权、完整数据或生产部署。
来源顺序只由 `source_authority` 管理；没有消费证据的产品保留未知，禁止根据名称自动扩采或删除。

| 任务 | 数据产品 | 具体消费端 |
|---|---|---|
| audit_data_quality | quality | trade_system.collection_profiles.operational_readiness<br>trade_system.p0_observation.build_financial_certification |
| audit_multisource_readiness | quality | trade_system.collection_profiles.operational_readiness<br>trade_system.p0_observation.build_financial_certification |
| backfill_2026_ths_concepts | concept_membership | trade_system.concept_flow.qualified_concept_review<br>trade_system.v2.market_workspace.theme_facts |
| backfill_2026_tushare | daily_price, valuation, adjustment, stock_flow, sector_flow | trade_system.capital_flow_health.assess_capital_flow_health<br>trade_system.concept_flow.qualified_concept_review<br>trade_system.readiness.flow_observation_capability<br>trade_system.readiness.market_view_capability<br>trade_system.review_queries._valuation_capability_review<br>trade_system.tushare_history.TushareHistoryCollector<br>trade_system.v2.market_workspace.project |
| build_auction_evidence | auction_observation | trade_system.readiness.assess_trade_date_readiness |
| build_data_catalog | quality | trade_system.collection_profiles.operational_readiness<br>trade_system.p0_observation.build_financial_certification |
| build_normalized_views | normalization | trade_system.v2.market_workspace.project |
| check_capital_flow_health | quality | trade_system.collection_profiles.operational_readiness<br>trade_system.p0_observation.build_financial_certification |
| check_data_readiness | quality | trade_system.collection_profiles.operational_readiness<br>trade_system.p0_observation.build_financial_certification |
| collect_advanced_lhb_daily | lhb | trade_system.daily_review.build_daily_review_context |
| collect_auction_evidence | auction_observation | trade_system.readiness.assess_trade_date_readiness |
| collect_auction_market_daily | auction_match | trade_system.auction_evidence.build_auction_evidence_status |
| collect_executable_quotes | observation_quote | trade_system.v2.observation_workspace.project |
| collect_history_supplement | financial_history, margin, lhb, northbound | trade_system.daily_review.build_daily_review_context |
| collect_hithink_limit_pool_daily | limit_pool | trade_system.review_queries._concept_limit_up_review<br>trade_system.v2.market_workspace.limit_facts |
| collect_index_kline_daily | index | trade_system.readiness.assess_trade_date_readiness |
| collect_intraday_sector_flow_full | sector_flow | trade_system.capital_flow_health.assess_capital_flow_health<br>trade_system.readiness.flow_observation_capability |
| collect_intraday_stock_flow_market | stock_flow | trade_system.concept_flow.qualified_concept_review<br>trade_system.readiness.flow_observation_capability |
| collect_kpl_stock_flow_focus | stock_flow | trade_system.concept_flow.qualified_concept_review<br>trade_system.readiness.flow_observation_capability |
| collect_l2_focus | l2_observation | 未证实；见产品保留理由 |
| collect_lhb_daily | lhb | trade_system.daily_review.build_daily_review_context |
| collect_market_context | market_context | trade_system.daily_review.build_daily_review_context<br>trade_system.v2.market_workspace.project |
| collect_northbound_daily | northbound | 未证实；见产品保留理由 |
| collect_realtime_limit_pool | limit_pool | trade_system.review_queries._concept_limit_up_review<br>trade_system.v2.market_workspace.limit_facts |
| collect_review_supplement | review_supplement | trade_system.daily_review.build_daily_review_context |
| collect_ths_concepts_api | concept_membership | trade_system.concept_flow.qualified_concept_review<br>trade_system.v2.market_workspace.theme_facts |
| collect_xiaodefa | chips, margin, disclosures | 未证实；见产品保留理由 |
| collect_xiaodefa_critical | chips, margin | 未证实；见产品保留理由 |
| derive_market_context | market_context | trade_system.daily_review.build_daily_review_context<br>trade_system.v2.market_workspace.project |
| prepare_stock_reference | stock_reference | trade_system.readiness.qualified_stock_flow_scope |
| prepare_valuation_session | valuation | trade_system.review_queries._valuation_capability_review |
| reconcile_independent_stock_flow | quality | trade_system.collection_profiles.operational_readiness<br>trade_system.p0_observation.build_financial_certification |
| sync_tushare_close | daily_price, valuation, adjustment, stock_flow, sector_flow | trade_system.capital_flow_health.assess_capital_flow_health<br>trade_system.concept_flow.qualified_concept_review<br>trade_system.readiness.flow_observation_capability<br>trade_system.readiness.market_view_capability<br>trade_system.review_queries._valuation_capability_review<br>trade_system.tushare_history.TushareHistoryCollector<br>trade_system.v2.market_workspace.project |

| 产品 | 原始关系/材料 | 规范投影 | 资格与边界 | 保留理由 |
|---|---|---|---|---|
| market_context | daily_summary | v_market_daily | market_contract.market_daily_status_sql: five dated observed counts with provenance | 市场背景与复盘；部分统计不代表全市场认证 |
| daily_price | tushare_daily, multi_source_kline | v_kline_daily | readiness.market_view_capability: security/date/provider/adjustment/units/clock | 价格事实、广度和同口径历史比较 |
| concept_membership | ths_concept_daily, ths_concept_stock_history | v_default_concept_daily, v_default_concept_stock_history | ths_quality: dated qualified view, declared member denominator and bounded snapshot age | 题材成员与资金聚合；保留快照不证明历史当时可得 |
| limit_pool | official_limit_pool, xdf_limit_pool, l2_realtime_all_boards | v_limit_pool | dated native boards; empty only with qualified complete receipt | 涨停池、连板高度与复盘；不是可执行候选 |
| stock_flow | multi_source_stock_flow | multi_source_stock_flow | flow_contract + readiness: original origin, definition, units, dated scope and TTL | 单产品资金观察；独立确认另验 |
| sector_flow | multi_source_sector_flow | multi_source_sector_flow | flow_contract: taxonomy and denominator cannot cross products | 板块资金观察与覆盖核验 |
| observation_quote | executable_quote_snapshot, multi_source_quote | retained_source_quote_observation_not_order_or_account_quote | observation_workspace.qualify: event time, receipt time and TTL before source rank | 观察条件跟踪；旧表名不授予执行资格 |
| auction_observation | auction_tick, auction_quote_snapshot | v_auction_evidence | auction_evidence: bounded dated tick/order-book products remain distinct | 竞价观察窗口核验 |
| auction_match | multi_source_observation, auction_evidence_snapshot | v_auction_evidence | dated final matched result; never pre-open tick evidence | 盘后竞价复盘 |
| index | index_kline | index_kline | requested session equals source session | 指数背景与数据资格核验 |
| lhb | lhb_list | lhb_list | disclosure date and provider retained | 龙虎榜复盘补充，不阻断基础市场发布 |
| valuation | tushare_daily_basic, valuation_intake | valuation_capabilities | valuation_capabilities: security/field/session qualification; unknown stays unknown | 分证券估值用途，不承诺全市场完整 |
| adjustment | tushare_adj_factor | qualified_price_window | positive dated factors; raw and adjusted identities remain separate | 历史价格核对与隔离研究，不触发活动预测 |
| chips | xdf_cyq_chips | 无当前发布投影 | dated provider receipt, bounded scope | 有界历史补充；当前工作台未证明消费，不据此自动扩采 |
| margin | xdf_margin_summary | 无当前发布投影 | source publication date, not requested date | 有界披露补充；当前工作台未证明消费 |
| disclosures | xdf_share_float, xdf_hk_hold, xdf_stk_premarket, xdf_kpl_list | 无当前发布投影 | per-product source date, disclosure availability and explicit scope | 已有有界采集保留；消费未证实，不能将入库量当发布量 |
| review_supplement | collector:scripts/collect_review_supplement.py | review_context | dated qualified optional observations | 历史预览复盘补充 |
| l2_observation | l2_stock_intraday | 无当前发布投影 | dated bounded price curves, no full-market claim | 已有盘中有界采集保留；当前工作台消费尚未证实 |
| northbound | multi_source_observation | 无当前发布投影 | provider definition and entitlement unverified | 历史及显式有界诊断；未证实用途，不宣称真实北向流量 |
| financial_history | collector:scripts/collect_multisource.py | 无当前发布投影 | announcement time and source date; bounded explicit history only | 历史恢复材料；不构成当时可得或完整估值 |
| quality | qualified_source_facts | operational_capabilities, strict_financial_certification | separate factual uses, strict certification and publication | 分用途判断及问题清单，不产生执行权限 |
| normalization | source_separated_facts | canonical_read_only_views | source/adjustment/units preserved | 只读投影，避免重复物理拷贝 |
| stock_reference | tushare_stock_basic, multi_source_observation | dated_security_scope | ths_quality.qualified_stock_reference | 证券身份及覆盖分母 |
