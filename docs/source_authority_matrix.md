# 数据源权威矩阵

由 `trade_system.source_authority.render_policy_markdown` 生成。描述当前开发合同，不证明生产部署或供应商授权。资格先于来源排序；更新的抓取时间不能提升不合格备用数据。
同一合格产品按 HiThink 官方 > xiaodefa TuShare 中继 > 其他选择；不同产品不可互换。AKShare 仅末级不稳定兜底。

| 数据域 | 核心关系 | 主源 | 备用/核对源 | 写入责任 | 生命周期 | 说明 |
|---|---|---|---|---|---|---|
| `observation_quote` | `multi_source_quote` | hithink, hithink_quote, hithink_official | xiaodefa, tushare_relay, tencent_spot_quote, tencent, sina | `existing_quote_collectors_read_only_observation_consumer` | retained_data_product | Observation only. Source event time, receipt order and TTL must qualify before priority is applied; never an executable/account quote. |
| `market_state` | `daily_summary` | kpl_market | secondary_verified, derived_current, fallback | `derive_market_context.py` | retained_data_product | The source_kind/as_of gate remains authoritative. |
| `kline` | `v_kline_daily` | xiaodefa, tushare_daily, tushare | tushare_relay, baostock, pytdx, tencent, sina, kpl, existing_core, cache | `TushareHistoryCollector (raw facts); normalize (read-only projection)` | retained_data_product | xiaodefa is the only retained TuShare transport; historical relay identities remain readable. Normalized daily views read source facts without a second physical copy. |
| `ths_concept_daily` | `ths_concept_daily` | hithink_index_api | ths_official_api, ths_web, tushare_dc_member, browser_repair, kpl | `collect_ths_concepts_api.py` | retained_data_product | HiThink official is the primary concept catalogue. A partial snapshot must never replace a complete snapshot. |
| `stock_flow` | `multi_source_stock_flow` | xiaodefa_moneyflow_dc, eastmoney_intraday_clist, eastmoney_intraday_clist_delay | tushare, tushare_relay, kpl, sina, eastmoney_market, eastmoney, resilient, cache | `collect_intraday_stock_flow_market.py` | retained_data_product | Official flow API is not externally open. Dated DC relay is preferred after close; native realtime is intraday. Different origins may be compared, never summed; a shared relay is not independent transport. |
| `sector_flow` | `multi_source_sector_flow` | eastmoney_sector_full | tushare_sector_full, tushare, derived_ths_stock_aggregate, kpl, existing_core, eastmoney, cache | `collect_intraday_sector_flow_full.py` | retained_data_product | Derived THS rows retain their taxonomy and are not silently merged as raw EM rows. |
| `limit_pool` | `v_limit_pool` | hithink, xiaodefa | kpl, eastmoney, derived | `collect_realtime_limit_pool.py` | retained_data_product | Qualify native dated board heights before ranking: HiThink > xiaodefa > other. Derived local streaks are not interchangeable native board facts. |
| `auction` | `auction_tick` | kpl_auction_market | tencent_qt, auction_quote_snapshot, kpl_legacy | `collect_auction_market_daily.py` | retained_data_product | KPL observations and scoped ticks only. Tencent is an order-book snapshot, never trade ticks. TuShare matched results use auction_match; official final snapshots and anomalies are distinct products. |
| `auction_match` | `v_auction_evidence` | xiaodefa |  | `collect_auction_market_daily.py --product match` | retained_data_product | Retained TuShare stk_auction matched-result product. No interchangeable official product is configured here; official final snapshots, opening bars and pre-open ticks remain distinct. Source priority never substitutes a different product. |
| `index` | `index_kline` | kpl_index_kline | eastmoney_index, tushare_index, existing_core | `collect_index_kline_daily.py` | retained_data_product | Index source date must match the requested session; stale history cannot satisfy the close gate. |
| `lhb` | `lhb_list` | kpl_lhb | eastmoney_lhb, tushare_lhb, existing_core | `collect_lhb_daily.py` | retained_data_product | 龙虎榜 is review evidence and does not block the close publication gate. |
| `chips` | `xdf_cyq_chips` | xiaodefa | eastmoney_chip, akshare_chip | `collectors/xiaodefa.py` | retained_data_product | Chip distribution is a bounded research/review supplement; all-stock unbounded fetches are prohibited. |
| `margin` | `xdf_margin_summary` | xiaodefa | tushare_margin, eastmoney_margin | `collectors/xiaodefa.py` | retained_data_product | Margin data is supplemental and must retain its source date and provider. |
| `qlib` | `qlib_candidate_pool` | qlib_shadow | external_qlib_file | `none (historical artifacts are read-only)` | historical_read_only | Historical research evidence only. Active training, prediction refresh and promotion are retired. Environment validation does not grant research or execution readiness. |
| `operator_outcome` | `operator_trade_outcome` | operator_input |  | `none (legacy import permanently retired)` | historical_read_only | Historical records only. Legacy outcome import is permanently retired; no current write authority. |

## 规则与消费关系

1. 原始来源分开保留，规范视图不复制第二份事实；来源、复权、单位和时间必须保留。
2. 主源缺失不授权恢复退役入口；历史兼容读取不授予当前写入权。
3. 单产品观察与独立确认分开；共用原始来源或中继不能算独立证据。
4. [产品消费合同](business_product_contract.md)记录具体消费端及未证实消费的保留理由。
5. [当前业务边界](current_business_contract.md)是入口导航；实际运行仍须独立取证。
