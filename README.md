# stock_data

A 股人工研究与复盘助手。项目分为日常数据、独立研究和显式维护；不创建真实订单。

## 当前状态

本地整套业务运行已按用户要求暂停。清理候选在 `D:\accio\stock_data-retirement` 的
`codex/cleanup-20260917` 分支；`D:\accio\stock_data` 仍是旧运行目录。
候选尚未完成全部清理验收，不能当作已部署版本。不要启动旧任务或照历史文档恢复运行。

唯一清理关闭账是 [handover completion](reports/handover-20260917/completion.json) 的
`cleanup_20260917`：记录基线、结果版本、删除与迁移、验证及未执行的生产动作。
[状态入口](docs/v2/STATUS.json) 指向同一记录；历史交付材料不是当前授权或验收证明。

## 保留职责

- 日常数据：`scripts/run_integrated_daily.py` 组织获准采集和发布；采集保存原始事实，
  `trade_system/normalize.py` 建立标准视图。TuShare 仅保留 `XiaodefaClient`，来源配置不能恢复 fastapic。
- 复盘与人工记录：页面读取已有快照；人工记录保留独立事件身份。无模型仍可查看市场和记录判断。
  `reports/daily_review_latest.html` 是数据内联的静态页面，离线查看不会联网、训练或回测。
- 独立研究：使用冻结数据与模型，研究依赖单独安装；研究结果不自动获得当前决策权限。
- 维护与恢复：仅在明确范围内执行；[旧机器专用恢复包](recovery/20260916.zip) 成套保留原脚本、匹配代码与工件哈希，
  仅供历史检查和隔离恢复测试；旧脚本入口拒绝执行。实际恢复仍须检查保留的原始日志与机器状态。

旧日筛、旧大屏和 OHLC 复制命令已拒绝执行。原始表、历史 provider、人工事件和 unknown 状态必须保留。
永久 `.guard` 文件不删除；暂停或清理时不得按文件名推断锁已失效。

## 安装与验证

核心发行包由 `pyproject.toml`、`setup.py` 的模块白名单定义，依赖为 `requirements-core.lock`。
数据与研究分别使用 `requirements-data.lock`、`requirements-research-test.lock`；不要把整套研究依赖装进日常核心。

统一验证入口为 `tools/v2/verify_delivery.py`，检查源码、迁移、写入边界、目的明确的测试及已安装核心。
CI 数据与研究任务必须绑定同一提交；本地测试通过不代表实际任务成功。
清洁安装和旧环境升级也必须使用本次结果版本，不能沿用历史 0.3.15 的证明。

任务切换、恢复运行、凭据与 ACL 变更需要另行授权。当前暂停状态没有到期自动恢复机制。
