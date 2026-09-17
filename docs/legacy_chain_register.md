# Legacy chain register

This register is the P1/P2 maintenance boundary. It records what remains
reachable without allowing a second scheduled writer to compete with the
canonical path.

## Keep as canonical

| Area | Owner | Rule |
|---|---|---|
| Auction | `scripts/collect_auction_market_daily.py` | One KPL `/auction/market` request; writes `auction_tick` and `auction_quote_snapshot`. |
| Close history | `TushareHistoryCollector` | xiaodefa first, fast relay fallback; publish only certified same-date snapshots. |
| Collection | `scripts/run_integrated_daily.py` | Shared lock, calendar gate and bounded receipts; no user pages. Supplemental phase retains LHB, auction, index and chips/margin. |
| Review | `scripts/run_research_daily.ps1` | Unified local market workspace; independent of model refresh. |
| QLib | `scripts/run_research_daily.ps1 -RefreshResearch` | Current frozen-model update, no training/promotion or legacy registry backfill. |

2026-09-17: the four old supplemental/QLib scheduler files were deleted from
the remediation checkout, not the live checkout. Seven-task replacement is
proposal-only until protected installation and a new window are approved.

## Retain for recovery or comparison

- `collect_auction_tick_daily.py` and `collect_auction_anomaly_daily.py` remain
  compatibility scripts only. They are not part of the close schedule and must
  not be used to label a date as auction-ready.
- `fetch_all.py` and `collect_professional_sources.py` remain bounded recovery
  paths. They must not promote rows into canonical relations unless an
  explicitly reviewed migration command is used.
- `backfill_*`, `sync_*`, and legacy provider adapters remain historical
  recovery tools with checkpoints; they are not daily production writers.

## Do not delete yet

Empty tables are not deleted in routine maintenance. They are classified as
one of: `workflow_not_used`, `permission_denied`, `needs_trading_session`,
`api_available_not_collected`, `reachable_empty`, `external_missing`, or
`intentional_or_unclassified`. A table can be archived only after 30 days of
no runtime read/write dependency and a reversible migration is prepared.

## QLib boundary

QLib contributes a ranked research candidate pool and measurable shadow
metrics. It does not bypass `risk_approved`, does not create orders, and does
not become a champion model without a current-source evaluation window and
operator-reviewed outcomes.
