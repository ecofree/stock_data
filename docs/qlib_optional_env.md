# QLib 可选依赖环境

QLib 不放入每日采集解释器。核心采集使用 `D:\anaconda\python.exe`，研究/影子模型使用项目内的 `.venv-qlib`，避免 cvxpy、gym、MLflow 等依赖污染核心环境。

## 安装

```powershell
cd D:\accio\stock_data
D:\anaconda\python.exe -m venv --system-site-packages .venv-qlib
.\.venv-qlib\Scripts\python.exe -m pip install -r requirements-qlib.txt -i https://pypi.org/simple
.\.venv-qlib\Scripts\python.exe scripts\check_qlib_env.py
```

Windows/Python 3.12 下 `gym==0.26.2` 没有 wheel，需要源码构建；全量解析过慢时可按下面顺序执行：

```powershell
.\.venv-qlib\Scripts\python.exe -m pip install --only-binary=:all: --prefer-binary cvxpy==1.7.1 filelock==3.16.1 ruamel.yaml==0.18.10 -i https://pypi.org/simple
.\.venv-qlib\Scripts\python.exe -m pip install gym==0.26.2 --no-binary gym --no-deps -i https://pypi.org/simple
.\.venv-qlib\Scripts\python.exe -m pip install mlflow==3.14.0 --no-deps -i https://pypi.org/simple
.\.venv-qlib\Scripts\python.exe scripts\check_qlib_env.py
```

检查脚本只做导入和 `LGBModel` 构造 smoke test，不访问网络、不写 DuckDB、不启动 MLflow 服务。QLib 仍然只用于 shadow 训练、预测和后验评估，不直接改变候选池或交易信号。

## 运行

```powershell
.\.venv-qlib\Scripts\python.exe scripts\export_qlib_features.py --db kpl_data.duckdb --start-date 2026-01-01 --end-date 2026-07-14 --out reports\qlib_features_2026.csv --format both
.\.venv-qlib\Scripts\python.exe scripts\train_qlib_shadow.py --db kpl_data.duckdb --features reports\qlib_features_2026.parquet --model-id qlib_shadow_lgbm_2026_ytd
.\.venv-qlib\Scripts\python.exe scripts\evaluate_qlib_shadow.py --db kpl_data.duckdb --out reports\qlib_shadow_latest.md
```

