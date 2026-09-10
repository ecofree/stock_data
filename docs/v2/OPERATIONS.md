# V2 运行与恢复边界

当前操作边界见本文末尾“2026-09-10 整改运行说明”和 [新整改台账](RETIREMENT_REMEDIATION_20260910.md)。以下“最新”小节是历史记录，不能作为当前核心包或现网部署说明。

## 最新：历史日线病例登记与版本绑定

[第 12 段](STAGE_DAILY_CASES_20260910.md)使用 `tools/v2/run_daily_cases.py verify --folder reports/v2-daily-cases-20260910`。新病例必须先 `register --plan <JSON> --folder <新目录>`，再 `run --folder <目录>`。这是声明初始持仓的病例诊断，不是策略组合入口。

父账/权益实现增加历史类型后，旧公告/适配证据不再用当前源码摘要验证。登记绑定的第 11 段 wheel 会离线、临时展开并运行原验证器；摘要/父包/复制输入不符即停止。保留 `tmp/v2-disclosure-wheel-20260910/stock_data_trading_assistant-0.1.0-py3-none-any.whl`，不得作为普通临时文件删除。下面各段旧源码直验命令是历史版本操作说明，不能跳过校验改成当前成功。

历史 `at` 是本次修复登记时间，`effective_at` 是假设历史步骤。假设税额、股份到账和容量均不能写回公告来源字段；所有订单/到账仅假设事件，不接 Service 或券商。

真实病例核验当前须使用匹配原研究登记数据依赖的 `D:/anaconda/python.exe`。独立 Python 3.11 核心 wheel 可运行构造病例，但它的 NumPy 版本不同且不含 QLib/LightGBM，不能复核完整真实父包；应报告 `registered data runtime changed`，禁止略过该检查。后续可显式绑定独立的原版本数据验证进程，不要求把研究依赖混入核心服务环境；本段未实现跨运行库验证进程配置。

## 最新：逐字段公告补证

[第 11 段](STAGE_DISCLOSURE_20260910.md)的 `tools/v2/run_disclosure_fields.py verify --folder reports/v2-disclosure-fields-20260910` 离线核验逐字段候选与父包。`build --plan docs/v2/disclosure_fields_20260910.json --output <新目录>` 最多读取 7 份公开 PDF、无重试；已归档 PDF 复用父包字节。再次构建会产生新接收/转录时间，不能覆盖现有包或冒充旧时点可用。

所有字段都是待复核人工转录。现金日期严格限定中登代派/指定交易 A 股，不能无条件应用于自行派发股东；股份上市日不补造股份到账日。当前只产出候选，不接受账户写入或发送父账事件。错误/来源变更不封包，保留现场另案处理，不通过修改摘要恢复成功。

## 最新：冻结历史准备适配

[第 10 段](STAGE_ADAPTER_20260910.md)的 `run_historical_adapter.py` 仅生成/核验只读准备包，不运行历史父账。入口 `verify --folder reports/v2-historical-adapter-20260910` 重新验证原组合/审计/公告及依赖版本，逐字段重做转换，检查原始输入字节和可读缺口清单。

日线不是带容量的盘口观察，公告不是到账。`daily_observations.json` 与 `action_candidates.json` 不能直接送入父账事件接口；全部 ready 仍为 false。准备包区分当前历史修复与 `2026-07-29T18:00:00+08:00` 知识截止，不把本次接收时间改成市场当日时间。新诊断须显式冻结历史假设作用域与政策，并保留旧实验。

## 最新：隔离合成父账与停牌退出

[第 9 段](STAGE_INTEGRATED_20260910.md)用 `run_integrated_account.py` 执行/核验三条金标准。该入口复用旧权益子账并原子消费成本/数量/现金变化，不替换 Service、PaperBook 或原历史实验。所有 synthetic_fill 都是夹具记录，不是实际成交或券商订单。

停牌旧价仅在显式当前全天状态与冻结期限内给出指示性估值；新鲜净资产/收益仍 null。过期、除权前价格、未知状态和退市均保留资产且估值 null。受限/未释放/未到可卖日不成交，税款未知不放行可用买入现金。先执行 `tools/v2/run_integrated_account.py verify --folder reports/v2-integrated-golden-20260910`，勿将合成输入替换为真实数据而沿用同一实验身份。

## 最新：权益子账与公告补证

[第 8 段](STAGE_ENTITLEMENT_20260910.md)给出新入口和验证边界。`run_entitlements.py` 仅执行独立合成子账，不能替代 PaperBook/真实账户；成本转移未接父账，应收不是可用现金，到账不等于释放，税额 null 不按零处理。碎股需真实登记分配，本版不取整。

`run_case_supplement.py verify --folder reports/v2-announcement-supplement-20260910` 核验 4 份公开 PDF 的归档、人工转录来源与新候选裁决。三个停牌日期已有公告支持，但 `can_forward_fill_mark=false`，不能据此恢复收益或伪造 OHLC。飞沃转增条款不是具体账户到账证明。旧原生中继限流记录保留，本段未重新调用该来源。

## 最新：原生补证与来源停用

[第 7 段](STAGE_NATIVE_20260910.md)给出原生入口、真实响应与完整验证命令。`tools/v2/run_native_gap.py verify --folder reports/v2-native-gap-continuation-20260910` 只读核验本次最终包和父包。

实际中继仍为 HTTP 429，本轮已停止，不循环重试。账号级错误停止整个来源；单标的 code=1002 不外扩至其他证券，但也不认证退市。原始响应/代码存档是证据依赖，不得清理后仍宣称可复现；旧原生采集器用显式历史源码摘要校验，当前映射须一致。

没有自动将分红/送股/日线候选写回原库的入口。送股必须等待权益、红股上市和批次结算规则；复权因子不可替代公司行为账。实际 API Key 不进文件/进程参数，不输出原始认证请求。

## 最新：缺口证据工作台（离线命令）

参见 [接口契约](GAP_EVIDENCE_CONTRACT.md) 和 [本段结果](STAGE_GAP_20260910.md)。没有 UI/原生 API 自动抓取或账户写入。可只读复核当前证据：

```powershell
D:/anaconda/python.exe tools/v2/run_gap_evidence.py verify --folder reports/v2-gap-audit-20260910
D:/anaconda/python.exe tools/v2/run_gap_evidence.py verify --folder reports/v2-gap-adjudication-20260910
```

audit 入口需父实验匹配的 `.venv-qlib` 环境，因为核验原 QLib 实验运行依赖；import-response/adjudicate/verify 不执行模型拟合。新导入证据不可倒填 available_at；候选和支持性裁决均不能恢复组合或用旧估值补空。所有输出路径要求新目录，真实与合成夹具须分开，禁止把示例当成市场事实。

## 最新：冻结 OOS 日线假设组合

实现及当前阻塞见 [STAGE_PORTFOLIO_20260910.md](STAGE_PORTFOLIO_20260910.md)。命令支持 `freeze` → `run` → `verify`；新实验目录不覆盖，失败的 run 不能原地重试。使用父实验匹配的 `.venv-qlib` 环境，不能用不同 QLib/NumPy 等依赖跳过父实验校验。

```powershell
.venv-qlib/Scripts/python.exe tools/v2/run_portfolio_diagnostic.py verify --folder reports/v2-portfolio-research-20260910/historical-portfolio-20260910-v1
```

此命令只读完成清单，不重新计算收益。`completed.json` 仅表示诊断运行和产物保存成功；必须同时读 `period_complete` 与 `common_full_period_comparison_available`，本次均为 false，不能升级成通过回测。当前阻塞需补原始行情/停牌与生命周期证据，不能删除持仓或用后续报价补齐。

需要新增受授权诊断时，以 `freeze --experiment <父实验目录> --snapshot <只读备份> --policy <明确配置> --output <全新目录>` 登记，再 `run --folder <全新目录>`。输入必须包含显式 ts_code 身份、SSE 来源日历、未复权日线与因子。新阶段代码/输入变化必须建立新指纹；本次登记绑定的实现勿原地改写后仍声称可以复现旧结果。没有账户写入、服务下单或券商路由。

## 新增：历史市场/题材研究入口

```powershell
D:\anaconda\python.exe tools/v2/run_context_research.py --source D:\accio\stock_data\backups\v2-baseline-20260910\kpl_data.duckdb --output D:\accio\stock_data\reports\new-context-research --date 2026-09-09
```

仅从冻结副本读取，不发网络请求。每次必须使用新输出目录；普通真实日可能需约一分钟，源文件摘要与大量成员关系处理受本机并发影响。20 只候选是未校准观察上限，不是正式策略参数。

新增 `context_import` / `context` 命令及 `review(context_id=...)`；context_import 触发经过摘要绑定的 V2 schema 版本 2 加法迁移，不修改版本 1。完整成员输入可能超过 JSONL 的 1 MB 单行限制，因此批量入口经同一 Service Python API 提交，不要求用户把大批原始数据粘进终端。尚未实现通用网络 spool 协议。

上下文始终按显式 ID 读取，不自动把最新历史报告当作当前实盘上下文；raw 输入与派生结果篡改都会报错。许可、PIT/日历认证、真实竞价/资金/账户未完成，仍禁止真实执行。

## 只在独立目录启动

V2 是独立 paper-only 引擎，不是现有 `run_integrated_daily.py` 的替代生产入口。禁止把 `--db` 指向 `kpl_data.duckdb`；代码会拒绝不含 V2 schema 的既有库。

```powershell
# 从项目根目录，状态查询不接网络、不下单
'{"command":"status"}' | D:\anaconda\python.exe -m trade_system.v2 --db D:\accio\stock_data\tmp\my-v2-paper\live.duckdb

# 每次用新目录，保留之前的证据
D:\anaconda\python.exe tools/v2/run_paper_demo.py --output D:\accio\stock_data\reports\my-v2-paper-demo
```

JSONL 入口单行上限 1 MB、命令白名单、有界队列；每个进程内部只有一个持有连接的 actor。账户/确认优先于新发现；网络 worker、模型训练不在此 actor 中运行。不存在 HTTP 监听、远程 API 或生产操作身份体系。

调用超时可能发生在命令已经开始之后，**不等于未提交**。确认使用固定 `request_id`，通过 `action` 查询持久化结果或用相同请求重试；不得换请求 ID 盲目重做。进程退出前未被接受到持久存储的普通输入仍需调用者保留；没有声称 durable outbox/exactly-once 已完成。

## 账户输入

`account_import` 的 `raw` 是结构化 JSON 字符串，不是通用券商 CSV 自动识别。必需：account_id、mode(paper/live)、asof、valid_until、cash、frozen_cash、equity、positions、open_orders、declared_complete=true、source(manual_declaration/broker_export)。

持仓包含 instrument（如 SH.600000）、quantity、sellable、mark_price；挂单包含 order_id、instrument、remaining_quantity、frozen_cash。金额显式元、最多两位小数；未知值不可编造为零。持仓估值加现金与权益及挂单冻结控制数必须相符，否则保留未对账状态。真实导入仍未支持完整分批结算；独立 paper_ledger 已支持明确初始批次与事件重建，不能互相冒充。

即使导入了 `mode=live`，V2 仍然不签发真实执行资格；真实账户可用性与上线批准均待办。相同 ID 不可从 paper 改 live。系统外动作/费用/公司行为/更正事件追加后，旧快照失效，需新快照对账；未知预占不按 TTL 自动释放。

纸面示例和策略阈值都是 fixture，不是投资参数，不得直接拿去配置实盘。

## 锁、迁移和备份

- 新锁使用永久 `.guard` 文件和操作系统句柄。guard 文件存在不表示被占用，**永不删除 guard 文件**，也不使用 PID 探测或按文件时间抢锁。
- 遇到旧 `.pipeline.lock` 元数据没有 `os_handle_v2` 协议，保守阻塞，需要协调所有 writer 正常退出后的维护处理。不能自动把“旧”“未知”“权限不足”判为可删除。
- 不在 writer 持锁期间做递归清理、删锁或复制活动 WAL。备份工具要求新目标目录、足够空间、源无 WAL，并以只读句柄阻止直接写入。
- 新迁移摘要绑定实际执行字节；内容被改则报错。旧已执行但无摘要的迁移只告警保持未验证，不能补填当前文件摘要伪造历史证明。
- V2 初始 schema 摘要不匹配会拒绝打开；尚无完整升级器。开发 fixture 使用新数据库目录，禁止删除旧事实以解决版本不匹配。
- 出错向前恢复、保留失败目录和人工事实；当前没有授权生产恢复/回退动作。

## 产物兼容变化

QLib 导出从原来的单路径覆盖变成 `<stem>.versions/<run>/...` 加 `<stem>.current.json`。逻辑 CSV/Parquet 文件可能不再直接存在；调用 `trade_system.ml.feature_artifacts.resolve_feature_path` 获取经过 manifest/内容摘要校验的实际路径。项目训练/预测调用方已更新，外部手写读取方需要显式迁移。

模型预测在反序列化前核对模型 ID 和 SHA-256，并使用该模型训练时的特征顺序与中位数。旧模型缺少这些元数据会拒绝，不回退到零填充或无校验 pickle；需要在隔离目录重训并走影子评价，不能自动升级旧 champion。

V2 报告产物显式登记并放在不可变 run 目录，全部完成才在锁内切换 `current.json`。发布失败保留上一套完整产物，过期 generation 不可覆盖新指针。HTML 所需数据已经内联；发布指针是磁盘层机制，不是浏览器的 sidecar/fetch 依赖。

当前五区页只读，禁用确认按钮。不能让缓存 HTML 的历史证书直接确认；未来操作界面必须经服务重验并增加认证/Origin/CSRF。

## 安装与复验

Windows 3.11.15 为本轮验证基线；生产/开发用 `requirements.lock` / `requirements-dev.lock` 哈希安装。QLib 独立 `requirements-qlib.lock` 刚完成解析，尚未以新锁重建环境，不能声称该锁已通过安装验证。

```powershell
python -m pip install --require-hashes -r requirements-dev.lock
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
python -m pip check
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
python -m pytest -o addopts='' -q --junitxml=tmp/pytest-current.xml
```

当前轮次真实执行证据见 BASELINE.md。不要复制旧通过数，不要把本地验证当 GitHub CI 通过或独立生产验收。

## 事件驱动纸面账（本轮新增）

```powershell
D:\anaconda\python.exe tools/v2/run_event_replay.py --output D:\accio\stock_data\reports\my-new-event-replay
D:\anaconda\python.exe tools/v2/inspect_replay_sources.py --source D:\accio\stock_data\backups\v2-baseline-20260910\kpl_data.duckdb --output D:\accio\stock_data\reports\my-new-source-capabilities
```

输出必须使用不存在的新目录。第一个命令全部合成，包含独立账户配置、事件政策、策略、风控、三笔纸面成交和重启复验；第二个只核查冻结副本的字段与时间覆盖，不抓取网络、不生成真实委托。

服务顺序：登记 event 产品 → `paper_open` 新 paper 身份 → `market_event` → `event_signal` → `propose/confirm` → `paper_buy` → `paper_event` → `paper_status`。配置示例见入口中的 paper_config/policies，明确日历、费率、生效版本、整手、T+N、金额单位与 TTL；fixture 不可当正式参数。最终性由调用方提供源证据，本地标志不是授权认证。

`paper_event` 只接受 event_id/kind/payload，接收时间由服务赋值，禁止调用方倒填。相同 ID 重试须保持完整请求一致。买入和卖出都必须消费有效确认，不能用通用 submit 绕过。旧的直接卖出调用方须迁移至 propose_exit → confirm → paper_sell；过去已落账的事件照常重建，不补写历史确认。账本头/序号/输入/后态摘要不一致时停止恢复，保留事实并调查，禁止通过删事件“修复”。

`unknown` 与撤单待确认占用不自动释放。服务命令执行期间超时需查询或按原 ID 重试，不是任意重发许可。原始归档可能在失败事务前写入，保留孤立原始记录用于调查，不删生产锁。

详见 STAGE_REPLAY_20260910.md 的公司行为、盘口代理与真实源边界。未迁移旧主复盘页，也未上线新的纸面成交界面。

## 统一退出与归因（后续增量）

propose_exit 需要与入场相同的显式 risk_policy，以及 account_id/code/quote_manifest/quote_dataset/exit_policy。exit_policy 包含 version、reason(manual_reduce/price_below)、reference_id、expires_at；price_below 另需正整数 trigger_price_fen。同版本不可改变；不是生产推荐参数。confirm 接口保持不变，paper_sell 使用 account_id/confirmation_request_id/order_id。

退出按当前可卖股数扣除活动卖单和 held/unknown 数量预占。减仓不受入场仓位比例门槛阻挡，但仍受账户/报价/日历/规则/可卖量/单笔上限限制。只发出撤单意图或证书过期不会释放预占；mark_exit_unknown 保守保留，尚无完整人工解决界面。

attribution 命令只需 account_id，读取并重演已有纸面账，不增加 ledger 事件；保留 asof、generated_at、current_valuation_complete 的区别。不可把陈旧估值解释为当前绩效。独立回放命令新增 attribution.json / attribution.md，详见 STAGE_EXIT_20260910.md。尚未生成新的 HTML 退出操作页。

## 多折研究：先登记、后执行

使用已有隔离 QLib 环境。以下已运行过的 ID 不能再次执行；重现需新登记 ID 与新目录，不能覆盖历史证据。

```powershell
.venv-qlib\Scripts\python.exe tools/v2/run_rolling_research.py register --features tmp/v2-real-research/qlib_features.parquet --plan docs/v2/rolling_experiment_20260910.json --registry reports/v2-rolling-research-20260910
.venv-qlib\Scripts\python.exe tools/v2/run_rolling_research.py run --experiment reports/v2-rolling-research-20260910/historical-rolling-20260910-v1
```

register 解析并验证不可变导出，不允许无元数据/摘要的临时特征文件。run 核对登记中的代码/数据/依赖，先写 started，然后写逐折产物；错误写 failed，不能把部分折说成完成。completed 清单绑定模型文本、预测、预处理、指标和 review.md；read_result 在登记匹配的环境中重验所有权威产物。修改研究实现或依赖后需新登记；旧文件保持历史记录，不能冒充新版本证据。

预算限制抽样输出、折数、轮数和模型线程，不是全流程硬超时/内存隔离。不要向单 writer actor 投递训练任务。QLib recorder 使用实验目录内的本地 file URI；不会把多折结果导入真实信号、旧模型库或生产数据库。

excluded_tail 不评分，但当前快照曾经检查过，不能认证 untouched；配置中的评价上限不等于实际接收时间。成本是标签均值敏感性，portfolio_return=null，不能用它替代纸面结算。详见 STAGE_ROLLING_20260910.md 的实际负结果和剩余范围。
# 2026-09-10 整改运行说明（优先于下方历史命令）

整改在 `D:/accio/stock_data-retirement` 隔离工作树中实施，原 `D:/accio/stock_data` 和已注册任务未切换。核心包与研究环境分离。核心入口只提供观察、纸面证书、显式人工纸面确认和复盘，不提供真实委托。

已确认但从未送达的预占，可经 `stock-data-paper-desk --db <已有纸面库> close-unsent --confirmation-request-id <原确认ID> --operator <声明身份> --request-id <固定取消意向ID> --reason cancelled` 显式关闭。到期未送达使用 `expired_not_sent`；`unknown` 或已送达禁止走此路径，不能因过期自动解冻。

`stock-data-daily case` 只能链接已验证原生观察包；没有独立事件证据、规则或纸面账户时保持仅观察，不能把涨停池价格冒充当前可成交报价。原生证据被保留不等于当前仍新鲜。

锁恢复：正常关闭写者；不删除永久 `.guard` 文件，不凭 `.pipeline.lock` 元数据抢锁。旧任务注册、旧下单、旧人工计划自动写入已在整改版本撤销。实际任务修改、数据退役和生产切换另行审批。

完整进度与限制：[整改台账](RETIREMENT_REMEDIATION_20260910.md)。以下为历史阶段说明，研究命令仅用于保留的隔离源码环境，不代表核心 wheel 内可用。
