# 基线与本轮证据 · 2026-09-10

## 最新增量：历史日线病例诊断

双环境各 949 passed，原数据环境实际病例重算通过；源码集合 `4f32bd317a75c6a0ef31aa682f9def120321e2fd330929e41ed1b9f4fd4170b4`、wheel `6d8ecd9259a62d18f821e40124e4180def02b8f283fbcfa2bac4c03cdca8ffbd`，证据为 `reports/v2-daily-cases-verification-20260910/verification.json`。离源码功能探针通过，但核心安装环境真实父包核验被原登记数据依赖门禁阻止；`historical_runtime_boundary.json` 单列此限制，不升级为完整真实链可移植验收。

见 [STAGE_DAILY_CASES_20260910.md](STAGE_DAILY_CASES_20260910.md)。实际病例登记 `c58851c669afa072651833bf5b2a8374394df93b84d2f6b4574d658433b3460b`、完成清单 `a6c8c8a4063aa72b57c4e56189b0e61f5fa40a39a2330909875f3fb3871c799a`。三个真实历史病例一件按假设完成、两件留存资产，所有组合收益仍 null。父账/权益类型入口变化；旧证据使用绑定的第 11 段旧 wheel 离线递归验证，原三条金标准完整结果未变。以下为历史版本证据。

## 最新增量：公告逐字段补证

双环境各 920 passed，Python 3.12.4 / 3.11.15 分别 105.148 / 105.257 秒，无失败/错误/跳过。`reports/v2-disclosure-verification-20260910/verification.json` 绑定源码集合 `f7c8c776a983005e31b9f738a1db930a9c6bcba32559338f2040395949a23f3b`、wheel `0d60c8ecd704f726261f3c9e715dc37618c73d922402ff3a517f4c1fa4f76425`；离源码安装探针、39 包兼容、13 迁移检查通过。探针为合成契约，真实公告补证单列，不认证账户到账。

见 [STAGE_DISCLOSURE_20260910.md](STAGE_DISCLOSURE_20260910.md)。实际归档 `reports/v2-disclosure-fields-20260910/completed.json` SHA-256 为 `187c25f9f7d19fb6428d3716b11da40bd2b3040a5dcc7fde387f7ed573e67a0a`；10 份 PDF（7 新取、3 复用）、11 案/2400 窗口、23 项字段映射待复核。全部既有 `trade_system/v2/` 模块与第 10 段安装验证摘要一致。本段无账户事件或历史组合恢复；以下为历史证据。

## 最新增量：冻结历史只读适配

见 [STAGE_ADAPTER_20260910.md](STAGE_ADAPTER_20260910.md)。`reports/v2-adapter-verification-20260910/verification.json` 绑定源码 `a8bf44fad9c27070e5f4e125c610f1b70a327a53829353e6003ea1ce0abb7986`、wheel `bed679c41ac8f8891b7c528ae6b9ac92750095526c5051515e1e0be1dbb9ba85`；双环境各 885 passed，离源码适配契约及旧父账探针通过。实际准备包保留 3 个变体/2400 窗口/3603 条日线，11 案区分历史修复与知识回放，生成父账事件 0 条。13 个原科学计算/证据模块摘要不变；以下为历史证据。

## 最新增量：权益父账与停牌退出

见 [STAGE_INTEGRATED_20260910.md](STAGE_INTEGRATED_20260910.md)。`reports/v2-integrated-verification-20260910/verification.json` 绑定源码 `690410a7f15c4ae6a4276068c4330e0a45b1a0b9aadfdf253043a22fc11de902`、wheel `9a02841ffb937ef99da4c4beeda12dccdc2a750289d09b19241657e51672dcec`；双环境各 858 passed，离源码重放三条父账金标准通过。旧关键模块、权益/公告/原生包仍可校验。仅合成父账接入，不是恢复旧历史组合或真实账户；以下为历史版本证据。

## 最新增量：权益子账与公告补证

见 [STAGE_ENTITLEMENT_20260910.md](STAGE_ENTITLEMENT_20260910.md)。`reports/v2-entitlement-verification-20260910/verification.json` 绑定源码 `7d3a010c3d6e93e094b4d43c3d7e4059afcba0d8b715783c12a52681b667fa43`、wheel `a50a11c7533010bebd3ef286506bdf8393bb6c6c426d7c77184928374503fcc1`；Python 3.12/3.11 各 809 passed，离源码子账重放与公告契约探针通过。4 份真实公告、独立合成权益账各有新清单，保留原生父包和旧裁决；原账户未恢复，父账接入未完成。以下皆为历史版本证据。

## 最新增量：原生数据源补证

见 [STAGE_NATIVE_20260910.md](STAGE_NATIVE_20260910.md)。`reports/v2-native-verification-20260910/verification.json` 绑定源码 `b7a608fd26750856b4008b9ec7c57814445b6f4fe1dd50c06c0ef66e2eddcb41`、wheel `9077cad0fe4b29d72231883e69ae055aa40937f390a604fe496f00097415498e`；双环境各 748 passed、离源码安装探针通过。真实补证首轮/续跑都保留，新轮绑定旧清单和代码存档；累计 16 次，未恢复账户。以下为历史证据，不混同为当前源码。

## 最新增量：缺口证据案件

见 [STAGE_GAP_20260910.md](STAGE_GAP_20260910.md)。`reports/v2-gap-verification-20260910/verification.json` 绑定源码 `d8eed339e75f832ff7da0546d3f12fedd61578e58e4058398301729679e8c561`、wheel `9f93493166bd72429dd0d60c408fd664562265a806b2cae591208ada9f71255b`。双环境各 715 passed，离源码安装探针通过；真实 11 个案件仍 unknown，无账户恢复。本轮没有重训或修改前段实验。下文保持原时点证据。

## 最新增量：OOS 组合诊断

见 [STAGE_PORTFOLIO_20260910.md](STAGE_PORTFOLIO_20260910.md)。`reports/v2-portfolio-verification-20260910/verification.json` 绑定源码 SHA-256 `c52be36dd9d33a888e01c77d7d35151960eacab5a09825b8390fe026b2066c74`、wheel `d40c350e1fdcb9b871b82348967cbb0d071b3301f40673dc732ff6cbc4b4074b`。Python 3.12.4 / 3.11.15 各 672 passed，无失败/错误/跳过；新安装包离源码探针通过。真实历史三组组合均因缺持仓原始日线中止，完整区间收益保持 null，不是完整回测验收。下面保留前段证据，不混同为当前源码指纹。

## 代码与输入

项目：`D:/accio/stock_data`。本地 HEAD 与本地缓存 `origin/main` 均为 `78324ba622bf6c5fc6cbb736a490e5818788d824`，remote 为 `https://github.com/ecofree/stock_data.git`。本轮未联网刷新 Git ref，因此这不是远端实时状态证明。

用户方案文件 SHA-256：`14BD6AE5830A6B9AADD73A3648B0E9140D845709F011FC35782478BBACCC684C`。复制为 `MASTER_PLAN.md`，不把文档中的执行描述自动当作额外授权。

原 51 项摘要从方案附录 B 导入，保留原证据性质。当前 Git 文件树未定位到独立的完整 51 项原始审计包，未伪造 `baseline_audit/` 或宣称已复核全部原附件。源码仍以当前工作区与 Git 基线为准。

本轮未改真实库内容、未改系统任务、未强杀持锁进程。修改在现有工作区，代码变更可能被下一次计划任务加载，不能把“未切 V2 库”理解为“旧链代码完全未变”。`.workbuddy/` 与既有 `research/phase_abc/snapshot/` 未纳入本轮编辑。

## 真实数据库保护

只读数据库句柄和流水线锁内完成源文件复制，拒绝有 WAL 的源；没有进行生产数据迁移。

备份：`D:/accio/stock_data/backups/v2-baseline-20260910/kpl_data.duckdb`，5,188,366,336 字节。

SHA-256：`194fe540f6b57395e782599f0200684b14df148922838f9ce248cd4542c8faba`。

备份重新只读打开，全表行数及指定控制数与源相同；证据为同目录 `verification.json`。额外导出 5 张人工相关表为 Parquet：

| 表 | 行数 |
|---|---:|
| portfolio_snapshot | 36 |
| trade_plan | 406 |
| watchlist | 406 |
| trade_journal | 220 |
| operator_trade_outcome | 0 |

旧持仓百分比总和为 0，只是复制一致性控制数，**不是空仓证明，更不是真实账户对账通过**。源库中真实交易结果为空不能用纸面样本替代。

## 最终本地实现验证

证据：`D:/accio/stock_data/reports/v2-verification-20260910/verification.json`。包含逐源文件 SHA-256、发行物、安装路径和测试报告摘要。范围是本地实现验证，不是独立生产验收。

- 源文件集合摘要：`a229b192a0634a156686100b09adee9750f89c20d52faae40a7e2c7bd159df11`。
- Windows Python 3.12.4 (`D:/anaconda/python.exe`)：525 passed，0 failed/errors/skipped，86.59 秒。
- 独立 Windows Python 3.11.15：525 passed，0 failed/errors/skipped，82.79 秒。
- 两次测试各 1 条原有旧 DeepSeek 键兼容性 warning，不包含密钥值。
- Ruff、13 个迁移文件检查、`git diff --check` 通过。
- core/dev 哈希依赖锁安装并 `uv pip check` 通过，独立环境 39 包（含项目 wheel）。
- QLib 传递哈希锁已成功解析；实际研究使用既有 `.venv-qlib`，**尚未从该新锁重建 QLib 环境**，不混称为可重复安装验收。

发行物：`D:/accio/stock_data/tmp/v2-final-wheel-r2/stock_data_trading_assistant-0.1.0-py3-none-any.whl`。

wheel SHA-256：`9fc60abd72927b2e67952ceb84f628095f44e744bed8c1e4900b48764e7f612c`。

实际使用 `python -I`、非源码工作目录，从 site-packages 导入；发现 13 个迁移文件、离线建立 238 张表，独立 V2 paper service 启动返回 `execution_ready=false`。Python 3.11.15 是本轮明确验证的维护版本；其他元数据允许版本不自动获得支持认证。

测试 XML：`tmp/v2-final-source-tests.xml` 与 `tmp/v2-final-py311-tests.xml`；摘要已收录验证 JSON。测试之后仅补说明文档，不用旧提交的测试数为当前脏工作树背书。

## 浏览器与合成闭环

`reports/v2-demo-20260910-r2/reports/runs/demo/`：自包含 HTML、日期 CSV、证据 JSON；合成 D 收盘到 D+1 盘后，包含等待晚到竞价、触发、题材取消、缺报价、到期、未知预占和保留原持仓。不是实时行情。

浏览器验证桌面 1440×1000、手机 390×844：手机横向溢出已修复；股票代码筛选有效，所有确认按钮禁用，外部脚本数 0，禁止网络获取数据。截图位于 demo 根目录 `review-desktop.png` / `review-mobile.png`。

## 2026-09-10 事件/纸面结算增量证据

最新阶段见 STAGE_REPLAY_20260910.md；以上历史摘要保持原样，不代表当前源码。

- 当前源码集合：`44dd3b83ac960a6e5453c0ca5bde9ceb7e4f8f043fa2c9bc89aa0657af65e2cc`。
- 当前 wheel：`tmp/v2-replay-wheel-20260910/stock_data_trading_assistant-0.1.0-py3-none-any.whl`，SHA-256 `f893ad79b0d74aaedb5512f369252786b0cacae9815515f14ffde7980de0fa11`。
- `tmp/v2-replay-source-final.xml`：Python 3.12.4，578 passed，131.68 秒。
- `tmp/v2-replay-py311-final.xml`：Python 3.11.15，578 passed，104.43 秒；两者均无失败/错误/跳过，各 1 条原有配置旧键 warning。
- `reports/v2-replay-verification-20260910/verification.json`：逐文件/测试/wheel 摘要，实际 site-packages 中建库、上下文/事件/纸面账迁移、记账重启复验通过；依赖兼容/Ruff/迁移静态检查通过。
- `reports/v2-event-replay-20260910/reports/runs/replay/evidence.json`：合成事件与三账、纸面部分成交和下一交易日退出、重启状态哈希。不是实际收益。
- `reports/v2-replay-sources-20260910/capabilities.json`：真实冻结副本完整 SHA 再次匹配；竞价最终性、资金业务时点和盘口容量缺口仍在。

本轮仅实施与本地验证，不是独立生产验收；未改现网 DB/任务/主复盘页，未提交或推送 Git，真实账户仍待提供。

## 2026-09-10 统一退出/归因增量证据

最新细节见 STAGE_EXIT_20260910.md；此前各轮测试和摘要保留为历史记录。

- 当前源码集合 SHA-256：`38efe6121badbc504680ee70505bf3214e4f600b4f479d1869078e65ba7f3376`。
- wheel：`tmp/v2-exit-wheel-r2-20260910/stock_data_trading_assistant-0.1.0-py3-none-any.whl`，SHA-256 `d894513e10349cd11dee993925ae74c6d904be9310f2938c6bd89f0ef62cbfd3`。
- 最终 Python 3.12.4 / 3.11.15 全量各 606 passed，分别 96.66 / 96.99 秒；0 failed/errors/skipped，各 1 条原有旧键 warning。
- XML：`tmp/v2-exit-source-r2.xml`、`tmp/v2-exit-py311-r2.xml`；发行物/逐文件/实际安装探针：`reports/v2-exit-verification-20260910/verification.json`。
- 安装包离开源码实际退出提案/确认/成交/归因核对与重启通过；依赖、Ruff、迁移静态检查通过。尚无远端 CI 或生产验收。
- 归因产物：`reports/v2-exit-attribution-20260910/reports/runs/replay/attribution.md` 和同目录 JSON。当前源码重生成与已保存三份文件逐字节相同。

本轮没有真实账户输入、QLib 训练、网络数据调用、生产库/调度/主复盘页修改或 Git 推送；合成损益仅作手算金标准。

## 2026-09-10 冻结实验/多折评价增量证据

本轮细节见 STAGE_ROLLING_20260910.md；前轮记录不覆盖或改写。

- 源码集合 SHA-256：`a255b0b76faab21261e0309c246143f9049eb5cba43bbd89256acbe86dd2da08`。
- wheel：`tmp/v2-rolling-wheel-20260910/stock_data_trading_assistant-0.1.0-py3-none-any.whl`，SHA-256 `bd06caeafefbcd7cc59713e05c166b6fec348b22b1d467ecc50c3f16320cd5ec`。
- 最终双环境全量各 635 passed，3.12.4 / 3.11.15 分别 84.02 / 83.14 秒；无失败/错误/跳过，各 1 条原有旧键 warning。
- XML：`tmp/v2-rolling-source-final.xml`、`tmp/v2-rolling-py311-final.xml`；完整逐文件/测试/安装证据：`reports/v2-rolling-verification-20260910/verification.json`。
- 核心安装环境离开源码执行多折夹具后端、登记/完成清单核验通过；真实 QLib 在独立 `.venv-qlib` 执行 4 折 8 次拟合，标签评分 14319 行/80 日，不与夹具混称。
- 真实实验：`reports/v2-rolling-research-20260910/historical-rolling-20260910-v1/registration.json`，ID `01932e87f8b2958a862fde6751b5771cfb9b2ac883f4d20037a0c9a86fb31956`；同目录 run/completed.json 绑定权威输出，可读结果 run/review.md。
- 两组 Rank IC 负、MSE 差于常数，资金组相对价格组差异的描述区间跨零；没有按结果调参重试，也没有更新 champion/执行权重。

未改变现网 DB/调度/主复盘页，未推送 Git；真实账户和完整组合回测仍待验收。

## 公开依赖与平台依据（前轮记录）

本轮核实 [PyPI baostock](https://pypi.org/project/baostock/0.9.3/) 可安装版本为 0.9.3，原 0.9.30 不存在；Requests 依赖提升至 2.34.2 下限并冻结解析版本。[Python Windows 文件锁文档](https://docs.python.org/3/library/msvcrt.html#msvcrt.locking) 和 [os.kill 平台文档](https://docs.python.org/3/library/os.html#os.kill) 用于锁实现取舍。实际安全性仍由本机真实双进程/崩溃测试提供证据，而非仅凭文档声明。
