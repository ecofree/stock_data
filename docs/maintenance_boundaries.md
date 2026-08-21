# Maintenance boundaries

This project has one production path and several research/compatibility paths.
The following boundaries are now the maintenance contract.

## Canonical path

1. `scripts/run_integrated_daily.py --phase close` is the close operator path.
2. `trade_system/gate_contract.py` owns the operator state vocabulary:
   `data_certified_ready`, `flow_certified_ready`, `analysis_ready`, and
   `execution_ready`.
3. `trade_system/daily_review.py` assembles the shared review context.
4. `trade_system/review_facts.py` assembles page-only facts.
5. `trade_system/review_web.py` renders HTML and must not issue page-specific
   SQL queries. `write_review_web` publishes a bounded HTML shell plus a
   same-directory `daily_review_latest.lazy.js` detail asset; the shell keeps
   only summaries and the first 50 stock cards.
6. `trade_system/pipeline_runtime.py` publishes latest artifacts through the
   run transaction and `reports/pipeline_run_latest.json`.

The close path excludes Qlib, backtests, news, AI snapshots, and other
research steps by default. Use `--include-research` only for a deliberate
research run.

The two read/render-only daily reports run in-process by default to remove
interpreter and DuckDB reconnect churn. Collection, migration, signal and
other write-sensitive tasks remain isolated subprocesses. Use
`--subprocess-reports` only for compatibility troubleshooting.

## Compatibility boundary

Legacy boolean aliases such as `certified_ready` and `analytics_ready` remain
in payloads so old reports do not break. New code must read the explicit
operator fields. Private compatibility fallbacks in the web module are not
entry points and should not acquire new callers.

## Change and acceptance rules

- Do not delete DuckDB tables or historical reports as part of ordinary code
  cleanup. First classify them as active, compatibility, archive, or orphaned
  and record the result.
- Do not treat a passing historical report as current readiness. Use the same
  trade date and a fresh post-close readiness check.
- `source_ready` or a populated candidate pool does not imply
  `analysis_ready` or `execution_ready`; flow certification must be explicitly
  true before either downstream gate can turn green.
- Every change to the close path must pass the dry-run plan check, the full
  pytest suite, and the P2 maintenance gate.
- The P2 gate is intentionally read-only against the database. It validates
  source structure and tests; data acceptance remains the responsibility of
  the P0 readiness and flow gates.

## Routine commands

```powershell
D:\anaconda\python.exe scripts\run_maintenance_gate.py --pytest
D:\anaconda\python.exe scripts\run_integrated_daily.py --db kpl_data.duckdb --trade-date YYYY-MM-DD --phase close --skip-collect --dry-run
```
