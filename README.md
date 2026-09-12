# stock_data

## Current local workspace (2026-09-12)

Open http://127.0.0.1:8769/ or double-click `Start Research.cmd` in this checkout.
The launcher verifies the workspace and running source identity before reuse.
Viewing does not fetch or train. Judgements and human condition reviews return
independent, retry-safe receipts; static exports cannot save records.
The current local patch is **not** the approved fixed scheduler release and has
not changed production tasks. See the single delivery ledger for exact boundaries.

Approved maintenance installer: see [Administrator operation sheet](docs/v2/DEPLOYMENT_OPERATOR_20260913.md).
`scripts/deploy_research_cutover.ps1 -Mode Check` is read-only; Apply requires the
approved 2026-09-13 16:00–17:00 China-time window and an elevated operator shell.
The installer is prepared and tested, not deployed. Never substitute the old task installer.

## Previous delivery: 0.3.10 permission and utility verification (2026-09-12)

Installed runtimes now refuse legacy disk writers even without Git metadata;
hardlink aliases of the live database are also refused. A direct-writer inventory
gate prevents new unclassified entry points. This is not OS-level isolation.
`python -m trade_system.v2.research_product utility --destination <new-folder>`
independently recomputes sealed OOS selection comparisons without fitting.
The present candidate has not demonstrated a promotion-worthy advantage.
`scripts/run_research_daily.ps1` is the checked research-only daily entry point;
no Windows task has been registered or changed. See the ledger for remaining
human, prospective-data, legacy-consumer and cutover acceptance boundaries.

## Previous 0.3.9 delivery

Daily updates now include a same-date read-only market projection, complete
declared theme membership and matched previous-session breadth. Real incremental
refresh and explicit holiday-calendar reuse have been exercised. The full source
database was copied/restored in isolation; source-release rollback was rehearsed.
Start manually with `scripts/start_research_workbench.ps1 -Port 8769`.
The tested 8769 service was gracefully stopped; restarting it was blocked by the
execution environment. No scheduled tasks or production database were replaced.

The current implementation and remaining boundaries are in the single
[delivery ledger](docs/v2/CONSOLIDATED_REMEDIATION_20260912.md).
The local research workspace now joins same-date market review, frozen model
comparison, candidate explanations, manual notes and pending follow-up queues.
Start with `scripts/start_research_workbench.ps1`; viewing never trains implicitly.
The research source distribution uses `requirements-research-replay.lock` in a
separate Windows CPython 3.12 environment. The operational wheel is a different,
minimal product, not a package of the entire legacy collector/UI.

The legacy trading-terminal generator now refuses execution explicitly; its
historical HTML remains untouched. Current production tasks and database have
not been replaced. Four-batch convergence is **partially implemented**, not fully
accepted; real human decisions, model superiority and production cutover are not
established by local tests. The sections below are historical context.

## Historical remediation boundary (2026-09-11)

The current implementation record is [the four-stage continuation](docs/v2/FOUR_STAGES_20260911.md), extending [the retirement remediation ledger](docs/v2/RETIREMENT_REMEDIATION_20260910.md).
This checkout is an isolated remediation branch, **not a deployed replacement**.
The operational wheel contains only the explicit V2 core allowlist and supports
Python 3.11–3.12. Install `requirements-core.lock` plus the verified wheel in a
fresh environment; research runs from a separate source environment.

Approved core commands: `stock-data-v2`, `stock-data-daily`,
`stock-data-paper-desk`, and offline `stock-data-recovery`. Broker routing is absent.
The old paper order CLI, automatic manual-plan loop, task registration, and
`--report-only` recovery are rejected. Historical raw collection is transitional,
not a second decision authority. Existing Windows tasks have **not** been changed.

Sections below describe the **historical source system**, not installation,
deployment, or retirement instructions for the new core. Do not register its
tasks, delete tables, or use its broad dependency set as the core runtime.

`stock_data` is an A-share short-term/theme/emotion-cycle trading assistant.
It is designed for pre-market preparation, auction confirmation, intraday
monitoring, close review, post-market journaling, backtest validation, and
risk constraints.

It does **not** place orders and should not be extended into automatic trading
without a separate risk and compliance review.

## Current Shape

- Storage: DuckDB (`kpl_data.duckdb`)
- Runtime: Python + DuckDB
- Tests: pytest
- Main upstream API: KPL/opening-market data
- Reports: Markdown + static HTML under `reports/`
- Workflow: one-command daily runner plus focused scripts

The database currently contains hundreds of raw/derived tables and normalized
views. For current counts, run:

```powershell
    D:\anaconda\python.exe scripts\audit_data_quality.py --db kpl_data.duckdb --schema schema.py --out reports\data_quality_latest.md
```

## Install And Test

```powershell
cd D:\accio\stock_data
D:\anaconda\python.exe -m pip install -r requirements.txt
D:\anaconda\python.exe -m pytest -q -p no:cacheprovider
```

`pytest.ini` sets `pythonpath = .`, so tests can be run from the project root
without manually setting `PYTHONPATH`.

The declared core runtime range is Python 3.11 through 3.12. The legacy
`requirements.txt` is for whole-source development, not minimal core deployment.

## Historical Daily Workflow (migration reference, not the V2 default)

The integrated runner is phase-aware. By default `--phase auto` selects the
current China-market window and skips fresh snapshots; it does not run the
historical/backtest/news fan-out during trading hours:

```powershell
D:\anaconda\python.exe scripts\run_integrated_daily.py --db kpl_data.duckdb --trade-date <YYYY-MM-DD> --phase auto
```

Use the four profiles explicitly when operating the system:

```powershell
# 09:15-09:27: KPL market context/candidates plus KPL tick and independent
# Tencent five-level auction-window snapshots
D:\anaconda\python.exe scripts\run_integrated_daily.py --db kpl_data.duckdb --phase auction
# 09:30-15:00: market context, full-market stock flow and full-sector flow only
D:\anaconda\python.exe scripts\run_integrated_daily.py --db kpl_data.duckdb --phase intraday
# 15:00-18:30: final snapshots, bounded finance gap-fill and close review
D:\anaconda\python.exe scripts\run_integrated_daily.py --db kpl_data.duckdb --phase close
# Off-hours/manual only: checkpointed 2026 history and after-close sources
D:\anaconda\python.exe scripts\run_integrated_daily.py --db kpl_data.duckdb --phase history --history-start 20260101 --history-end 20260715 --history-max-days 5
```

The exact source/cadence matrix and current TTL decisions are written by:

```powershell
D:\anaconda\python.exe scripts\describe_collection_profiles.py --db kpl_data.duckdb --phase intraday
```

Collection runs require an explicit phase (`auction`, `intraday`, `close`, or
`history`). The retired `--phase full` compatibility fan-out is no longer a
supported production entry point.

The integrated runner collects market and focused capital-flow data by
default, takes an exclusive database lock, and writes an atomic run manifest
below `reports/runs/<run-id>/run.json`. Intraday data gates block signals but
do not stop the retry watcher. At the close, failed data gates remain
non-actionable while the runner continues far enough to publish the review,
dashboard and exact gap reports. A close run that published its reports but
was blocked by a data gate is recorded as `completed_blocked` and exits 0 on
purpose: the manifest/readiness report carries the blocked state, and the
zero exit prevents Task Scheduler from mislabeling a published fail-closed
review as an infrastructure failure. Other data or chain failures still exit
nonzero. Use `--skip-collect` only for an offline/research rerun of data
already captured.

Before any signal run, verify same-date readiness. A nonzero exit means the
requested stage is not actionable:

```powershell
D:\anaconda\python.exe scripts\check_data_readiness.py --db kpl_data.duckdb --date <YYYY-MM-DD> --stage close --gate data
```

During the trading session, refresh bounded intraday capital-flow evidence
before regenerating signals and reports:

```powershell
D:\anaconda\python.exe scripts\collect_capital_flow_focus.py --db kpl_data.duckdb --date <YYYY-MM-DD> --max-stocks 8 --max-sectors 6 --strict
D:\anaconda\python.exe scripts\generate_signals.py --db kpl_data.duckdb --date <YYYY-MM-DD>
D:\anaconda\python.exe scripts\generate_operator_reports.py --db kpl_data.duckdb
D:\anaconda\python.exe scripts\generate_web_dashboard.py --db kpl_data.duckdb --out reports\trading_dashboard_latest.html
```

The focused collector writes `reports/capital_flow_freshness_latest.md` and
separately reports individual-stock and sector-flow coverage. Sector volume is
not accepted as a replacement for directional sector capital flow. It uses a
short request timeout, a total collection budget, and a circuit breaker so a
network outage cannot hold the daily pipeline indefinitely. Historical
integrated reruns must use `--skip-collect` because some endpoints expose only
the current snapshot.

To audit already stored evidence without making network calls:

```powershell
D:\anaconda\python.exe scripts\check_capital_flow_health.py --db kpl_data.duckdb --date <YYYY-MM-DD> --report-only
```

Important generated reports:

- `reports/operator_report_latest.md`
- `reports/trading_dashboard_latest.html`
- `reports/daily_review_latest.md`
- `reports/daily_review_statistics_latest.md`
- `reports/stage_backtest_latest.md`
- `reports/strategy_backtest_latest.md`
- `reports/real_data_backfill_latest.md`
- `reports/empty_table_catalog_latest.md`
- `reports/data_readiness_latest.md`
- `reports/capital_flow_freshness_latest.md`

`*_latest` 复盘文件只能由 integrated pipeline 的事务发布更新。手工预览请输出到
`reports/qa_preview_<date>/` 等隔离目录；若确需受控恢复，必须显式使用
`--allow-direct-publish`，并在恢复后重新执行 close run 与发布指针核验。

## Manual Trading Loop

The daily loop converts signals into manual workflow records:

- `watchlist`: pre-market candidates and thesis
- `trade_plan`: manual plan, position cap, entry/stop conditions
- `risk_snapshot`: market-state position limits
- `portfolio_snapshot`: current/manual position snapshot
- `trade_journal`: stage decisions and review tags
- `operator_trade_outcome`: imported real execution/skipped/cancelled outcomes

These records are review aids only. They are not orders.

Import reviewed operator outcomes after the close:

```powershell
D:\anaconda\python.exe scripts\import_operator_trade_outcomes.py --db kpl_data.duckdb --csv path\to\operator_outcomes.csv
D:\anaconda\python.exe scripts\run_operator_backtest.py --db kpl_data.duckdb
D:\anaconda\python.exe scripts\generate_daily_review.py --db kpl_data.duckdb
```

The CSV should include at least `trade_date`, `stock_code`, and
`execution_status`. Common review columns are `entry_price`, `exit_price`,
`position_pct`, `outcome_tag`, `mistake_tag`, and `review_note`.

## Signal Layers

Key normalized evidence views:

- `v_market_state_inputs`
- `v_theme_mainline_evidence`
- `v_intraday_capital_flow_evidence`
- `v_intraday_strength_evidence`
- `v_research_event_evidence`
- `v_lhb_review_evidence`

Candidate stages:

- `premarket_pool`
- `auction_confirmation`
- `intraday_strength`
- `close_decision`

Run them only in their real decision windows:

```powershell
# Before 09:15; consumes the previous completed session only.
D:\anaconda\python.exe scripts\generate_stage_signals.py --db kpl_data.duckdb --date <YYYY-MM-DD> --stage premarket_pool
# 09:15-09:30; requires real per-stock auction evidence.
D:\anaconda\python.exe scripts\generate_stage_signals.py --db kpl_data.duckdb --date <YYYY-MM-DD> --stage auction_confirmation
# 09:30-11:30 or 13:00-14:50; requires real per-stock intraday evidence.
D:\anaconda\python.exe scripts\generate_stage_signals.py --db kpl_data.duckdb --date <YYYY-MM-DD> --stage intraday_strength
# From 14:50; requires a valid same-date close/reference price per stock.
D:\anaconda\python.exe scripts\generate_stage_signals.py --db kpl_data.duckdb --date <YYYY-MM-DD> --stage close_decision
```

Every stage-v2 signal preserves the source trade date, as-of cutoff, run id,
readiness snapshot, feature version, and individual-stock actionability. Missing
or fallback evidence is retained as `blocked_data_quality` and cannot enter the
operator plan. Close-stage proxy backtests use next-session open-to-close;
intraday signals without a captured executable price are excluded.

## Backfill And Coverage

Historical coverage is tracked by:

```powershell
D:\anaconda\python.exe scripts\report_real_data_backfill.py --db kpl_data.duckdb
```

Optional TuShare relay basic-data supplement:

```powershell
$env:TUSHARE_FAST_RELAY_TOKEN = "<your-token>"
$env:TUSHARE_RELAY_MIN_INTERVAL_SECONDS = "0.6"
D:\anaconda\python.exe scripts\collect_tushare_basic_data.py --db kpl_data.duckdb --start-date 20250101 --end-date <YYYYMMDD> --max-stocks 20 --sync-core
```

This collects bounded `trade_cal`, `stock_basic`, `daily`,
`daily_basic`, `adj_factor`, and `index_daily` samples into `tushare_*`
staging tables. `--sync-core` copies only the requested code/date scope into
the existing `kline` and `index_kline` tables for backtest coverage. TuShare
relay data is a base-data supplement, not an automatic trading signal.

For slow, recoverable history growth, prefer the incremental backfill runner:

```powershell
D:\anaconda\python.exe scripts\backfill_tushare_incremental.py --db kpl_data.duckdb --start-date 20260701 --end-date <YYYYMMDD> --max-stocks 10 --index-codes SH000001,SZ399001,SZ399006 --run-limit 5 --sync-core
```

It writes `tushare_gap_status`, creates resumable `tushare_backfill_task`
records, runs only pending tasks, keeps completed tasks done across reruns, and
generates `reports/tushare_gap_latest.md`. Stock and index universes are kept
separate; always pass `--index-codes` when requesting `index_daily`. Increase
`--run-limit`, widen the date range, or rotate `--stock-codes` gradually to
expand history without forcing a full rescan.

Generate the historical return/risk/valuation report:

```powershell
D:\anaconda\python.exe scripts\analyze_tushare_history.py --db kpl_data.duckdb --start-date 20250101 --end-date <YYYYMMDD> --stock-codes <comma-separated-codes> --out reports\tushare_history_analysis_latest.md
```

The report includes interval return, annualized volatility, maximum drawdown,
win rate, latest PE/PB, adjustment-factor coverage, industry grouping, and
three-index comparison. It explicitly flags that the sample is not a random
全市场 sample and does not constitute an investment recommendation.

Empty tables are classified by:

```powershell
D:\anaconda\python.exe scripts\build_empty_table_catalog.py --db kpl_data.duckdb
```

Missing auction ticks must remain explicit gaps. Do not synthesize raw market
data. The independent Tencent source is stored as
`auction_quote_snapshot` and is explicitly labelled as a five-level order-book
snapshot, not an exchange transaction tick. Its server date and 09:15-09:27
timestamp must both pass before it can satisfy auction evidence.

Before repairing a production database, make and verify a separate backup.
The remediation utility is intentionally explicit:

```powershell
D:\anaconda\python.exe scripts\repair_critical_integrity.py --db kpl_data.duckdb --dry-run
```

## Extension Rules

### Optional QLib environment and Eastmoney recovery

QLib is installed only in the project-local `.venv-qlib`; verify it before shadow training:

```powershell
cd D:\accio\stock_data
.\.venv-qlib\Scripts\python.exe scripts\check_qlib_env.py
```

The live Eastmoney clist collector uses a persisted circuit breaker and bounded front-door rotation. When the upstream route resets connections, it keeps the last auditable partial snapshot and resumes missing pages after the cooldown; it never relabels historical data as today's intraday flow. See `docs\qlib_optional_env.md` and `docs\eastmoney_clist_recovery.md`.

### Production data gates

The scheduler now fails closed when the requested trade date is missing, a
snapshot is older than the phase freshness window, a fallback-only market
state is being used, or full-market flow coverage is below 99.5%. Check the
same gates manually with:

```powershell
D:\anaconda\python.exe scripts\check_data_readiness.py --db kpl_data.duckdb --date <YYYY-MM-DD> --stage intraday --max-age-seconds 600
D:\anaconda\python.exe scripts\check_capital_flow_health.py --db kpl_data.duckdb --date <YYYY-MM-DD> --max-age-seconds 600 --min-coverage-pct 99.5
D:\anaconda\python.exe scripts\audit_p0_p3_acceptance.py --db kpl_data.duckdb --date <YYYY-MM-DD> --max-age-seconds 600
D:\anaconda\python.exe scripts\audit_p0_five_day_observation.py --db kpl_data.duckdb --as-of <YYYY-MM-DD>
```

The rolling P0 observer is stricter than a green unit test. It requires five
consecutive exchange-calendar sessions with completed (not degraded)
auction/intraday/close manifests, at least 99.5% stock and sector flow
coverage, all five TuShare close datasets, a complete weekly THS 374+
concept/member snapshot, and committed review/dashboard artifacts. The close
runner writes both a latest report and a dated report automatically.

The daily review publishes individual-stock main-net inflow/outflow Top 50,
THS concept inflow/outflow Top 10, Eastmoney industry inflow/outflow Top 10,
THS concepts with their limit-up constituents, and research-only candidate
picks. Multi-source stock rows are deduplicated before market-wide flow ranks
are calculated.

Register unattended tasks from an elevated PowerShell. The installer verifies
the SYSTEM principal after registration and refuses a partial non-elevated
install:

```powershell
PowerShell.exe -NoProfile -ExecutionPolicy Bypass -File D:\accio\stock_data\scripts\install_stock_data_task.ps1 -RegisterAll -Register
```

`-RegisterAll` also installs two post-close tasks: `StockData-SupplementalRetry`
at 20:00 for late KPL/xiaodefa supplements and `StockData-QLibResearch` at
20:30 for the isolated QLib shadow refresh. Both use the single-writer lock;
each successful late refresh republishes the self-contained review page.
KPL/HiThink HTTPS uses verified direct transport first and falls back to the
configured proxy. If a provider uses a private root, configure
`KPL_SSL_CA_BUNDLE`; certificate verification is never disabled.

`is_executable` means stock-level evidence is complete; a candidate is
counted as execution-ready only when `risk_approved` is also true. Blocked,
skipped, or price-missing operator outcomes are excluded from realized-return
statistics. Production backtests use the T+1 path; the compatibility flag is
reserved for legacy unit fixtures.

- Prefer adapters over copying external projects into this repo.
- Keep qlib in shadow mode until predictions have enough validated samples.
- Use second-source market data for long K-line history if KPL cannot provide
  enough depth.
- Import real operator outcomes before judging operator backtest quality.
- Do not store API keys in reports or commit environment files with secrets.
