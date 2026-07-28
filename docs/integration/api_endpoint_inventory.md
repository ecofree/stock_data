# Full API Endpoint Inventory

## Scope

- Date: `2026-07-06`
- Stock sample: `603137`
- Sector sample: `801001`
- Discovered endpoints: `175`
- Safety: no API key or raw response body is written to this report.

## Summary

- `api_available`: 59
- `api_error`: 1
- `needs_trading_session`: 3
- `param_uncertain`: 2
- `reachable_empty`: 14
- `stable_available`: 96

## Source Coverage

- `code`: 128
- `doc`: 140
- `schema`: 174

## Parameter Types

- `batch`: 2
- `code`: 56
- `code_date`: 19
- `combination`: 5
- `date`: 28
- `none`: 58
- `pagination`: 4
- `range`: 3

## Professional Usefulness

- `low_priority`: 15
- `professional_core`: 53
- `professional_useful`: 102
- `reference`: 5

## Stable Available

| Endpoint | Params | Status | Count | Rows | Usefulness | Sources | Note |
| --- | --- | --- | --- | --- | --- | --- | --- |
| /advanced/agency-list | date=2026-07-06 | ok | 44 | 104 | professional_useful | code,doc,schema | Probe returned data and local table has stored rows. |
| /advanced/bidvol-kline | code=603137 | ok | 600 | 200 | professional_useful | code,doc,schema | Probe returned data and local table has stored rows. |
| /advanced/business-list |  | ok | 273 | 598 | professional_useful | code,doc,schema | Probe returned data and local table has stored rows. |
| /advanced/chouma | code=603137&date=2026-07-06 | ok | 49 | 200 | professional_useful | code,doc,schema | Probe returned data and local table has stored rows. |
| /advanced/concept-point |  | ok | 3 | 8 | professional_useful | code,doc,schema | Probe returned data and local table has stored rows. |
| /advanced/dadan-kline | code=603137 | ok | 768 | 115 | professional_useful | code,doc,schema | Probe returned data and local table has stored rows. |
| /advanced/dadan-kline-new | code=603137 | ok | 600 | 200 | professional_useful | code,doc,schema | Probe returned data and local table has stored rows. |
| /advanced/dadan-kline-today | code=603137&ktype=d | ok | 1 | 200 | professional_useful | code,doc,schema | Probe returned data and local table has stored rows. |
| /advanced/disk-review | date=2026-07-06 | ok | 2 | 2 | professional_useful | code,doc,schema | Probe returned data and local table has stored rows. |
| /advanced/duidao-kline | code=603137 | ok | 600 | 200 | professional_useful | code,doc,schema | Probe returned data and local table has stored rows. |
| /advanced/fengk-best |  | ok | 30 | 60 | professional_useful | code,doc,schema | Probe returned data and local table has stored rows. |
| /advanced/gudong-info | code=603137 | ok | 16 | 200 | professional_useful | code,doc,schema | Probe returned data and local table has stored rows. |
| /advanced/gudong-renshu | code=603137 | ok | 6 | 200 | professional_useful | code,doc,schema | Probe returned data and local table has stored rows. |
| /advanced/gujia-kline | code=603137 | ok | 8 | 200 | professional_useful | code,doc,schema | Probe returned data and local table has stored rows. |
| /advanced/his-ranking-info | date=2026-07-06 | ok | 26 | 2 | professional_useful | code,schema | Probe returned data and local table has stored rows. |
| /advanced/kline-today | code=603137&ktype=d | ok | 1 | 50 | professional_useful | code,schema | Probe returned data and local table has stored rows. |
| /advanced/main-activity-kline | code=603137 | ok | 600 | 200 | professional_useful | code,doc,schema | Probe returned data and local table has stored rows. |
| /advanced/main-monitor | code=603137 | ok | 30 | 200 | professional_useful | code,doc,schema | Probe returned data and local table has stored rows. |
| /advanced/market-mood-count |  | ok | 1 | 2 | professional_useful | code,doc,schema | Probe returned data and local table has stored rows. |
| /advanced/market-radar |  | ok | 15 | 30 | professional_useful | code,doc,schema | Probe returned data and local table has stored rows. |
| /advanced/market-scln | date=2026-07-06 | ok | 9 | 2 | professional_useful | code,doc,schema | Probe returned data and local table has stored rows. |
| /advanced/morning-bidding-summary |  | ok | 8 | 7 | professional_useful | code,doc,schema | Probe returned data and local table has stored rows. |
| /advanced/news-flash |  | ok | 2 | 4 | professional_useful | code,doc,schema | Probe returned data and local table has stored rows. |
| /advanced/on-the-lhb |  | ok | 8 | 18 | professional_useful | code,doc,schema | Probe returned data and local table has stored rows. |
| /advanced/pankou | code=603137 | ok | 1 | 71 | professional_useful | code,doc,schema | Probe returned data and local table has stored rows. |
| /advanced/pianlizhi |  | ok | 1 | 2 | professional_useful | code,doc,schema | Probe returned data and local table has stored rows. |
| /advanced/relation |  | ok | 1 | 2 | professional_useful | code,doc,schema | Probe returned data and local table has stored rows. |
| /advanced/rqz-data | code=603137 | ok | 200 | 200 | professional_useful | code,doc,schema | Probe returned data and local table has stored rows. |
| /advanced/tuoyadan-kline | code=603137 | ok | 599 | 200 | professional_useful | code,doc,schema | Probe returned data and local table has stored rows. |
| /advanced/vol-tur | code=603137 | ok | 68 | 200 | professional_useful | code,doc,schema | Probe returned data and local table has stored rows. |
| /auction/bidding-anomaly | code=603137&date=2026-07-06 | ok | 3 | 3 | professional_core | code,doc,schema | Probe returned data and local table has stored rows. |
| /daily | date=2026-07-06 | ok | 1 | 250 | professional_core | code,doc,schema | Probe returned data and local table has stored rows. |
| /daily/export | date=2026-07-06 | ok | 1 | 2 | professional_core | code,doc,schema | Probe returned data and local table has stored rows. |
| /daily/new-high | date=2026-07-06 | ok | 1 | 250 | professional_core | code,doc,schema | Probe returned data and local table has stored rows. |
| /daily/sentiment | date=2026-07-06 | ok | 1 | 250 | professional_core | code,doc,schema | Probe returned data and local table has stored rows. |
| /dingpan/all |  | ok | 1 | 4 | professional_useful | code,doc,schema | Probe returned data and local table has stored rows. |
| /dingpan/module-versatile |  | ok | 1 | 6 | professional_useful | code,doc,schema | Probe returned data and local table has stored rows. |
| /dingpan/northbound-close-date |  | ok | 225 | 450 | professional_useful | code,schema | Probe returned data and local table has stored rows. |
| /dingpan/radar | st=0 | ok | 15 | 36 | professional_useful | code,doc,schema | Probe returned data and local table has stored rows. |
| /dingpan/southbound-close-date |  | ok | 213 | 426 | professional_useful | code,schema | Probe returned data and local table has stored rows. |
| /dingpan/weipan |  | ok | 3 | 18 | professional_useful | code,doc,schema | Probe returned data and local table has stored rows. |
| /etf/ranking | date=2026-07-06 | ok | 4 | 8 | low_priority | code,doc,schema | Probe returned data and local table has stored rows. |
| /fengk/list | date=2026-07-06&index=0&page_size=20 | ok | 1 | 1272 | professional_useful | code,doc,schema | Probe returned data and local table has stored rows. |
| /index/full-info | code=603137&date=2026-07-06 | ok | 1 | 4 | reference | code,doc,schema | Probe returned data and local table has stored rows. |
| /index/intraday | code=603137&date=2026-07-06 | ok | 241 | 964 | reference | code,doc,schema | Probe returned data and local table has stored rows. |
| /index/zhishu-kline | code=603137 | ok | 630 | 2520 | reference | code,schema | Probe returned data and local table has stored rows. |
| /kline | code=603137&count=5&ktype=d | ok | 5 | 448 | reference | code,doc,schema | Probe returned data and local table has stored rows. |
| /l2/realtime/all-boards |  | ok | 1 | 64 | professional_core | code,doc,schema | Probe returned data and local table has stored rows. |
| /l2/realtime/index-list |  | ok | 4 | 12 | professional_core | code,doc,schema | Probe returned data and local table has stored rows. |
| /l2/realtime/index-trend |  | ok | 1 | 1 | professional_core | code,doc,schema | Probe returned data and local table has stored rows. |
| /l2/sector-intraday | code=801001&date=2026-07-06 | ok | 69 | 3840 | professional_core | code,schema | Probe returned data and local table has stored rows. |
| /l2/sector-volume | code=801001 | ok | 69 | 88 | professional_core | code,schema | Probe returned data and local table has stored rows. |
| /l2/stock-bigorder | code=603137&date=2026-07-06 | ok | 4 | 2666 | professional_core | code,doc,schema | Probe returned data and local table has stored rows. |
| /l2/stock-intraday | code=603137&date=2026-07-06 | ok | 241 | 5543 | professional_core | code,doc,schema | Probe returned data and local table has stored rows. |
| /l2/tick-history | code=603137&date=2026-07-06 | ok | 484 | 3033 | professional_core | code,doc,schema | Probe returned data and local table has stored rows. |
| /l2/tick-orders | code=603137&date=2026-07-06 | ok | 30 | 82 | professional_core | code,doc,schema | Probe returned data and local table has stored rows. |
| /l2/tick-orders-all | code=603137&date=2026-07-06 | ok | 5686 | 1320 | professional_core | code,doc,schema | Probe returned data and local table has stored rows. |
| /ladder/board-stocks | date=2026-07-06 | ok | 22 | 236 | professional_core | code,doc,schema | Probe returned data and local table has stored rows. |
| /ladder/broken | date=2026-07-06 | ok | 30 | 31 | professional_core | code,doc,schema | Probe returned data and local table has stored rows. |
| /ladder/consecutive | date=2026-07-06 | ok | 5 | 53 | professional_core | code,doc,schema | Probe returned data and local table has stored rows. |
| /ladder/market | date=2026-07-06 | ok | 5 | 241 | professional_core | code,doc,schema | Probe returned data and local table has stored rows. |
| /ladder/realtime-boards |  | ok | 22 | 112 | professional_core | code,doc,schema | Probe returned data and local table has stored rows. |
| /ladder/sector | date=2026-07-06 | ok | 7 | 53 | professional_core | code,doc,schema | Probe returned data and local table has stored rows. |
| /ladder/sharp-withdrawal | date=2026-07-06 | ok | 3 | 41 | professional_core | code,doc,schema | Probe returned data and local table has stored rows. |
| /lhb/dataframe | date=2026-07-06 | ok | 86 | 15 | professional_core | code,doc,schema | Probe returned data and local table has stored rows. |
| /lhb/detail | code=603137&date=2026-07-06 | ok | 1 | 161 | professional_core | code,doc,schema | Probe returned data and local table has stored rows. |
| /lhb/list | date=2026-07-06 | ok | 86 | 93 | professional_core | code,doc,schema | Probe returned data and local table has stored rows. |
| /lhb/raw-list | date=2026-07-06 | ok | 86 | 15 | professional_core | code,doc,schema | Probe returned data and local table has stored rows. |
| /lhb/top-title |  | ok | 1 | 22 | professional_core | code,doc,schema | Probe returned data and local table has stored rows. |
| /lhb/update-list |  | ok | 80 | 14 | professional_core | code,doc,schema | Probe returned data and local table has stored rows. |
| /lhb/youzi-dongxiang | date=2026-07-06 | ok | 1 | 30 | professional_core | code,doc,schema | Probe returned data and local table has stored rows. |
| /market/emotion-money-date |  | ok | 1 | 60 | professional_core | code,doc,schema | Probe returned data and local table has stored rows. |
| /market/emotion-money-detail |  | ok | 1 | 22 | professional_core | code,doc,schema | Probe returned data and local table has stored rows. |
| /market/limit-up-down |  | ok | 250 | 500 | professional_core | code,doc,schema | Probe returned data and local table has stored rows. |
| /market/mood |  | ok | 1 | 1 | professional_core | code,doc,schema | Probe returned data and local table has stored rows. |
| /market/rise-fall | date=2026-07-06 | ok | 1 | 250 | professional_core | code,doc,schema | Probe returned data and local table has stored rows. |
| /news/columns |  | ok | 1 | 20 | professional_useful | code,schema | Probe returned data and local table has stored rows. |
| /news/concept-jxbk |  | ok | 13 | 26 | professional_useful | code,doc,schema | Probe returned data and local table has stored rows. |
| /sector/all-stocks | code=801001 | ok | 5 | 14 | professional_core | code,doc,schema | Probe returned data and local table has stored rows. |
| /sector/boom-reason | code=801001&date=2026-07-06 | ok | 1 | 116 | professional_core | code,doc,schema | Probe returned data and local table has stored rows. |
| /sector/capital | code=801001&date=2026-07-06 | ok | 1 | 18 | professional_core | code,doc,schema | Probe returned data and local table has stored rows. |
| /sector/parent-plate | code=801001 | ok | 1 | 100 | professional_core | code,schema | Probe returned data and local table has stored rows. |
| /sector/plate-info-qj | code=801001 | ok | 22 | 40 | professional_core | code,doc,schema | Probe returned data and local table has stored rows. |
| /sector/plates |  | ok | 58 | 58 | professional_core | code,schema | Probe returned data and local table has stored rows. |
| /sector/ranking | date=2026-07-06 | ok | 9 | 17 | professional_core | code,doc,schema | Probe returned data and local table has stored rows. |
| /sector/stocks | code=801001&date=2026-07-06 | ok | 5 | 71 | professional_core | code,doc,schema | Probe returned data and local table has stored rows. |
| /sector/strength | code=801001&date=2026-07-06 | ok | 1 | 58 | professional_core | code,doc,schema | Probe returned data and local table has stored rows. |
| /sector/strength-dataframe | code=801001&end=2026-07-06&start=2026-06-01 | ok | 26 | 40 | professional_core | code,doc,schema | Probe returned data and local table has stored rows. |
| /sector/strength-ndays | code=801001&days=5&end_date=2026-07-06 | ok | 63 | 60 | professional_core | code,doc,schema | Probe returned data and local table has stored rows. |
| /stock/articles | code=603137 | ok | 1 | 200 | professional_useful | code,schema | Probe returned data and local table has stored rows. |
| /stock/company-info | code=603137 | ok | 3 | 71 | professional_useful | code,doc,schema | Probe returned data and local table has stored rows. |
| /stock/gudong | code=603137 | ok | 9 | 639 | professional_useful | code,doc,schema | Probe returned data and local table has stored rows. |
| /stock/holding-funds | code=603137 | ok | 1 | 200 | professional_useful | code,doc,schema | Probe returned data and local table has stored rows. |
| /stock/institutional-positions | code=603137 | ok | 1 | 639 | professional_useful | code,doc,schema | Probe returned data and local table has stored rows. |
| /stock/message-bar | code=603137 | ok | 1 | 200 | professional_useful | code,doc,schema | Probe returned data and local table has stored rows. |
| /theme/hot |  | ok | 13 | 26 | professional_useful | code,doc,schema | Probe returned data and local table has stored rows. |

## Available But Not Stored

| Endpoint | Params | Status | Count | Rows | Usefulness | Sources | Note |
| --- | --- | --- | --- | --- | --- | --- | --- |
| /advanced/bkjj-bl |  | ok | 3 | 0 | professional_useful | doc,schema | Probe returned data; local table is not populated or not mapped. |
| /advanced/company-count | codes=801001%2C603137 | ok | 1 | 0 | professional_useful | code,schema | Probe returned data; local table is not populated or not mapped. |
| /advanced/convertible-bonds-option |  | ok | 2 | 0 | professional_useful | doc,schema | Probe returned data; local table is not populated or not mapped. |
| /advanced/corporate-news | code=603137 | ok | 25 | 0 | professional_useful | schema | Probe returned data; local table is not populated or not mapped. |
| /advanced/dadan-trend-incremental | code=603137 | ok | 67 | 0 | professional_useful | doc,schema | Probe returned data; local table is not populated or not mapped. |
| /advanced/dp-explain |  | ok | 3 | 0 | professional_useful | doc,schema | Probe returned data; local table is not populated or not mapped. |
| /advanced/dp-realdata |  | ok | 9 | 0 | professional_useful | doc,schema | Probe returned data; local table is not populated or not mapped. |
| /advanced/fenbi2 | code=603137 | ok | 811 | 0 | professional_useful | schema | Probe returned data; local table is not populated or not mapped. |
| /advanced/fenshi-kline-option | code=603137 | ok | 1 | 0 | professional_useful | doc,schema | Probe returned data; local table is not populated or not mapped. |
| /advanced/his-ranking | date=2026-07-06 | ok | 26 | 0 | professional_useful | code,schema | Probe returned data; local table is not populated or not mapped. |
| /advanced/his-sharp-withdrawal | date=2026-07-06 | ok | 3 | 0 | professional_useful | code,doc,schema | Probe returned data; local table is not populated or not mapped. |
| /advanced/holiday | year=2026 | ok | 316 | 0 | professional_useful | code,doc,schema | Probe returned data; local table is not populated or not mapped. |
| /advanced/interviews | end=2026-07-06&start=2026-06-01 | ok | 30 | 0 | professional_useful | code,doc,schema | Probe returned data; local table is not populated or not mapped. |
| /advanced/kline-today-dadan-new | code=603137&ktype=d | ok | 6 | 0 | professional_useful | code,doc,schema | Probe returned data; local table is not populated or not mapped. |
| /advanced/kline-today-duidao | code=603137&ktype=d | ok | 7 | 0 | professional_useful | code,doc,schema | Probe returned data; local table is not populated or not mapped. |
| /advanced/kline-today-main-activity | code=603137&ktype=d | ok | 7 | 0 | professional_useful | code,doc,schema | Probe returned data; local table is not populated or not mapped. |
| /advanced/kline-today-tyd | code=603137&ktype=d | ok | 8 | 0 | professional_useful | code,doc,schema | Probe returned data; local table is not populated or not mapped. |
| /advanced/kline-volume-forecast | code=603137 | ok | 6 | 0 | professional_useful | doc,schema | Probe returned data; local table is not populated or not mapped. |
| /advanced/kline-zhangting-reason | code=603137 | ok | 7 | 0 | professional_useful | doc,schema | Probe returned data; local table is not populated or not mapped. |
| /advanced/newhigh-group-count |  | ok | 15 | 0 | professional_useful | code,doc,schema | Probe returned data; local table is not populated or not mapped. |
| /advanced/news-flash-top |  | ok | 30 | 0 | professional_useful | doc,schema | Probe returned data; local table is not populated or not mapped. |
| /advanced/pianlizhi-many |  | ok | 1 | 0 | professional_useful | doc,schema | Probe returned data; local table is not populated or not mapped. |
| /advanced/pianlizhi-w32 |  | ok | 1 | 0 | professional_useful | doc,schema | Probe returned data; local table is not populated or not mapped. |
| /advanced/stock-plate-new | code=603137 | ok | 6 | 0 | professional_useful | schema | Probe returned data; local table is not populated or not mapped. |
| /advanced/trend-min | code=603137&date=2026-07-06 | ok | 241 | 0 | professional_useful | schema | Probe returned data; local table is not populated or not mapped. |
| /advanced/turnover-ten | code=603137 | ok | 1 | 0 | professional_useful | doc,schema | Probe returned data; local table is not populated or not mapped. |
| /advanced/weight-performance | date=2026-07-06 | ok | 2 | 0 | professional_useful | code,doc,schema | Probe returned data; local table is not populated or not mapped. |
| /advanced/weipan-qiangchou |  | ok | 3 | 0 | professional_useful | code,doc,schema | Probe returned data; local table is not populated or not mapped. |
| /advanced/zhangting-expression | date=2026-07-06 | ok | 12 | 0 | professional_useful | code,doc,schema | Probe returned data; local table is not populated or not mapped. |
| /advanced/zhangting-gene | code=603137 | ok | 6 | 0 | professional_useful | doc,schema | Probe returned data; local table is not populated or not mapped. |
| /advanced/zjmm-min | code=603137&date=2026-07-06 | ok | 241 | 0 | professional_useful | doc,schema | Probe returned data; local table is not populated or not mapped. |
| /dingpan/jijin |  | ok | 1 | 0 | professional_useful | code,doc,schema | Probe returned data; local table is not populated or not mapped. |
| /etf/all | date=2026-07-06 | ok | 3 | 0 | low_priority | code,doc,schema | Probe returned data; local table is not populated or not mapped. |
| /fengk/yd-plate | date=2026-07-06 | ok | 204 | 0 | professional_useful | code,doc,schema | Probe returned data; local table is not populated or not mapped. |
| /finance/balance | code=603137 | ok | 5 | 0 | low_priority | doc,schema | Probe returned data; local table is not populated or not mapped. |
| /finance/cashflow | code=603137 | ok | 5 | 0 | low_priority | doc,schema | Probe returned data; local table is not populated or not mapped. |
| /finance/compare | code=603137 | ok | 4 | 0 | low_priority | doc,schema | Probe returned data; local table is not populated or not mapped. |
| /finance/income | code=603137 | ok | 8 | 0 | low_priority | doc,schema | Probe returned data; local table is not populated or not mapped. |
| /finance/summary | code=603137 | ok | 22 | 0 | low_priority | doc,schema | Probe returned data; local table is not populated or not mapped. |
| /forums/column |  | ok | 1 | 0 | low_priority | schema | Probe returned data; local table is not populated or not mapped. |
| /forums/focus |  | ok | 1 | 0 | low_priority | schema | Probe returned data; local table is not populated or not mapped. |
| /forums/sel-list | index=0&page_size=20 | ok | 1 | 0 | low_priority | schema | Probe returned data; local table is not populated or not mapped. |
| /index/list | date=2026-07-06 | ok | 4 | 0 | reference | code,doc,schema | Probe returned data; local table is not populated or not mapped. |
| /l2/realtime/sharp-withdrawal |  | ok | 5 | 0 | professional_core | doc,schema | Probe returned data; local table is not populated or not mapped. |
| /news/index-plate |  | ok | 6 | 0 | professional_useful | code,schema | Probe returned data; local table is not populated or not mapped. |
| /news/plate | code=603137 | ok | 1 | 0 | professional_useful | code,doc,schema | Probe returned data; local table is not populated or not mapped. |
| /news/theme | index=0&page_size=20&type=-1 | ok | 1 | 0 | professional_useful | code,doc,schema | Probe returned data; local table is not populated or not mapped. |
| /sector/bk-fenshi-zhibo | code=801001 | ok | 69 | 0 | professional_core | code,doc,schema | Probe returned data; local table is not populated or not mapped. |
| /sector/son-plate-direct | code=801001 | ok | 1 | 0 | professional_core | schema | Probe returned data; local table is not populated or not mapped. |
| /sector/son-plates | code=801001 | ok | 1 | 0 | professional_core | code,doc,schema | Probe returned data; local table is not populated or not mapped. |
| /sector/strength-batch | codes=801001%2C801001&date=2026-07-06 | ok | 1 | 0 | professional_core | code,doc,schema | Probe returned data; local table is not populated or not mapped. |
| /sector/sub-concepts | code=801001 | ok | 1 | 0 | professional_core | code,doc,schema | Probe returned data; local table is not populated or not mapped. |
| /stock/gpcphbts-tag | code=603137 | ok | 7 | 0 | professional_useful | code,doc | Probe returned data; local table is not populated or not mapped. |
| /stock/institutional-dates | code=603137 | ok | 1 | 0 | professional_useful | schema | Probe returned data; local table is not populated or not mapped. |
| /topic/detail | topic_id=1 | ok | 1 | 0 | low_priority | schema | Probe returned data; local table is not populated or not mapped. |
| /topic/list | index=0&page_size=20 | ok | 1 | 0 | low_priority | code,schema | Probe returned data; local table is not populated or not mapped. |
| /tuyere/by-stock | code=603137 | ok | 1 | 0 | professional_useful | doc,schema | Probe returned data; local table is not populated or not mapped. |
| /tuyere/tags | code=603137 | ok | 13 | 0 | professional_useful | schema | Probe returned data; local table is not populated or not mapped. |
| /xianhuo/list |  | ok | 383 | 0 | low_priority | code,doc,schema | Probe returned data; local table is not populated or not mapped. |

## Reachable But Empty

| Endpoint | Params | Status | Count | Rows | Usefulness | Sources | Note |
| --- | --- | --- | --- | --- | --- | --- | --- |
| /advanced/bid-history |  | ok | 0 | 0 | professional_useful | doc,schema | Endpoint is reachable but returned empty data. |
| /advanced/big-reminder |  | ok | 0 | 0 | professional_useful | doc,schema | Endpoint is reachable but returned empty data. |
| /advanced/bk-dj-arrange |  | ok | 0 | 0 | professional_useful | doc,schema | Endpoint is reachable but returned empty data. |
| /advanced/f10-concept-jxbk |  | ok | 0 | 0 | professional_useful | schema | Endpoint is reachable but returned empty data. |
| /advanced/f10-index |  | ok | 0 | 0 | professional_useful | schema | Endpoint is reachable but returned empty data. |
| /advanced/his-zhangfu-detail |  | ok | 0 | 0 | professional_useful | doc,schema | Endpoint is reachable but returned empty data. |
| /advanced/newhigh-group-stocks | group_type=all | ok | 0 | 0 | professional_useful | code,doc,schema | Endpoint is reachable but returned empty data. |
| /advanced/pmsl |  | ok | 0 | 0 | professional_useful | doc,schema | Endpoint is reachable but returned empty data. |
| /advanced/stock-trend-incremental-ph | code=603137 | ok | 0 | 0 | professional_useful | schema | Endpoint is reachable but returned empty data. |
| /advanced/voltur-history |  | ok | 0 | 0 | professional_useful | doc,schema | Endpoint is reachable but returned empty data. |
| /advanced/zs-trend-narrow |  | ok | 0 | 0 | professional_useful | schema | Endpoint is reachable but returned empty data. |
| /dingpan/art-title |  | ok | 0 | 0 | professional_useful | code,schema | Endpoint is reachable but returned empty data. |
| /fengk/yd-plate-info | date=2026-07-06&plate=801001 | ok | 0 | 0 | professional_useful | code,doc,schema | Endpoint is reachable but returned empty data. |
| /topic/vote | topic_id=1 | ok | 0 | 0 | low_priority | schema | Endpoint is reachable but returned empty data. |

## Needs Trading Session

| Endpoint | Params | Status | Count | Rows | Usefulness | Sources | Note |
| --- | --- | --- | --- | --- | --- | --- | --- |
| /advanced/morning-bidding-list |  | ok | 0 | 0 | professional_useful | code,doc,schema | Endpoint is reachable but empty; likely trading-session/date/sample dependent. |
| /advanced/zs-real |  | ok | 0 | 0 | professional_useful | doc,schema | Endpoint is reachable but empty; likely trading-session/date/sample dependent. |
| /auction/tick | code=603137&date=2026-07-06 | ok | 0 | 0 | professional_core | code,doc,schema | Endpoint is reachable but empty; likely trading-session/date/sample dependent. |

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
| /stock/tags | code=603137 | http_404 |  | 0 | professional_useful | schema | Probe failed with http_404. |

## Low Priority / Reference

| Endpoint | Params | Status | Count | Rows | Usefulness | Sources | Note |
| --- | --- | --- | --- | --- | --- | --- | --- |
| /comments |  | http_422 |  | 0 | low_priority | schema | Endpoint exists but rejected the inferred parameter shape. |
| /etf/all | date=2026-07-06 | ok | 3 | 0 | low_priority | code,doc,schema | Probe returned data; local table is not populated or not mapped. |
| /etf/ranking | date=2026-07-06 | ok | 4 | 8 | low_priority | code,doc,schema | Probe returned data and local table has stored rows. |
| /finance/balance | code=603137 | ok | 5 | 0 | low_priority | doc,schema | Probe returned data; local table is not populated or not mapped. |
| /finance/cashflow | code=603137 | ok | 5 | 0 | low_priority | doc,schema | Probe returned data; local table is not populated or not mapped. |
| /finance/compare | code=603137 | ok | 4 | 0 | low_priority | doc,schema | Probe returned data; local table is not populated or not mapped. |
| /finance/income | code=603137 | ok | 8 | 0 | low_priority | doc,schema | Probe returned data; local table is not populated or not mapped. |
| /finance/summary | code=603137 | ok | 22 | 0 | low_priority | doc,schema | Probe returned data; local table is not populated or not mapped. |
| /forums/column |  | ok | 1 | 0 | low_priority | schema | Probe returned data; local table is not populated or not mapped. |
| /forums/focus |  | ok | 1 | 0 | low_priority | schema | Probe returned data; local table is not populated or not mapped. |
| /forums/sel-list | index=0&page_size=20 | ok | 1 | 0 | low_priority | schema | Probe returned data; local table is not populated or not mapped. |
| /index/full-info | code=603137&date=2026-07-06 | ok | 1 | 4 | reference | code,doc,schema | Probe returned data and local table has stored rows. |
| /index/intraday | code=603137&date=2026-07-06 | ok | 241 | 964 | reference | code,doc,schema | Probe returned data and local table has stored rows. |
| /index/list | date=2026-07-06 | ok | 4 | 0 | reference | code,doc,schema | Probe returned data; local table is not populated or not mapped. |
| /index/zhishu-kline | code=603137 | ok | 630 | 2520 | reference | code,schema | Probe returned data and local table has stored rows. |
| /kline | code=603137&count=5&ktype=d | ok | 5 | 448 | reference | code,doc,schema | Probe returned data and local table has stored rows. |
| /topic/detail | topic_id=1 | ok | 1 | 0 | low_priority | schema | Probe returned data; local table is not populated or not mapped. |
| /topic/list | index=0&page_size=20 | ok | 1 | 0 | low_priority | code,schema | Probe returned data; local table is not populated or not mapped. |
| /topic/vote | topic_id=1 | ok | 0 | 0 | low_priority | schema | Endpoint is reachable but returned empty data. |
| /xianhuo/list |  | ok | 383 | 0 | low_priority | code,doc,schema | Probe returned data; local table is not populated or not mapped. |

## Full Matrix

| Endpoint | Category | Param Type | Params | Verdict | Item Count | Table | Rows | Sources |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| /advanced/agency-list | advanced | date | date=2026-07-06 | stable_available | 44 | advanced_agency_list | 104 | code,doc,schema |
| /advanced/bid-history | advanced | none |  | reachable_empty | 0 | advanced_bid_history | 0 | doc,schema |
| /advanced/bidvol-kline | advanced | code | code=603137 | stable_available | 600 | advanced_bidvol_kline | 200 | code,doc,schema |
| /advanced/big-reminder | advanced | none |  | reachable_empty | 0 | advanced_big_reminder | 0 | doc,schema |
| /advanced/bk-dj-arrange | advanced | none |  | reachable_empty | 0 | advanced_bk_dj_arrange | 0 | doc,schema |
| /advanced/bkjj-bl | advanced | none |  | api_available | 3 | advanced_bkjj_bl | 0 | doc,schema |
| /advanced/business-list | advanced | none |  | stable_available | 273 | advanced_business_list | 598 | code,doc,schema |
| /advanced/chouma | advanced | code_date | code=603137&date=2026-07-06 | stable_available | 49 | advanced_chouma | 200 | code,doc,schema |
| /advanced/company-count | advanced | batch | codes=801001%2C603137 | api_available | 1 | advanced_company_count | 0 | code,schema |
| /advanced/concept-point | advanced | none |  | stable_available | 3 | advanced_concept_point | 8 | code,doc,schema |
| /advanced/convertible-bonds-option | advanced | none |  | api_available | 2 | advanced_convertible_bonds_option | 0 | doc,schema |
| /advanced/corporate-news | advanced | code | code=603137 | api_available | 25 | advanced_corporate_news | 0 | schema |
| /advanced/dadan-kline | advanced | code | code=603137 | stable_available | 768 | advanced_dadan_kline | 115 | code,doc,schema |
| /advanced/dadan-kline-new | advanced | code | code=603137 | stable_available | 600 | advanced_dadan_kline_new | 200 | code,doc,schema |
| /advanced/dadan-kline-today | advanced | code | code=603137&ktype=d | stable_available | 1 | advanced_dadan_kline_today | 200 | code,doc,schema |
| /advanced/dadan-trend-incremental | advanced | code | code=603137 | api_available | 67 | advanced_dadan_trend_incremental | 0 | doc,schema |
| /advanced/disk-review | advanced | date | date=2026-07-06 | stable_available | 2 | advanced_disk_review | 2 | code,doc,schema |
| /advanced/dp-explain | advanced | none |  | api_available | 3 | advanced_dp_explain | 0 | doc,schema |
| /advanced/dp-realdata | advanced | none |  | api_available | 9 | advanced_dp_realdata | 0 | doc,schema |
| /advanced/duidao-kline | advanced | code | code=603137 | stable_available | 600 | advanced_duidao_kline | 200 | code,doc,schema |
| /advanced/f10-concept-jxbk | advanced | none |  | reachable_empty | 0 | advanced_f10_concept_jxbk | 0 | schema |
| /advanced/f10-index | advanced | none |  | reachable_empty | 0 | advanced_f10_index | 0 | schema |
| /advanced/fenbi2 | advanced | code | code=603137 | api_available | 811 | advanced_fenbi2 | 0 | schema |
| /advanced/fengk-best | advanced | none |  | stable_available | 30 | advanced_fengk_best | 60 | code,doc,schema |
| /advanced/fenshi-kline-option | advanced | code | code=603137 | api_available | 1 | advanced_fenshi_kline_option | 0 | doc,schema |
| /advanced/gudong-info | advanced | code | code=603137 | stable_available | 16 | advanced_gudong_info | 200 | code,doc,schema |
| /advanced/gudong-renshu | advanced | code | code=603137 | stable_available | 6 | advanced_gudong_renshu | 200 | code,doc,schema |
| /advanced/gujia-kline | advanced | code | code=603137 | stable_available | 8 | advanced_gujia_kline | 200 | code,doc,schema |
| /advanced/his-ranking | advanced | date | date=2026-07-06 | api_available | 26 | advanced_his_ranking | 0 | code,schema |
| /advanced/his-ranking-info | advanced | date | date=2026-07-06 | stable_available | 26 | advanced_his_ranking_info | 2 | code,schema |
| /advanced/his-sharp-withdrawal | advanced | date | date=2026-07-06 | api_available | 3 | advanced_his_sharp_withdrawal | 0 | code,doc,schema |
| /advanced/his-zhangfu-detail | advanced | none |  | reachable_empty | 0 | advanced_his_zhangfu_detail | 0 | doc,schema |
| /advanced/holiday | advanced | combination | year=2026 | api_available | 316 | advanced_holiday | 0 | code,doc,schema |
| /advanced/interviews | advanced | range | end=2026-07-06&start=2026-06-01 | api_available | 30 | advanced_interviews | 0 | code,doc,schema |
| /advanced/kline-today | advanced | code | code=603137&ktype=d | stable_available | 1 | advanced_kline_today | 50 | code,schema |
| /advanced/kline-today-dadan-new | advanced | code | code=603137&ktype=d | api_available | 6 | advanced_kline_today_dadan_new | 0 | code,doc,schema |
| /advanced/kline-today-duidao | advanced | code | code=603137&ktype=d | api_available | 7 | advanced_kline_today_duidao | 0 | code,doc,schema |
| /advanced/kline-today-main-activity | advanced | code | code=603137&ktype=d | api_available | 7 | advanced_kline_today_main_activity | 0 | code,doc,schema |
| /advanced/kline-today-tyd | advanced | code | code=603137&ktype=d | api_available | 8 | advanced_kline_today_tyd | 0 | code,doc,schema |
| /advanced/kline-volume-forecast | advanced | code | code=603137 | api_available | 6 | advanced_kline_volume_forecast | 0 | doc,schema |
| /advanced/kline-zhangting-reason | advanced | code | code=603137 | api_available | 7 | advanced_kline_zhangting_reason | 0 | doc,schema |
| /advanced/main-activity-kline | advanced | code | code=603137 | stable_available | 600 | advanced_main_activity_kline | 200 | code,doc,schema |
| /advanced/main-monitor | advanced | code | code=603137 | stable_available | 30 | advanced_main_monitor | 200 | code,doc,schema |
| /advanced/market-mood-count | advanced | none |  | stable_available | 1 | advanced_market_mood_count | 2 | code,doc,schema |
| /advanced/market-radar | advanced | none |  | stable_available | 15 | advanced_market_radar | 30 | code,doc,schema |
| /advanced/market-scln | advanced | date | date=2026-07-06 | stable_available | 9 | advanced_market_scln | 2 | code,doc,schema |
| /advanced/morning-bidding-list | advanced | none |  | needs_trading_session | 0 | advanced_morning_bidding_list | 0 | code,doc,schema |
| /advanced/morning-bidding-summary | advanced | none |  | stable_available | 8 | advanced_morning_bidding_summary | 7 | code,doc,schema |
| /advanced/newhigh-group-count | advanced | none |  | api_available | 15 | advanced_newhigh_group_count | 0 | code,doc,schema |
| /advanced/newhigh-group-stocks | advanced | combination | group_type=all | reachable_empty | 0 | advanced_newhigh_group_stocks | 0 | code,doc,schema |
| /advanced/news-flash | advanced | none |  | stable_available | 2 | advanced_news_flash | 4 | code,doc,schema |
| /advanced/news-flash-top | advanced | none |  | api_available | 30 | advanced_news_flash_top | 0 | doc,schema |
| /advanced/on-the-lhb | advanced | none |  | stable_available | 8 | advanced_on_the_lhb | 18 | code,doc,schema |
| /advanced/pankou | advanced | code | code=603137 | stable_available | 1 | advanced_pankou | 71 | code,doc,schema |
| /advanced/pianlizhi | advanced | none |  | stable_available | 1 | advanced_pianlizhi | 2 | code,doc,schema |
| /advanced/pianlizhi-many | advanced | none |  | api_available | 1 | advanced_pianlizhi_many | 0 | doc,schema |
| /advanced/pianlizhi-w32 | advanced | none |  | api_available | 1 | advanced_pianlizhi_w32 | 0 | doc,schema |
| /advanced/pmsl | advanced | none |  | reachable_empty | 0 | advanced_pmsl | 0 | doc,schema |
| /advanced/relation | advanced | none |  | stable_available | 1 | advanced_relation | 2 | code,doc,schema |
| /advanced/rqz-data | advanced | code | code=603137 | stable_available | 200 | advanced_rqz_data | 200 | code,doc,schema |
| /advanced/stock-plate-new | advanced | code | code=603137 | api_available | 6 | advanced_stock_plate_new | 0 | schema |
| /advanced/stock-trend-incremental-ph | advanced | code | code=603137 | reachable_empty | 0 | advanced_stock_trend_incremental_ph | 0 | schema |
| /advanced/trend-min | advanced | code_date | code=603137&date=2026-07-06 | api_available | 241 | advanced_trend_min | 0 | schema |
| /advanced/tuoyadan-kline | advanced | code | code=603137 | stable_available | 599 | advanced_tuoyadan_kline | 200 | code,doc,schema |
| /advanced/turnover-ten | advanced | code | code=603137 | api_available | 1 | advanced_turnover_ten | 0 | doc,schema |
| /advanced/vol-tur | advanced | code | code=603137 | stable_available | 68 | advanced_vol_tur | 200 | code,doc,schema |
| /advanced/voltur-history | advanced | none |  | reachable_empty | 0 | advanced_voltur_history | 0 | doc,schema |
| /advanced/weight-performance | advanced | date | date=2026-07-06 | api_available | 2 | advanced_weight_performance | 0 | code,doc,schema |
| /advanced/weipan-qiangchou | advanced | none |  | api_available | 3 | advanced_weipan_qiangchou | 0 | code,doc,schema |
| /advanced/zhangting-expression | advanced | date | date=2026-07-06 | api_available | 12 | advanced_zhangting_expression | 0 | code,doc,schema |
| /advanced/zhangting-gene | advanced | code | code=603137 | api_available | 6 | advanced_zhangting_gene | 0 | doc,schema |
| /advanced/zjmm-min | advanced | code_date | code=603137&date=2026-07-06 | api_available | 241 | advanced_zjmm_min | 0 | doc,schema |
| /advanced/zs-real | advanced | none |  | needs_trading_session | 0 | advanced_zs_real | 0 | doc,schema |
| /advanced/zs-trend-narrow | advanced | none |  | reachable_empty | 0 | advanced_zs_trend_narrow | 0 | schema |
| /auction/bidding-anomaly | auction | code_date | code=603137&date=2026-07-06 | stable_available | 3 | auction_bidding_anomaly | 3 | code,doc,schema |
| /auction/tick | auction | code_date | code=603137&date=2026-07-06 | needs_trading_session | 0 | auction_tick | 0 | code,doc,schema |
| /comments | comments | none |  | param_uncertain |  | comments | 0 | schema |
| /daily | daily | date | date=2026-07-06 | stable_available | 1 | daily_summary | 250 | code,doc,schema |
| /daily/export | daily | date | date=2026-07-06 | stable_available | 1 | daily_export | 2 | code,doc,schema |
| /daily/new-high | daily | date | date=2026-07-06 | stable_available | 1 | daily_new_high | 250 | code,doc,schema |
| /daily/sentiment | daily | date | date=2026-07-06 | stable_available | 1 | daily_sentiment | 250 | code,doc,schema |
| /dingpan/all | dingpan | none |  | stable_available | 1 | dingpan_all | 4 | code,doc,schema |
| /dingpan/art-title | dingpan | none |  | reachable_empty | 0 | dingpan_art_title | 0 | code,schema |
| /dingpan/jijin | dingpan | none |  | api_available | 1 | dingpan_jijin | 0 | code,doc,schema |
| /dingpan/module-versatile | dingpan | none |  | stable_available | 1 | dingpan_module_versatile | 6 | code,doc,schema |
| /dingpan/northbound-close-date | dingpan | none |  | stable_available | 225 | dingpan_northbound_close_date | 450 | code,schema |
| /dingpan/radar | dingpan | combination | st=0 | stable_available | 15 | dingpan_radar | 36 | code,doc,schema |
| /dingpan/southbound-close-date | dingpan | none |  | stable_available | 213 | dingpan_southbound_close_date | 426 | code,schema |
| /dingpan/weipan | dingpan | none |  | stable_available | 3 | dingpan_weipan | 18 | code,doc,schema |
| /etf/all | etf | date | date=2026-07-06 | api_available | 3 | etf_all | 0 | code,doc,schema |
| /etf/ranking | etf | date | date=2026-07-06 | stable_available | 4 | etf_ranking | 8 | code,doc,schema |
| /fengk/list | fengk | pagination | date=2026-07-06&index=0&page_size=20 | stable_available | 1 | fengk_list | 1272 | code,doc,schema |
| /fengk/yd-plate | fengk | date | date=2026-07-06 | api_available | 204 | fengk_yd_plate | 0 | code,doc,schema |
| /fengk/yd-plate-info | fengk | code_date | date=2026-07-06&plate=801001 | reachable_empty | 0 | fengk_yd_plate_info | 0 | code,doc,schema |
| /finance/balance | finance | code | code=603137 | api_available | 5 | finance_balance | 0 | doc,schema |
| /finance/cashflow | finance | code | code=603137 | api_available | 5 | finance_cashflow | 0 | doc,schema |
| /finance/compare | finance | code | code=603137 | api_available | 4 | finance_compare | 0 | doc,schema |
| /finance/income | finance | code | code=603137 | api_available | 8 | finance_income | 0 | doc,schema |
| /finance/summary | finance | code | code=603137 | api_available | 22 | finance_summary | 0 | doc,schema |
| /forums/column | forums | none |  | api_available | 1 | forums_column | 0 | schema |
| /forums/focus | forums | none |  | api_available | 1 | forums_focus | 0 | schema |
| /forums/sel-list | forums | pagination | index=0&page_size=20 | api_available | 1 | forums_sel_list | 0 | schema |
| /index/full-info | index | code_date | code=603137&date=2026-07-06 | stable_available | 1 | index_full_info | 4 | code,doc,schema |
| /index/intraday | index | code_date | code=603137&date=2026-07-06 | stable_available | 241 | index_intraday | 964 | code,doc,schema |
| /index/list | index | date | date=2026-07-06 | api_available | 4 | index_list | 0 | code,doc,schema |
| /index/zhishu-kline | index | code | code=603137 | stable_available | 630 | index_kline | 2520 | code,schema |
| /kline | kline | code | code=603137&count=5&ktype=d | stable_available | 5 | kline | 448 | code,doc,schema |
| /l2/realtime/all-boards | l2 | none |  | stable_available | 1 | l2_realtime_all_boards | 64 | code,doc,schema |
| /l2/realtime/index-list | l2 | none |  | stable_available | 4 | l2_realtime_index_list | 12 | code,doc,schema |
| /l2/realtime/index-trend | l2 | none |  | stable_available | 1 | l2_realtime_index_trend | 1 | code,doc,schema |
| /l2/realtime/sharp-withdrawal | l2 | none |  | api_available | 5 | l2_realtime_sharp_withdrawal | 0 | doc,schema |
| /l2/sector-intraday | l2 | code_date | code=801001&date=2026-07-06 | stable_available | 69 | l2_sector_intraday | 3840 | code,schema |
| /l2/sector-volume | l2 | code | code=801001 | stable_available | 69 | l2_sector_volume | 88 | code,schema |
| /l2/stock-bigorder | l2 | code_date | code=603137&date=2026-07-06 | stable_available | 4 | l2_stock_bigorder | 2666 | code,doc,schema |
| /l2/stock-intraday | l2 | code_date | code=603137&date=2026-07-06 | stable_available | 241 | l2_stock_intraday | 5543 | code,doc,schema |
| /l2/tick-history | l2 | code_date | code=603137&date=2026-07-06 | stable_available | 484 | l2_tick_history | 3033 | code,doc,schema |
| /l2/tick-orders | l2 | code_date | code=603137&date=2026-07-06 | stable_available | 30 | l2_tick_orders | 82 | code,doc,schema |
| /l2/tick-orders-all | l2 | code_date | code=603137&date=2026-07-06 | stable_available | 5686 | l2_tick_orders_all | 1320 | code,doc,schema |
| /ladder/board-stocks | ladder | date | date=2026-07-06 | stable_available | 22 | ladder_board_stocks | 236 | code,doc,schema |
| /ladder/broken | ladder | date | date=2026-07-06 | stable_available | 30 | ladder_broken | 31 | code,doc,schema |
| /ladder/consecutive | ladder | date | date=2026-07-06 | stable_available | 5 | ladder_consecutive | 53 | code,doc,schema |
| /ladder/market | ladder | date | date=2026-07-06 | stable_available | 5 | ladder_market | 241 | code,doc,schema |
| /ladder/realtime-boards | ladder | none |  | stable_available | 22 | ladder_realtime_boards | 112 | code,doc,schema |
| /ladder/sector | ladder | date | date=2026-07-06 | stable_available | 7 | ladder_sector | 53 | code,doc,schema |
| /ladder/sharp-withdrawal | ladder | date | date=2026-07-06 | stable_available | 3 | ladder_sharp_withdrawal | 41 | code,doc,schema |
| /lhb/dataframe | lhb | date | date=2026-07-06 | stable_available | 86 | lhb_dataframe | 15 | code,doc,schema |
| /lhb/detail | lhb | code_date | code=603137&date=2026-07-06 | stable_available | 1 | lhb_detail | 161 | code,doc,schema |
| /lhb/list | lhb | date | date=2026-07-06 | stable_available | 86 | lhb_list | 93 | code,doc,schema |
| /lhb/raw-list | lhb | date | date=2026-07-06 | stable_available | 86 | lhb_raw_list | 15 | code,doc,schema |
| /lhb/top-title | lhb | none |  | stable_available | 1 | lhb_top_title | 22 | code,doc,schema |
| /lhb/update-list | lhb | none |  | stable_available | 80 | lhb_update_list | 14 | code,doc,schema |
| /lhb/youzi-dongxiang | lhb | date | date=2026-07-06 | stable_available | 1 | lhb_youzi_dongxiang | 30 | code,doc,schema |
| /market/emotion-money-date | market | none |  | stable_available | 1 | market_emotion_money | 60 | code,doc,schema |
| /market/emotion-money-detail | market | none |  | stable_available | 1 | market_emotion_detail | 22 | code,doc,schema |
| /market/limit-up-down | market | none |  | stable_available | 250 | market_limit_up_down | 500 | code,doc,schema |
| /market/mood | market | none |  | stable_available | 1 | market_mood | 1 | code,doc,schema |
| /market/rise-fall | market | date | date=2026-07-06 | stable_available | 1 | market_rise_fall | 250 | code,doc,schema |
| /news/columns | news | none |  | stable_available | 1 | news_columns | 20 | code,schema |
| /news/concept-jxbk | news | none |  | stable_available | 13 | news_concept_jxbk | 26 | code,doc,schema |
| /news/index-plate | news | none |  | api_available | 6 | news_index_plate | 0 | code,schema |
| /news/plate | news | code | code=603137 | api_available | 1 | news_plate | 0 | code,doc,schema |
| /news/theme | news | pagination | index=0&page_size=20&type=-1 | api_available | 1 | news_theme | 0 | code,doc,schema |
| /sector/all-stocks | sector | code | code=801001 | stable_available | 5 | sector_all_stocks | 14 | code,doc,schema |
| /sector/bk-fenshi-zhibo | sector | code | code=801001 | api_available | 69 | sector_bk_fenshi_zhibo | 0 | code,doc,schema |
| /sector/boom-reason | sector | code_date | code=801001&date=2026-07-06 | stable_available | 1 | sector_boom_reason | 116 | code,doc,schema |
| /sector/capital | sector | code_date | code=801001&date=2026-07-06 | stable_available | 1 | sector_capital | 18 | code,doc,schema |
| /sector/parent-plate | sector | code | code=801001 | stable_available | 1 | sector_parent_plate | 100 | code,schema |
| /sector/plate-info-qj | sector | code | code=801001 | stable_available | 22 | sector_plate_info_qj | 40 | code,doc,schema |
| /sector/plates | sector | none |  | stable_available | 58 | sector_plates | 58 | code,schema |
| /sector/ranking | sector | date | date=2026-07-06 | stable_available | 9 | sector_ranking | 17 | code,doc,schema |
| /sector/son-plate-direct | sector | code | code=801001 | api_available | 1 | sector_son_plate_direct | 0 | schema |
| /sector/son-plates | sector | code | code=801001 | api_available | 1 | sector_son_plates | 0 | code,doc,schema |
| /sector/stocks | sector | code_date | code=801001&date=2026-07-06 | stable_available | 5 | sector_stocks | 71 | code,doc,schema |
| /sector/strength | sector | code_date | code=801001&date=2026-07-06 | stable_available | 1 | sector_strength | 58 | code,doc,schema |
| /sector/strength-batch | sector | batch | codes=801001%2C801001&date=2026-07-06 | api_available | 1 | sector_strength_batch | 0 | code,doc,schema |
| /sector/strength-dataframe | sector | range | code=801001&end=2026-07-06&start=2026-06-01 | stable_available | 26 | sector_strength_dataframe | 40 | code,doc,schema |
| /sector/strength-history | sector | none |  | param_uncertain |  | sector_strength_history | 0 | schema |
| /sector/strength-ndays | sector | range | code=801001&days=5&end_date=2026-07-06 | stable_available | 63 | sector_strength_ndays | 60 | code,doc,schema |
| /sector/sub-concepts | sector | code | code=801001 | api_available | 1 | sector_sub_concepts | 0 | code,doc,schema |
| /stock/articles | stock | code | code=603137 | stable_available | 1 | stock_articles | 200 | code,schema |
| /stock/company-info | stock | code | code=603137 | stable_available | 3 | stock_company_info | 71 | code,doc,schema |
| /stock/gpcphbts-tag | stock | code | code=603137 | api_available | 7 | stock_tags | 0 | code,doc |
| /stock/gudong | stock | code | code=603137 | stable_available | 9 | stock_gudong | 639 | code,doc,schema |
| /stock/holding-funds | stock | code | code=603137 | stable_available | 1 | stock_holding_funds | 200 | code,doc,schema |
| /stock/institutional-dates | stock | code | code=603137 | api_available | 1 | stock_institutional_dates | 0 | schema |
| /stock/institutional-positions | stock | code | code=603137 | stable_available | 1 | stock_institutional_positions | 639 | code,doc,schema |
| /stock/message-bar | stock | code | code=603137 | stable_available | 1 | stock_message_bar | 200 | code,doc,schema |
| /stock/tags | stock | code | code=603137 | api_error |  | stock_tags | 0 | schema |
| /theme/hot | theme | none |  | stable_available | 13 | theme_hot | 26 | code,doc,schema |
| /topic/detail | topic | combination | topic_id=1 | api_available | 1 | topic_detail | 0 | schema |
| /topic/list | topic | pagination | index=0&page_size=20 | api_available | 1 | topic_list | 0 | code,schema |
| /topic/vote | topic | combination | topic_id=1 | reachable_empty | 0 | topic_vote | 0 | schema |
| /tuyere/by-stock | tuyere | code | code=603137 | api_available | 1 | tuyere_by_stock | 0 | doc,schema |
| /tuyere/tags | tuyere | code | code=603137 | api_available | 13 | tuyere_tags | 0 | schema |
| /xianhuo/list | xianhuo | none |  | api_available | 383 | xianhuo_list | 0 | code,doc,schema |