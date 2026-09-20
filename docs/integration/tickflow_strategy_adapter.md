# TickFlow Strategy Adapter

本文件保留历史接入说明。2026-09-20 已退出旧分数扫描与 SQL 写入链；当前策略使用 `trade_system/v2/strategies.py` 的现有路径。

## 已退出入口

- `trade_system.strategy.definition`：策略定义协议。
- `trade_system.strategy.engine`：四阶段候选股策略扫描。
- `trade_system.strategy.schema`：策略定义、扫描结果、回测结果表。
- `trade_system.strategy_stage_backtest`：基于 T+1 下一交易日收盘价的策略结果回测。
- `scripts/run_strategy_scan.py`：从 `v_operator_candidates` 或 `stock_candidate_stage_signal` 扫描策略候选。
- `scripts/run_strategy_result_backtest.py`：回测策略扫描结果。

以上模块和命令均已删除，不再用于日常运行。历史 `strategy_scan_result`、`strategy_backtest_result` 表及其只读研究消费者继续保留；旧源码可从 Git 基线 `d1b0d89` 恢复用于核对，不能据此恢复生产写入资格。

## 历史阶段映射

- `premarket_pool` -> `pre_market`
- `auction_confirmation` -> `auction_confirm`
- `intraday_strength` -> `intraday_strength`
- `close_decision` -> `closing_decision`

## 安全边界

- 不自动下单。
- 不迁移 tickflow 整套 FastAPI + React。
- 不使用未经验证的内置策略阈值作为实盘依据。
- 所有结果必须带入选原因、风险点、失效条件和可回测样本。
