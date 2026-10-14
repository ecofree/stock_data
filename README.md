# stock_data

A股人工研究与复盘助手，保留合格行情、带来源的资金观察和人工记录；不创建真实订单。

## 当前运行状态

[状态入口](docs/v2/STATUS.json)只定位稳定运行目录的`state/current.json`。
源码版本、候选测试、历史关闭账均不是当前部署证明。状态文件未建立时报告“未配置/未核验”，
不能自行推断全量暂停、已恢复或五日通过。当前状态只在实际交接成功后由相应回执建立。

历史回执、失败和原始输入保留，不修改原日期、来源或哈希。旧关闭账移入历史归档后不再承担活动准入。
运营完整日与严格金融完整日分别累计；账户未导出只限制账户分析，不阻断合格市场事实。

## 固定职责与目录

- 一个Git开发工作树；生产执行版本化只读发布包，不读取开发或历史整改目录。
- `STOCKDATA_RUNTIME_ROOT`显式指定稳定运行根：`market/`、`workspace/`、`collection-reports/`保持原职责。
- 可变内容在`state/acquisition/`、`logs/`、`tmp/duckdb_spill/`，不写入发布源目录。
- 提供商凭据只由采集身份通过显式`KPL_ENV_FILE`读取；发布不隐式读取源码或cwd的`.env`。
- 财务原件放在`evidence/objects/<sha256>`；历史路径仅作来源标识，缺件或错哈希不得回退旧目录。
- 源码分发身份不等于写入或交接授权。采集仍须准确合同、明确目标、单写者guard、账户库排除及OS权限。
- 当前版和一个已验证的回滚版之外，只保留必要历史归档，不永久展开旧环境和测试负载。

`market/kpl_data.duckdb`、原guard、人工事件和窗口快照在本次目录收敛中不改路径、不删表。
永久guard不得删除；超时不能强杀写者或凭PID/元数据抢占写入。

## 运行与查看

- 采集入口：`scripts/run_integrated_daily.py`，由既有PowerShell任务包装器执行。
  新布局任务显式传入`-RuntimeRoot`，该路径也由采集合同绑定；计划任务恢复需单独批准。
- 独立发布：`scripts/run_research_daily.ps1`，使用已封存发布目录、专用受限身份及现有workspace。
- 人工查看：`Start Research.cmd -RuntimeRoot "D:\accio\stock_data-runtime"`。
  它只读取已接受的`state/current.json`中的发布绑定；未交接的准备记录不能启动该入口。
  也可显式提供Python、workspace、release及manifest哈希；禁止与部署状态混用。
- 页面为数据内联静态产物。查看不会采集、训练、自动晋级或执行交易。

岗底斯活动请求、无人值守全市场完整估值承诺、活动模型训练/预测/晋级已退役。
不合格估值只限制相应证券的估值依赖用途；单产品资金事实不冒充独立资金确认。

## 安装与验证

- 生产采集使用`requirements-collector.lock`的运行闭包；`requirements-data.lock`用于开发数据测试，不把pytest/ruff放入生产。核心安装与日常发布使用`requirements-core.lock`。
- 冻结历史研究使用独立、显式的`requirements-research-replay.lock`环境，不是每日发布的前置条件。
- 统一发布验证仍为`tools/v2/verify_delivery.py`及现有测试；日常`tools/v2/run_tests.py --profile quick --output <新证据目录>`只给短反馈，不能代替发布门。
- `release`测试集合包含数据、历史恢复及压力案例；`history`/`research`/`stress`可按变更域单独运行。完整CI仍要求数据与历史研究共同通过，不删除安全反例。
- 现有Git `pre-push`钩子默认使用独立开发环境运行完整数据集合（含历史恢复与压力反例），不再回退全局解释器。可显式提供两小时内同提交/同字节的完整验证回执及SHA，实际复读全部证据并核对当前测试集合后复用，不重复展开同一套测试；不接受旧XML或仅有通过标志。独立研究环境可显式指定；未指定时明确报告研究未在本地验收，仍须远端独立研究CI通过，不能以本地数据门代替。
- 开发只使用当前唯一工作树中的独立`.venv/python.exe`。离线重建入口`tools/v2/prepare_development_runtime.py`，输入必须是保留的原始CPython ZIP与完整哈希锁定wheel，不依赖全局Anaconda。
- 新测试展开目录位于源码之外，统一owner/finished记录和容量预算；参见[工件保留规则](docs/v2/ARTIFACT_RETENTION.md)。
- 新原件的私有审核、精确SHA接收及权限边界见[估值接收规范](docs/v2/VALUATION_INTAKE.md)。接收文件不授予金融资格，不导入业务库。
- Git-free源码包保留`source-distribution.json`身份，完整采集依赖由collector合同另行绑定。
  核心wheel不获得旧式磁盘写入权，V2/账户库继续拒绝旧写入。

目录迁移验收与五日金融验收独立。只有实际任务、输入、提交、发布和窗口回执能证明相应生产结果。
