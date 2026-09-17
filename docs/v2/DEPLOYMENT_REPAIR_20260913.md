# 0.3.10 安装失败后的有界修复

## 当前状态（2026-09-16）：批处理登录权已补齐，仍未部署成功

管理员诊断已确认：原探针没有启动，Security 4625 为批处理登录类型 4、状态 `0xc000015b`；专用账户没有 `SeBatchLogonRight`，相关拒绝策略为空。不是 Python 或 QLib 执行后报错。

用户单独批准最小权限修复，并执行了 `repair_research_batch_logon.ps1 -Mode Apply`。实际成功回执为 `reports/repair-20260916/batch-repair-5c956ac1649142e69d78669187cae5de.complete.json`：原有四个授权 SID 保持不变，仅增加既有 `StockDataResearch` 账户的批处理登录权；拒绝策略未变，账户和任务均仍停用。本步骤没有验证专用身份实际启动成功。

**不要重跑本页历史的 retry / Apply / ArchiveFailure 命令。** 9 月 14 日的一次性许可已经过期；第二次失败已创建账户和任务，旧 `InspectRecovery` / `ArchiveFailure` 只接受“未创建账户”的首次失败现场，不适用于当前状态。失败目录、许可和日志必须保留，不手工删除、改名或覆盖。

第一轮补充了诊断保存、回滚前失败证据捕获和探针启动前批处理登录权检查，没有赋予安装器自动补权或自动续期能力。当前 0.3.12 已在隔离目录通过代码检查和包切换/回滚演练，不得替换进旧 0.3.10 的授权许可。

第二轮已实现针对“已有账户/任务且已回滚”的**离线恢复探针**，详见下一节。它不安装日常触发器、不接管原任务。实际日更部署仍需另行完成并确认新版维护窗口及发布范围，不能把离线恢复成功说成现网切换完成。完整当前进度见 [9 月 16 日修复记录](REPAIR_PROGRESS_20260916.md)。

### 新入口：先只读核对，不启动恢复

在管理员 Windows PowerShell 执行：

```powershell
& 'D:\accio\stock_data-retirement\scripts\recover_research_probe.ps1' -Mode Check
```

看到 `RECOVERY_CHECK_SAVED` 后，工具直接读取 `reports/repair-20260916/retained-check-*.json`，用户不需要回贴大量内容。该命令不启用账户/任务、不创建维护许可；只读核对会生成诊断文件。输出文件中 `passed=true` 也不表示部署成功。

9 月 16 日 13:05 的实际回执 `reports/repair-20260916/retained-check-bca3621cc4a54ad4889dcd8254c1f5e2.json` 为 `passed=false`、`system_changes=0`，不是部署或核对通过。原因已定位为核对器误判：两份本次 `secedit` 原始导出均使用 `SeBatchLogonRight = StockDataResearch,*S-1-5-32-544,*S-1-5-32-551,*S-1-5-32-559,*S-1-5-32-568`，而旧代码只比较 SID。两份完整导出 SHA-256 均为 `0589d9bfc474f98d22de1bef34b5853264de11aecdec22161e1f97fc0b3dee21`，没有批处理拒绝项。此前单项补权仍有效，不应重复 Apply 补权。

已修正为通过 Windows 原生账户解析取得规范 SID，同时处理带星号 SID、不带星号 SID 和本地化/限定账户名称；无法解析、损坏或空身份项仍阻止启动，允许和拒绝项使用同一规则。在 Windows PowerShell 5.1 中只读重放上述两份导出均通过；实际名称解析为原账户 SID `S-1-5-21-3027070730-734606845-1610825463-1006`，模拟增加同名拒绝项会被阻止。这里只验证权限解析，不替代完整管理员 Check，更未启用账户或运行探针。

修正版管理员 Check 已于 9 月 16 日 13:20 通过，回执 `reports/repair-20260916/retained-check-3b6d2d6adf06418eb8c1b03f6fe70155.json`：`passed=true`、`system_changes=0`，账户/任务仍停用，尚无本次恢复目录；固定包哈希与下表一致。用户随后单独批准下面这一次离线验证。批准不是已经执行，尚未生成新的维护许可。

只接受已知第二次失败的安装器哈希、原 SID、已回滚状态、停用且无触发器的原任务，以及未改变的三份备份/原任务配置。逐项核验旧权限恢复，保留批处理登录权的单项修复。继承权限按父目录恢复后的实际权限核对，不把部署期间暂时继承的权限重新授回。任何未知状态先停下，不自动重建、重设密码或提升身份。

### 已获单项批准、尚待安静时段执行的离线探针范围

本次 `Apply` 的确切离线范围已获批准，仍须管理员现场输入大写 `START`。当前没有创建真实许可或执行探针。13:23 左右只读核对发现 `StockData-Intraday` 仍为 Running，其既有参数为 `-EndAt 15:05`，收盘任务下次运行时间为当天 17:30。不得为探针暂停、强杀或修改原任务。9 月 16 日 15:10–16:20 仅是候选空闲时段，不是已开启或固定的新许可；必须等原任务实际自然结束，并通过执行时的未来一小时调度及 guard 复查，否则停止并保留诊断。

在上述条件满足后，管理员执行唯一的新恢复入口：

```powershell
& 'D:\accio\stock_data-retirement\scripts\recover_research_probe.ps1' -Mode Apply
```

看到范围确认提示后输入 `START`，才开始一次不可续期的 60 分钟窗口。出现 `OFFLINE_PROBE_COMPLETE` 后核查受保护回执；报错或超时不得重复 Apply，不运行历史 retry 入口，不删除任何失败目录。

- 使用固定 0.3.12 源码包和现有账户/任务，凭据不重置；只修改任务动作，不传入新 User/Password。微软文档支持单独修改任务动作，但本机保留凭据后的实际启动能力仍须探针实证，不能由接口文档或合成测试推定。[Set-ScheduledTask](https://learn.microsoft.com/en-us/powershell/module/scheduledtasks/set-scheduledtask?view=windowsserver2025-ps)
- 新证据目录固定为 `D:\accio\stock-data-runtime\recovery-probe-0312-once`。目录已存在就拒绝再次 Apply，不删除、不覆盖、不自动续期，也不复用旧许可。
- 新窗口从 START 起恰好 3,600 秒，绑定恢复脚本、共用脚本、原回滚 journal 和固定包哈希。现场重查原任务状态、未来一小时调度和四把 guard；不强杀、不删除锁。
- 临时授予读取数据/模型/发行包及写入探针结果的权限；保护主数据库、模型/配置、人工记录和判断锁。权限原状态先落盘，再修改；中断产生的 journal.new 保留，不覆盖证据。
- 临时启用专用账户和无触发器的原任务，最多等待 4 分钟；探针仅做只读查库、保护权限验证和隔离渲染，取数/训练/交易均为 0。未通过前绝不安装日常触发器。
- 无论探针成功或失败，都停用账户/任务并恢复临时权限及原任务动作；原有批处理登录权保留。发现独立权限变更时，先阻止已知身份再次启动，再拒绝覆盖无关变更。进程仍运行时不强杀，保留现场，退出后再使用 Rollback。
- `OFFLINE_PROBE_COMPLETE` 仅表示探针通过且已恢复停用，不表示已经部署日更。失败时不得重跑 Apply；`Rollback` 可在窗口到期后使用，但不会启动任务或新建许可。回滚异常以 `rollback_needs_inspection` 留证，不能视为成功。

固定文件：

| 文件 | SHA-256 |
|---|---|
| recover_research_probe.ps1 | `54c3ab55070733100786fc6903f9d6746ef6d43c7963a7fd56643ff7ab408c5b` |
| deploy_research_cutover.ps1（共用函数） | `a140248b353f22a92d934d27a22efb675ee0cf8b48a8a8f3383634fd69889027` |
| stock-data-workspace-0.3.12.zip | `294fbc9d7488be9bce86561e0df861bd9ef911a3389ec37bd9e7e29f404fbf67` |

修正权限名称解析后，50 项部署/恢复专项测试通过，新增 6 项原生名称解析、错误身份及拒绝策略回归。此前 44 项的回执 `reports/recovery-probe-20260916/final-tests.xml` 仅作为历史。测试覆盖真实 PowerShell 控制流和临时文件，但账户、任务、系统权限变更均使用替身，**不构成本机实际探针验收**。

本次修正后的完整代码验证为 **1,650 项通过、0 失败/错误/跳过**（14 条警告）；lint、迁移、写者边界、已安装核心依赖及运行检查均通过。回执 `reports/recovery-policy-20260916/verification/verification.json`，源码 SHA-256 `8925ac38688640b72d83e122767296cc76298669954e23397ba853a492f3be87`，测试期间源码未变化。`execution_ready=false`、`production_cutover=false` 保持不变。

权限名称修正前的完整验证为 **1,644 项通过、0 失败/错误/跳过**，含 21 项新增恢复测试；lint、迁移、写者边界和已安装核心检查均通过。历史回执 `reports/recovery-probe-20260916/verification-final/verification.json`，对应源码 SHA-256 `4c8b2acc1e6762d9ea50c8d782aca82f8f54bc69452800174390802a5ecde65c`，不替代本次修改后验证。真实固定包已在 `reports/recovery-probe-20260916/package-check/release` 解压验证 58 个文件及清单哈希，但没有使用专用身份运行。

以下均为历史记录，旧命令和时间不构成当前执行授权。

## 历史：管理员现场确认的一次性 60 分钟窗口

用户已批准改为实际启动并确认后计时。之前所有固定时段仅作历史记录，不再用于当前安装器。

管理员执行同一个入口：

```powershell
& 'D:\accio\stock_data-retirement\scripts\retry_research_cutover.ps1'
```

入口先进入 `StartMaintenance`：核验管理员身份、主机时区、原失败状态及未来一小时的 StockData 任务；显示授权范围，等待用户输入大写 `START`。其他输入取消，尚未生成许可。等待输入期间不占用写者锁；确认后重新检查任务及回滚状态，再取得三把现有句柄锁并创建一次性许可。

- 时间从现场确认时刻计算，恰好 3,600 秒；每个后续步骤读取同一个截止时间，不能重新起算。
- 许可存放于独立的 `D:\accio\stock-data-maintenance-20260914-once\permit.json`，目录限管理员和 SYSTEM 访问，与失败目录归档分离。
- 许可使用新建文件方式写入，绑定当前安装器 SHA-256、原 0.3.10 包、明确的只读部署范围及确认者 SID。目录已存在就拒绝再次开启；中断、损坏、过期或版本变化均不自动更新许可。
- 已运行任务、未来一小时内到期的任务、未知或过期的下次运行时间都拒绝开始。停用任务保持停用，不修改任何原调度。
- 成功取得许可后依次执行 `ArchiveFailure`、`Check`、`Apply`；任一步失败立即停止。四个阶段均继承用户现有身份，不提升权限、不绕过执行策略。
- 不手工删除许可目录、不重复执行已开始的序列；失败后保留证据，根据实际进度决定恢复方式。尚未创建许可的取消或任务冲突，不视为部署已开始。

当前安装器 SHA-256：`7114316bde2548f78054570269fee4e0a31a34480b78bd36b7ec207ee16e4de4`；顺序入口 SHA-256：`86f74918f725cd668f531bf9abbed9536f98fe6e47be498e8199b7319fb59652`；原 ZIP 仍为 `e8bed4e550c81e44d85863cb3cd6b43bc48dfe633e699c5b6d20ac0e7f3acdb3`。

18 项 Windows 专项测试通过，包括实际子进程失败截断、取消无许可、重复启动不续期、3,600 秒边界、调度冲突与未知状态、私有权限及版本绑定、恢复保护和隔离目录归档。回执：`reports/deployment-interactive-20260914/tests.xml`。部分系统状态使用模拟对象，真实目录归档测试仅使用隔离合成目录，不能冒充管理员安装验收。

2026-09-14 05:49 只读检查：许可目录尚不存在，三个原任务 Ready，月度压缩 Disabled；原任务下次运行分别为竞价 09:15、盘中 09:30、收盘 17:30。该信息随时间变化，启动器仍现场重查。本轮没有创建真实许可、归档现场、部署账户或修改任务。原首次日更计划仍为 9 月 14 日 18:30，实际成功产物须另行验收。

以下为已废止固定窗口的历史记录。

## 历史：聊天确认后的 1 小时窗口与单入口执行

19:00–20:00 窗口已经过期。用户再次批准“下一次确认后的 1 小时”；本次处理确认的 UTC 时刻为 2026-09-13 15:29:16，因此固定的新窗口为北京时间 **2026-09-13 23:29:16（含）至 2026-09-14 00:29:16（不含）**。不是每次运行自动重新计时，窗口外仍拒绝归档和安装。

用户提供的管理员截图已显示两次 `RECOVERY_CHECK_PASS`、`Changes=0`。归档时仍重新核对，不能复用旧检查结果绕过当前状态检查。

在此窗口内，管理员只执行以下一个入口：

```powershell
& 'D:\accio\stock_data-retirement\scripts\retry_research_cutover.ps1'
```

入口依次在继承当前身份的 Windows PowerShell 子进程运行 `ArchiveFailure`、`Check`、`Apply`，任何非零退出立即停止。不会提升权限、绕过执行策略或忽略失败。若出现报错，回传末尾输出，不重复运行入口、不删除任何失败目录。该入口不提供已归档后的自动续装；部分步骤成功后的恢复须根据现场证据另行处理。

当前安装器 SHA-256：`71f9810b0559917a1067af1ab10cb658617a8bae100102d198b9769aef1252a0`；顺序入口 SHA-256：`20c95c9d4138ca84bf3cddec241822d1cccf9f5f30af1b0ed26fd671f47c0dc9`。固定 0.3.10 ZIP 没有改变，首个正式调度仍计划于 2026-09-14 18:30。

验证包括新窗口跨午夜边界、恢复保护，以及隔离合成安装器的三种中途失败和全步骤成功。专项测试不等于实际部署成功；以管理员输出 `INSTALL_COMPLETE` 和实际任务/权限核对为安装证据，未来日更产物仍单独验收。

以下为之前窗口的历史执行记录；旧时间和旧哈希不再作为当前授权。

## 历史：用户批准 19:00–20:00 重试窗口

用户已批准北京时间 2026-09-13 19:00–20:00 重新部署，先核验回滚与权限，再保留失败目录并归档，最后仍部署固定 0.3.10。原任务时间不变，月度压缩保持 Disabled，不开放交易执行。

截图返回的回滚状态与三个现网文件、未创建账户/任务的现场核对一致。受保护回执的完整结构和权限仍需在管理员环境实际验证，不能用截图替代该步骤。

安装器新增两个有界模式：

1. `InspectRecovery`：管理员只读检查。仅接受原安装器哈希、明确已回滚、账户未创建、空 SID、空权限修改记录的本次失败；核对三个备份与现网原哈希、四个原任务配置、受保护目录权限，并获取有界目录指纹。持有三把现有句柄锁期间检查，不删除锁、不抢占写者。
2. `ArchiveFailure`：只能在新批准窗口中执行，重新做同样核验，再将确切目录 `D:\accio\stock-data-runtime` 同父目录改名为 `D:\accio\stock-data-runtime.failed-20260913-164649`。目标必须不存在；拒绝链接、异常规模和任何未知状态。归档后逐文件内容哈希与完整 SDDL 比较，保留所有内容和权限，不删除或覆盖。

当前安装器 SHA-256：`273f6c6087c68c8115c5cb225c1bd95658979a6ef880d01022fca33751d8683e`。固定 ZIP 哈希仍为下述原值。10 项专项测试通过；测试使用隔离合成目录和模拟回滚记录，不代表实际管理员恢复验收通过。

### 管理员执行顺序

现在只执行检查，并核对 `RECOVERY_CHECK_PASS`：

```powershell
& 'D:\accio\stock_data-retirement\scripts\deploy_research_cutover.ps1' -Mode InspectRecovery
```

19:00–20:00 内先执行归档；必须看到 `ARCHIVE_COMPLETE` 后再执行后面的检查和安装。任何报错停止，不盲目重跑：

```powershell
& 'D:\accio\stock_data-retirement\scripts\deploy_research_cutover.ps1' -Mode ArchiveFailure
```

```powershell
& 'D:\accio\stock_data-retirement\scripts\deploy_research_cutover.ps1' -Mode Check
```

仅当上述预检通过且仍在窗口内，执行：

```powershell
& 'D:\accio\stock_data-retirement\scripts\deploy_research_cutover.ps1' -Mode Apply
```

若归档已完成但后续安装失败，旧归档仍保留；不要重复归档，也不自动删除新失败现场。记录新的安装/回滚状态，再判断下一步。`INSTALL_COMPLETE` 仍只证明安装和离线身份探针通过；首个实际日更产物须于 2026-09-14 18:30 的任务运行后验收。

以下保留第一次修复的历史记录；其中旧窗口、旧脚本哈希和“尚未返回状态”的描述不是当前状态。

## 历史：账户描述修复

2026-09-13，用户已批准仅修复账户描述与前置校验；失败现场及原发布包保留，重新部署须先核对回滚并确认新窗口。

## 原因与实际改动

管理员窗口在 `New-LocalUser` 的参数绑定阶段失败：账户描述为 59 字符，超过 48 字符限制。此前 `Check` 没有检查此参数，因而预检通过不能证明安装可成功。

- 描述改为 40 字符，与实际创建账户共用同一变量。
- 在路径检查、锁定与目录创建之前验证账户名和描述长度，并读取已安装 Windows 命令的长度约束。不实际调用账户创建来验证。
- 新增 `ValidateParameters`，只验证参数并返回 `Changes=0`；它不是完整部署预检，更不是安装成功。
- `Rollback` 不受新账户参数验证阻断。原窗口检查、既有目录拒绝覆盖、账户和任务保护均保留。

## 验证

8 项安装专项测试通过，覆盖原 59 字符故障、49 字符拒绝、48 字符边界、账户名长度、Windows PowerShell 解析、原窗口边界及现有锁协议。实际运行 `ValidateParameters` 返回 PASS，描述长度 40，变更 0；静态检查通过。未把专项测试称为本次全套回归。

- 修复后安装器 SHA-256：`0e404a577ca9e6f7708e09e8015944d20b58fd09f4b9aa50251dc7f9f51d5fae`。
- 原 0.3.10 ZIP SHA-256 仍为 `e8bed4e550c81e44d85863cb3cd6b43bc48dfe633e699c5b6d20ac0e7f3acdb3`，没有换为 0.3.11。
- 测试回执：`reports/deployment-repair-20260913/tests.xml`。
- 原失败安装器保留在 Git 提交 `a70e1929a0133e19c8eb3eee229b5911b87df4f5` 中。旧安装器哈希不能再代表修复后的脚本。

## 部署仍然停止

当前旧三项任务仍为 Ready，月度压缩 Disabled；未发现新研究任务。此前逐文件检查确认三项热修复目标仍匹配安装前内容。Ready 不代表任务最近运行成功。

失败目录 `D:\accio\stock-data-runtime` 没有删除、移动、改权或覆盖，内部包含受保护配置。用户目前只返回 `False`，尚不能据此确认完整回滚；当前非管理员会话无法读取回执。

管理员窗口下一步仅执行：

```powershell
(Get-Content -LiteralPath 'D:\accio\stock-data-runtime\deployment-20260913\journal.json' -Raw | ConvertFrom-Json).status
```

需要回传完整状态。若状态不是 `rolled_back_files_and_acls_account_and_task_disabled_artifacts_retained`，先调查或完成恢复；即使状态匹配，仍须核对目标文件、账户/任务和权限记录，不能单凭字符串宣布恢复成功。

原 16:00–17:00 窗口已经结束，未自行延长。回滚核对后再给出保留失败现场的恢复步骤和新窗口供确认；现在不要重跑 Apply，也不要手动删除目录。此修复未创建自动跟进任务。
