# Phase 11 外部能力矩阵

能力矩阵用于决定外部项目能力如何进入 `stock_data`。`port` 表示迁移思想或 adapter；`reference` 表示只参考；`forbid` 表示禁止迁入。

| Capability | Project | Type | Decision | Exists | Target adapter | Blocked reason |
|---|---|---|---|---:|---|---|
| `kpl_qds.legacy_data` | kpl_qds | data | `port` | `True` | `trade_system.integration.legacy_a_share` |  |
| `kpl_qds.signal_fusion` | kpl_qds | signal | `port` | `True` | `trade_system.signals evidence scoring` |  |
| `kpl_qds.risk_enforcer` | kpl_qds | risk | `port` | `True` | `trade_system.operator_risk` |  |
| `kpl_qds.auction_analyzer` | kpl_qds | auction | `reference` | `True` | `trade_system.auction_deep` |  |
| `kpl_qds.performance_tracker` | kpl_qds | review | `port` | `True` | `trade_system.review attribution` |  |
| `kpl_qds.qmt_bridge` | kpl_qds | execution | `forbid` | `True` | `` | 自动交易/下单路径禁止进入 stock_data。 |
| `kpl_qds.hardcoded_settings` | kpl_qds | secret | `forbid` | `True` | `` | 硬编码 key 或敏感配置禁止迁入，审计只记录文件存在，不读取内容。 |
| `kpl_qds.qlib_shadow` | kpl_qds | ml_shadow | `reference` | `True` | `trade_system.ml.qlib_shadow` | 当前不迁移重型 qlib 训练/二进制数据。 |
| `tickflow.strategy_engine` | tickflow | strategy | `port` | `True` | `trade_system.strategy` |  |
| `tickflow.indicator_pipeline` | tickflow | indicator | `port` | `True` | `trade_system.strategy.indicator_pipeline` |  |
| `tickflow.backtest_engine` | tickflow | backtest | `port` | `True` | `trade_system.backtest.stage_backtest` |  |
| `tickflow.monitor_rules` | tickflow | monitor | `reference` | `True` | `trade_system.operator_risk alert rules` |  |
| `tickflow.custom_data_source` | tickflow | adapter | `port` | `True` | `trade_system.integration.data_catalog` |  |
| `tickflow.react_workbench` | tickflow | ui | `reference` | `True` | `reports/trading_dashboard_latest.html` |  |
| `vibe.news_radar` | vibe | research | `port` | `True` | `trade_system.research.news_radar` |  |
| `vibe.a_stock_data` | vibe | data | `port` | `True` | `trade_system.adapters.vibe_research` |  |
| `vibe.research_records` | vibe | review | `port` | `True` | `trade_system.research.report_registry` |  |
| `vibe.portfolio_review` | vibe | review | `reference` | `True` | `trade_system.review positions` |  |
| `vibe.mcp_tools` | vibe | tooling | `reference` | `True` | `future local tools` |  |
| `vibe.fastapi_app` | vibe | service | `reference` | `True` | `none` |  |
