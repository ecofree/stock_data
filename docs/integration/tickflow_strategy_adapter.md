# TickFlow Strategy Adapter

本阶段吸收 `tickflow-stock-panel` 的策略定义、扫描、监控和回测思想，但不引入 tickflow 包作为硬依赖，也不迁入 React 工作台。

## 已接入边界

- `trade_system.strategy.definition`：策略定义协议。
- `trade_system.strategy.engine`：四阶段候选股策略扫描。
- `trade_system.strategy.schema`：策略定义、扫描结果、回测结果表。
- `trade_system.strategy_stage_backtest`：基于 T+1 下一交易日收盘价的策略结果回测。
- `scripts/run_strategy_scan.py`：从 `v_operator_candidates` 或 `stock_candidate_stage_signal` 扫描策略候选。
- `scripts/run_strategy_result_backtest.py`：回测策略扫描结果。

## 阶段映射

- `premarket_pool` -> `pre_market`
- `auction_confirmation` -> `auction_confirm`
- `intraday_strength` -> `intraday_strength`
- `close_decision` -> `closing_decision`

## 安全边界

- 不自动下单。
- 不迁移 tickflow 整套 FastAPI + React。
- 不使用未经验证的内置策略阈值作为实盘依据。
- 所有结果必须带入选原因、风险点、失效条件和可回测样本。
