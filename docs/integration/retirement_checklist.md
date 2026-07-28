# 旧项目淘汰检查清单

本清单只判断删除准备度，不执行删除。`delete_safe=false` 表示仍需人工确认或归档备份。

| Project | Capability | Migrated to | Verified | delete_safe | Remaining dependency | Notes |
|---|---|---|---:|---:|---|---|
| kpl_qds | legacy_data_and_rules | legacy_qds_* tables; trade_system.integration; operator views | `True` | `False` | 确认 stock_data 不再需要直接读取 kpl-qds 路径；删除前需备份 legacy DB。 | source_exists=True; QMT 自动交易路径禁止迁入。 |
| tickflow | strategy_backtest_monitor_reference | trade_system.strategy; strategy_scan_result; strategy_backtest_result | `True` | `False` | 确认策略协议、回测约束和监控思想已满足；React 工作台仅参考。 | source_exists=True; 不迁移整套 FastAPI/React。 |
| vibe | research_news_reports_records | trade_system.research; news_radar_item; research_note; research_report_file | `True` | `False` | 确认新闻源、研报/公告和研究记录满足复盘需求；AI chat 不作为交易信号。 | source_exists=True; global-stock-data 只作为外围参考。 |

## 删除前必须满足

- `stock_data` 一键日跑不依赖旧项目路径。
- 所有迁移能力都有测试和报告。
- 没有自动下单入口。
- 没有硬编码 key 或敏感配置进入主项目。
- 用户手动确认备份和删除范围。
