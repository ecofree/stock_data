# 数据源权威矩阵

本文件是生产数据选择契约。项目保留多源数据用于容灾和交叉验证，但备用源不能因为 `fetched_at` 更新而自动升级为最终权威源。

| 数据域 | 核心关系 | 主源 | 备用/校验源 | 唯一晋级写入者 | 兼容用途 |
|---|---|---|---|---|---|
| market_state | `daily_summary` | `kpl_market` | secondary/derived/fallback | `derive_market_context.py` | `fetch_all.py` full 模式 |
| kline | `kline` | `xiaodefa`、`tushare_daily`、`tushare` | `tushare_relay`、BaoStock、pytdx、Tencent、Sina、KPL、cache | `sync_tushare_ohlc.py` / `TushareHistoryCollector` | 历史恢复 |
| ths_concept | `ths_concept_daily` | THS 官方 API | THS Web、TuShare、浏览器修复、KPL | `collect_ths_concepts_api.py` | 历史恢复 |
| stock_flow | `multi_source_stock_flow` | EastMoney 全市场 | TuShare、KPL、Sina、旧 EastMoney、resilient、cache | `collect_intraday_stock_flow_market.py` | 重点股补充/独立校验 |
| sector_flow | `multi_source_sector_flow` | EastMoney 全板块 | TuShare、THS 个股聚合、KPL、旧 EastMoney、cache | `collect_intraday_sector_flow_full.py` | 兼容恢复 |
| limit_pool | `v_limit_pool` | KPL/THS 专业链 | EastMoney、derived | `collect_realtime_limit_pool.py` | 兼容恢复 |
| auction | `auction_tick` | KPL `/auction/market` | Tencent order-book snapshot、旧 auction routes | `collect_auction_market_daily.py` | 旧逐股脚本仅兼容 |
| index | `index_kline` | KPL index K-line | EastMoney、TuShare、existing core | `collect_index_kline_daily.py` | 历史恢复 |
| lhb | `lhb_list` | KPL LHB | EastMoney、TuShare | `collect_lhb_daily.py` | 复盘补充，不阻塞收盘 |
| chips | `xdf_cyq_chips` | xiaodefa | EastMoney/AkShare | `trade_system/xiaodefa_source.py` | 有界重点股补采 |
| margin | `xdf_margin_summary` | xiaodefa | TuShare/EastMoney | `trade_system/xiaodefa_source.py` | 收盘后补充 |
| qlib | `qlib_candidate_pool` | QLib shadow | 外部 QLib 文件 | `scripts/run_qlib_research_daily.py` | 研究排序与后验评估 |
| operator_outcome | `operator_trade_outcome` | 人工复核输入 | - | `trade_system/operator_outcomes.py` | QLib 反馈，不是自动订单 |

## 强制规则

1. 原始数据按 provider 分开保存，不能把不同口径的数据直接求和。
2. 只有“唯一晋级写入者”可以把数据复制到核心关系。
3. 备用数据必须带 provider、来源接口、单位、口径和 fallback 标记。
4. 同一业务主键出现多个 provider 时，使用本矩阵选择，不使用最新 `fetched_at` 代替权威判断。
5. 旧脚本允许用于历史恢复和故障排查，但不能成为第二个定时生产写入者。
6. 独立源用于认证和对账，不代表可以直接覆盖主源或与主源相加。
7. QLib 只进入研究排序、候选融合和后验评估；没有 current-source 评估与人工结果反馈时，始终保持 `shadow`。

## 审计

执行以下只读检查可以发现同键多源记录和 THS 重复业务键：

```powershell
D:\anaconda\python.exe scripts\audit_source_conflicts.py --db kpl_data.duckdb
```

冲突不一定代表错误，但每个非零冲突都必须检查来源、单位、分类体系、`as_of` 日期和写入者。
