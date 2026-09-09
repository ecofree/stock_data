# Full API Endpoint Inventory

## Scope

- Date: `2026-08-28`
- Stock sample: `002396`
- Sector sample: `801008`
- Discovered endpoints: `140`
- Safety: no API key or raw response body is written to this report.

## Summary

- `api_available`: 6
- `api_error`: 1
- `not_probed`: 130
- `reachable_empty`: 3

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

## Reachable But Empty

| Endpoint | Params | Status | Count | Rows | Usefulness | Sources | Note |
| --- | --- | --- | --- | --- | --- | --- | --- |
| /advanced/bid-history |  | ok | 0 |  | professional_useful | doc | Endpoint is reachable but returned empty data. |
| /advanced/bidvol-kline | code=002396 | ok | 0 |  | professional_useful | doc | Endpoint is reachable but returned empty data. |
| /advanced/big-reminder |  | ok | 0 |  | professional_useful | doc | Endpoint is reachable but returned empty data. |

## Needs Trading Session

| Endpoint | Params | Status | Count | Rows | Usefulness | Sources | Note |
| --- | --- | --- | --- | --- | --- | --- | --- |

## Needs Pagination Or Batch

| Endpoint | Params | Status | Count | Rows | Usefulness | Sources | Note |
| --- | --- | --- | --- | --- | --- | --- | --- |

## Parameter Uncertain

| Endpoint | Params | Status | Count | Rows | Usefulness | Sources | Note |
| --- | --- | --- | --- | --- | --- | --- | --- |

## API Error

| Endpoint | Params | Status | Count | Rows | Usefulness | Sources | Note |
| --- | --- | --- | --- | --- | --- | --- | --- |
| /advanced/bk-dj-arrange |  | http_404 |  |  | professional_useful | doc | Probe failed with http_404. |

## Low Priority / Reference

| Endpoint | Params | Status | Count | Rows | Usefulness | Sources | Note |
| --- | --- | --- | --- | --- | --- | --- | --- |
| /etf/all | date | not_probed |  |  | low_priority | doc |  |
| /etf/ranking | date | not_probed |  |  | low_priority | doc |  |
| /finance/balance | code | not_probed |  |  | low_priority | doc |  |
| /finance/cashflow | code | not_probed |  |  | low_priority | doc |  |
| /finance/compare | code | not_probed |  |  | low_priority | doc |  |
| /finance/income | code | not_probed |  |  | low_priority | doc |  |
| /finance/summary | code | not_probed |  |  | low_priority | doc |  |
| /index/full-info | code,date | not_probed |  |  | reference | doc |  |
| /index/intraday | code,date | not_probed |  |  | reference | doc |  |
| /index/list | date | not_probed |  |  | reference | doc |  |
| /kline | code,count,ktype | not_probed |  |  | reference | doc |  |
| /xianhuo/list |  | not_probed |  |  | low_priority | doc |  |

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
| /advanced/dadan-kline | advanced | code | code | not_probed |  |  |  | doc |
| /advanced/dadan-kline-new | advanced | code | code | not_probed |  |  |  | doc |
| /advanced/dadan-kline-today | advanced | code | code,ktype | not_probed |  |  |  | doc |
| /advanced/dadan-trend-incremental | advanced | code | code | not_probed |  |  |  | doc |
| /advanced/disk-review | advanced | none |  | not_probed |  |  |  | doc |
| /advanced/dp-explain | advanced | none |  | not_probed |  |  |  | doc |
| /advanced/dp-realdata | advanced | none |  | not_probed |  |  |  | doc |
| /advanced/duidao-kline | advanced | code | code | not_probed |  |  |  | doc |
| /advanced/fengk-best | advanced | none |  | not_probed |  |  |  | doc |
| /advanced/fenshi-kline-option | advanced | code | code | not_probed |  |  |  | doc |
| /advanced/gudong-info | advanced | code | code | not_probed |  |  |  | doc |
| /advanced/gudong-renshu | advanced | code | code | not_probed |  |  |  | doc |
| /advanced/gujia-kline | advanced | code | code | not_probed |  |  |  | doc |
| /advanced/his-sharp-withdrawal | advanced | none |  | not_probed |  |  |  | doc |
| /advanced/his-zhangfu-detail | advanced | none |  | not_probed |  |  |  | doc |
| /advanced/holiday | advanced | combination | year | not_probed |  |  |  | doc |
| /advanced/interviews | advanced | range | end,start | not_probed |  |  |  | doc |
| /advanced/kline-today-dadan-new | advanced | code | code,ktype | not_probed |  |  |  | doc |
| /advanced/kline-today-duidao | advanced | code | code,ktype | not_probed |  |  |  | doc |
| /advanced/kline-today-main-activity | advanced | code | code,ktype | not_probed |  |  |  | doc |
| /advanced/kline-today-tyd | advanced | code | code,ktype | not_probed |  |  |  | doc |
| /advanced/kline-volume-forecast | advanced | code | code | not_probed |  |  |  | doc |
| /advanced/kline-zhangting-reason | advanced | code | code | not_probed |  |  |  | doc |
| /advanced/main-activity-kline | advanced | code | code | not_probed |  |  |  | doc |
| /advanced/main-monitor | advanced | code | code | not_probed |  |  |  | doc |
| /advanced/market-mood-count | advanced | none |  | not_probed |  |  |  | doc |
| /advanced/market-radar | advanced | none |  | not_probed |  |  |  | doc |
| /advanced/market-scln | advanced | none |  | not_probed |  |  |  | doc |
| /advanced/morning-bidding-list | advanced | none |  | not_probed |  |  |  | doc |
| /advanced/morning-bidding-summary | advanced | none |  | not_probed |  |  |  | doc |
| /advanced/newhigh-group-count | advanced | none |  | not_probed |  |  |  | doc |
| /advanced/newhigh-group-stocks | advanced | combination | group_type | not_probed |  |  |  | doc |
| /advanced/news-flash | advanced | none |  | not_probed |  |  |  | doc |
| /advanced/news-flash-top | advanced | none |  | not_probed |  |  |  | doc |
| /advanced/on-the-lhb | advanced | none |  | not_probed |  |  |  | doc |
| /advanced/pankou | advanced | code | code | not_probed |  |  |  | doc |
| /advanced/pianlizhi | advanced | none |  | not_probed |  |  |  | doc |
| /advanced/pianlizhi-many | advanced | none |  | not_probed |  |  |  | doc |
| /advanced/pianlizhi-w32 | advanced | none |  | not_probed |  |  |  | doc |
| /advanced/pmsl | advanced | none |  | not_probed |  |  |  | doc |
| /advanced/relation | advanced | none |  | not_probed |  |  |  | doc |
| /advanced/rqz-data | advanced | code | code | not_probed |  |  |  | doc |
| /advanced/tuoyadan-kline | advanced | code | code | not_probed |  |  |  | doc |
| /advanced/turnover-ten | advanced | code | code | not_probed |  |  |  | doc |
| /advanced/vol-tur | advanced | code | code | not_probed |  |  |  | doc |
| /advanced/voltur-history | advanced | none |  | not_probed |  |  |  | doc |
| /advanced/weight-performance | advanced | none |  | not_probed |  |  |  | doc |
| /advanced/weipan-qiangchou | advanced | none |  | not_probed |  |  |  | doc |
| /advanced/zhangting-expression | advanced | none |  | not_probed |  |  |  | doc |
| /advanced/zhangting-gene | advanced | code | code | not_probed |  |  |  | doc |
| /advanced/zjmm-min | advanced | code_date | code,date | not_probed |  |  |  | doc |
| /advanced/zs-real | advanced | none |  | not_probed |  |  |  | doc |
| /auction/bidding-anomaly | auction | code_date | code,date | not_probed |  |  |  | doc |
| /auction/tick | auction | code_date | code,date | not_probed |  |  |  | doc |
| /daily | daily | date | date | not_probed |  |  |  | doc |
| /daily/export | daily | date | date | not_probed |  |  |  | doc |
| /daily/new-high | daily | date | date | not_probed |  |  |  | doc |
| /daily/sentiment | daily | date | date | not_probed |  |  |  | doc |
| /dingpan/all | dingpan | none |  | not_probed |  |  |  | doc |
| /dingpan/jijin | dingpan | none |  | not_probed |  |  |  | doc |
| /dingpan/module-versatile | dingpan | none |  | not_probed |  |  |  | doc |
| /dingpan/radar | dingpan | combination | st | not_probed |  |  |  | doc |
| /dingpan/weipan | dingpan | none |  | not_probed |  |  |  | doc |
| /etf/all | etf | date | date | not_probed |  |  |  | doc |
| /etf/ranking | etf | date | date | not_probed |  |  |  | doc |
| /fengk/list | fengk | pagination | date,index,page_size | not_probed |  |  |  | doc |
| /fengk/yd-plate | fengk | none |  | not_probed |  |  |  | doc |
| /fengk/yd-plate-info | fengk | none |  | not_probed |  |  |  | doc |
| /finance/balance | finance | code | code | not_probed |  |  |  | doc |
| /finance/cashflow | finance | code | code | not_probed |  |  |  | doc |
| /finance/compare | finance | code | code | not_probed |  |  |  | doc |
| /finance/income | finance | code | code | not_probed |  |  |  | doc |
| /finance/summary | finance | code | code | not_probed |  |  |  | doc |
| /index/full-info | index | code_date | code,date | not_probed |  |  |  | doc |
| /index/intraday | index | code_date | code,date | not_probed |  |  |  | doc |
| /index/list | index | date | date | not_probed |  |  |  | doc |
| /kline | kline | code | code,count,ktype | not_probed |  |  |  | doc |
| /l2/realtime/all-boards | l2 | none |  | not_probed |  |  |  | doc |
| /l2/realtime/index-list | l2 | none |  | not_probed |  |  |  | doc |
| /l2/realtime/index-trend | l2 | none |  | not_probed |  |  |  | doc |
| /l2/realtime/sharp-withdrawal | l2 | none |  | not_probed |  |  |  | doc |
| /l2/stock-bigorder | l2 | code_date | code,date | not_probed |  |  |  | doc |
| /l2/stock-intraday | l2 | code_date | code,date | not_probed |  |  |  | doc |
| /l2/tick-history | l2 | code_date | code,date | not_probed |  |  |  | doc |
| /l2/tick-orders | l2 | code_date | code,date | not_probed |  |  |  | doc |
| /l2/tick-orders-all | l2 | code_date | code,date | not_probed |  |  |  | doc |
| /ladder/board-stocks | ladder | date | date | not_probed |  |  |  | doc |
| /ladder/broken | ladder | date | date | not_probed |  |  |  | doc |
| /ladder/consecutive | ladder | date | date | not_probed |  |  |  | doc |
| /ladder/market | ladder | date | date | not_probed |  |  |  | doc |
| /ladder/realtime-boards | ladder | none |  | not_probed |  |  |  | doc |
| /ladder/sector | ladder | date | date | not_probed |  |  |  | doc |
| /ladder/sharp-withdrawal | ladder | date | date | not_probed |  |  |  | doc |
| /lhb/dataframe | lhb | date | date | not_probed |  |  |  | doc |
| /lhb/detail | lhb | code_date | code,date | not_probed |  |  |  | doc |
| /lhb/list | lhb | date | date | not_probed |  |  |  | doc |
| /lhb/raw-list | lhb | date | date | not_probed |  |  |  | doc |
| /lhb/top-title | lhb | none |  | not_probed |  |  |  | doc |
| /lhb/update-list | lhb | none |  | not_probed |  |  |  | doc |
| /lhb/youzi-dongxiang | lhb | date | date | not_probed |  |  |  | doc |
| /market/emotion-money-date | market | none |  | not_probed |  |  |  | doc |
| /market/emotion-money-detail | market | none |  | not_probed |  |  |  | doc |
| /market/limit-up-down | market | none |  | not_probed |  |  |  | doc |
| /market/mood | market | none |  | not_probed |  |  |  | doc |
| /market/rise-fall | market | date | date | not_probed |  |  |  | doc |
| /news/concept-jxbk | news | none |  | not_probed |  |  |  | doc |
| /news/plate | news | none |  | not_probed |  |  |  | doc |
| /news/theme | news | pagination | index,page_size,type | not_probed |  |  |  | doc |
| /sector/all-stocks | sector | code | code | not_probed |  |  |  | doc |
| /sector/bk-fenshi-zhibo | sector | code_date | code,date | not_probed |  |  |  | doc |
| /sector/boom-reason | sector | code_date | code,date | not_probed |  |  |  | doc |
| /sector/capital | sector | code_date | code,date | not_probed |  |  |  | doc |
| /sector/plate-info-qj | sector | code | code | not_probed |  |  |  | doc |
| /sector/ranking | sector | date | date | not_probed |  |  |  | doc |
| /sector/son-plates | sector | code | code | not_probed |  |  |  | doc |
| /sector/stocks | sector | code_date | code,date | not_probed |  |  |  | doc |
| /sector/strength | sector | code_date | code,date | not_probed |  |  |  | doc |
| /sector/strength-batch | sector | batch | codes,date | not_probed |  |  |  | doc |
| /sector/strength-dataframe | sector | range | code,end,start | not_probed |  |  |  | doc |
| /sector/strength-ndays | sector | range | code,days,end_date | not_probed |  |  |  | doc |
| /sector/sub-concepts | sector | code | code | not_probed |  |  |  | doc |
| /stock/company-info | stock | code | code | not_probed |  |  |  | doc |
| /stock/gpcphbts-tag | stock | code | code | not_probed |  |  |  | doc |
| /stock/gudong | stock | code | code | not_probed |  |  |  | doc |
| /stock/holding-funds | stock | code | code | not_probed |  |  |  | doc |
| /stock/institutional-positions | stock | code | code | not_probed |  |  |  | doc |
| /stock/message-bar | stock | code | code | not_probed |  |  |  | doc |
| /theme/hot | theme | none |  | not_probed |  |  |  | doc |
| /tuyere/by-stock | tuyere | code | code | not_probed |  |  |  | doc |
| /xianhuo/list | xianhuo | none |  | not_probed |  |  |  | doc |