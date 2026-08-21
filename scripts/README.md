# scripts/ entry points

## CLI conventions

- Database: `--db <path>` (default `kpl_data.duckdb` at project root).
- Trade date: **both** `--trade-date YYYY-MM-DD` and its alias `--date` are
  accepted; inside the script the value is always `args.trade_date`.
  New scripts should use `trade_system.cli.add_trade_date_argument` /
  `add_db_argument` instead of hand-rolling flags.
- Every script is runnable standalone and exits nonzero when the requested
  stage is not actionable (fail-closed).

## Daily operation

| Script | Window | Purpose |
|---|---|---|
| `run_integrated_daily.py` | phase-aware | One-command integrated runner (auction/intraday/close/history) |
| `run_stock_data_daily.ps1` | scheduled close | SYSTEM task wrapper: backup → runner → P0 observation |
| `check_data_readiness.py` | before signals | Same-date readiness gate (`--stage`) |
| `collect_capital_flow_focus.py` | intraday | Bounded capital-flow refresh with circuit breaker |
| `generate_signals.py` | after flow refresh | Operator signal generation |
| `generate_stage_signals.py` | per-stage windows | premarket/auction/intraday/close stage signals |
| `generate_operator_reports.py` | close | Markdown operator report |
| `generate_web_dashboard.py` | close | Static HTML dashboard |
| `generate_daily_review.py` | post-close | Daily review markdown (+ statistics) |
| `generate_health_trend.py` | post-close (auto) | Rolling data-health trend report |
| `generate_cycle_analytics.py` | post-close (auto) | Emotion-cycle phase + premium/promotion matrices |
| `generate_edge_profiles.py` | post-close | Hot-money seat profiles + auction pattern stats |
| `generate_signal_attribution.py` | post-close (auto) | Stage-signal outcomes by phase (advisory position cap) |

## Strategy validation

- `run_strategy_backtest.py` — limit-up continuation backtest
  (T+1, unfillable one-price boards skipped, slippage both sides).
  Example: `--start 2026-07-01 --end 2026-08-15 --hold-days 1`.
- `feature_ic_analysis.py` — cross-sectional Spearman IC for any feature
  table before any model training. Example: `--table multi_source_stock_flow
  --date-col source_date --features "main_net,super_net" --horizon 3`.

## Ops & monitoring

- `pipeline_notify.py --event start|success|failure` — heartbeat touch,
  optional healthcheck ping and push alerts (env: `KPL_NOTIFY_*`,
  `KPL_NOTIFY_HEALTHCHECK_URL`).
- `check_pipeline_heartbeat.py [--alert-on-stale]` — external liveness gate.
- `offsite_backup.ps1 -Destination <path|rclone:remote> [-Execute]` —
  off-site copy of the newest daily/weekly backups (dry-run default).

## Research console

`research_server.py --port 8765` — read-only SQL console on localhost
(SELECT-only guard, results clamped to 500 rows).
| `generate_health_trend.py` | post-close (auto) | Rolling data-health trend report |

## Storage lifecycle

`archive_legacy_tables.py` (--dry-run first), `split_cold_storage.py`
(EXPERIMENTAL, --execute only after fresh backup + scheduler stop),
`reclaim_disk_space.py`

## Collection (focused)

`collect_daily.py`, `collect_market.py`, `collect_sector.py`, `collect_lhb.py`,
`collect_finance.py`, `collect_index.py`, `collect_news.py`, `collect_l2.py`,
`collect_dingpan.py`, `collect_fengk.py`, `collect_misc.py`,
`collect_advanced.py`, `collect_advanced_stock.py`, `collect_stock.py` —
legacy per-endpoint collectors driven by `fetch_all.py`; prefer
`run_integrated_daily.py` in production.

## Backfill / history

`backfill_tushare_incremental.py`, `backfill_legacy_baostock.py`,
`backfill_2026_tushare.py`, `backfill_2026_ths_concepts.py`,
`collect_tushare_basic_data.py`, `import_tushare_ths_members.py`,
`analyze_tushare_history.py`, `report_real_data_backfill.py`

## Audits & gates

`audit_data_quality.py`, `audit_p0_p3_acceptance.py`,
`audit_p0_five_day_observation.py`, `audit_p3_candidates.py`,
`check_capital_flow_health.py`, `build_empty_table_catalog.py`,
`describe_collection_profiles.py`, `run_maintenance_gate.py`,
`audit_maintenance_surface.py`, `lint_migrations.py`

## Maintenance (explicit, backup first)

`repair_critical_integrity.py` (`--dry-run` first), `repair_duplicates.py`,
`reclaim_disk_space.py`, `archive_legacy_tables.py` (`--dry-run` first),
`pre_push_check.ps1`, `install_stock_data_task.ps1`
