# 受控原件与估值清单接收

本入口管理**原始字节保管**，不是金融资格判定，也不是业务数据库导入。缺估值、独立资金或账户证据的状态不因此改变。

## 固定职责和位置

- 资料提供者：提供真实官方PDF/原始JSON、实际来源/到达时间、当前会话输入；不能由AI制造缺失材料。
- 具名审核人：审核范围、原件、提取/修订链与当日输入，签署清单的`reviewed_by`、带时区`reviewed_at`及精确SHA。字段有名字不等于实际审核发生，责任仍由该实际审核人承担。
- 管理员操作员：在新固定维护范围内运行封存版本的接收工具，保留plan、原manifest、接收回执和失败。不得使用可写开发源码作为生产授权来源。
- SYSTEM采集器：仅通过原有实际数据库guard与受控采集入口消费已审核清单；本工具不授予数据库写者资格。SYSTEM本身有管理员特权，不声称其在OS层绝对不能修改文件。
- StockDataResearch：原件只读；不能改原件、写接收清单或访问私有账户入口；不代替审核人。

稳定根以下：

- `evidence/objects/<sha256>`：按原始字节SHA寻址；只新增、不覆盖。已有对象有差异时拒绝，不自动修补。
- `workspace/valuation-intake/.staging/<delivery-id>/`：管理员私有待审核材料。
- `workspace/valuation-intake/YYYYMMDD/manifest.json`与`manifest.sha256`：已接收的实际当日清单。
- `workspace/valuation-intake/admission.guard`：跨进程永久句柄锁，绝不按PID/时间删除或抢占。
- `workspace/acceptance-inputs/`：真实账户等私有验收输入，仅管理员/SYSTEM可访问，绝不提交Git或展示凭据内容。

## 材料与边界

staging内包含原`manifest.json`、`bundles/`下的审核包，以及`originals/<sha256>`下的新原件。清单schema为`valuation_daily_intake_v1`，会话日必须等于实际上海当前日；审核时间不得在未来，须为同日带时区时间。1—16个bundle，单包≤8MB、合计≤32MB；原件单份≤32MB、一次总量≤256MB、引用图有界。

bundle必须保留真实reviews和reviewed_source_documents等原字段。原件必须为PDF或结构化JSON。已在稳定对象库中的同SHA原件可复用；旧文字路径仅作原始出处，不能退回旧工作树读取。新原件只从审核staging取；路径越界、链接、硬链接、错SHA、来源变化均拒绝。

## 操作

1. 使用将要封存的`tools/v2/admit_valuation_intake.py plan --runtime-root ... --stage ... --manifest-sha256 ... --output <新计划文件>`。
2. 审核计划范围与原manifest；计划阶段不改接收区或对象库。
3. 使用同版本工具`admit --runtime-root ... --plan ... --plan-sha256 ... --output <新回执文件>`。执行前复核计划SHA，取得真实永久guard后重新校验输入。
4. 对象通过不可覆盖的原子占位发布；所有文件验证后先写SHA、最后发布manifest。中途中断只能留下未发布或可对账的intent，不能成为金融通过。
5. 相同计划重试返回同一manifest身份。不同清单不能覆盖已接收日目录；同日更正需要新具名审核和单独的显式清单导入方案，不能静默替换原审核。已有无intent目录、无关文件或漂移一律停止，不删除以“修复”。
6. 下游仍使用原`run_local_valuation_session`、实际PipelineLock、合格同会话股票范围与原件/字段/修订验证。文件接收不构成复运、P1、全市场认证、账户绩效或交易授权。

暂停期间不创建伪装真实材料的示例清单，不补造审核人和交易日。正式部署和真实身份权限检查未完成前，仅在隔离夹具验证新工具。
