# QLib 在本项目中的使用方式

前 3 节记录旧研究表与显式实验工具，不是当前调度安装指南，也不允许对生产库直接执行历史实验。当前调度方案见“每日运行”。QLib 不直接改变交易计划、仓位或执行信号。

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

2026-09-17：旧每日回填调度入口已在整改目录删除。计划将 `StockData-QLibResearch`
接到现有 `scripts/run_research_daily.ps1 -RefreshResearch`；完整参数来自七任务提案，
包括独立研究 Python、固定发布包及哈希、工作区和显式受保护的来源配置。

先发布日期匹配的本地市场快照，再由当前研究产品 `update` 使用既有冻结模型推理。
明确休市时不请求供应商；市场日期不符或发布失败则退出。模型刷新失败保留已发布市场页；
历史预测必须标明其日期，不能当作当日候选。无自动训练、晋级或旧表回填。

这是待部署职责，不代表系统任务已经切换。真实运行验收须核对任务返回值、调度日志、
工作区发布清单和页面日期；排队、非空预测与研究效用分别验收。
