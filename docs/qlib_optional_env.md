# QLib 隔离研究环境

2026-10-22：研究依赖使用独立 Windows CPython 3.12 环境和单一哈希锁。
新环境为 `D:\accio\stock_data-research-isolated-20261022`，解释器是
`Scripts\python.exe`。它没有系统 site-packages 继承，未接入生产任务。
旧 `.venv-qlib` 和 Anaconda 保留原样；旧研究环境依赖冲突没有通过修改父环境掩盖。

## 重建方式

选择一个不存在的新目录，使用已验证的 CPython 3.12 解释器创建环境：

```powershell
uv venv --python D:\accio\stock_data-dev\.venv\python.exe D:\accio\stock_data-research-new
uv pip install --require-hashes --python D:\accio\stock_data-research-new\Scripts\python.exe -r D:\accio\stock_data-dev\requirements-research-test.lock
D:\accio\stock_data-research-new\Scripts\python.exe -I -m ensurepip
D:\accio\stock_data-research-new\Scripts\python.exe -I -B -m pip check
```

`requirements-research-test.lock` 包含 `requirements-research-replay.lock`；
这是一次完整解析后的 200 个研究及测试依赖。ensurepip 仅安装解释器自带的 pip 检查工具，
不修改锁中的依赖。纯重放环境可只安装 replay 锁。
不能使用 `--system-site-packages`，不能先装另一套 QLib 锁再用 `--no-deps` 覆盖降级。
本次冻结契约包括 NumPy 1.26.4、Pandas 2.2.2、LightGBM 4.6.0、QLib 0.9.7、DuckDB 1.5.4。

## 验收边界

需要同时核实 pyvenv.cfg、隔离启动的 sys.path、包元数据重复项、实际导入路径、
锁哈希、全部安装版本和 pip check。只有导入成功不足以验收。
本次环境证据位于 `D:\accio\stock_data-independent-fixes-20261022\research-environment.json`。

在项目外冻结源码、清空供应商凭据与生产配置、阻止外网和越界写入后运行：

- `tests/test_research_runtime_contract.py`：合成数据的 158 因子、未来扰动隔离、拟合及冻结重载。
- `tests/test_research_delivery.py::test_prediction_and_contributions_use_frozen_feature_order`：模型特征顺序和贡献度。
- 已有历史产物只读核验与重放：原注册、模型、特征及预测文件的哈希必须保持原值；
  版本或实现不匹配时明确拒绝，不能改登记信息来制造通过。

这里没有授权生产训练、模型发布、任务切换或恢复运营。主动训练/刷新等已退役入口保持拒绝；
环境建立及合成测试通过不代表研究有效、历史策略有效或可以实盘。
