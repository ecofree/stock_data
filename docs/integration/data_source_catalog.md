# 统一数据源目录

本目录定义 `stock_data` 后续可以接入的数据源。目录只描述来源、频率、权限、风险和启用状态，不保存密钥。

| Source | Domain | Provider | Frequency | Auth | Enabled | Risk | Notes |
|---|---|---|---|---|---:|---|---|
| `kpl_api` | auction | KPL | intraday/daily | env | `True` | medium | 主采集源；密钥只允许从环境读取，不写入报告。 |
| `legacy_qds` | sector_capital | local_duckdb | historical | none | `True` | low | 补充 watchlist、涨停、板块资金、龙虎榜和情绪样本。 |
| `stock_data_kline` | kline | duckdb | daily | none | `True` | medium | 服务分阶段回测；当前指数和样本历史仍需扩容。 |
| `tushare_relay_basic` | kline | tushare_relay | daily/historical | env | `True` | medium | 补 stock_basic、trade_cal、daily、daily_basic、adj_factor、index_daily；只作为基础数据和回测补源，不直接触发交易信号。 |
| `eastmoney_market_flow` | stock_capital_flow | eastmoney_market | intraday/close | public_web | `True` | high | Full-market paginated snapshot; one request per page and checkpointed. |
| `eastmoney_sector_flow` | sector_capital_flow | eastmoney | intraday/close | public_web | `True` | high | Direct sector flow where available; THS-derived fallback stays separately labelled. |
| `ths_web_concepts` | concept_membership | ths_web | daily_snapshot | public_web | `True` | high | Default concept source; snapshots are not historical unless date_verified. |
| `sina_kline_flow` | kline/stock_capital_flow | sina | daily/on_demand | public_web | `True` | high | Fallback only; provider_main_net is not mixed with main_orders_net without reconciliation. |
| `tencent_kline_quote` | kline/quote | tencent | intraday/daily | public_web | `True` | medium | Fallback quote/K-line source with cached response provenance. |
| `tickflow_custom` | strategy | adapter_contract | daily/intraday | env/header/query | `False` | medium | 先迁移字段映射和降级协议，不直接依赖 tickflow 服务。 |
| `vibe_a_stock_data` | research | public_web/adapters | daily/on_demand | none/env_optional | `False` | medium | 补公告、研报、热点原因、资金、龙虎榜、互动问答和新闻雷达。 |
| `qlib_shadow` | ml_shadow | external_file | batch | none | `False` | high | 只导入外部预测结果做旁路验证，默认不参与交易信号。 |
