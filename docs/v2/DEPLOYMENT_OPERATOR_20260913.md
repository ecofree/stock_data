# 只读研究部署：管理员操作单

批准范围：三文件热修复、0.3.10 固定研究发行、StockDataResearch 非管理员身份、新增 StockData-ResearchDaily。维护窗口：**2026-09-13 16:00–17:00，北京时间**。首次计划日更：9 月 14 日 18:30，之后周一至周五 18:30。保持电脑开机；日更获取数据还需要网络。

本操作单与脚本已经准备，不代表已经部署。用户确认自己的管理员 PowerShell 检查返回 True；助手会话没有因此提升权限。由用户在该管理员窗口执行以下命令，密码不用发给助手。

## 现在可运行：只读预检

```powershell
& 'D:\accio\stock_data-retirement\scripts\deploy_research_cutover.ps1' -Mode Check
```

应显示 `Preflight: PASS_read_only`、`Administrator: true`、`Changes: 0`。预检检查发行/热修复/启动器/权限探针哈希、既有任务状态、目标路径与新账户/新任务是否冲突；它不会创建账户、目录、任务、修改 ACL 或申请数据库锁。核实真实句柄所有权在 Apply 阶段完成。

如果提示哈希变化、已有账户/任务/部署目录、流水线占用、权限不足或脚本策略错误，停止并提供错误文本，不重复 Apply、不删除锁、不改变全局执行策略。

## 在批准窗口运行：安装

```powershell
& 'D:\accio\stock_data-retirement\scripts\deploy_research_cutover.ps1' -Mode Apply
```

无需修改参数或输入服务账户密码。脚本在本机生成随机服务密码，注册时交给 Windows 保存，不写入日志或 JSON；行情凭据只从现有环境文件提取明确的 HiThink/中继字段，放入新身份只读、管理员可管理的独立文件。

安装顺序：

1. 检查管理员权限、固定维护时间、UTC+08:00 主机时区及现场空闲状态；占用现有永久流水线/更新/发布句柄锁，不删除锁文件。
2. 备份三个原文件、既有任务 XML 内容和本次涉及的 ACL；只在全新专用运行目录落地发行。
3. 创建非管理员研究账户，设定源库/冻结输入/模型/发行/配置只读、研究输出可写；真人判断目录和锁不允许自动身份写入。
4. 使用**同一个新任务名称、同一个专用身份、同一种 PowerShell -File 启动方式**运行离线探针，先不设置日更触发器。探针核对真实 SID、只读 DuckDB 查询、冻结模型和页面读取、源库/模型/配置拒写、真人记录创建拒绝、普通研究输出可写。权限检测不会写入源库、模型或真人记录，不发行情请求，不训练。
5. 探针通过后才覆盖三个旧文件，实际只读重建收盘页并运行覆盖/日期/涨停关系审计；失败恢复文件，不启用日更。
6. 再确认维护窗口和旧任务未变，设置新任务的工作日 18:30 触发器。

此过程可能需要数分钟。若新任务还在运行，脚本不会强制结束进程；失败时禁用后续启动，报告需要等待和检查。

成功标志：`INSTALL_COMPLETE`。它表示安装与离线身份探针通过，**首个真实计划日更仍待验收**，不是账户或交易执行资格通过。旧三条采集任务的定义和压缩任务 Disabled 状态保持；不会恢复旧策略计划写入。

## 结果与回滚

安装记录及原文件备份：

`D:\accio\stock-data-runtime\deployment-20260913\journal.json`

专用身份探针与离线页面：

`D:\accio\stock-data-runtime\probe-results\`

新任务可在 Windows“任务计划程序”中按名称 `StockData-ResearchDaily` 查看。不要手动改成 SYSTEM。首次真实日更结束后，需核对任务退出状态、原始回执、行情日期、预测和发布指针，不仅看“就绪”。

失败时脚本会尝试恢复本次文件与权限变动，禁用新任务和新账户；**不删除数据库、原始回执、真人记录、日志或永久 guard**。如果报告回滚未完成，先检查是否还有进程运行或文件被另外修改，再在管理员窗口执行：

```powershell
& 'D:\accio\stock_data-retirement\scripts\deploy_research_cutover.ps1' -Mode Rollback
```

Rollback 不受安装时间窗口限制，但要求空闲现场、原始备份与目标哈希匹配。发现独立文件改动或账户 SID 变化会停止，不覆盖或禁用无关对象。回滚保留新目录、禁用账户和任务供审计；之后不要直接再次 Apply，需先确定新的维护方案。

## 验证与限制

安装脚本 SHA-256：`8924c867edadf56f86da7c192560bd5340cda2ccc253d81e03b79fff1b91a16e`。

权限探针 SHA-256：`c692467bf751d635dfb403acfd35991d5221c215909643844b4107560ff6dffe`。

最终 **1,549 项测试通过**，1 个既有配置弃用警告；静态检查、迁移、写入口门禁、安装依赖与最小核心检查均通过。源码指纹 `7dba82558436c13120b8139a4652954a71e61fec3b6d1d9dc5405c094122c46d`，验证期间未变化。回执为 `reports/deployment-installer-20260912/verification-final/verification.json`。首轮验证期间补充了同 PowerShell 启动方式和只读 DuckDB 检查，其源码绑定失败，不作为最终通过证据。真实账户创建、ACL 应用及专用身份运行尚待用户在维护窗口执行，离线测试不冒称已通过这些现场步骤。

Windows 任务计划程序通常会管理批处理登录权限，但组织组策略可能阻止自动分配；脚本以真实身份探针为准，遇到策略拒绝不绕过。依据：[微软批处理登录权限说明](https://learn.microsoft.com/en-us/previous-versions/windows/it-pro/windows-10/security/threat-protection/security-policy-settings/log-on-as-a-batch-job)。目录拒写检测只请求既有目录句柄，不创建假真人记录；方法依据：[微软目录句柄说明](https://learn.microsoft.com/en-us/windows/win32/fileio/obtaining-a-handle-to-a-directory)。
