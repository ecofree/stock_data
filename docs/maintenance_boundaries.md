# Maintenance boundaries

Updated 2026-09-17 in the remediation checkout. This describes prepared
responsibilities, not successful production deployment.

## One collection owner, one product publisher

- The existing integrated runner owns auction, intraday, close and explicit
  supplemental collection. All use the same database lock, calendar gate,
  runtime/source contract, bounded child processes and per-run receipts.
- Supplemental retains LHB, auction, index and bounded chips/margin retrieval.
  It does not render legacy pages or run research. After its lock is released,
  the PowerShell adapter may request StockData-ResearchDaily using a unique
  completed receipt; a degraded source remains degraded. No request is made
  after a lock conflict, calendar block or failed normalization.
- A publication request is not proof of a successful publication. If the
  destination is disabled, unsafe or already running, the adapter fails
  explicitly; it does not enable it or assume a request was queued.
- run_research_daily.ps1 owns local market publication. RefreshResearch adds
  the current research product's frozen-model update, never training,
  promotion, legacy registry backfill or old page callbacks.
- The workspace separates current market data from dated historical
  predictions. Missing forecasts/accounts do not block local observation.
  Date mismatch does block a purported current publication.
- Pages remain self-contained; no sidecar data or runtime fetch is introduced.

## Scheduler and recovery

install_stock_data_task.ps1 is proposal-only. It requires the administrator's
complete seven-task export and hashes; Register/RegisterAll are rejected.
Auction, intraday, close and supplemental retain their existing schedules and
collector identity. Both research tasks propose the retained dedicated
Limited/Password identity. MonthlyCompact stays disabled with its action intact.
All before-XML definitions are included for a separately authenticated rollback.

Do not run old deployment Apply commands or renew old windows. Before cutover:
compare all seven current task definitions, verify rollback and ACL restoration,
protect exact runtimes/release/configuration, validate dedicated-identity launch
and authenticated task updates, then approve a new scope and time window.
A current-version installer still needs implementation and rehearsal.

## Evidence and cleanup

Four legacy supplemental/QLib scheduler files and their obsolete catch-up test
were removed from this checkout. Live scripts, databases, historical receipts,
human notes and retained recovery directories are not cleanup targets.
Do not delete immutable guard files or kill writers to make a run pass.
Tests use synthetic tasks and temporary databases; no real scheduler writes.

Source presence, fresh observations, analysis readiness and execution readiness
remain distinct. Publication must be checked at the final output directory.
Passing tests or a generated proposal does not certify live deployment, model
usefulness, human judgement quality or account acceptance.
