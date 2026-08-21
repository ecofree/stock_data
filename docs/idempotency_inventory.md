# insert_rows idempotency inventory (2026-08-21)

Baseline: 125 `insert_rows` call sites; 94 had `replace_on` (true upsert),
31 were already keyless appends before this sweep; the audit found 95
keyless sites of which **18 high-confidence sites were converted** in this
pass.  Keys were chosen from declared primary keys (`duckdb_constraints`)
and verified row semantics (max rows per date) against the production DB.

## Converted this pass (18)

| Table | Key | Evidence |
|---|---|---|
| daily_new_high | date | 1 row/date observed |
| daily_sentiment | date | 1 row/date observed |
| daily_export | date | per-date snapshot |
| advanced_market_mood_count | date | per-date summary |
| advanced_market_scln | date + mtype | two mtypes/day |
| advanced_disk_review | date | per-date raw snapshot |
| market_limit_up_down_summary | date (+transaction) | PK(date) declared |
| market_limit_up_down | date + stock_code | detail rows |
| ladder_market | date + board_level + stock_code | ladder rows |
| ladder_consecutive | date + stock_code | |
| ladder_sector | date + sector_code | |
| ladder_broken | date + stock_code + broken_time | intraday events |
| ladder_sharp_withdrawal | date + stock_code | last-wins per stock |
| ladder_board_stocks | date + board_type + stock_code | |
| ladder_realtime_boards | date + board_type + stock_code + limit_up_time | intraday snapshots |
| etf_ranking | date + etf_code | |
| theme_hot | date + theme_code | |
| sector_plates | sector_code | PK(sector_code) declared |
| stock_company_info | stock_code (+transaction) | PK(stock_code) declared |

## Append-by-design (leave as-is)

News/event feeds and true time series where every fetch is a distinct fact:
`advanced_news_flash`, `news_*`, `topic_list`, `lhb_update_list`,
`lhb_raw_list`, `l2_stock_intraday` / `l2_sector_intraday` /
`l2_stock_bigorder` / `l2_realtime_*` (minute-level snapshots),
`dingpan_*` realtime captures, `auction_*`.

## Needs a domain decision before converting

Multi-row history tables whose natural key is plausible but unverified —
converting with a wrong key would silently collapse legitimate rows:

- `advanced_his_ranking(_info)`, `advanced_interviews`,
  `advanced_weight_performance`, `advanced_zhangting_expression`,
  `advanced_pianlizhi`, `advanced_relation`, `advanced_company_count`
  (no `date` column — needs its own review)
- `collect_dingpan.py` close-date tables (`dingpan_northbound_close_date`
  etc.): likely `date`-keyed, but confirm one-row semantics first.
- `stock_tags`, `stock_gudong`, `stock_holding_funds`,
  `stock_institutional_positions`: candidate keys
  `(stock_code, tag/announce_date)` need schema confirmation.

Re-run the classifier any time:

```powershell
D:\anaconda\python.exe -c "import subprocess,sys; subprocess.run([r'D:\anaconda\python.exe', r'C:\Users\coumoo\AppData\Local\Temp\opencode\inventory_inserts.py'])"
```

(The inventory helper is a session artifact; copy it under scripts/ if it
should become permanent.)
