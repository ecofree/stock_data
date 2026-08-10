# Full API Endpoint Inventory

## Scope

- Date: `2026-08-10`
- Stock sample: `600721`
- Sector sample: `801008`
- Discovered endpoints: `177`
- Safety: no API key or raw response body is written to this report.

## Summary

- `api_available`: 46
- `api_error`: 5
- `needs_trading_session`: 3
- `param_uncertain`: 2
- `reachable_empty`: 35
- `stable_available`: 86

## Source Coverage

- `code`: 118
- `doc`: 140
- `schema`: 176

## Parameter Types

- `batch`: 2
- `code`: 57
- `code_date`: 19
- `combination`: 5
- `date`: 29
- `none`: 58
- `pagination`: 4
- `range`: 3

## Professional Usefulness

- `low_priority`: 16
- `professional_core`: 54
- `professional_useful`: 102
- `reference`: 5

## Stable Available

| Endpoint | Params | Status | Count | Rows | Usefulness | Sources | Note |
| --- | --- | --- | --- | --- | --- | --- | --- |
| /advanced/agency-list | date=2026-08-10 | ok | 48 | 155 | professional_useful | code,doc,schema | Probe returned data and local table has stored rows. |
| /advanced/bidvol-kline | code=600721 | ok | 600 | 200 | professional_useful | doc,schema | Probe returned data and local table has stored rows. |
| /advanced/business-list |  | ok | 206 | 855 | professional_useful | code,doc,schema | Probe returned data and local table has stored rows. |
| /advanced/concept-point |  | ok | 3 | 14 | professional_useful | code,doc,schema | Probe returned data and local table has stored rows. |
| /advanced/dadan-kline | code=600721 | ok | 850 | 115 | professional_useful | code,doc,schema | Probe returned data and local table has stored rows. |
| /advanced/dadan-kline-new | code=600721 | ok | 600 | 200 | professional_useful | doc,schema | Probe returned data and local table has stored rows. |
| /advanced/dadan-kline-today | code=600721&ktype=d | ok | 1 | 200 | professional_useful | code,doc,schema | Probe returned data and local table has stored rows. |
| /advanced/duidao-kline | code=600721 | ok | 600 | 200 | professional_useful | doc,schema | Probe returned data and local table has stored rows. |
| /advanced/fengk-best |  | ok | 30 | 60 | professional_useful | code,doc,schema | Probe returned data and local table has stored rows. |
| /advanced/gudong-info | code=600721 | ok | 20 | 200 | professional_useful | doc,schema | Probe returned data and local table has stored rows. |
| /advanced/gudong-renshu | code=600721 | ok | 10 | 200 | professional_useful | doc,schema | Probe returned data and local table has stored rows. |
| /advanced/gujia-kline | code=600721 | ok | 8 | 115 | professional_useful | doc,schema | Probe returned data and local table has stored rows. |
| /advanced/kline-today | code=600721&ktype=d | ok | 1 | 100 | professional_useful | code,schema | Probe returned data and local table has stored rows. |
| /advanced/main-activity-kline | code=600721 | ok | 600 | 117 | professional_useful | code,doc,schema | Probe returned data and local table has stored rows. |
| /advanced/main-monitor | code=600721 | ok | 30 | 145 | professional_useful | code,doc,schema | Probe returned data and local table has stored rows. |
| /advanced/market-mood-count |  | ok | 1 | 3 | professional_useful | code,doc,schema | Probe returned data and local table has stored rows. |
| /advanced/market-radar |  | ok | 15 | 45 | professional_useful | code,doc,schema | Probe returned data and local table has stored rows. |
| /advanced/morning-bidding-summary |  | ok | 8 | 12 | professional_useful | code,doc,schema | Probe returned data and local table has stored rows. |
| /advanced/news-flash |  | ok | 2 | 6 | professional_useful | code,doc,schema | Probe returned data and local table has stored rows. |
| /advanced/on-the-lhb | date=2026-08-10 | ok | 1 | 26 | professional_useful | code,doc,schema | Probe returned data and local table has stored rows. |
| /advanced/pankou | code=600721 | ok | 1 | 130 | professional_useful | code,doc,schema | Probe returned data and local table has stored rows. |
| /advanced/pianlizhi |  | ok | 1 | 3 | professional_useful | code,doc,schema | Probe returned data and local table has stored rows. |
| /advanced/relation |  | ok | 1 | 3 | professional_useful | code,doc,schema | Probe returned data and local table has stored rows. |
| /advanced/rqz-data | code=600721 | ok | 200 | 200 | professional_useful | doc,schema | Probe returned data and local table has stored rows. |
| /advanced/tuoyadan-kline | code=600721 | ok | 600 | 200 | professional_useful | doc,schema | Probe returned data and local table has stored rows. |
| /advanced/vol-tur | code=600721 | ok | 241 | 200 | professional_useful | doc,schema | Probe returned data and local table has stored rows. |
| /advanced/zjmm-min | code=600721&date=2026-08-10 | ok | 242 | 69996 | professional_useful | code,doc,schema | Probe returned data and local table has stored rows. |
| /auction/bidding-anomaly | code=600721&date=2026-08-10 | ok | 3 | 28 | professional_core | code,doc,schema | Probe returned data and local table has stored rows. |
| /auction/tick | code=600721&date=2026-08-10 | ok | 1 | 120917 | professional_core | code,doc,schema | Probe returned data and local table has stored rows. |
| /daily | date=2026-08-10 | ok | 1 | 275 | professional_core | code,doc,schema | Probe returned data and local table has stored rows. |
| /daily/export | date=2026-08-10 | ok | 1 | 3 | professional_core | code,doc,schema | Probe returned data and local table has stored rows. |
| /daily/new-high | date=2026-08-10 | ok | 1 | 251 | professional_core | code,doc,schema | Probe returned data and local table has stored rows. |
| /daily/sentiment | date=2026-08-10 | ok | 1 | 251 | professional_core | code,doc,schema | Probe returned data and local table has stored rows. |
| /dingpan/all |  | ok | 1 | 6 | professional_useful | code,doc,schema | Probe returned data and local table has stored rows. |
| /dingpan/module-versatile |  | ok | 1 | 9 | professional_useful | code,doc,schema | Probe returned data and local table has stored rows. |
| /dingpan/northbound-close-date |  | ok | 225 | 675 | professional_useful | code,schema | Probe returned data and local table has stored rows. |
| /dingpan/radar | st=0 | ok | 15 | 54 | professional_useful | code,doc,schema | Probe returned data and local table has stored rows. |
| /dingpan/southbound-close-date |  | ok | 213 | 639 | professional_useful | code,schema | Probe returned data and local table has stored rows. |
| /dingpan/weipan |  | ok | 3 | 27 | professional_useful | code,doc,schema | Probe returned data and local table has stored rows. |
| /etf/ranking | date=2026-08-10 | ok | 4 | 12 | low_priority | code,doc,schema | Probe returned data and local table has stored rows. |
| /fengk/list | date=2026-08-10&index=0&page_size=20 | ok | 1 | 1765 | professional_useful | code,doc,schema | Probe returned data and local table has stored rows. |
| /finance/balance | code=600721 | ok | 30 | 42 | low_priority | doc,schema | Probe returned data and local table has stored rows. |
| /finance/cashflow | code=600721 | ok | 25 | 42 | low_priority | doc,schema | Probe returned data and local table has stored rows. |
| /finance/income | code=600721 | ok | 33 | 42 | low_priority | doc,schema | Probe returned data and local table has stored rows. |
| /finance/summary | code=600721 | ok | 114 | 6 | low_priority | doc,schema | Probe returned data and local table has stored rows. |
| /index/full-info | code=600721&date=2026-08-10 | ok | 1 | 12 | reference | code,doc,schema | Probe returned data and local table has stored rows. |
| /index/zhishu-kline | code=600721 | ok | 630 | 2612 | reference | code,schema | Probe returned data and local table has stored rows. |
| /kline | code=600721&count=5&ktype=d | ok | 5 | 804055 | reference | code,doc,schema | Probe returned data and local table has stored rows. |
| /l2/realtime/all-boards |  | ok | 1 | 23955 | professional_core | code,doc,schema | Probe returned data and local table has stored rows. |
| /l2/realtime/index-list |  | ok | 4 | 16 | professional_core | code,doc,schema | Probe returned data and local table has stored rows. |
| /l2/realtime/index-trend |  | ok | 1 | 4 | professional_core | code,doc,schema | Probe returned data and local table has stored rows. |
| /l2/sector-intraday | code=801008&date=2026-08-10 | ok | 241 | 4667 | professional_core | code,schema | Probe returned data and local table has stored rows. |
| /l2/sector-volume | code=801008 | ok | 241 | 930 | professional_core | code,schema | Probe returned data and local table has stored rows. |
| /l2/tick-history | code=600721&date=2026-08-10 | ok | 1433 | 17739 | professional_core | code,doc,schema | Probe returned data and local table has stored rows. |
| /l2/tick-orders | code=600721&date=2026-08-10 | ok | 30 | 332 | professional_core | code,doc,schema | Probe returned data and local table has stored rows. |
| /l2/tick-orders-all | code=600721&date=2026-08-10 | ok | 3284 | 3766 | professional_core | code,doc,schema | Probe returned data and local table has stored rows. |
| /ladder/board-stocks | date=2026-08-10 | ok | 87 | 319 | professional_core | code,doc,schema | Probe returned data and local table has stored rows. |
| /ladder/broken | date=2026-08-10 | ok | 57 | 31 | professional_core | code,doc,schema | Probe returned data and local table has stored rows. |
| /ladder/realtime-boards |  | ok | 87 | 406 | professional_core | code,doc,schema | Probe returned data and local table has stored rows. |
| /lhb/detail | code=600721&date=2026-08-10 | ok | 1 | 71711 | professional_core | code,doc,schema | Probe returned data and local table has stored rows. |
| /lhb/raw-list | date=2026-08-10 | ok | 1 | 44 | professional_core | code,doc,schema | Probe returned data and local table has stored rows. |
| /lhb/top-title |  | ok | 1 | 22 | professional_core | code,doc,schema | Probe returned data and local table has stored rows. |
| /lhb/update-list |  | ok | 66 | 14 | professional_core | code,doc,schema | Probe returned data and local table has stored rows. |
| /lhb/youzi-dongxiang | date=2026-08-10 | ok | 1 | 36 | professional_core | code,doc,schema | Probe returned data and local table has stored rows. |
| /market/emotion-money-date |  | ok | 1 | 11670 | professional_core | code,doc,schema | Probe returned data and local table has stored rows. |
| /market/emotion-money-detail |  | ok | 1 | 3894 | professional_core | code,doc,schema | Probe returned data and local table has stored rows. |
| /market/limit-up-down |  | ok | 250 | 1000 | professional_core | code,doc,schema | Probe returned data and local table has stored rows. |
| /market/mood |  | ok | 1 | 22 | professional_core | code,doc,schema | Probe returned data and local table has stored rows. |
| /market/rise-fall | date=2026-08-10 | ok | 1 | 275 | professional_core | code,doc,schema | Probe returned data and local table has stored rows. |
| /news/columns |  | ok | 1 | 30 | professional_useful | code,schema | Probe returned data and local table has stored rows. |
| /news/concept-jxbk |  | ok | 13 | 39 | professional_useful | code,doc,schema | Probe returned data and local table has stored rows. |
| /sector/boom-reason | code=801008&date=2026-08-10 | ok | 1 | 174 | professional_core | code,doc,schema | Probe returned data and local table has stored rows. |
| /sector/parent-plate | code=801008 | ok | 1 | 150 | professional_core | code,schema | Probe returned data and local table has stored rows. |
| /sector/plate-info-qj | code=801008 | ok | 22 | 60 | professional_core | code,doc,schema | Probe returned data and local table has stored rows. |
| /sector/plates |  | ok | 58 | 58 | professional_core | code,schema | Probe returned data and local table has stored rows. |
| /sector/ranking | date=2026-08-10 | ok | 50 | 43 | professional_core | code,doc,schema | Probe returned data and local table has stored rows. |
| /sector/strength | code=801008&date=2026-08-10 | ok | 1 | 129 | professional_core | code,doc,schema | Probe returned data and local table has stored rows. |
| /sector/strength-dataframe | code=801008&end=2026-08-10&start=2026-06-01 | ok | 51 | 60 | professional_core | code,doc,schema | Probe returned data and local table has stored rows. |
| /sector/strength-ndays | code=801008&days=5&end_date=2026-08-10 | ok | 50 | 90 | professional_core | code,doc,schema | Probe returned data and local table has stored rows. |
| /stock/articles | code=600721 | ok | 1 | 200 | professional_useful | schema | Probe returned data and local table has stored rows. |
| /stock/company-info | code=600721 | ok | 3 | 119 | professional_useful | code,doc,schema | Probe returned data and local table has stored rows. |
| /stock/gudong | code=600721 | ok | 9 | 1133 | professional_useful | code,doc,schema | Probe returned data and local table has stored rows. |
| /stock/holding-funds | code=600721 | ok | 1 | 200 | professional_useful | code,doc,schema | Probe returned data and local table has stored rows. |
| /stock/institutional-positions | code=600721 | ok | 1 | 1133 | professional_useful | code,doc,schema | Probe returned data and local table has stored rows. |
| /stock/message-bar | code=600721 | ok | 1 | 200 | professional_useful | doc,schema | Probe returned data and local table has stored rows. |
| /theme/hot |  | ok | 13 | 39 | professional_useful | code,doc,schema | Probe returned data and local table has stored rows. |

## Available But Not Stored

| Endpoint | Params | Status | Count | Rows | Usefulness | Sources | Note |
| --- | --- | --- | --- | --- | --- | --- | --- |
| /advanced/company-count | codes=801008%2C600721 | ok | 1 | 0 | professional_useful | code,schema | Probe returned data; local table is not populated or not mapped. |
| /advanced/convertible-bonds-option |  | ok | 2 | 0 | professional_useful | doc,schema | Probe returned data; local table is not populated or not mapped. |
| /advanced/corporate-news | code=600721 | ok | 25 | 0 | professional_useful | schema | Probe returned data; local table is not populated or not mapped. |
| /advanced/dadan-trend-incremental | code=600721 | ok | 242 | 0 | professional_useful | doc,schema | Probe returned data; local table is not populated or not mapped. |
| /advanced/dp-explain |  | ok | 3 | 0 | professional_useful | doc,schema | Probe returned data; local table is not populated or not mapped. |
| /advanced/dp-realdata |  | ok | 9 | 0 | professional_useful | doc,schema | Probe returned data; local table is not populated or not mapped. |
| /advanced/fenbi2 | code=600721 | ok | 1433 | 0 | professional_useful | schema | Probe returned data; local table is not populated or not mapped. |
| /advanced/fenshi-kline-option | code=600721 | ok | 1 | 0 | professional_useful | doc,schema | Probe returned data; local table is not populated or not mapped. |
| /advanced/holiday | year=2026 | ok | 316 | 0 | professional_useful | code,doc,schema | Probe returned data; local table is not populated or not mapped. |
| /advanced/kline-today-dadan-new | code=600721&ktype=d | ok | 6 | 0 | professional_useful | code,doc,schema | Probe returned data; local table is not populated or not mapped. |
| /advanced/kline-today-duidao | code=600721&ktype=d | ok | 7 | 0 | professional_useful | code,doc,schema | Probe returned data; local table is not populated or not mapped. |
| /advanced/kline-today-main-activity | code=600721&ktype=d | ok | 7 | 0 | professional_useful | code,doc,schema | Probe returned data; local table is not populated or not mapped. |
| /advanced/kline-today-tyd | code=600721&ktype=d | ok | 8 | 0 | professional_useful | code,doc,schema | Probe returned data; local table is not populated or not mapped. |
| /advanced/kline-volume-forecast | code=600721 | ok | 6 | 0 | professional_useful | doc,schema | Probe returned data; local table is not populated or not mapped. |
| /advanced/kline-zhangting-reason | code=600721 | ok | 7 | 0 | professional_useful | doc,schema | Probe returned data; local table is not populated or not mapped. |
| /advanced/morning-bidding-list |  | ok | 1 | 0 | professional_useful | code,doc,schema | Probe returned data; local table is not populated or not mapped. |
| /advanced/newhigh-group-count |  | ok | 16 | 0 | professional_useful | code,doc,schema | Probe returned data; local table is not populated or not mapped. |
| /advanced/pianlizhi-many |  | ok | 1 | 0 | professional_useful | doc,schema | Probe returned data; local table is not populated or not mapped. |
| /advanced/pianlizhi-w32 |  | ok | 1 | 0 | professional_useful | doc,schema | Probe returned data; local table is not populated or not mapped. |
| /advanced/stock-plate-new | code=600721 | ok | 6 | 0 | professional_useful | schema | Probe returned data; local table is not populated or not mapped. |
| /advanced/stock-trend-incremental-ph | code=600721 | ok | 25 | 0 | professional_useful | schema | Probe returned data; local table is not populated or not mapped. |
| /advanced/trend-min | code=600721&date=2026-08-10 | ok | 241 | 0 | professional_useful | schema | Probe returned data; local table is not populated or not mapped. |
| /advanced/turnover-ten | code=600721 | ok | 1 | 0 | professional_useful | doc,schema | Probe returned data; local table is not populated or not mapped. |
| /advanced/weipan-qiangchou |  | ok | 3 | 0 | professional_useful | code,doc,schema | Probe returned data; local table is not populated or not mapped. |
| /advanced/zhangting-gene | code=600721 | ok | 6 | 0 | professional_useful | doc,schema | Probe returned data; local table is not populated or not mapped. |
| /dingpan/jijin |  | ok | 1 | 0 | professional_useful | code,doc,schema | Probe returned data; local table is not populated or not mapped. |
| /etf/all | date=2026-08-10 | ok | 3 | 0 | low_priority | code,doc,schema | Probe returned data; local table is not populated or not mapped. |
| /finance/compare | code=600721 | ok | 26 | 0 | low_priority | doc,schema | Probe returned data; local table is not populated or not mapped. |
| /forums/column |  | ok | 1 | 0 | low_priority | schema | Probe returned data; local table is not populated or not mapped. |
| /forums/focus |  | ok | 1 | 0 | low_priority | schema | Probe returned data; local table is not populated or not mapped. |
| /forums/sel-list | index=0&page_size=20 | ok | 1 | 0 | low_priority | schema | Probe returned data; local table is not populated or not mapped. |
| /l2/realtime/sharp-withdrawal |  | ok | 3 | 0 | professional_core | doc,schema | Probe returned data; local table is not populated or not mapped. |
| /news/index-plate |  | ok | 6 | 0 | professional_useful | code,schema | Probe returned data; local table is not populated or not mapped. |
| /news/plate | code=600721 | ok | 1 | 0 | professional_useful | code,doc,schema | Probe returned data; local table is not populated or not mapped. |
| /news/theme | index=0&page_size=20&type=-1 | ok | 1 | 0 | professional_useful | code,doc,schema | Probe returned data; local table is not populated or not mapped. |
| /sector/son-plate-direct | code=801008 | ok | 1 | 0 | professional_core | schema | Probe returned data; local table is not populated or not mapped. |
| /sector/son-plates | code=801008 | ok | 1 | 0 | professional_core | code,doc,schema | Probe returned data; local table is not populated or not mapped. |
| /sector/strength-batch | codes=801008%2C801008&date=2026-08-10 | ok | 1 | 0 | professional_core | code,doc,schema | Probe returned data; local table is not populated or not mapped. |
| /sector/sub-concepts | code=801008 | ok | 1 | 0 | professional_core | code,doc,schema | Probe returned data; local table is not populated or not mapped. |
| /stock/gpcphbts-tag | code=600721 | ok | 7 | 0 | professional_useful | code,doc | Probe returned data; local table is not populated or not mapped. |
| /stock/institutional-dates | code=600721 | ok | 1 | 0 | professional_useful | schema | Probe returned data; local table is not populated or not mapped. |
| /topic/detail | topic_id=1 | ok | 1 | 0 | low_priority | schema | Probe returned data; local table is not populated or not mapped. |
| /topic/list | index=0&page_size=20 | ok | 1 | 0 | low_priority | code,schema | Probe returned data; local table is not populated or not mapped. |
| /tuyere/by-stock | code=600721 | ok | 1 | 0 | professional_useful | doc,schema | Probe returned data; local table is not populated or not mapped. |
| /tuyere/tags | code=600721 | ok | 13 | 0 | professional_useful | schema | Probe returned data; local table is not populated or not mapped. |
| /xianhuo/list |  | ok | 383 | 0 | low_priority | code,doc,schema | Probe returned data; local table is not populated or not mapped. |

## Reachable But Empty

| Endpoint | Params | Status | Count | Rows | Usefulness | Sources | Note |
| --- | --- | --- | --- | --- | --- | --- | --- |
| /advanced/bid-history |  | ok | 0 | 0 | professional_useful | doc,schema | Endpoint is reachable but returned empty data. |
| /advanced/big-reminder |  | ok | 0 | 0 | professional_useful | doc,schema | Endpoint is reachable but returned empty data. |
| /advanced/bkjj-bl |  | ok | 0 | 0 | professional_useful | doc,schema | Endpoint is reachable but returned empty data. |
| /advanced/chouma | code=600721&date=2026-08-10 | ok | 0 | 200 | professional_useful | code,doc,schema | Endpoint is reachable but returned empty data. |
| /advanced/disk-review | date=2026-08-10 | ok | 0 | 3 | professional_useful | code,doc,schema | Endpoint is reachable but returned empty data. |
| /advanced/f10-concept-jxbk |  | ok | 0 | 0 | professional_useful | schema | Endpoint is reachable but returned empty data. |
| /advanced/f10-index |  | ok | 0 | 0 | professional_useful | schema | Endpoint is reachable but returned empty data. |
| /advanced/his-ranking | date=2026-08-10 | ok | 0 | 0 | professional_useful | code,schema | Endpoint is reachable but returned empty data. |
| /advanced/his-ranking-info | date=2026-08-10 | ok | 0 | 3 | professional_useful | code,schema | Endpoint is reachable but returned empty data. |
| /advanced/his-sharp-withdrawal | date=2026-08-10 | ok | 0 | 0 | professional_useful | code,doc,schema | Endpoint is reachable but returned empty data. |
| /advanced/his-zhangfu-detail |  | ok | 0 | 0 | professional_useful | doc,schema | Endpoint is reachable but returned empty data. |
| /advanced/interviews | end=2026-08-10&start=2026-06-01 | ok | 0 | 30 | professional_useful | code,doc,schema | Endpoint is reachable but returned empty data. |
| /advanced/market-scln | date=2026-08-10 | ok | 0 | 3 | professional_useful | code,doc,schema | Endpoint is reachable but returned empty data. |
| /advanced/newhigh-group-stocks | group_type=all | ok | 0 | 0 | professional_useful | code,doc,schema | Endpoint is reachable but returned empty data. |
| /advanced/news-flash-top |  | ok | 0 | 0 | professional_useful | doc,schema | Endpoint is reachable but returned empty data. |
| /advanced/pmsl |  | ok | 0 | 0 | professional_useful | doc,schema | Endpoint is reachable but returned empty data. |
| /advanced/voltur-history |  | ok | 0 | 0 | professional_useful | doc,schema | Endpoint is reachable but returned empty data. |
| /advanced/weight-performance | date=2026-08-10 | ok | 0 | 0 | professional_useful | code,doc,schema | Endpoint is reachable but returned empty data. |
| /advanced/zhangting-expression | date=2026-08-10 | ok | 0 | 0 | professional_useful | code,doc,schema | Endpoint is reachable but returned empty data. |
| /advanced/zs-trend-narrow |  | ok | 0 | 0 | professional_useful | schema | Endpoint is reachable but returned empty data. |
| /fengk/yd-plate | date=2026-08-10 | ok | 0 | 0 | professional_useful | code,doc,schema | Endpoint is reachable but returned empty data. |
| /fengk/yd-plate-info | date=2026-08-10&plate=801008 | ok | 0 | 0 | professional_useful | code,doc,schema | Endpoint is reachable but returned empty data. |
| /index/list | date=2026-08-10 | ok | 0 | 28 | reference | code,doc,schema | Endpoint is reachable but returned empty data. |
| /l2/stock-bigorder | code=600721&date=2026-08-10 | ok | 0 | 2524 | professional_core | code,doc,schema | Endpoint is reachable but returned empty data. |
| /ladder/consecutive | date=2026-08-10 | ok | 0 | 53 | professional_core | code,doc,schema | Endpoint is reachable but returned empty data. |
| /ladder/market | date=2026-08-10 | ok | 0 | 241 | professional_core | code,doc,schema | Endpoint is reachable but returned empty data. |
| /ladder/sector | date=2026-08-10 | ok | 0 | 53 | professional_core | code,doc,schema | Endpoint is reachable but returned empty data. |
| /ladder/sharp-withdrawal | date=2026-08-10 | ok | 0 | 44 | professional_core | code,doc,schema | Endpoint is reachable but returned empty data. |
| /lhb/dataframe | date=2026-08-10 | ok | 0 | 44 | professional_core | code,doc,schema | Endpoint is reachable but returned empty data. |
| /lhb/list | date=2026-08-10 | ok | 0 | 1925 | professional_core | code,doc,schema | Endpoint is reachable but returned empty data. |
| /sector/all-stocks | code=801008 | ok | 0 | 69 | professional_core | code,doc,schema | Endpoint is reachable but returned empty data. |
| /sector/bk-fenshi-zhibo | code=801008 | ok | 0 | 0 | professional_core | code,doc,schema | Endpoint is reachable but returned empty data. |
| /sector/capital | code=801008&date=2026-08-10 | ok | 0 | 16210 | professional_core | code,doc,schema | Endpoint is reachable but returned empty data. |
| /sector/stocks | code=801008&date=2026-08-10 | ok | 0 | 181 | professional_core | code,doc,schema | Endpoint is reachable but returned empty data. |
| /topic/vote | topic_id=1 | ok | 0 | 0 | low_priority | schema | Endpoint is reachable but returned empty data. |

## Needs Trading Session

| Endpoint | Params | Status | Count | Rows | Usefulness | Sources | Note |
| --- | --- | --- | --- | --- | --- | --- | --- |
| /advanced/zs-real |  | ok | 0 | 0 | professional_useful | doc,schema | Endpoint is reachable but empty; likely trading-session/date/sample dependent. |
| /index/intraday | code=600721&date=2026-08-10 | ok | 0 | 1928 | reference | code,doc,schema | Endpoint is reachable but empty; likely trading-session/date/sample dependent. |
| /l2/stock-intraday | code=600721&date=2026-08-10 | ok | 0 | 185689 | professional_core | code,doc,schema | Endpoint is reachable but empty; likely trading-session/date/sample dependent. |

## Needs Pagination Or Batch

| Endpoint | Params | Status | Count | Rows | Usefulness | Sources | Note |
| --- | --- | --- | --- | --- | --- | --- | --- |

## Parameter Uncertain

| Endpoint | Params | Status | Count | Rows | Usefulness | Sources | Note |
| --- | --- | --- | --- | --- | --- | --- | --- |
| /comments |  | http_422 |  | 0 | low_priority | schema | Endpoint exists but rejected the inferred parameter shape. |
| /sector/strength-history |  | http_422 |  | 0 | professional_core | schema | Endpoint exists but rejected the inferred parameter shape. |

## API Error

| Endpoint | Params | Status | Count | Rows | Usefulness | Sources | Note |
| --- | --- | --- | --- | --- | --- | --- | --- |
| /advanced/bk-dj-arrange |  | http_404 |  | 0 | professional_useful | doc,schema | Probe failed with http_404. |
| /dingpan/art-title |  | http_404 |  | 0 | professional_useful | code,schema | Probe failed with http_404. |
| /finance/fetch-checkpoint | code=600721 | http_404 |  | 11 | low_priority | schema | Probe failed with http_404. |
| /market/limit-up-down-summary |  | http_404 |  | 10 | professional_core | schema | Probe failed with http_404. |
| /stock/tags | code=600721 | http_404 |  | 0 | professional_useful | schema | Probe failed with http_404. |

## Low Priority / Reference

| Endpoint | Params | Status | Count | Rows | Usefulness | Sources | Note |
| --- | --- | --- | --- | --- | --- | --- | --- |
| /comments |  | http_422 |  | 0 | low_priority | schema | Endpoint exists but rejected the inferred parameter shape. |
| /etf/all | date=2026-08-10 | ok | 3 | 0 | low_priority | code,doc,schema | Probe returned data; local table is not populated or not mapped. |
| /etf/ranking | date=2026-08-10 | ok | 4 | 12 | low_priority | code,doc,schema | Probe returned data and local table has stored rows. |
| /finance/balance | code=600721 | ok | 30 | 42 | low_priority | doc,schema | Probe returned data and local table has stored rows. |
| /finance/cashflow | code=600721 | ok | 25 | 42 | low_priority | doc,schema | Probe returned data and local table has stored rows. |
| /finance/compare | code=600721 | ok | 26 | 0 | low_priority | doc,schema | Probe returned data; local table is not populated or not mapped. |
| /finance/fetch-checkpoint | code=600721 | http_404 |  | 11 | low_priority | schema | Probe failed with http_404. |
| /finance/income | code=600721 | ok | 33 | 42 | low_priority | doc,schema | Probe returned data and local table has stored rows. |
| /finance/summary | code=600721 | ok | 114 | 6 | low_priority | doc,schema | Probe returned data and local table has stored rows. |
| /forums/column |  | ok | 1 | 0 | low_priority | schema | Probe returned data; local table is not populated or not mapped. |
| /forums/focus |  | ok | 1 | 0 | low_priority | schema | Probe returned data; local table is not populated or not mapped. |
| /forums/sel-list | index=0&page_size=20 | ok | 1 | 0 | low_priority | schema | Probe returned data; local table is not populated or not mapped. |
| /index/full-info | code=600721&date=2026-08-10 | ok | 1 | 12 | reference | code,doc,schema | Probe returned data and local table has stored rows. |
| /index/intraday | code=600721&date=2026-08-10 | ok | 0 | 1928 | reference | code,doc,schema | Endpoint is reachable but empty; likely trading-session/date/sample dependent. |
| /index/list | date=2026-08-10 | ok | 0 | 28 | reference | code,doc,schema | Endpoint is reachable but returned empty data. |
| /index/zhishu-kline | code=600721 | ok | 630 | 2612 | reference | code,schema | Probe returned data and local table has stored rows. |
| /kline | code=600721&count=5&ktype=d | ok | 5 | 804055 | reference | code,doc,schema | Probe returned data and local table has stored rows. |
| /topic/detail | topic_id=1 | ok | 1 | 0 | low_priority | schema | Probe returned data; local table is not populated or not mapped. |
| /topic/list | index=0&page_size=20 | ok | 1 | 0 | low_priority | code,schema | Probe returned data; local table is not populated or not mapped. |
| /topic/vote | topic_id=1 | ok | 0 | 0 | low_priority | schema | Endpoint is reachable but returned empty data. |
| /xianhuo/list |  | ok | 383 | 0 | low_priority | code,doc,schema | Probe returned data; local table is not populated or not mapped. |

## Full Matrix

| Endpoint | Category | Param Type | Params | Verdict | Item Count | Table | Rows | Sources |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| /advanced/agency-list | advanced | date | date=2026-08-10 | stable_available | 48 | advanced_agency_list | 155 | code,doc,schema |
| /advanced/bid-history | advanced | none |  | reachable_empty | 0 | advanced_bid_history | 0 | doc,schema |
| /advanced/bidvol-kline | advanced | code | code=600721 | stable_available | 600 | advanced_bidvol_kline | 200 | doc,schema |
| /advanced/big-reminder | advanced | none |  | reachable_empty | 0 | advanced_big_reminder | 0 | doc,schema |
| /advanced/bk-dj-arrange | advanced | none |  | api_error |  | advanced_bk_dj_arrange | 0 | doc,schema |
| /advanced/bkjj-bl | advanced | none |  | reachable_empty | 0 | advanced_bkjj_bl | 0 | doc,schema |
| /advanced/business-list | advanced | none |  | stable_available | 206 | advanced_business_list | 855 | code,doc,schema |
| /advanced/chouma | advanced | code_date | code=600721&date=2026-08-10 | reachable_empty | 0 | advanced_chouma | 200 | code,doc,schema |
| /advanced/company-count | advanced | batch | codes=801008%2C600721 | api_available | 1 | advanced_company_count | 0 | code,schema |
| /advanced/concept-point | advanced | none |  | stable_available | 3 | advanced_concept_point | 14 | code,doc,schema |
| /advanced/convertible-bonds-option | advanced | none |  | api_available | 2 | advanced_convertible_bonds_option | 0 | doc,schema |
| /advanced/corporate-news | advanced | code | code=600721 | api_available | 25 | advanced_corporate_news | 0 | schema |
| /advanced/dadan-kline | advanced | code | code=600721 | stable_available | 850 | advanced_dadan_kline | 115 | code,doc,schema |
| /advanced/dadan-kline-new | advanced | code | code=600721 | stable_available | 600 | advanced_dadan_kline_new | 200 | doc,schema |
| /advanced/dadan-kline-today | advanced | code | code=600721&ktype=d | stable_available | 1 | advanced_dadan_kline_today | 200 | code,doc,schema |
| /advanced/dadan-trend-incremental | advanced | code | code=600721 | api_available | 242 | advanced_dadan_trend_incremental | 0 | doc,schema |
| /advanced/disk-review | advanced | date | date=2026-08-10 | reachable_empty | 0 | advanced_disk_review | 3 | code,doc,schema |
| /advanced/dp-explain | advanced | none |  | api_available | 3 | advanced_dp_explain | 0 | doc,schema |
| /advanced/dp-realdata | advanced | none |  | api_available | 9 | advanced_dp_realdata | 0 | doc,schema |
| /advanced/duidao-kline | advanced | code | code=600721 | stable_available | 600 | advanced_duidao_kline | 200 | doc,schema |
| /advanced/f10-concept-jxbk | advanced | none |  | reachable_empty | 0 | advanced_f10_concept_jxbk | 0 | schema |
| /advanced/f10-index | advanced | none |  | reachable_empty | 0 | advanced_f10_index | 0 | schema |
| /advanced/fenbi2 | advanced | code | code=600721 | api_available | 1433 | advanced_fenbi2 | 0 | schema |
| /advanced/fengk-best | advanced | none |  | stable_available | 30 | advanced_fengk_best | 60 | code,doc,schema |
| /advanced/fenshi-kline-option | advanced | code | code=600721 | api_available | 1 | advanced_fenshi_kline_option | 0 | doc,schema |
| /advanced/gudong-info | advanced | code | code=600721 | stable_available | 20 | advanced_gudong_info | 200 | doc,schema |
| /advanced/gudong-renshu | advanced | code | code=600721 | stable_available | 10 | advanced_gudong_renshu | 200 | doc,schema |
| /advanced/gujia-kline | advanced | code | code=600721 | stable_available | 8 | advanced_gujia_kline | 115 | doc,schema |
| /advanced/his-ranking | advanced | date | date=2026-08-10 | reachable_empty | 0 | advanced_his_ranking | 0 | code,schema |
| /advanced/his-ranking-info | advanced | date | date=2026-08-10 | reachable_empty | 0 | advanced_his_ranking_info | 3 | code,schema |
| /advanced/his-sharp-withdrawal | advanced | date | date=2026-08-10 | reachable_empty | 0 | advanced_his_sharp_withdrawal | 0 | code,doc,schema |
| /advanced/his-zhangfu-detail | advanced | none |  | reachable_empty | 0 | advanced_his_zhangfu_detail | 0 | doc,schema |
| /advanced/holiday | advanced | combination | year=2026 | api_available | 316 | advanced_holiday | 0 | code,doc,schema |
| /advanced/interviews | advanced | range | end=2026-08-10&start=2026-06-01 | reachable_empty | 0 | advanced_interviews | 30 | code,doc,schema |
| /advanced/kline-today | advanced | code | code=600721&ktype=d | stable_available | 1 | advanced_kline_today | 100 | code,schema |
| /advanced/kline-today-dadan-new | advanced | code | code=600721&ktype=d | api_available | 6 | advanced_kline_today_dadan_new | 0 | code,doc,schema |
| /advanced/kline-today-duidao | advanced | code | code=600721&ktype=d | api_available | 7 | advanced_kline_today_duidao | 0 | code,doc,schema |
| /advanced/kline-today-main-activity | advanced | code | code=600721&ktype=d | api_available | 7 | advanced_kline_today_main_activity | 0 | code,doc,schema |
| /advanced/kline-today-tyd | advanced | code | code=600721&ktype=d | api_available | 8 | advanced_kline_today_tyd | 0 | code,doc,schema |
| /advanced/kline-volume-forecast | advanced | code | code=600721 | api_available | 6 | advanced_kline_volume_forecast | 0 | doc,schema |
| /advanced/kline-zhangting-reason | advanced | code | code=600721 | api_available | 7 | advanced_kline_zhangting_reason | 0 | doc,schema |
| /advanced/main-activity-kline | advanced | code | code=600721 | stable_available | 600 | advanced_main_activity_kline | 117 | code,doc,schema |
| /advanced/main-monitor | advanced | code | code=600721 | stable_available | 30 | advanced_main_monitor | 145 | code,doc,schema |
| /advanced/market-mood-count | advanced | none |  | stable_available | 1 | advanced_market_mood_count | 3 | code,doc,schema |
| /advanced/market-radar | advanced | none |  | stable_available | 15 | advanced_market_radar | 45 | code,doc,schema |
| /advanced/market-scln | advanced | date | date=2026-08-10 | reachable_empty | 0 | advanced_market_scln | 3 | code,doc,schema |
| /advanced/morning-bidding-list | advanced | none |  | api_available | 1 | advanced_morning_bidding_list | 0 | code,doc,schema |
| /advanced/morning-bidding-summary | advanced | none |  | stable_available | 8 | advanced_morning_bidding_summary | 12 | code,doc,schema |
| /advanced/newhigh-group-count | advanced | none |  | api_available | 16 | advanced_newhigh_group_count | 0 | code,doc,schema |
| /advanced/newhigh-group-stocks | advanced | combination | group_type=all | reachable_empty | 0 | advanced_newhigh_group_stocks | 0 | code,doc,schema |
| /advanced/news-flash | advanced | none |  | stable_available | 2 | advanced_news_flash | 6 | code,doc,schema |
| /advanced/news-flash-top | advanced | none |  | reachable_empty | 0 | advanced_news_flash_top | 0 | doc,schema |
| /advanced/on-the-lhb | advanced | date | date=2026-08-10 | stable_available | 1 | advanced_on_the_lhb | 26 | code,doc,schema |
| /advanced/pankou | advanced | code | code=600721 | stable_available | 1 | advanced_pankou | 130 | code,doc,schema |
| /advanced/pianlizhi | advanced | none |  | stable_available | 1 | advanced_pianlizhi | 3 | code,doc,schema |
| /advanced/pianlizhi-many | advanced | none |  | api_available | 1 | advanced_pianlizhi_many | 0 | doc,schema |
| /advanced/pianlizhi-w32 | advanced | none |  | api_available | 1 | advanced_pianlizhi_w32 | 0 | doc,schema |
| /advanced/pmsl | advanced | none |  | reachable_empty | 0 | advanced_pmsl | 0 | doc,schema |
| /advanced/relation | advanced | none |  | stable_available | 1 | advanced_relation | 3 | code,doc,schema |
| /advanced/rqz-data | advanced | code | code=600721 | stable_available | 200 | advanced_rqz_data | 200 | doc,schema |
| /advanced/stock-plate-new | advanced | code | code=600721 | api_available | 6 | advanced_stock_plate_new | 0 | schema |
| /advanced/stock-trend-incremental-ph | advanced | code | code=600721 | api_available | 25 | advanced_stock_trend_incremental_ph | 0 | schema |
| /advanced/trend-min | advanced | code_date | code=600721&date=2026-08-10 | api_available | 241 | advanced_trend_min | 0 | schema |
| /advanced/tuoyadan-kline | advanced | code | code=600721 | stable_available | 600 | advanced_tuoyadan_kline | 200 | doc,schema |
| /advanced/turnover-ten | advanced | code | code=600721 | api_available | 1 | advanced_turnover_ten | 0 | doc,schema |
| /advanced/vol-tur | advanced | code | code=600721 | stable_available | 241 | advanced_vol_tur | 200 | doc,schema |
| /advanced/voltur-history | advanced | none |  | reachable_empty | 0 | advanced_voltur_history | 0 | doc,schema |
| /advanced/weight-performance | advanced | date | date=2026-08-10 | reachable_empty | 0 | advanced_weight_performance | 0 | code,doc,schema |
| /advanced/weipan-qiangchou | advanced | none |  | api_available | 3 | advanced_weipan_qiangchou | 0 | code,doc,schema |
| /advanced/zhangting-expression | advanced | date | date=2026-08-10 | reachable_empty | 0 | advanced_zhangting_expression | 0 | code,doc,schema |
| /advanced/zhangting-gene | advanced | code | code=600721 | api_available | 6 | advanced_zhangting_gene | 0 | doc,schema |
| /advanced/zjmm-min | advanced | code_date | code=600721&date=2026-08-10 | stable_available | 242 | advanced_zjmm_min | 69996 | code,doc,schema |
| /advanced/zs-real | advanced | none |  | needs_trading_session | 0 | advanced_zs_real | 0 | doc,schema |
| /advanced/zs-trend-narrow | advanced | none |  | reachable_empty | 0 | advanced_zs_trend_narrow | 0 | schema |
| /auction/bidding-anomaly | auction | code_date | code=600721&date=2026-08-10 | stable_available | 3 | auction_bidding_anomaly | 28 | code,doc,schema |
| /auction/tick | auction | code_date | code=600721&date=2026-08-10 | stable_available | 1 | auction_tick | 120917 | code,doc,schema |
| /comments | comments | none |  | param_uncertain |  | comments | 0 | schema |
| /daily | daily | date | date=2026-08-10 | stable_available | 1 | daily_summary | 275 | code,doc,schema |
| /daily/export | daily | date | date=2026-08-10 | stable_available | 1 | daily_export | 3 | code,doc,schema |
| /daily/new-high | daily | date | date=2026-08-10 | stable_available | 1 | daily_new_high | 251 | code,doc,schema |
| /daily/sentiment | daily | date | date=2026-08-10 | stable_available | 1 | daily_sentiment | 251 | code,doc,schema |
| /dingpan/all | dingpan | none |  | stable_available | 1 | dingpan_all | 6 | code,doc,schema |
| /dingpan/art-title | dingpan | none |  | api_error |  | dingpan_art_title | 0 | code,schema |
| /dingpan/jijin | dingpan | none |  | api_available | 1 | dingpan_jijin | 0 | code,doc,schema |
| /dingpan/module-versatile | dingpan | none |  | stable_available | 1 | dingpan_module_versatile | 9 | code,doc,schema |
| /dingpan/northbound-close-date | dingpan | none |  | stable_available | 225 | dingpan_northbound_close_date | 675 | code,schema |
| /dingpan/radar | dingpan | combination | st=0 | stable_available | 15 | dingpan_radar | 54 | code,doc,schema |
| /dingpan/southbound-close-date | dingpan | none |  | stable_available | 213 | dingpan_southbound_close_date | 639 | code,schema |
| /dingpan/weipan | dingpan | none |  | stable_available | 3 | dingpan_weipan | 27 | code,doc,schema |
| /etf/all | etf | date | date=2026-08-10 | api_available | 3 | etf_all | 0 | code,doc,schema |
| /etf/ranking | etf | date | date=2026-08-10 | stable_available | 4 | etf_ranking | 12 | code,doc,schema |
| /fengk/list | fengk | pagination | date=2026-08-10&index=0&page_size=20 | stable_available | 1 | fengk_list | 1765 | code,doc,schema |
| /fengk/yd-plate | fengk | date | date=2026-08-10 | reachable_empty | 0 | fengk_yd_plate | 0 | code,doc,schema |
| /fengk/yd-plate-info | fengk | code_date | date=2026-08-10&plate=801008 | reachable_empty | 0 | fengk_yd_plate_info | 0 | code,doc,schema |
| /finance/balance | finance | code | code=600721 | stable_available | 30 | finance_balance | 42 | doc,schema |
| /finance/cashflow | finance | code | code=600721 | stable_available | 25 | finance_cashflow | 42 | doc,schema |
| /finance/compare | finance | code | code=600721 | api_available | 26 | finance_compare | 0 | doc,schema |
| /finance/fetch-checkpoint | finance | code | code=600721 | api_error |  | finance_fetch_checkpoint | 11 | schema |
| /finance/income | finance | code | code=600721 | stable_available | 33 | finance_income | 42 | doc,schema |
| /finance/summary | finance | code | code=600721 | stable_available | 114 | finance_summary | 6 | doc,schema |
| /forums/column | forums | none |  | api_available | 1 | forums_column | 0 | schema |
| /forums/focus | forums | none |  | api_available | 1 | forums_focus | 0 | schema |
| /forums/sel-list | forums | pagination | index=0&page_size=20 | api_available | 1 | forums_sel_list | 0 | schema |
| /index/full-info | index | code_date | code=600721&date=2026-08-10 | stable_available | 1 | index_full_info | 12 | code,doc,schema |
| /index/intraday | index | code_date | code=600721&date=2026-08-10 | needs_trading_session | 0 | index_intraday | 1928 | code,doc,schema |
| /index/list | index | date | date=2026-08-10 | reachable_empty | 0 | index_list | 28 | code,doc,schema |
| /index/zhishu-kline | index | code | code=600721 | stable_available | 630 | index_kline | 2612 | code,schema |
| /kline | kline | code | code=600721&count=5&ktype=d | stable_available | 5 | kline | 804055 | code,doc,schema |
| /l2/realtime/all-boards | l2 | none |  | stable_available | 1 | l2_realtime_all_boards | 23955 | code,doc,schema |
| /l2/realtime/index-list | l2 | none |  | stable_available | 4 | l2_realtime_index_list | 16 | code,doc,schema |
| /l2/realtime/index-trend | l2 | none |  | stable_available | 1 | l2_realtime_index_trend | 4 | code,doc,schema |
| /l2/realtime/sharp-withdrawal | l2 | none |  | api_available | 3 | l2_realtime_sharp_withdrawal | 0 | doc,schema |
| /l2/sector-intraday | l2 | code_date | code=801008&date=2026-08-10 | stable_available | 241 | l2_sector_intraday | 4667 | code,schema |
| /l2/sector-volume | l2 | code | code=801008 | stable_available | 241 | l2_sector_volume | 930 | code,schema |
| /l2/stock-bigorder | l2 | code_date | code=600721&date=2026-08-10 | reachable_empty | 0 | l2_stock_bigorder | 2524 | code,doc,schema |
| /l2/stock-intraday | l2 | code_date | code=600721&date=2026-08-10 | needs_trading_session | 0 | l2_stock_intraday | 185689 | code,doc,schema |
| /l2/tick-history | l2 | code_date | code=600721&date=2026-08-10 | stable_available | 1433 | l2_tick_history | 17739 | code,doc,schema |
| /l2/tick-orders | l2 | code_date | code=600721&date=2026-08-10 | stable_available | 30 | l2_tick_orders | 332 | code,doc,schema |
| /l2/tick-orders-all | l2 | code_date | code=600721&date=2026-08-10 | stable_available | 3284 | l2_tick_orders_all | 3766 | code,doc,schema |
| /ladder/board-stocks | ladder | date | date=2026-08-10 | stable_available | 87 | ladder_board_stocks | 319 | code,doc,schema |
| /ladder/broken | ladder | date | date=2026-08-10 | stable_available | 57 | ladder_broken | 31 | code,doc,schema |
| /ladder/consecutive | ladder | date | date=2026-08-10 | reachable_empty | 0 | ladder_consecutive | 53 | code,doc,schema |
| /ladder/market | ladder | date | date=2026-08-10 | reachable_empty | 0 | ladder_market | 241 | code,doc,schema |
| /ladder/realtime-boards | ladder | none |  | stable_available | 87 | ladder_realtime_boards | 406 | code,doc,schema |
| /ladder/sector | ladder | date | date=2026-08-10 | reachable_empty | 0 | ladder_sector | 53 | code,doc,schema |
| /ladder/sharp-withdrawal | ladder | date | date=2026-08-10 | reachable_empty | 0 | ladder_sharp_withdrawal | 44 | code,doc,schema |
| /lhb/dataframe | lhb | date | date=2026-08-10 | reachable_empty | 0 | lhb_dataframe | 44 | code,doc,schema |
| /lhb/detail | lhb | code_date | code=600721&date=2026-08-10 | stable_available | 1 | lhb_detail | 71711 | code,doc,schema |
| /lhb/list | lhb | date | date=2026-08-10 | reachable_empty | 0 | lhb_list | 1925 | code,doc,schema |
| /lhb/raw-list | lhb | date | date=2026-08-10 | stable_available | 1 | lhb_raw_list | 44 | code,doc,schema |
| /lhb/top-title | lhb | none |  | stable_available | 1 | lhb_top_title | 22 | code,doc,schema |
| /lhb/update-list | lhb | none |  | stable_available | 66 | lhb_update_list | 14 | code,doc,schema |
| /lhb/youzi-dongxiang | lhb | date | date=2026-08-10 | stable_available | 1 | lhb_youzi_dongxiang | 36 | code,doc,schema |
| /market/emotion-money-date | market | none |  | stable_available | 1 | market_emotion_money | 11670 | code,doc,schema |
| /market/emotion-money-detail | market | none |  | stable_available | 1 | market_emotion_detail | 3894 | code,doc,schema |
| /market/limit-up-down | market | none |  | stable_available | 250 | market_limit_up_down | 1000 | code,doc,schema |
| /market/limit-up-down-summary | market | none |  | api_error |  | market_limit_up_down_summary | 10 | schema |
| /market/mood | market | none |  | stable_available | 1 | market_mood | 22 | code,doc,schema |
| /market/rise-fall | market | date | date=2026-08-10 | stable_available | 1 | market_rise_fall | 275 | code,doc,schema |
| /news/columns | news | none |  | stable_available | 1 | news_columns | 30 | code,schema |
| /news/concept-jxbk | news | none |  | stable_available | 13 | news_concept_jxbk | 39 | code,doc,schema |
| /news/index-plate | news | none |  | api_available | 6 | news_index_plate | 0 | code,schema |
| /news/plate | news | code | code=600721 | api_available | 1 | news_plate | 0 | code,doc,schema |
| /news/theme | news | pagination | index=0&page_size=20&type=-1 | api_available | 1 | news_theme | 0 | code,doc,schema |
| /sector/all-stocks | sector | code | code=801008 | reachable_empty | 0 | sector_all_stocks | 69 | code,doc,schema |
| /sector/bk-fenshi-zhibo | sector | code | code=801008 | reachable_empty | 0 | sector_bk_fenshi_zhibo | 0 | code,doc,schema |
| /sector/boom-reason | sector | code_date | code=801008&date=2026-08-10 | stable_available | 1 | sector_boom_reason | 174 | code,doc,schema |
| /sector/capital | sector | code_date | code=801008&date=2026-08-10 | reachable_empty | 0 | sector_capital | 16210 | code,doc,schema |
| /sector/parent-plate | sector | code | code=801008 | stable_available | 1 | sector_parent_plate | 150 | code,schema |
| /sector/plate-info-qj | sector | code | code=801008 | stable_available | 22 | sector_plate_info_qj | 60 | code,doc,schema |
| /sector/plates | sector | none |  | stable_available | 58 | sector_plates | 58 | code,schema |
| /sector/ranking | sector | date | date=2026-08-10 | stable_available | 50 | sector_ranking | 43 | code,doc,schema |
| /sector/son-plate-direct | sector | code | code=801008 | api_available | 1 | sector_son_plate_direct | 0 | schema |
| /sector/son-plates | sector | code | code=801008 | api_available | 1 | sector_son_plates | 0 | code,doc,schema |
| /sector/stocks | sector | code_date | code=801008&date=2026-08-10 | reachable_empty | 0 | sector_stocks | 181 | code,doc,schema |
| /sector/strength | sector | code_date | code=801008&date=2026-08-10 | stable_available | 1 | sector_strength | 129 | code,doc,schema |
| /sector/strength-batch | sector | batch | codes=801008%2C801008&date=2026-08-10 | api_available | 1 | sector_strength_batch | 0 | code,doc,schema |
| /sector/strength-dataframe | sector | range | code=801008&end=2026-08-10&start=2026-06-01 | stable_available | 51 | sector_strength_dataframe | 60 | code,doc,schema |
| /sector/strength-history | sector | none |  | param_uncertain |  | sector_strength_history | 0 | schema |
| /sector/strength-ndays | sector | range | code=801008&days=5&end_date=2026-08-10 | stable_available | 50 | sector_strength_ndays | 90 | code,doc,schema |
| /sector/sub-concepts | sector | code | code=801008 | api_available | 1 | sector_sub_concepts | 0 | code,doc,schema |
| /stock/articles | stock | code | code=600721 | stable_available | 1 | stock_articles | 200 | schema |
| /stock/company-info | stock | code | code=600721 | stable_available | 3 | stock_company_info | 119 | code,doc,schema |
| /stock/gpcphbts-tag | stock | code | code=600721 | api_available | 7 | stock_tags | 0 | code,doc |
| /stock/gudong | stock | code | code=600721 | stable_available | 9 | stock_gudong | 1133 | code,doc,schema |
| /stock/holding-funds | stock | code | code=600721 | stable_available | 1 | stock_holding_funds | 200 | code,doc,schema |
| /stock/institutional-dates | stock | code | code=600721 | api_available | 1 | stock_institutional_dates | 0 | schema |
| /stock/institutional-positions | stock | code | code=600721 | stable_available | 1 | stock_institutional_positions | 1133 | code,doc,schema |
| /stock/message-bar | stock | code | code=600721 | stable_available | 1 | stock_message_bar | 200 | doc,schema |
| /stock/tags | stock | code | code=600721 | api_error |  | stock_tags | 0 | schema |
| /theme/hot | theme | none |  | stable_available | 13 | theme_hot | 39 | code,doc,schema |
| /topic/detail | topic | combination | topic_id=1 | api_available | 1 | topic_detail | 0 | schema |
| /topic/list | topic | pagination | index=0&page_size=20 | api_available | 1 | topic_list | 0 | code,schema |
| /topic/vote | topic | combination | topic_id=1 | reachable_empty | 0 | topic_vote | 0 | schema |
| /tuyere/by-stock | tuyere | code | code=600721 | api_available | 1 | tuyere_by_stock | 0 | doc,schema |
| /tuyere/tags | tuyere | code | code=600721 | api_available | 13 | tuyere_tags | 0 | schema |
| /xianhuo/list | xianhuo | none |  | api_available | 383 | xianhuo_list | 0 | code,doc,schema |