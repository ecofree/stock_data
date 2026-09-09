# Full API Endpoint Inventory

## Scope

- Date: `2026-08-28`
- Stock sample: `002396`
- Sector sample: `801008`
- Discovered endpoints: `140`
- Safety: no API key or raw response body is written to this report.

## Summary

- `api_available`: 125
- `api_error`: 5
- `param_uncertain`: 3
- `reachable_empty`: 7

## Source Coverage

- `doc`: 140

## Parameter Types

- `batch`: 1
- `code`: 42
- `code_date`: 16
- `combination`: 3
- `date`: 19
- `none`: 54
- `pagination`: 2
- `range`: 3

## Professional Usefulness

- `low_priority`: 8
- `professional_core`: 47
- `professional_useful`: 81
- `reference`: 4

## Stable Available

| Endpoint | Params | Status | Count | Rows | Usefulness | Sources | Note |
| --- | --- | --- | --- | --- | --- | --- | --- |

## Available But Not Stored

| Endpoint | Params | Status | Count | Rows | Usefulness | Sources | Note |
| --- | --- | --- | --- | --- | --- | --- | --- |
| /advanced/agency-list |  | ok | 25 |  | professional_useful | doc | Probe returned data; local table is not populated or not mapped. |
| /advanced/bkjj-bl |  | ok | 3 |  | professional_useful | doc | Probe returned data; local table is not populated or not mapped. |
| /advanced/business-list |  | ok | 204 |  | professional_useful | doc | Probe returned data; local table is not populated or not mapped. |
| /advanced/chouma | code=002396 | ok | 49 |  | professional_useful | doc | Probe returned data; local table is not populated or not mapped. |
| /advanced/concept-point |  | ok | 4 |  | professional_useful | doc | Probe returned data; local table is not populated or not mapped. |
| /advanced/convertible-bonds-option |  | ok | 2 |  | professional_useful | doc | Probe returned data; local table is not populated or not mapped. |
| /advanced/dadan-kline | code=002396 | ok | 850 |  | professional_useful | doc | Probe returned data; local table is not populated or not mapped. |
| /advanced/dadan-kline-new | code=002396 | ok | 600 |  | professional_useful | doc | Probe returned data; local table is not populated or not mapped. |
| /advanced/dadan-kline-today | code=002396&ktype=d | ok | 1 |  | professional_useful | doc | Probe returned data; local table is not populated or not mapped. |
| /advanced/dadan-trend-incremental | code=002396 | ok | 242 |  | professional_useful | doc | Probe returned data; local table is not populated or not mapped. |
| /advanced/disk-review |  | ok | 2 |  | professional_useful | doc | Probe returned data; local table is not populated or not mapped. |
| /advanced/dp-explain |  | ok | 3 |  | professional_useful | doc | Probe returned data; local table is not populated or not mapped. |
| /advanced/dp-realdata |  | ok | 9 |  | professional_useful | doc | Probe returned data; local table is not populated or not mapped. |
| /advanced/duidao-kline | code=002396 | ok | 600 |  | professional_useful | doc | Probe returned data; local table is not populated or not mapped. |
| /advanced/fengk-best |  | ok | 30 |  | professional_useful | doc | Probe returned data; local table is not populated or not mapped. |
| /advanced/fenshi-kline-option | code=002396 | ok | 1 |  | professional_useful | doc | Probe returned data; local table is not populated or not mapped. |
| /advanced/gudong-info | code=002396 | ok | 20 |  | professional_useful | doc | Probe returned data; local table is not populated or not mapped. |
| /advanced/gudong-renshu | code=002396 | ok | 11 |  | professional_useful | doc | Probe returned data; local table is not populated or not mapped. |
| /advanced/gujia-kline | code=002396 | ok | 8 |  | professional_useful | doc | Probe returned data; local table is not populated or not mapped. |
| /advanced/his-sharp-withdrawal |  | ok | 3 |  | professional_useful | doc | Probe returned data; local table is not populated or not mapped. |
| /advanced/his-zhangfu-detail |  | ok | 37 |  | professional_useful | doc | Probe returned data; local table is not populated or not mapped. |
| /advanced/holiday | year=2026 | ok | 316 |  | professional_useful | doc | Probe returned data; local table is not populated or not mapped. |
| /advanced/interviews | end=2026-08-28&start=2026-06-01 | ok | 1 |  | professional_useful | doc | Probe returned data; local table is not populated or not mapped. |
| /advanced/kline-today-dadan-new | code=002396&ktype=d | ok | 6 |  | professional_useful | doc | Probe returned data; local table is not populated or not mapped. |
| /advanced/kline-today-duidao | code=002396&ktype=d | ok | 7 |  | professional_useful | doc | Probe returned data; local table is not populated or not mapped. |
| /advanced/kline-today-main-activity | code=002396&ktype=d | ok | 7 |  | professional_useful | doc | Probe returned data; local table is not populated or not mapped. |
| /advanced/kline-today-tyd | code=002396&ktype=d | ok | 8 |  | professional_useful | doc | Probe returned data; local table is not populated or not mapped. |
| /advanced/kline-volume-forecast | code=002396 | ok | 6 |  | professional_useful | doc | Probe returned data; local table is not populated or not mapped. |
| /advanced/kline-zhangting-reason | code=002396 | ok | 7 |  | professional_useful | doc | Probe returned data; local table is not populated or not mapped. |
| /advanced/main-activity-kline | code=002396 | ok | 600 |  | professional_useful | doc | Probe returned data; local table is not populated or not mapped. |
| /advanced/main-monitor | code=002396 | ok | 30 |  | professional_useful | doc | Probe returned data; local table is not populated or not mapped. |
| /advanced/market-mood-count |  | ok | 1 |  | professional_useful | doc | Probe returned data; local table is not populated or not mapped. |
| /advanced/market-radar |  | ok | 15 |  | professional_useful | doc | Probe returned data; local table is not populated or not mapped. |
| /advanced/market-scln |  | ok | 11 |  | professional_useful | doc | Probe returned data; local table is not populated or not mapped. |
| /advanced/newhigh-group-count |  | ok | 19 |  | professional_useful | doc | Probe returned data; local table is not populated or not mapped. |
| /advanced/news-flash |  | ok | 2 |  | professional_useful | doc | Probe returned data; local table is not populated or not mapped. |
| /advanced/news-flash-top |  | ok | 30 |  | professional_useful | doc | Probe returned data; local table is not populated or not mapped. |
| /advanced/on-the-lhb |  | ok | 1 |  | professional_useful | doc | Probe returned data; local table is not populated or not mapped. |
| /advanced/pankou | code=002396 | ok | 1 |  | professional_useful | doc | Probe returned data; local table is not populated or not mapped. |
| /advanced/pianlizhi |  | ok | 1 |  | professional_useful | doc | Probe returned data; local table is not populated or not mapped. |
| /advanced/pianlizhi-many |  | ok | 1 |  | professional_useful | doc | Probe returned data; local table is not populated or not mapped. |
| /advanced/pianlizhi-w32 |  | ok | 1 |  | professional_useful | doc | Probe returned data; local table is not populated or not mapped. |
| /advanced/pmsl |  | ok | 9 |  | professional_useful | doc | Probe returned data; local table is not populated or not mapped. |
| /advanced/relation |  | ok | 1 |  | professional_useful | doc | Probe returned data; local table is not populated or not mapped. |
| /advanced/tuoyadan-kline | code=002396 | ok | 600 |  | professional_useful | doc | Probe returned data; local table is not populated or not mapped. |
| /advanced/turnover-ten | code=002396 | ok | 1 |  | professional_useful | doc | Probe returned data; local table is not populated or not mapped. |
| /advanced/vol-tur | code=002396 | ok | 241 |  | professional_useful | doc | Probe returned data; local table is not populated or not mapped. |
| /advanced/weight-performance |  | ok | 2 |  | professional_useful | doc | Probe returned data; local table is not populated or not mapped. |
| /advanced/weipan-qiangchou |  | ok | 3 |  | professional_useful | doc | Probe returned data; local table is not populated or not mapped. |
| /advanced/zhangting-expression |  | ok | 12 |  | professional_useful | doc | Probe returned data; local table is not populated or not mapped. |
| /advanced/zhangting-gene | code=002396 | ok | 6 |  | professional_useful | doc | Probe returned data; local table is not populated or not mapped. |
| /advanced/zjmm-min | code=002396&date=2026-08-28 | ok | 241 |  | professional_useful | doc | Probe returned data; local table is not populated or not mapped. |
| /advanced/zs-real |  | ok | 4 |  | professional_useful | doc | Probe returned data; local table is not populated or not mapped. |
| /daily | date=2026-08-28 | ok | 1 |  | professional_core | doc | Probe returned data; local table is not populated or not mapped. |
| /daily/export | date=2026-08-28 | ok | 1 |  | professional_core | doc | Probe returned data; local table is not populated or not mapped. |
| /daily/new-high | date=2026-08-28 | ok | 1 |  | professional_core | doc | Probe returned data; local table is not populated or not mapped. |
| /daily/sentiment | date=2026-08-28 | ok | 1 |  | professional_core | doc | Probe returned data; local table is not populated or not mapped. |
| /dingpan/all |  | ok | 1 |  | professional_useful | doc | Probe returned data; local table is not populated or not mapped. |
| /dingpan/jijin |  | ok | 1 |  | professional_useful | doc | Probe returned data; local table is not populated or not mapped. |
| /dingpan/module-versatile |  | ok | 1 |  | professional_useful | doc | Probe returned data; local table is not populated or not mapped. |
| /dingpan/radar | st=0 | ok | 15 |  | professional_useful | doc | Probe returned data; local table is not populated or not mapped. |
| /dingpan/weipan |  | ok | 3 |  | professional_useful | doc | Probe returned data; local table is not populated or not mapped. |
| /etf/all | date=2026-08-28 | ok | 9 |  | low_priority | doc | Probe returned data; local table is not populated or not mapped. |
| /etf/ranking | date=2026-08-28 | ok | 9 |  | low_priority | doc | Probe returned data; local table is not populated or not mapped. |
| /fengk/list | date=2026-08-28&index=0&page_size=20 | ok | 1 |  | professional_useful | doc | Probe returned data; local table is not populated or not mapped. |
| /fengk/yd-plate |  | ok | 193 |  | professional_useful | doc | Probe returned data; local table is not populated or not mapped. |
| /finance/balance | code=002396 | ok | 18 |  | low_priority | doc | Probe returned data; local table is not populated or not mapped. |
| /finance/cashflow | code=002396 | ok | 18 |  | low_priority | doc | Probe returned data; local table is not populated or not mapped. |
| /finance/compare | code=002396 | ok | 17 |  | low_priority | doc | Probe returned data; local table is not populated or not mapped. |
| /finance/income | code=002396 | ok | 21 |  | low_priority | doc | Probe returned data; local table is not populated or not mapped. |
| /finance/summary | code=002396 | ok | 74 |  | low_priority | doc | Probe returned data; local table is not populated or not mapped. |
| /index/full-info | code=002396&date=2026-08-28 | ok | 1 |  | reference | doc | Probe returned data; local table is not populated or not mapped. |
| /index/intraday | code=002396&date=2026-08-28 | ok | 241 |  | reference | doc | Probe returned data; local table is not populated or not mapped. |
| /index/list | date=2026-08-28 | ok | 4 |  | reference | doc | Probe returned data; local table is not populated or not mapped. |
| /kline | code=002396&count=5&ktype=d | ok | 5 |  | reference | doc | Probe returned data; local table is not populated or not mapped. |
| /l2/realtime/all-boards |  | ok | 1 |  | professional_core | doc | Probe returned data; local table is not populated or not mapped. |
| /l2/realtime/index-list |  | ok | 4 |  | professional_core | doc | Probe returned data; local table is not populated or not mapped. |
| /l2/realtime/index-trend |  | ok | 1 |  | professional_core | doc | Probe returned data; local table is not populated or not mapped. |
| /l2/realtime/sharp-withdrawal |  | ok | 7 |  | professional_core | doc | Probe returned data; local table is not populated or not mapped. |
| /l2/stock-bigorder | code=002396&date=2026-08-28 | ok | 174 |  | professional_core | doc | Probe returned data; local table is not populated or not mapped. |
| /l2/stock-intraday | code=002396&date=2026-08-28 | ok | 241 |  | professional_core | doc | Probe returned data; local table is not populated or not mapped. |
| /l2/tick-history | code=002396&date=2026-08-28 | ok | 4565 |  | professional_core | doc | Probe returned data; local table is not populated or not mapped. |
| /l2/tick-orders | code=002396&date=2026-08-28 | ok | 30 |  | professional_core | doc | Probe returned data; local table is not populated or not mapped. |
| /l2/tick-orders-all | code=002396&date=2026-08-28 | ok | 16760 |  | professional_core | doc | Probe returned data; local table is not populated or not mapped. |
| /ladder/board-stocks | date=2026-08-28 | ok | 64 |  | professional_core | doc | Probe returned data; local table is not populated or not mapped. |
| /ladder/broken | date=2026-08-28 | ok | 39 |  | professional_core | doc | Probe returned data; local table is not populated or not mapped. |
| /ladder/consecutive | date=2026-08-28 | ok | 5 |  | professional_core | doc | Probe returned data; local table is not populated or not mapped. |
| /ladder/market | date=2026-08-28 | ok | 6 |  | professional_core | doc | Probe returned data; local table is not populated or not mapped. |
| /ladder/realtime-boards |  | ok | 64 |  | professional_core | doc | Probe returned data; local table is not populated or not mapped. |
| /ladder/sector | date=2026-08-28 | ok | 8 |  | professional_core | doc | Probe returned data; local table is not populated or not mapped. |
| /ladder/sharp-withdrawal | date=2026-08-28 | ok | 3 |  | professional_core | doc | Probe returned data; local table is not populated or not mapped. |
| /lhb/dataframe | date=2026-08-28 | ok | 51 |  | professional_core | doc | Probe returned data; local table is not populated or not mapped. |
| /lhb/detail | code=002396&date=2026-08-28 | ok | 1 |  | professional_core | doc | Probe returned data; local table is not populated or not mapped. |
| /lhb/list | date=2026-08-28 | ok | 51 |  | professional_core | doc | Probe returned data; local table is not populated or not mapped. |
| /lhb/raw-list | date=2026-08-28 | ok | 51 |  | professional_core | doc | Probe returned data; local table is not populated or not mapped. |
| /lhb/top-title |  | ok | 1 |  | professional_core | doc | Probe returned data; local table is not populated or not mapped. |
| /lhb/update-list |  | ok | 51 |  | professional_core | doc | Probe returned data; local table is not populated or not mapped. |
| /lhb/youzi-dongxiang | date=2026-08-28 | ok | 1 |  | professional_core | doc | Probe returned data; local table is not populated or not mapped. |
| /market/emotion-money-date |  | ok | 1 |  | professional_core | doc | Probe returned data; local table is not populated or not mapped. |
| /market/limit-up-down |  | ok | 250 |  | professional_core | doc | Probe returned data; local table is not populated or not mapped. |
| /market/mood |  | ok | 1 |  | professional_core | doc | Probe returned data; local table is not populated or not mapped. |
| /market/rise-fall | date=2026-08-28 | ok | 1 |  | professional_core | doc | Probe returned data; local table is not populated or not mapped. |
| /news/theme | index=0&page_size=20&type=-1 | ok | 1 |  | professional_useful | doc | Probe returned data; local table is not populated or not mapped. |
| /sector/all-stocks | code=801008 | ok | 1 |  | professional_core | doc | Probe returned data; local table is not populated or not mapped. |
| /sector/bk-fenshi-zhibo | code=801008&date=2026-08-28 | ok | 241 |  | professional_core | doc | Probe returned data; local table is not populated or not mapped. |
| /sector/boom-reason | code=801008&date=2026-08-28 | ok | 1 |  | professional_core | doc | Probe returned data; local table is not populated or not mapped. |
| /sector/capital | code=801008&date=2026-08-28 | ok | 1 |  | professional_core | doc | Probe returned data; local table is not populated or not mapped. |
| /sector/plate-info-qj | code=801008 | ok | 23 |  | professional_core | doc | Probe returned data; local table is not populated or not mapped. |
| /sector/ranking | date=2026-08-28 | ok | 16 |  | professional_core | doc | Probe returned data; local table is not populated or not mapped. |
| /sector/son-plates | code=801008 | ok | 1 |  | professional_core | doc | Probe returned data; local table is not populated or not mapped. |
| /sector/stocks | code=801008&date=2026-08-28 | ok | 1 |  | professional_core | doc | Probe returned data; local table is not populated or not mapped. |
| /sector/strength | code=801008&date=2026-08-28 | ok | 1 |  | professional_core | doc | Probe returned data; local table is not populated or not mapped. |
| /sector/strength-batch | codes=801008%2C801008&date=2026-08-28 | ok | 1 |  | professional_core | doc | Probe returned data; local table is not populated or not mapped. |
| /sector/strength-dataframe | code=801008&end=2026-08-28&start=2026-06-01 | ok | 65 |  | professional_core | doc | Probe returned data; local table is not populated or not mapped. |
| /sector/strength-ndays | code=801008&days=5&end_date=2026-08-28 | ok | 112 |  | professional_core | doc | Probe returned data; local table is not populated or not mapped. |
| /sector/sub-concepts | code=801008 | ok | 1 |  | professional_core | doc | Probe returned data; local table is not populated or not mapped. |
| /stock/company-info | code=002396 | ok | 3 |  | professional_useful | doc | Probe returned data; local table is not populated or not mapped. |
| /stock/gpcphbts-tag | code=002396 | ok | 7 |  | professional_useful | doc | Probe returned data; local table is not populated or not mapped. |
| /stock/gudong | code=002396 | ok | 9 |  | professional_useful | doc | Probe returned data; local table is not populated or not mapped. |
| /stock/holding-funds | code=002396 | ok | 1 |  | professional_useful | doc | Probe returned data; local table is not populated or not mapped. |
| /stock/institutional-positions | code=002396 | ok | 1 |  | professional_useful | doc | Probe returned data; local table is not populated or not mapped. |
| /stock/message-bar | code=002396 | ok | 1 |  | professional_useful | doc | Probe returned data; local table is not populated or not mapped. |
| /theme/hot |  | ok | 13 |  | professional_useful | doc | Probe returned data; local table is not populated or not mapped. |
| /tuyere/by-stock | code=002396 | ok | 1 |  | professional_useful | doc | Probe returned data; local table is not populated or not mapped. |
| /xianhuo/list |  | ok | 385 |  | low_priority | doc | Probe returned data; local table is not populated or not mapped. |

## Reachable But Empty

| Endpoint | Params | Status | Count | Rows | Usefulness | Sources | Note |
| --- | --- | --- | --- | --- | --- | --- | --- |
| /advanced/bid-history |  | ok | 0 |  | professional_useful | doc | Endpoint is reachable but returned empty data. |
| /advanced/bidvol-kline | code=002396 | ok | 0 |  | professional_useful | doc | Endpoint is reachable but returned empty data. |
| /advanced/big-reminder |  | ok | 0 |  | professional_useful | doc | Endpoint is reachable but returned empty data. |
| /advanced/newhigh-group-stocks | group_type=all | ok | 0 |  | professional_useful | doc | Endpoint is reachable but returned empty data. |
| /advanced/rqz-data | code=002396 | ok | 0 |  | professional_useful | doc | Endpoint is reachable but returned empty data. |
| /advanced/voltur-history |  | ok | 0 |  | professional_useful | doc | Endpoint is reachable but returned empty data. |
| /market/emotion-money-detail |  | ok | 0 |  | professional_core | doc | Endpoint is reachable but returned empty data. |

## Needs Trading Session

| Endpoint | Params | Status | Count | Rows | Usefulness | Sources | Note |
| --- | --- | --- | --- | --- | --- | --- | --- |

## Needs Pagination Or Batch

| Endpoint | Params | Status | Count | Rows | Usefulness | Sources | Note |
| --- | --- | --- | --- | --- | --- | --- | --- |

## Parameter Uncertain

| Endpoint | Params | Status | Count | Rows | Usefulness | Sources | Note |
| --- | --- | --- | --- | --- | --- | --- | --- |
| /fengk/yd-plate-info |  | http_422 |  |  | professional_useful | doc | Endpoint exists but rejected the inferred parameter shape. |
| /news/concept-jxbk |  | http_422 |  |  | professional_useful | doc | Endpoint exists but rejected the inferred parameter shape. |
| /news/plate |  | http_422 |  |  | professional_useful | doc | Endpoint exists but rejected the inferred parameter shape. |

## API Error

| Endpoint | Params | Status | Count | Rows | Usefulness | Sources | Note |
| --- | --- | --- | --- | --- | --- | --- | --- |
| /advanced/bk-dj-arrange |  | http_404 |  |  | professional_useful | doc | Probe failed with http_404. |
| /advanced/morning-bidding-list |  | http_403 |  |  | professional_useful | doc | Probe failed with http_403. |
| /advanced/morning-bidding-summary |  | http_403 |  |  | professional_useful | doc | Probe failed with http_403. |
| /auction/bidding-anomaly | code=002396&date=2026-08-28 | http_403 |  |  | professional_core | doc | Probe failed with http_403. |
| /auction/tick | code=002396&date=2026-08-28 | http_403 |  |  | professional_core | doc | Probe failed with http_403. |

## Low Priority / Reference

| Endpoint | Params | Status | Count | Rows | Usefulness | Sources | Note |
| --- | --- | --- | --- | --- | --- | --- | --- |
| /etf/all | date=2026-08-28 | ok | 9 |  | low_priority | doc | Probe returned data; local table is not populated or not mapped. |
| /etf/ranking | date=2026-08-28 | ok | 9 |  | low_priority | doc | Probe returned data; local table is not populated or not mapped. |
| /finance/balance | code=002396 | ok | 18 |  | low_priority | doc | Probe returned data; local table is not populated or not mapped. |
| /finance/cashflow | code=002396 | ok | 18 |  | low_priority | doc | Probe returned data; local table is not populated or not mapped. |
| /finance/compare | code=002396 | ok | 17 |  | low_priority | doc | Probe returned data; local table is not populated or not mapped. |
| /finance/income | code=002396 | ok | 21 |  | low_priority | doc | Probe returned data; local table is not populated or not mapped. |
| /finance/summary | code=002396 | ok | 74 |  | low_priority | doc | Probe returned data; local table is not populated or not mapped. |
| /index/full-info | code=002396&date=2026-08-28 | ok | 1 |  | reference | doc | Probe returned data; local table is not populated or not mapped. |
| /index/intraday | code=002396&date=2026-08-28 | ok | 241 |  | reference | doc | Probe returned data; local table is not populated or not mapped. |
| /index/list | date=2026-08-28 | ok | 4 |  | reference | doc | Probe returned data; local table is not populated or not mapped. |
| /kline | code=002396&count=5&ktype=d | ok | 5 |  | reference | doc | Probe returned data; local table is not populated or not mapped. |
| /xianhuo/list |  | ok | 385 |  | low_priority | doc | Probe returned data; local table is not populated or not mapped. |

## Full Matrix

| Endpoint | Category | Param Type | Params | Verdict | Item Count | Table | Rows | Sources |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| /advanced/agency-list | advanced | none |  | api_available | 25 |  |  | doc |
| /advanced/bid-history | advanced | none |  | reachable_empty | 0 |  |  | doc |
| /advanced/bidvol-kline | advanced | code | code=002396 | reachable_empty | 0 |  |  | doc |
| /advanced/big-reminder | advanced | none |  | reachable_empty | 0 |  |  | doc |
| /advanced/bk-dj-arrange | advanced | none |  | api_error |  |  |  | doc |
| /advanced/bkjj-bl | advanced | none |  | api_available | 3 |  |  | doc |
| /advanced/business-list | advanced | none |  | api_available | 204 |  |  | doc |
| /advanced/chouma | advanced | code | code=002396 | api_available | 49 |  |  | doc |
| /advanced/concept-point | advanced | none |  | api_available | 4 |  |  | doc |
| /advanced/convertible-bonds-option | advanced | none |  | api_available | 2 |  |  | doc |
| /advanced/dadan-kline | advanced | code | code=002396 | api_available | 850 |  |  | doc |
| /advanced/dadan-kline-new | advanced | code | code=002396 | api_available | 600 |  |  | doc |
| /advanced/dadan-kline-today | advanced | code | code=002396&ktype=d | api_available | 1 |  |  | doc |
| /advanced/dadan-trend-incremental | advanced | code | code=002396 | api_available | 242 |  |  | doc |
| /advanced/disk-review | advanced | none |  | api_available | 2 |  |  | doc |
| /advanced/dp-explain | advanced | none |  | api_available | 3 |  |  | doc |
| /advanced/dp-realdata | advanced | none |  | api_available | 9 |  |  | doc |
| /advanced/duidao-kline | advanced | code | code=002396 | api_available | 600 |  |  | doc |
| /advanced/fengk-best | advanced | none |  | api_available | 30 |  |  | doc |
| /advanced/fenshi-kline-option | advanced | code | code=002396 | api_available | 1 |  |  | doc |
| /advanced/gudong-info | advanced | code | code=002396 | api_available | 20 |  |  | doc |
| /advanced/gudong-renshu | advanced | code | code=002396 | api_available | 11 |  |  | doc |
| /advanced/gujia-kline | advanced | code | code=002396 | api_available | 8 |  |  | doc |
| /advanced/his-sharp-withdrawal | advanced | none |  | api_available | 3 |  |  | doc |
| /advanced/his-zhangfu-detail | advanced | none |  | api_available | 37 |  |  | doc |
| /advanced/holiday | advanced | combination | year=2026 | api_available | 316 |  |  | doc |
| /advanced/interviews | advanced | range | end=2026-08-28&start=2026-06-01 | api_available | 1 |  |  | doc |
| /advanced/kline-today-dadan-new | advanced | code | code=002396&ktype=d | api_available | 6 |  |  | doc |
| /advanced/kline-today-duidao | advanced | code | code=002396&ktype=d | api_available | 7 |  |  | doc |
| /advanced/kline-today-main-activity | advanced | code | code=002396&ktype=d | api_available | 7 |  |  | doc |
| /advanced/kline-today-tyd | advanced | code | code=002396&ktype=d | api_available | 8 |  |  | doc |
| /advanced/kline-volume-forecast | advanced | code | code=002396 | api_available | 6 |  |  | doc |
| /advanced/kline-zhangting-reason | advanced | code | code=002396 | api_available | 7 |  |  | doc |
| /advanced/main-activity-kline | advanced | code | code=002396 | api_available | 600 |  |  | doc |
| /advanced/main-monitor | advanced | code | code=002396 | api_available | 30 |  |  | doc |
| /advanced/market-mood-count | advanced | none |  | api_available | 1 |  |  | doc |
| /advanced/market-radar | advanced | none |  | api_available | 15 |  |  | doc |
| /advanced/market-scln | advanced | none |  | api_available | 11 |  |  | doc |
| /advanced/morning-bidding-list | advanced | none |  | api_error |  |  |  | doc |
| /advanced/morning-bidding-summary | advanced | none |  | api_error |  |  |  | doc |
| /advanced/newhigh-group-count | advanced | none |  | api_available | 19 |  |  | doc |
| /advanced/newhigh-group-stocks | advanced | combination | group_type=all | reachable_empty | 0 |  |  | doc |
| /advanced/news-flash | advanced | none |  | api_available | 2 |  |  | doc |
| /advanced/news-flash-top | advanced | none |  | api_available | 30 |  |  | doc |
| /advanced/on-the-lhb | advanced | none |  | api_available | 1 |  |  | doc |
| /advanced/pankou | advanced | code | code=002396 | api_available | 1 |  |  | doc |
| /advanced/pianlizhi | advanced | none |  | api_available | 1 |  |  | doc |
| /advanced/pianlizhi-many | advanced | none |  | api_available | 1 |  |  | doc |
| /advanced/pianlizhi-w32 | advanced | none |  | api_available | 1 |  |  | doc |
| /advanced/pmsl | advanced | none |  | api_available | 9 |  |  | doc |
| /advanced/relation | advanced | none |  | api_available | 1 |  |  | doc |
| /advanced/rqz-data | advanced | code | code=002396 | reachable_empty | 0 |  |  | doc |
| /advanced/tuoyadan-kline | advanced | code | code=002396 | api_available | 600 |  |  | doc |
| /advanced/turnover-ten | advanced | code | code=002396 | api_available | 1 |  |  | doc |
| /advanced/vol-tur | advanced | code | code=002396 | api_available | 241 |  |  | doc |
| /advanced/voltur-history | advanced | none |  | reachable_empty | 0 |  |  | doc |
| /advanced/weight-performance | advanced | none |  | api_available | 2 |  |  | doc |
| /advanced/weipan-qiangchou | advanced | none |  | api_available | 3 |  |  | doc |
| /advanced/zhangting-expression | advanced | none |  | api_available | 12 |  |  | doc |
| /advanced/zhangting-gene | advanced | code | code=002396 | api_available | 6 |  |  | doc |
| /advanced/zjmm-min | advanced | code_date | code=002396&date=2026-08-28 | api_available | 241 |  |  | doc |
| /advanced/zs-real | advanced | none |  | api_available | 4 |  |  | doc |
| /auction/bidding-anomaly | auction | code_date | code=002396&date=2026-08-28 | api_error |  |  |  | doc |
| /auction/tick | auction | code_date | code=002396&date=2026-08-28 | api_error |  |  |  | doc |
| /daily | daily | date | date=2026-08-28 | api_available | 1 |  |  | doc |
| /daily/export | daily | date | date=2026-08-28 | api_available | 1 |  |  | doc |
| /daily/new-high | daily | date | date=2026-08-28 | api_available | 1 |  |  | doc |
| /daily/sentiment | daily | date | date=2026-08-28 | api_available | 1 |  |  | doc |
| /dingpan/all | dingpan | none |  | api_available | 1 |  |  | doc |
| /dingpan/jijin | dingpan | none |  | api_available | 1 |  |  | doc |
| /dingpan/module-versatile | dingpan | none |  | api_available | 1 |  |  | doc |
| /dingpan/radar | dingpan | combination | st=0 | api_available | 15 |  |  | doc |
| /dingpan/weipan | dingpan | none |  | api_available | 3 |  |  | doc |
| /etf/all | etf | date | date=2026-08-28 | api_available | 9 |  |  | doc |
| /etf/ranking | etf | date | date=2026-08-28 | api_available | 9 |  |  | doc |
| /fengk/list | fengk | pagination | date=2026-08-28&index=0&page_size=20 | api_available | 1 |  |  | doc |
| /fengk/yd-plate | fengk | none |  | api_available | 193 |  |  | doc |
| /fengk/yd-plate-info | fengk | none |  | param_uncertain |  |  |  | doc |
| /finance/balance | finance | code | code=002396 | api_available | 18 |  |  | doc |
| /finance/cashflow | finance | code | code=002396 | api_available | 18 |  |  | doc |
| /finance/compare | finance | code | code=002396 | api_available | 17 |  |  | doc |
| /finance/income | finance | code | code=002396 | api_available | 21 |  |  | doc |
| /finance/summary | finance | code | code=002396 | api_available | 74 |  |  | doc |
| /index/full-info | index | code_date | code=002396&date=2026-08-28 | api_available | 1 |  |  | doc |
| /index/intraday | index | code_date | code=002396&date=2026-08-28 | api_available | 241 |  |  | doc |
| /index/list | index | date | date=2026-08-28 | api_available | 4 |  |  | doc |
| /kline | kline | code | code=002396&count=5&ktype=d | api_available | 5 |  |  | doc |
| /l2/realtime/all-boards | l2 | none |  | api_available | 1 |  |  | doc |
| /l2/realtime/index-list | l2 | none |  | api_available | 4 |  |  | doc |
| /l2/realtime/index-trend | l2 | none |  | api_available | 1 |  |  | doc |
| /l2/realtime/sharp-withdrawal | l2 | none |  | api_available | 7 |  |  | doc |
| /l2/stock-bigorder | l2 | code_date | code=002396&date=2026-08-28 | api_available | 174 |  |  | doc |
| /l2/stock-intraday | l2 | code_date | code=002396&date=2026-08-28 | api_available | 241 |  |  | doc |
| /l2/tick-history | l2 | code_date | code=002396&date=2026-08-28 | api_available | 4565 |  |  | doc |
| /l2/tick-orders | l2 | code_date | code=002396&date=2026-08-28 | api_available | 30 |  |  | doc |
| /l2/tick-orders-all | l2 | code_date | code=002396&date=2026-08-28 | api_available | 16760 |  |  | doc |
| /ladder/board-stocks | ladder | date | date=2026-08-28 | api_available | 64 |  |  | doc |
| /ladder/broken | ladder | date | date=2026-08-28 | api_available | 39 |  |  | doc |
| /ladder/consecutive | ladder | date | date=2026-08-28 | api_available | 5 |  |  | doc |
| /ladder/market | ladder | date | date=2026-08-28 | api_available | 6 |  |  | doc |
| /ladder/realtime-boards | ladder | none |  | api_available | 64 |  |  | doc |
| /ladder/sector | ladder | date | date=2026-08-28 | api_available | 8 |  |  | doc |
| /ladder/sharp-withdrawal | ladder | date | date=2026-08-28 | api_available | 3 |  |  | doc |
| /lhb/dataframe | lhb | date | date=2026-08-28 | api_available | 51 |  |  | doc |
| /lhb/detail | lhb | code_date | code=002396&date=2026-08-28 | api_available | 1 |  |  | doc |
| /lhb/list | lhb | date | date=2026-08-28 | api_available | 51 |  |  | doc |
| /lhb/raw-list | lhb | date | date=2026-08-28 | api_available | 51 |  |  | doc |
| /lhb/top-title | lhb | none |  | api_available | 1 |  |  | doc |
| /lhb/update-list | lhb | none |  | api_available | 51 |  |  | doc |
| /lhb/youzi-dongxiang | lhb | date | date=2026-08-28 | api_available | 1 |  |  | doc |
| /market/emotion-money-date | market | none |  | api_available | 1 |  |  | doc |
| /market/emotion-money-detail | market | none |  | reachable_empty | 0 |  |  | doc |
| /market/limit-up-down | market | none |  | api_available | 250 |  |  | doc |
| /market/mood | market | none |  | api_available | 1 |  |  | doc |
| /market/rise-fall | market | date | date=2026-08-28 | api_available | 1 |  |  | doc |
| /news/concept-jxbk | news | none |  | param_uncertain |  |  |  | doc |
| /news/plate | news | none |  | param_uncertain |  |  |  | doc |
| /news/theme | news | pagination | index=0&page_size=20&type=-1 | api_available | 1 |  |  | doc |
| /sector/all-stocks | sector | code | code=801008 | api_available | 1 |  |  | doc |
| /sector/bk-fenshi-zhibo | sector | code_date | code=801008&date=2026-08-28 | api_available | 241 |  |  | doc |
| /sector/boom-reason | sector | code_date | code=801008&date=2026-08-28 | api_available | 1 |  |  | doc |
| /sector/capital | sector | code_date | code=801008&date=2026-08-28 | api_available | 1 |  |  | doc |
| /sector/plate-info-qj | sector | code | code=801008 | api_available | 23 |  |  | doc |
| /sector/ranking | sector | date | date=2026-08-28 | api_available | 16 |  |  | doc |
| /sector/son-plates | sector | code | code=801008 | api_available | 1 |  |  | doc |
| /sector/stocks | sector | code_date | code=801008&date=2026-08-28 | api_available | 1 |  |  | doc |
| /sector/strength | sector | code_date | code=801008&date=2026-08-28 | api_available | 1 |  |  | doc |
| /sector/strength-batch | sector | batch | codes=801008%2C801008&date=2026-08-28 | api_available | 1 |  |  | doc |
| /sector/strength-dataframe | sector | range | code=801008&end=2026-08-28&start=2026-06-01 | api_available | 65 |  |  | doc |
| /sector/strength-ndays | sector | range | code=801008&days=5&end_date=2026-08-28 | api_available | 112 |  |  | doc |
| /sector/sub-concepts | sector | code | code=801008 | api_available | 1 |  |  | doc |
| /stock/company-info | stock | code | code=002396 | api_available | 3 |  |  | doc |
| /stock/gpcphbts-tag | stock | code | code=002396 | api_available | 7 |  |  | doc |
| /stock/gudong | stock | code | code=002396 | api_available | 9 |  |  | doc |
| /stock/holding-funds | stock | code | code=002396 | api_available | 1 |  |  | doc |
| /stock/institutional-positions | stock | code | code=002396 | api_available | 1 |  |  | doc |
| /stock/message-bar | stock | code | code=002396 | api_available | 1 |  |  | doc |
| /theme/hot | theme | none |  | api_available | 13 |  |  | doc |
| /tuyere/by-stock | tuyere | code | code=002396 | api_available | 1 |  |  | doc |
| /xianhuo/list | xianhuo | none |  | api_available | 385 |  |  | doc |