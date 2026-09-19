# scripts/ entry points

## Retirement override · 2026-09-10

This is a historical script catalog, not an approved V2 deployment list.
Use `stock-data-v2`, `stock-data-daily`, and `stock-data-paper-desk` from the
allowlisted core wheel. Old paper/order/operator/report reject-only shims have
been removed; task registration and `run_integrated_daily.py --report-only`
are disabled. Daily plans filter old signal generation and schema repair steps.
Other retained collectors are migration adapters, with legacy connections that
refuse V2/account database identity even when the V2 service is stopped.
This does not sandbox arbitrary programs or grant source-table deletion.

`archive_legacy_tables.py` defaults to read-only inventory. An explicit table
list and a new output directory permit verified export only; `--execute` is
rejected. Export success does not certify full-schema restore or source deletion.
See [implementation and remaining acceptance](../docs/v2/RETIREMENT_REMEDIATION_20260910.md).

The old signal/stage generators, adjacent-row stage backtests, SQL-registry
QLib training/prediction commands and legacy report rollback transaction have
been removed. Historical formulas and fixtures remain reproducible at Git
`84141f0`; existing raw tables, model files and human records are preserved.
Current research uses frozen `research_product` artifacts and exact-session
labels; operator statistics read actual executions only. Publication uses
verified immutable bundles. `stock-data-recovery` restores into a new isolated
folder, without replacing live facts or orders.

## CLI conventions

- Database: `--db <path>` (default `kpl_data.duckdb` at project root).
- Trade date: **both** `--trade-date YYYY-MM-DD` and its alias `--date` are
  accepted; inside the script the value is always `args.trade_date`.
  New scripts should use `trade_system.cli.add_trade_date_argument` /
  `add_db_argument` instead of hand-rolling flags.
- Retained scripts have different lifecycle states; presence in this catalog
  does not imply authority to run. Retired entry points have been removed.

## Daily operation

| Script | Window | Purpose |
|---|---|---|
| `run_integrated_daily.py` | phase-aware | One-command integrated runner (auction/intraday/close/history) |
| `run_stock_data_daily.ps1` | scheduled close | SYSTEM task wrapper: backup → runner → P0 observation |
| `check_data_readiness.py` | before signals | Same-date readiness gate (`--stage`) |
| `collect_capital_flow_focus.py` | intraday | Bounded capital-flow refresh with circuit breaker |
| `generate_operator_reports.py` | explicit history | Read-only historical inventory; no snapshot writes or current decision authority |
| `generate_daily_review.py` | post-close | Daily review markdown (+ statistics) |
| `generate_health_trend.py` | post-close (auto) | Rolling data-health trend report |
| `generate_cycle_analytics.py` | explicit historical analysis | Observed pools and verified calendar only; incomplete cohorts remain unknown |
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
These modules live only in `collectors/` and are driven by `fetch_all.py`.
Configuration, schema and the existing store live in `trade_system/config.py`,
`trade_system/schema.py` and `trade_system/data_store.py`; root import shims were removed.
Production remains paused pending the separate handover.

## Backfill / history

`backfill_2026_tushare.py`, `backfill_2026_ths_concepts.py`,
`import_tushare_ths_members.py`,
`analyze_tushare_history.py`, `report_real_data_backfill.py`

BaoStock 行情沿用 `collect_multisource.py` 的共享取数与来源表；旧串行／并行回填脚本已删除。历史影子评估只读，不覆盖旧评估表。

## Audits & gates

`audit_data_quality.py`, `audit_p0_p3_acceptance.py`,
`audit_p0_five_day_observation.py`, `audit_p3_candidates.py`,
`check_capital_flow_health.py`, `build_empty_table_catalog.py`,
`describe_collection_profiles.py`,
`audit_maintenance_surface.py`, `lint_migrations.py`

## Maintenance (explicit, backup first)

`repair_critical_integrity.py` (`--dry-run` first), `repair_duplicates.py`,
`reclaim_disk_space.py`, `archive_legacy_tables.py` (`--dry-run` first),
`pre_push_check.ps1`, `install_stock_data_task.ps1`

旧 SQL 注册表选模、候选融合和筛选入口已退出：`run_qlib_daily.py`、`build_qlib_candidate_pool.py`、`screen_with_qlib.py`、`audit_qlib_model_gate.py`。当前研究沿用 `python -m trade_system.v2.research_product` 的冻结工件与页面；没有冻结模型时保持缺失，不从旧 champion 表补选。旧预测、注册表和模型文件仍保留为历史资料。

历史人工数据：`create_operator_outcome_template.py` 只读导出到新文件，不建表、不覆盖已有 CSV；`manage_holdings.py show` 只读查看全部历史持仓，不计算当前市值。旧 add/remove/close 已撤掉；当前账户使用 `stock-data-v2 --db <独立库>` 的 `account_import`，提交完整人工声明或券商导出快照并核对，不从旧持仓表自动补现金、可卖量或未结委托。

旧 `generate_backtest_vs_actual.py` 已退出；实际执行统计使用 `run_operator_backtest.py` 的真实结果证据，历史记录查看使用 `generate_operator_reports.py`。旧信号按自然日连接、跨日取最大收益的统计不再生成当前报告。池类快照按供应商字段和产品身份隔离，动态缓存默认 60 秒；不以涨停列表替代其他池或情绪统计，旧合同缓存不复用。

旧 `generate_stock_detail.py` 及复盘页按磁盘旧文件自动发现的链接已删除；当前证券依据沿用内联页面，旧持仓只由 `manage_holdings.py show` 显式查看。`limit_up_sentiment` 仅从三个合格原始池回执计算，不再独立联网、缓存或作为供应商事实落库；采集 `--all` 只处理原始产品。空池未经覆盖证明仍保留未知，不生成零计数。

旧 `build_derived_limit_pool.py` 及周期统计的估算名单回退已退出；历史估算表保留，但不再补成真实涨停样本。
官方涨停池历史补录复用同一规范化和写入入口，日期区间须显式指定；日历未知、空响应、失败或预算截断不能报全部完成。行数门槛仅用于重试，不代表完整覆盖认证。

财务三表沿用现有报表期数回执复用，利润表/资产负债表/现金流量表分别绑定缓存身份；财务概要表冒充三表、跨供应商榜单混用、全市场榜单替代个股概念、报价替代希腊值的路径已删除。测试按现存职责合并，旧入口退役只维护一份检查清单。
