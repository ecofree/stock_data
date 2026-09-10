# stock_data V2 首批整改记录

日期：2026-09-10。范围：当前工作区的核心正确性修复，不代表 V2 总方案或任一完整里程碑已经验收。

## 依据与授权

- 用户要求检查整改方案，并在核验后开始修改；本批落实已复现的门禁、风控、持仓保护和回测结算缺陷。
- 输入方案：`C:/Users/coumoo/Downloads/share/stock_data_v2_master_plan.md`。
- 输入 SHA-256：`14BD6AE5830A6B9AADD73A3648B0E9140D845709F011FC35782478BBACCC684C`。
- 修改前 Git 基线：`78324ba622bf6c5fc6cbb736a490e5818788d824`。以下结果针对该基线加本批工作区修改，不借用基线的旧测试结论。
- 测试仅使用合成数据和隔离数据库；未运行生产收盘流水线、生产回测或账户导入，没有修改调度、生产数据库或锁文件。
- `.workbuddy/`、`research/phase_abc/snapshot/` 是已有未跟踪内容，本批不处理。

## 本批完成和未完成

| 审计项 | 本批处理 | 状态及边界 |
| --- | --- | --- |
| GATE-01 | `execution_ready` 与统一状态的 `executable` 严格一致；128 组门禁组合验证 | 局部缺陷已修；尚未替换所有生产者为 DecisionService |
| GATE-05 | 评分、计划仓位、已有仓位和风险配置在比较前检查类型、有限值及 0–100 范围 | 局部缺陷已修；NaN、Inf、未知值和非法配置不批准 |
| GATE-03 | `daily_loop` 不再删除/伪造持仓；未知账户总仓位与回撤保持 NULL；计划最大仓位为 0、状态待审核；当前阶段执行许可撤销 | 保护措施已落地；WP08 真实账户导入、时效、冻结资金、可卖数量、完整性及对账尚未交付 |
| GATE-04 | 仅刷新当前阶段带 `[daily_loop:v2]` 生成标记且仍待复核的日志；保留人工日志、旧无标记记录及其他阶段记录 | 部分修复；watchlist/trade_plan 的人工覆盖问题及正式所有权/版本模型仍待解决 |
| BT-01 | 入场扣现金并冻结数量；出场按数量结算；当日目标仓位取前一收盘权益 | 已修复重复复利；100 资金两笔各 50、各涨 20% 得到 120 |
| BT-02 | 逐交易日记录现金、持仓、市值及净值并计算回撤 | 已修复仅平仓计回撤；持仓中下跌 50% 后盈利退出仍记录 50% 回撤 |
| BT-03 | 拒绝 0、负值和非整数持有期；校验仓位数、资本与滑点参数 | 已修复；最早退出日在买入日之后 |
| BT-04 | 偶数样本收益中位数取中间两值平均 | 已修复 |
| BT-05 | 明示 `research_only_daily_bar_proxy`；缺价不删除已入场样本，延迟退出并保留期末未平仓 | 研究边界已明确，不是成交模型完整交付 |

账户保护的取舍：旧 `portfolio_snapshot` 行不能证明账户完整、实时或包含冻结委托，不能简单求和后声称账户通过。即使已有 60% 持仓记录，本批也只保护原始事实并保持执行关闭；“按真实 60% 及其余账户要素审批”仍属于 WP08 后续任务。研究候选和复盘数据仍可生成。

日志标记只是过渡所有权边界，不是完整事件模型。历史无标记记录不会被自动清理，首次新版刷新可能同时保留旧记录和新版生成记录；人工对生成日志作出处理时应移除生成标记或变更待复核状态。后续应以独立生成投影与追加人工事件彻底分离，不能以清理重复为由删除人工事实。

## 回测输出契约变化

- `contract_version=daily_cash_quantity_v2`，净值曲线由“每次平仓一项”变为“初始权益 + 每个交易日一项”。日期在 `daily_ledger` 中对应。
- 每笔平仓增加 `quantity` 与 `pnl`；损益按现金数量计算。重复候选不重复占用资金，同日收盘卖出资金不会倒流用于当日开盘买入。
- 缺收盘价沿用最后可观察估值，并记录 `stale_marks`、`valuation_complete=false`；期末未平仓保留，不虚构卖出。不完整估值下的回撤/收益仅为带缺口研究估计。
- CLI 继续写 Markdown 和交易 CSV，新增 `<prefix>_ledger_latest.json`，包含日账本、入场尝试、未平仓、统计与研究范围。该 JSON 是回测证据，不是复盘 HTML 的运行时依赖。
- 当前仍使用分数数量、固定滑点及日线一字板近似；无整手、完整税费、公司行为、排队、跌停退出或部分成交。日线最高价被用于近似成交判断，不能把结果当成可执行或时点无泄漏的策略收益。交易日列表来自观测行情而非独立认证日历。WP11 尚未完成。
- 旧回测收益不与新契约直接混用；实际历史数据重跑与归因对照尚未执行。

## 验证记录

环境：Windows，`D:/anaconda/python.exe`，Python 3.12.4。此环境测试不等于 WP01 的干净 Windows Python 3.11 安装与发行物验收。

1. 新增反例在修复前复现门禁矛盾、非法数值风控、结算及持仓覆盖问题。
2. 针对性测试：69 passed，包括隔离 DuckDB → CLI → Markdown/CSV/JSON 产物内容检查。
3. 首轮全量：487 passed、1 failed；失败为旧测试要求未知账户仍批准执行。测试更新为验证撤销原许可、NULL 风险状态与零新增额度，未删除安全断言。
4. 最终全量：488 passed，0 failed，0 skipped，1 warning，67.76 秒。warning 来自已有 DeepSeek 配置旧键兼容测试，不是本批新增故障。
5. Ruff（E9/F63/F7/F82/F401/F841）、迁移检查（13 个文件）及 `git diff --check` 最终复查通过。

复现命令（PowerShell）：

```powershell
D:\anaconda\python.exe -m pytest -o addopts='' -q -p no:cacheprovider --tb=short
D:\anaconda\python.exe -m ruff check --select E9,F63,F7,F82,F401,F841 .
D:\anaconda\python.exe scripts/lint_migrations.py
```

本轮代码和测试文件 SHA-256（路径相对项目根目录；换行格式改变也会改变 hash）：

```text
trade_system/gate_contract.py C8A7D1108E69BA6DC6395160F0B4A3AF1DD458C6DEEBF1DC48FBA34E3E80A9EB
trade_system/operator_risk.py 85A060243A9DD6CAAD92C180E73621AD6C6CEBFA0C3EAB1306CBA35167626747
trade_system/daily_loop.py 16849FF7074A722DE90CD678CE8F3945B1DBA3F048BE77E0C0A470E792E19B1B
trade_system/backtest_engine.py 5FC6AF07CF380E3CF7315961C2369DD21A5890A817F7F89AC32749078276211C
scripts/run_strategy_backtest.py AFACFD60E89E7DD4386CFBA8B8230F337884676B952021A9552B079D2078F850
tests/test_remediation_correctness.py 62AE5178C49136C904B122B62C1C85141A9E01FC7581E71DDF69D11DE539ED8D
tests/test_backtest_engine.py BBBC11B88B9ACB0691798B41EAEEC854939BC09B13C00B2D7C88C5C7327563DA
tests/test_daily_operator_loop.py FEA22091A1DAFD797CE8A0058A3D7B2BF42F2209329D0C4E4823F07A9141602F
tests/test_daily_loop_stage_fallback.py 8D4DA2BEEA9266701CD7AD81E0BAA62C13F47CB7F8CBB72B71AE0F91ECC089FE
```

## 下一批优先顺序

1. WP00/WP02：协调停写与恢复演练；完成安全 Windows 句柄锁及真实双进程验证；保护现有写进程和锁所有权。
2. WP03/WP08/WP10：事实契约、真实账户导入/对账、统一 DecisionService 和确认时额度预占；排查其余弱 ready 入口。当前修复不能替代这些能力。
3. WP11/WP15：真实结算规则和 QLib 时点数据、标签、滚动样本外协议及对照实验；缺少真实验证继续 research_only。

本批没有执行数据库迁移或切换，没有提交或推送新 Git 版本；完成范围以本记录列出的文件行为及本轮测试为准。
