# 行情、研究和人工观察：离线恢复验收

当前阶段不纳入真实账户，C3 真实账户对账为 `not_applicable_current_scope`，不是已通过真实对账。
新演练工作区在 `workspace-config.json` 中明确写入 `acceptance_scope: market_research_observation_v1`。
该范围禁止同时配置 `account_database` 或 `account_id`；旧工作区没有此声明时保持原有账户检查。
不迁移或删除历史账本，不恢复旧入口；行情、研究有效性、人工判断及执行资格仍分别判断。
切换范围后，旧计划的账户绑定与冲突状态仍保留在本地原始记录中；静态导出始终移除计划的
`account_id`、`account_snapshot_id`，不因“不适用”跳过脱敏。这不代表历史数据库整体已脱敏。

## 恢复材料与边界

恢复对象是显式批准的历史数据库副本、当前代码、环境重建材料，以及由历史事实生成的演练工作区。
演练人工判断必须署名为演练记录，不代表用户观点或实际交易。未指定的真实人工工作区、供应商权限及外部模型不能视为已恢复。
代码、配置和原始记录保留原字节；恢复后的路径绑定另外留证，只读核验不启动采集、训练、服务、计划任务或执行。
历史快照本身不证明今天数据新鲜，也不证明其旧来源/复权缺口已解决。

`tools/v2/offline_package.py` 使用标准库验证独立记录的清单 SHA、完整文件集合及每个文件身份。
离机包包含独立基础 Python、原始基础环境压缩包、离线 wheel、锁文件、恢复包和容量归档目录。
第 2 版包由 `tools/v2/offline_contract.py` 统一检查启动器、恢复入口及其本地导入模块、三份锁文件，
逐个匹配锁定版本/哈希、wheel 内部身份和 Windows x64 / CPython 3.12 标签；基础 Python 的完整
文件集合必须与固定压缩包的标准库子集一致。缺少或不匹配的材料不能封签，也不能进入恢复目标。
这是材料准入，不替代执行验证；恢复时还须从零安装、依赖检查、恢复入口导入检查和实际业务验证。
旧版包及其回执保留原样，新版验收不能沿用旧版指纹。
恢复必须指定全新输出目录，不覆盖现有环境；以离线、校验哈希的方式重新安装依赖，不复制当前 site-packages。
恢复过程依次进行环境重建、恢复包还原、数据库/引用校验、容量复核及自包含页面导出。
失败现场保留，不生成完成回执；重试使用新的输出目录。清单的外部指纹需要单独保管，自哈希不证明来源真实性。
每个子进程阶段在正常退出、非零退出、启动失败和超时时均保留有界、脱敏的 `.log` 与 `.json`
诊断，包括阶段、起止时间、超时预算和状态。超时日志保留已有输出，不伪造完成回执。
当前包面向 Windows x64 的离线读取与核验；它不包含活动模型训练许可，也不以核心环境代替独立研究环境。
历史演练页明确标识“离机只读验收”，以随包说明为操作依据，不引导启动在线工作台。

## 分批容量验收

`trade_system/v2/capacity_acceptance.py` 用不可变恢复快照内的行范围分批保存历史原始表。
每批最多 100000 行，最多 200 批；单线程、512 MiB 内存、1 GiB 临时磁盘上限。超预算拒绝，不自动提升资源限制。
每批重新比较列名/类型、行数及 `EXCEPT ALL` 双向差异，总体范围与独立恢复基线一致后才完成。
缺批、重复批、范围变化、文件变更、坏分片、重复执行和中断分别校验。坏文件不覆盖，原库不删除。
该能力保留未知产品键和重复原始行；它与 `cold_archive` 的合格产品键联合读取用途不同，不作为其兜底。
数据清洗与来源/复权裁定未完成时，历史材料仍可以原样保存，但不能标成可用于合格业务分析。
全库清单按已有产品合同映射；没有重建证明和源退役授权的表全部保留原库。
本次选择表的容量与耗时不外推到全部数据产品、最新生产规模或持续增长率。

## 实际验收与未完成事项

本机跨盘证明必须记录真实物理磁盘号，不能以盘符不同代替；D/E 同一物理盘不能算独立介质。
跨盘复制清单一致后，需要从目标盘的包和新环境独立完成恢复，并重新打开实际页面。
冻结包的恢复目标是文件和业务值零缺失；不将这一结果填写为持续运营 RPO=0。
完整耗时包括材料核验、环境准备、恢复和业务复核；未约定目标前只报告测量值，不写 RTO 达标。

仍需离机实测：在另一台机器或实际灾难替代环境取回包，核对外部指纹，从零重建并完成相同校验；
确认目标文件系统的不可覆盖发布/硬链接与中断恢复行为，检查依赖、中文页面和路径绑定。
独立环境结果、离机介质、业务恢复范围、数据损失时长与完整耗时分别留证。
持续备份、生产切换、任务变更和恢复运营需要新的明确授权，本轮离机包不是这些动作的入口。

## 第3版候选包：显式数据库角色与家庭电脑回执

第3版使用 `stockdata_observation_offline_package_v3`，旧第2版封签及材料闭包保持原有解释；
不能修改旧包或沿用旧源码/恢复回执来认证新版。新包必须在源码、迁移和观察材料全部冻结后构建。
`tools/v2/build_offline_candidate.py --plan <计划JSON> --plan-sha256 <独立SHA>` 只接受明确输入，
没有生产默认路径。源码输入为干净工作树的明确tracked文件字节清单，不使用会规范化换行的git archive，
不扫描或打包任意未跟踪文件。已有输出不覆盖，失败现场保留。

计划schema为 `offline_candidate_build_plan_v1`，必填项如下；path均由调用者明确指定，
`{path,sha256}`表示独立指纹绑定的文件：

- `source_root`；`source_manifest:{path,sha256}`。清单schema为`offline_source_export_v1`，
  `files`是相对源码成员到SHA的映射，必须包含恢复本地导入闭包、全部trade_system Python、迁移CLI、
  编号SQL和三份锁；不得夹带数据库、环境密钥、wheel或zip。
- `previous_package:{path,manifest_sha256}`；`raw_database_member`为旧包内完整相对成员名。
- `migration_receipt:{path,sha256}`；`migrated_database:{path,sha256}`。
- `observation:{workspace,pages,receipt:{path,sha256}}`，对应冻结且明确标识SYNTHETIC的观察演示材料。
- `targets:{path,sha256}`为直接用户批准的`stockdata_recovery_targets_v1`合同；
  `projection:{day,as_of}`指定历史日期和带时区的查看时钟；`output`必须为新的独立目录。

新bundle的`raw_facts/offline-bindings.json`以bundle内相对路径和SHA明确绑定原始归档库与迁移行情库，
并绑定迁移回执、其内嵌qualification的canonical JSON SHA、合成观察回执与工作区文件清单。
容量catalog始终比较原始库；迁移行情角色不能替换原始归档基线。恢复时重新使用迁移owner的
原表双向EXCEPT ALL、索引/结构和qualification核验，校验全量源码/SQL身份。
该复核单线程、1GB内存，独立scratch的spill预算为`min(16GiB,max(256MiB,4×原始库字节))`。
迁移身份不授予来源、复权或当前新鲜度资格。

派生`evidence/rebound-readonly-configuration.json`独立保存，只读历史行情消费者实际读取此绑定，
经现有market projection和renderer导出`historical-market.json/html`。
`workbench.html`另标SYNTHETIC，仅演示D0→修订→计划→D1绑定复盘→D2闭环；
不能用这张合成页声称真实数据库到页面已通过。原人工配置、日志和发布物保存在
`rehearsal/restores/offline/raw_facts/original-workspace`，不改写原件。

环境安装显式使用pip isolated、no-index、no-cache-dir、hash locks及禁用系统/用户配置的空配置文件。
平台仅声明Windows 10/11 x64候选支持，尚不等于跨版本认证；恢复前核对实际OS/架构、NTFS本地固定卷、
非覆盖目标及空间，并在新目标内留存硬链接/不可覆盖能力探针。目标盘额外空间预算为
`包文件字节×2.5 + 5GiB + migration verification spill预算`；同盘另需放下包和压缩件。
中断恢复行为仍需实际演练，不因探针通过自动通过。

构建输出`portable-package`、`portable-candidate.zip`、`package-pin.json`与有实际大小的
`HOME-DELIVERY.txt`。包内`HOME-STEPS.txt`是一页中文操作说明；含私有历史库与人工原件，严禁公开传播。
家庭电脑无需现有Python，可断网运行：

```powershell
$intake = [DateTimeOffset]::UtcNow.ToString('o') # 在取回/复制开始前记录
.\RUN-HOME.ps1 -ManifestSha256 <独立SHA> -NewDestination C:\StockData-Recovered-01 -IntakeStartedAt $intake
.\COLLECT-ACCEPTANCE.ps1 -ManifestSha256 <独立SHA> -Destination C:\StockData-Recovered-01 -NewOutput C:\StockData-Receipt-01
```

RUN-HOME在目标旁独立新建`.home-attempt`目录，材料/平台/启动失败也保留外层失败回执；
失败不能形成completed，重试必须使用新目标。收集工具只复制固定阶段的有界、再次脱敏日志与批准字段，
不递归复制数据库、原始人工记录或凭据。收集的`human-checklist.json`由实际操作者逐项确认断网页面、
中文、历史日期、合成区别、研究材料可用及只读状态，填写UTC确认时间和代号；
再次用新收集目录及`-HumanConfirmation <该JSON>`提交。未确认时保持pending。

已批准目标为最多1个交易日RPO，`unit=trading_session, maximum=1, seconds_equivalent=null`；
RTO为14400秒，从取回/复制开始，至技术核验通过且操作者确认页面及研究材料可用。
完整时长与技术时长分别记录。缺少取回开始时间或人工确认时不能判定RTO通过。
冻结历史包仍单独要求零文件缺失、零业务多重集差异；暂停旧快照不能证明持续运营RPO达标。
机器指纹仅提供核对线索，机器代号不同不能自动判离机通过；回执收集始终`offsite_accepted=false`，
仍需真实家庭机器/故障域、介质取回与人工签署的独立审核。

本候选范围是历史行情、原始人工记录、冻结研究材料与只读页面；不包含实时采集、退役训练/预测、
完整独立QLib研究环境或真实账户，不给予恢复运营授权。
