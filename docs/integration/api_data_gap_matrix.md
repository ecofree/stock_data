# API 数据能力与缺口审计

## 审计范围

- 交易日: `2026-08-10`
- 样本股票: `600721`
- 样本板块: `801008`
- 是否真实探测 API: `true`
- 安全约束: 报告只记录 endpoint、参数名/样本、状态、字段数量和本地行数，不输出 API key 或原始大段数据。

## 结论摘要

- `api_available`: 31
- `api_reachable_empty`: 6
- `derived_available`: 1
- `external_loaded`: 1
- `local_available`: 1
- `local_missing`: 2

## 职业操盘关键缺口

- `ladder_market` (limit_pool): api_reachable_empty; table_rows=241; API access method appears reachable, but this probe returned empty data; local table already has rows, so coverage may be date/session/sample dependent.
- `sector_stocks` (sector_theme): api_reachable_empty; table_rows=181; API access method appears reachable, but this probe returned empty data; local table already has rows, so coverage may be date/session/sample dependent.
- `sector_capital` (sector_theme): api_reachable_empty; table_rows=16210; API access method appears reachable, but this probe returned empty data; local table already has rows, so coverage may be date/session/sample dependent.

## 可由当前 API 获取且本次探测有数据

| Domain | Data | Endpoint/Source | Probe | API Count | Table Rows | Use | Note |
| --- | --- | --- | --- | --- | --- | --- | --- |
| market_state | daily_summary | /daily | ok | 1 | 275 | daily market breadth and limit-up/down context | API probe returned structured data. |
| market_state | daily_sentiment | /daily/sentiment | ok | 1 | 251 | sentiment-cycle scoring | API probe returned structured data. |
| market_state | daily_new_high | /daily/new-high | ok | 1 | 251 | new-high breadth and risk appetite | API probe returned structured data. |
| market_state | daily_export | /daily/export | ok | 1 | 3 | raw daily export snapshot | API probe returned structured data. |
| market_state | market_rise_fall | /market/rise-fall | ok | 1 | 275 | rise/fall distribution and limit-pool pressure | API probe returned structured data. |
| market_state | market_mood | /market/mood | ok | 1 | 22 | current market mood fallback | API probe returned structured data. |
| market_state | market_limit_up_down | /market/limit-up-down | ok | 250 | 1000 | limit-up/down stock pool | API probe returned structured data. |
| limit_pool | ladder_realtime_boards | /ladder/realtime-boards | ok | 87 | 406 | intraday board formation | API probe returned structured data. |
| limit_pool | l2_realtime_all_boards | /l2/realtime/all-boards | ok | 1 | 23955 | realtime board pool for intraday monitoring | API probe returned structured data. |
| sector_theme | sector_plates | /sector/plates | ok | 58 | 58 | sector universe | API probe returned structured data. |
| sector_theme | sector_ranking | /sector/ranking | ok | 50 | 43 | theme/mainline ranking | API probe returned structured data. |
| sector_theme | sector_strength | /sector/strength | ok | 1 | 129 | sector strength and seal-rate evidence | API probe returned structured data. |
| sector_theme | sector_intraday | /l2/sector-intraday | ok | 241 | 4667 | sector intraday strength curve | API probe returned structured data. |
| sector_theme | theme_hot | /theme/hot | ok | 13 | 39 | hot theme fallback | API probe returned structured data. |
| auction | advanced_morning_bidding_summary | /advanced/morning-bidding-summary | ok | 8 | 12 | auction-market aggregate strength | API probe returned structured data. |
| auction | advanced_morning_bidding_list | /advanced/morning-bidding-list | ok | 1 | 0 | stock-level auction pool | API probe returned structured data. |
| auction | auction_tick | /auction/tick | ok | 1 | 120917 | 09:15-09:25 auction confirmation | API probe returned structured data. |
| auction | auction_bidding_anomaly | /auction/bidding-anomaly | ok | 3 | 28 | auction anomaly and large-order confirmation | API probe returned structured data. |
| kline | stock_kline | /kline | ok | 5 | 804055 | candidate filters and staged backtest | API probe returned structured data. |
| index | l2_realtime_index_list | /l2/realtime/index-list | ok | 4 | 16 | index intraday risk context | API probe returned structured data. |
| l2_orderflow | advanced_main_monitor | /advanced/main-monitor | ok | 30 | 145 | main-fund monitor evidence for intraday strength | API probe returned structured data. |
| l2_orderflow | advanced_zjmm_min | /advanced/zjmm-min | ok | 242 | 69996 | minute-level main-money flow | API probe returned structured data. |
| l2_orderflow | advanced_dadan_kline | /advanced/dadan-kline | ok | 850 | 115 | large-order K-line evidence | API probe returned structured data. |
| l2_orderflow | advanced_main_activity_kline | /advanced/main-activity-kline | ok | 600 | 117 | main-activity K-line evidence | API probe returned structured data. |
| l2_orderflow | advanced_pankou | /advanced/pankou | ok | 1 | 130 | 盘口辅助证据 | API probe returned structured data. |
| dragon_tiger | lhb_detail | /lhb/detail | ok | 1 | 71711 | stock-level LHB attribution | API probe returned structured data. |
| news_research | advanced_news_flash | /advanced/news-flash | ok | 2 | 6 | intraday catalyst radar | API probe returned structured data. |
| news_research | news_columns | /news/columns | ok | 1 | 30 | news taxonomy | API probe returned structured data. |
| news_research | news_theme | /news/theme | ok | 1 | 0 | theme/news catalyst pool | API probe returned structured data. |
| stock_profile | stock_company_info | /stock/company-info | ok | 3 | 119 | F10/company profile | API probe returned structured data. |
| stock_profile | stock_institutional_positions | /stock/institutional-positions | ok | 1 | 1133 | institutional holding context | API probe returned structured data. |

## API 接入方式可达但本次返回空

| Domain | Data | Endpoint/Source | Probe | API Count | Table Rows | Use | Note |
| --- | --- | --- | --- | --- | --- | --- | --- |
| limit_pool | ladder_market | /ladder/market | ok | 0 | 241 | board ladder and height | API access method appears reachable, but this probe returned empty data; local table already has rows, so coverage may be date/session/sample dependent. |
| sector_theme | sector_stocks | /sector/stocks | ok | 0 | 181 | constituents for candidate expansion | API access method appears reachable, but this probe returned empty data; local table already has rows, so coverage may be date/session/sample dependent. |
| sector_theme | sector_capital | /sector/capital | ok | 0 | 16210 | sector money flow and mainline confirmation | API access method appears reachable, but this probe returned empty data; local table already has rows, so coverage may be date/session/sample dependent. |
| l2_orderflow | l2_stock_intraday | /l2/stock-intraday | ok | 0 | 185689 | intraday price/volume/main-fund curve | API access method appears reachable, but this probe returned empty data; local table already has rows, so coverage may be date/session/sample dependent. |
| l2_orderflow | l2_stock_bigorder | /l2/stock-bigorder | ok | 0 | 2524 | big-order confirmation | API access method appears reachable, but this probe returned empty data; local table already has rows, so coverage may be date/session/sample dependent. |
| dragon_tiger | lhb_list | /lhb/list | ok | 0 | 1925 | hot-money and post-trade review | API access method appears reachable, but this probe returned empty data; local table already has rows, so coverage may be date/session/sample dependent. |

## API 错误或未配置

| Domain | Data | Endpoint/Source | Probe | API Count | Table Rows | Use | Note |
| --- | --- | --- | --- | --- | --- | --- | --- |

## 非 API/派生/本地/外部文件数据

| Domain | Data | Endpoint/Source | Probe | API Count | Table Rows | Use | Note |
| --- | --- | --- | --- | --- | --- | --- | --- |
| index | index_kline | derived from /daily raw_json | not_probed |  | 2612 | index trend filter for market-state risk | Data exists locally but is derived/fallback, not a dedicated API feed. |
| research_layer | news_radar_item | local Vibe-style news radar import | not_probed |  | 35 | catalyst tagging and AI review context | Local/manual/research data exists; not a direct API feed. |
| research_layer | research_note | manual/local research-note table | not_probed |  | 0 | operator research notes and review memory | Local/manual/research data is missing; not a direct API feed. |
| research_layer | research_report_file | manual/local research-report registry | not_probed |  | 0 | report and announcement management | Local/manual/research data is missing; not a direct API feed. |
| ml_shadow | qlib_prediction | external CSV via scripts/import_qlib_shadow_predictions.py | not_probed |  | 10075 | shadow-mode factor/model validation | External/shadow data is loaded locally. |

## 全量数据矩阵

| Domain | Name | Type | Endpoint | Params | Table | Rows | Verdict | Access Method |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| market_state | daily_summary | api | /daily | date=2026-08-10 | daily_summary | 275 | api_available | GET /daily?date=<YYYY-MM-DD> |
| market_state | daily_sentiment | api | /daily/sentiment | date=2026-08-10 | daily_sentiment | 251 | api_available | GET /daily/sentiment?date=<YYYY-MM-DD> |
| market_state | daily_new_high | api | /daily/new-high | date=2026-08-10 | daily_new_high | 251 | api_available | GET /daily/new-high?date=<YYYY-MM-DD> |
| market_state | daily_export | api | /daily/export | date=2026-08-10 | daily_export | 3 | api_available | GET /daily/export?date=<YYYY-MM-DD> |
| market_state | market_rise_fall | api | /market/rise-fall | date=2026-08-10 | market_rise_fall | 275 | api_available | GET /market/rise-fall?date=<YYYY-MM-DD> |
| market_state | market_mood | api | /market/mood |  | market_mood | 22 | api_available | GET /market/mood |
| market_state | market_limit_up_down | api | /market/limit-up-down |  | market_limit_up_down | 1000 | api_available | GET /market/limit-up-down |
| limit_pool | ladder_market | api | /ladder/market | date=2026-08-10 | ladder_market | 241 | api_reachable_empty | GET /ladder/market?date=<YYYY-MM-DD> |
| limit_pool | ladder_realtime_boards | api | /ladder/realtime-boards |  | ladder_realtime_boards | 406 | api_available | GET /ladder/realtime-boards |
| limit_pool | l2_realtime_all_boards | api | /l2/realtime/all-boards |  | l2_realtime_all_boards | 23955 | api_available | GET /l2/realtime/all-boards |
| sector_theme | sector_plates | api | /sector/plates |  | sector_plates | 58 | api_available | GET /sector/plates |
| sector_theme | sector_ranking | api | /sector/ranking | date=2026-08-10 | sector_ranking | 43 | api_available | GET /sector/ranking?date=<YYYY-MM-DD> |
| sector_theme | sector_strength | api | /sector/strength | code=801008&date=2026-08-10 | sector_strength | 129 | api_available | GET /sector/strength?code=<sector_code>&date=<YYYY-MM-DD> |
| sector_theme | sector_stocks | api | /sector/stocks | code=801008&date=2026-08-10 | sector_stocks | 181 | api_reachable_empty | GET /sector/stocks?code=<sector_code>&date=<YYYY-MM-DD> |
| sector_theme | sector_capital | api | /sector/capital | code=801008&date=2026-08-10 | sector_capital | 16210 | api_reachable_empty | GET /sector/capital?code=<sector_code>&date=<YYYY-MM-DD> |
| sector_theme | sector_intraday | api | /l2/sector-intraday | code=801008&date=2026-08-10 | l2_sector_intraday | 4667 | api_available | GET /l2/sector-intraday?code=<sector_code>&date=<YYYY-MM-DD> |
| sector_theme | theme_hot | api | /theme/hot |  | theme_hot | 39 | api_available | GET /theme/hot |
| auction | advanced_morning_bidding_summary | api | /advanced/morning-bidding-summary |  | advanced_morning_bidding_summary | 12 | api_available | GET /advanced/morning-bidding-summary |
| auction | advanced_morning_bidding_list | api | /advanced/morning-bidding-list |  | advanced_morning_bidding_list | 0 | api_available | GET /advanced/morning-bidding-list |
| auction | auction_tick | api | /auction/tick | code=600721&date=2026-08-10 | auction_tick | 120917 | api_available | GET /auction/tick?code=<stock_code>&date=<YYYY-MM-DD> |
| auction | auction_bidding_anomaly | api | /auction/bidding-anomaly | code=600721&date=2026-08-10 | auction_bidding_anomaly | 28 | api_available | GET /auction/bidding-anomaly?code=<stock_code>&date=<YYYY-MM-DD> |
| kline | stock_kline | api | /kline | code=600721&ktype=d&count=5 | kline | 804055 | api_available | GET /kline?code=<stock_code>&ktype=d&count=5 |
| index | l2_realtime_index_list | api | /l2/realtime/index-list |  | l2_realtime_index_list | 16 | api_available | GET /l2/realtime/index-list |
| index | index_kline | derived |  |  | index_kline | 2612 | derived_available | derived from /daily raw_json |
| l2_orderflow | l2_stock_intraday | api | /l2/stock-intraday | code=600721&date=2026-08-10 | l2_stock_intraday | 185689 | api_reachable_empty | GET /l2/stock-intraday?code=<stock_code>&date=<YYYY-MM-DD> |
| l2_orderflow | l2_stock_bigorder | api | /l2/stock-bigorder | code=600721&date=2026-08-10 | l2_stock_bigorder | 2524 | api_reachable_empty | GET /l2/stock-bigorder?code=<stock_code>&date=<YYYY-MM-DD> |
| l2_orderflow | advanced_main_monitor | api | /advanced/main-monitor | code=600721 | advanced_main_monitor | 145 | api_available | GET /advanced/main-monitor?code=<stock_code> |
| l2_orderflow | advanced_zjmm_min | api | /advanced/zjmm-min | code=600721&date=2026-08-10 | advanced_zjmm_min | 69996 | api_available | GET /advanced/zjmm-min?code=<stock_code>&date=<YYYY-MM-DD> |
| l2_orderflow | advanced_dadan_kline | api | /advanced/dadan-kline | code=600721 | advanced_dadan_kline | 115 | api_available | GET /advanced/dadan-kline?code=<stock_code> |
| l2_orderflow | advanced_main_activity_kline | api | /advanced/main-activity-kline | code=600721 | advanced_main_activity_kline | 117 | api_available | GET /advanced/main-activity-kline?code=<stock_code> |
| l2_orderflow | advanced_pankou | api | /advanced/pankou | code=600721 | advanced_pankou | 130 | api_available | GET /advanced/pankou?code=<stock_code> |
| dragon_tiger | lhb_list | api | /lhb/list | date=2026-08-10 | lhb_list | 1925 | api_reachable_empty | GET /lhb/list?date=<YYYY-MM-DD> |
| dragon_tiger | lhb_detail | api | /lhb/detail | code=600721&date=2026-08-10 | lhb_detail | 71711 | api_available | GET /lhb/detail?code=<stock_code>&date=<YYYY-MM-DD> |
| news_research | advanced_news_flash | api | /advanced/news-flash |  | advanced_news_flash | 6 | api_available | GET /advanced/news-flash |
| news_research | news_columns | api | /news/columns |  | news_columns | 30 | api_available | GET /news/columns |
| news_research | news_theme | api | /news/theme | type=-1&index=0&page_size=20 | news_theme | 0 | api_available | GET /news/theme?type=-1&index=0&page_size=20 |
| stock_profile | stock_company_info | api | /stock/company-info | code=600721 | stock_company_info | 119 | api_available | GET /stock/company-info?code=<stock_code> |
| stock_profile | stock_institutional_positions | api | /stock/institutional-positions | code=600721 | stock_institutional_positions | 1133 | api_available | GET /stock/institutional-positions?code=<stock_code> |
| research_layer | news_radar_item | local |  |  | news_radar_item | 35 | local_available | local Vibe-style news radar import |
| research_layer | research_note | local |  |  | research_note | 0 | local_missing | manual/local research-note table |
| research_layer | research_report_file | local |  |  | research_report_file | 0 | local_missing | manual/local research-report registry |
| ml_shadow | qlib_prediction | external_file |  |  | qlib_prediction | 10075 | external_loaded | external CSV via scripts/import_qlib_shadow_predictions.py |

## 操盘视角判断

- `auction_tick` 若持续为 `api_reachable_empty`，不能伪造逐笔竞价，只能把竞价异动、早盘竞价汇总、L2 与盘口作为弱替代证据。
- `index_kline` 当前是 `/daily` 派生 fallback，能服务市场方向过滤，但不能做严格指数 OHLC 回测。
- 新闻、研究记录、研报、qlib shadow 不是当前 KPL API 的直接数据，应通过本地导入/适配器进入，不应混入主交易信号。
- 候选股四阶段信号要优先依赖已验证有数的 K 线、板块强度/资金、涨停梯队、L2/大单和竞价异常；缺口数据必须在信号证据字段里明确降级。
