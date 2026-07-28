# Operator Outcome Import

This project does not place orders. Operator outcomes are imported after the
session so the system can review plan quality, execution discipline, and mistake
patterns.

## CSV Format

Required columns:

- `trade_date`
- `stock_code`
- `execution_status`

Recommended columns:

- `stock_name`
- `entry_time`
- `exit_time`
- `entry_price`
- `exit_price`
- `position_pct`
- `outcome_tag`
- `mistake_tag`
- `review_note`

Allowed `execution_status` values:

- `executed`
- `skipped`
- `cancelled`
- `not_executed`
- `review_required`

Example:

```csv
trade_date,stock_code,stock_name,execution_status,entry_time,exit_time,entry_price,exit_price,position_pct,outcome_tag,mistake_tag,review_note
2026-07-06,000001,Example,executed,09:35,14:50,10.00,10.80,5.0,followed_plan,none,auction confirmed then held to close
2026-07-06,000002,Example B,skipped,09:25,15:00,,,0,avoided_weak_market,discipline_ok,auction failed confirmation
```

## Import

```powershell
D:\anaconda\python.exe scripts\import_operator_trade_outcomes.py --db kpl_data.duckdb --csv path\to\operator_outcomes.csv
```

The importer replaces existing rows for the same `trade_date` and `stock_code`.
It updates the matching `trade_plan.status`, writes one
`trade_journal.action = operator_outcome` row, and stores normalized returns in
`operator_trade_outcome`.

## Review

After import:

```powershell
D:\anaconda\python.exe scripts\run_operator_backtest.py --db kpl_data.duckdb
D:\anaconda\python.exe scripts\generate_daily_review.py --db kpl_data.duckdb
D:\anaconda\python.exe scripts\generate_operator_reports.py --db kpl_data.duckdb
```

`operator_backtest_latest.md` uses real outcomes first. If no imported outcomes
exist, it falls back to proxy K-line samples and labels them as proxy data.
