# Operator Candidate And Backtest Contract

## Candidate Tables

`stock_candidate_score`

- Purpose: native stock_data candidate score from limit-pool, sector, kline,
  auction, market-regime, and fallback evidence.
- Granularity: one row per stock/date.
- Use: pre-market pool and broad candidate review.

`stock_candidate_stage_signal`

- Purpose: four-stage operator signal.
- Stages: `premarket_pool`, `auction_confirmation`, `intraday_strength`,
  `close_decision`.
- Use: staged review, daily loop, stage backtest, daily review report.

`strategy_scan_result`

- Purpose: strategy-engine output inspired by TickFlow style strategy scanning.
- Use: strategy comparison and optional operator candidate union.
- It should not replace `stock_candidate_stage_signal` unless a future
  migration explicitly consolidates the contracts.

`operator_trade_outcome`

- Purpose: imported human-reviewed outcomes for planned trades, skipped trades,
  cancelled ideas, and post-close review notes.
- Granularity: one row per stock/date/operator outcome.
- Use: operator backtest, daily review, mistake tagging, and discipline
  statistics.
- Rule: this table stores review evidence only. It must never be used to create
  automatic orders.

## Backtest Modules

`trade_system.backtest.run_stage_candidate_backtest`

- Purpose: lightweight stage signal forward-return check.
- Limitation: close-to-next-close proxy; not enough for intraday execution
  quality.

`trade_system.operator_backtest`

- Purpose: operator-level validation with cost/slippage assumptions.
- Priority: use `operator_trade_outcome` when real outcomes exist; otherwise
  fall back to K-line proxy samples from `stock_candidate_stage_signal`.
- Limitation: proxy mode is not execution-quality evidence and must be labeled
  as such.

`trade_system.review_statistics`

- Purpose: daily review statistics and sample-confidence labels.
- Rule: if return samples are below threshold, report `insufficient_sample`
  instead of implying signal quality.

`scripts/run_operator_backtest.py` also emits a readiness gate. It requires at
least 250 historical K-line trading days, 50 executable return samples, and
30 imported `operator_trade_outcome` rows before reporting `ready`. A run with
stage signals but no `is_actionable=true` rows is reported as
`insufficient_sample`; proxy returns are never presented as execution quality.

## Consolidation Guidance

Do not merge these modules during routine feature work. Consolidate only after:

1. Historical K-line and sector samples cover at least 250 trading days.
2. Daily operator loop has produced enough `trade_plan` and `trade_journal`
   samples.
3. `operator_trade_outcome` has enough executed/skipped/cancelled samples for
   discipline review.
4. Stage statistics show stable sample sizes across market regimes.
5. The replacement contract has tests for all four operator stages.
