# QLib 在本项目中的使用方式

QLib 只作为研究/影子模型层，不直接改变候选池、交易计划或仓位建议。数据先由项目自己的 DuckDB 规范化表提供，模型输出再经过数据库评估和人工结果门禁。

## 1. 导出特征

PowerShell:

    D:\anaconda\python.exe scripts/export_qlib_features.py --db kpl_data.duckdb --start-date 2026-01-01 --end-date 2026-07-13 --out reports/qlib_features_2026.csv --format both

输入表为 tushare_daily、tushare_daily_basic、tushare_moneyflow。label_next_ret 是下一可用交易日收盘收益率，只能作为训练目标；label_date 和它都不能作为特征。导出旁边会生成 .metadata.json，记录覆盖范围、样本数和防泄漏声明。

## 2. 影子训练与导入

PowerShell:

    $env:MLFLOW_ALLOW_FILE_STORE = "true"
    D:\anaconda\python.exe scripts/train_qlib_shadow.py --db kpl_data.duckdb --features reports/qlib_features_2026.parquet --model-id qlib_shadow_lgbm_2026_ytd --max-rows 300000 --num-boost-round 120

脚本使用 QLib LGBModel，按日期切分训练集/验证集，将模型文件保存到 reports/qlib_models/，预测写入 qlib_prediction，注册信息写入 qlib_model_registry。默认 status=shadow，不会成为正式信号。

## 3. 评估

PowerShell:

    D:\anaconda\python.exe scripts/evaluate_qlib_shadow.py --db kpl_data.duckdb --out reports/qlib_shadow_latest.md

正式启用前必须同时满足：有足够的历史验证样本、有人工交易结果、有可解释的候选重合分析，并且通过项目的 readiness/backtest 门禁。当前 signal_impact 固定为 disabled。
