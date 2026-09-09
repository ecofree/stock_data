# QLib 在本项目中的使用方式

QLib 作为研究/影子模型层参与候选池排序融合和复盘解释，但不直接改变交易计划、仓位建议或执行信号。数据先由项目自己的 DuckDB 规范化表提供，模型输出再经过数据库评估和人工结果门禁。

## 1. 导出特征

PowerShell:

    .\.venv-qlib\Scripts\python.exe scripts/export_qlib_features.py --db kpl_data.duckdb --start-date 2026-01-01 --end-date 2026-07-13 --out reports/qlib_features_2026.csv --format both --label-mode t1_exec

输入表为 tushare_daily、tushare_daily_basic、tushare_moneyflow、tushare_adj_factor 和已认证的 qlib_stock_flow_features。价格、收益和标签按 adj_factor 统一为复权口径，成交量按反向因子处理；默认标签为 t1_exec（下一交易日开盘买入、再下一日收盘卖出的 T+1 合规口径），legacy 仅供研究对比，训练新模型禁止使用 legacy；label_date 和它都不能作为特征。导出旁边会生成 .metadata.json，记录覆盖范围、复权状态、样本数和防泄漏声明。

## 2. 影子训练与导入

PowerShell:

    $env:MLFLOW_ALLOW_FILE_STORE = "true"
    .\.venv-qlib\Scripts\python.exe scripts/train_qlib_shadow.py --db kpl_data.duckdb --features reports/qlib_features_2026.parquet --model-id qlib_shadow_lgbm_2026_ytd --max-rows 300000 --num-boost-round 120

脚本使用 QLib LGBModel，按日期切分训练集/验证集，将模型文件保存到 reports/qlib_models/，预测写入 qlib_prediction，注册信息写入 qlib_model_registry。默认 status=shadow；预测可进入 `qlib_candidate_pool` 做研究排序，但不会成为正式信号。

## 3. 评估

PowerShell:

    .\.venv-qlib\Scripts\python.exe scripts/evaluate_qlib_shadow.py --db kpl_data.duckdb --out reports/qlib_shadow_latest.md

正式启用前必须同时满足：有足够的历史验证样本、有人工交易结果、有可解释的候选重合分析，并且通过项目的 readiness/backtest 门禁。评估默认含 25bps 双边成本（`--round-trip-cost-bps` 可覆盖，传 0 为毛收益对比），结果按 (model, method, quantile, cost) 版本化追加，不删其他参数历史。当前 signal_impact 固定为 disabled。

## 每日运行

收盘数据通过认证后，使用隔离的 QLib 环境运行：

    .\.venv-qlib\Scripts\python.exe scripts\run_qlib_research_daily.py --db kpl_data.duckdb --trade-date YYYY-MM-DD --reports-dir reports

该任务由独立的 `StockData-QLibResearch` 计划任务运行，不阻塞收盘主链；失败时保留失败报告，页面仍以当前已认证数据生成。`qlib_research_latest.json` 中的 `usage` 字段记录当日预测行数、融合候选数、涨停池重合数和当前模型后验评估。当前仍是 `model_status=shadow`、`signal_impact=disabled`，不能直接改变交易计划。
