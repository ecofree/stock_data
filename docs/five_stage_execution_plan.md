# 五阶段实施计划与验收记录

更新时间：2026-07-14

这份计划按“先可观察、再补数据、再闭环、最后建模”的顺序执行。每个阶段都有可检查的产物；未满足验收条件时，阶段保持进行中，不把研究性数据误报为可交易数据。

## 第一阶段：可视化工作台 MVP

目标：让操作者在一个页面看到数据新鲜度、个股/板块资金流、候选池、概念完整性、人工复盘和 QLib 状态。

执行项：

1. 汇总 `data_readiness`、资金流新鲜度、THS/KPL 概念状态和 QLib 影子评估。
2. 增加个股资金流与板块资金流 TopN、筛选、缺口提示。
3. 增加候选池、人工结果模板、QLib 模型和候选重合度入口。
4. 生成静态页面并通过 HTTP 200 和页面文本检查。

验收：`reports/trading_dashboard_latest.html` 可打开；页面明确区分 `available`、`partial`、`unverified`、`stale`，不隐藏缺口。

状态：已完成。

## 第二阶段：盘中资金流覆盖与候选池

目标：盘中按优先级获取数据，避免逐股/逐日暴力请求，并让候选池可恢复生成。

执行项：

1. 优先使用当日批量资金流和板块资金流；只对候选范围补充个股接口。
2. 增加同日 THS 快照候选池回退，但标记为研究回退，不满足可交易就绪门槛。
3. 资金流和候选结果落库，保留来源、时间和覆盖数量。
4. 在集成日任务中允许盘中 partial 结果继续进入审计，但禁止自动下单。

验收：候选池能够生成且可审计；`data_readiness` 在非真实涨停池时保持 `actionable=false`；个股和板块覆盖分别统计。

状态：已完成实现；当前正式投入缺口是盘中个股资金流全市场覆盖，报告中已明确为 partial。

## 第三阶段：THS 概念及成分股完整性

目标：以同花顺概念为默认源，确保约 360 个概念及其全部成分股可对应，分页受限时不误报完成。

执行项：

1. 先用 TuShare `ths_index/ths_member` 映射可覆盖的概念。
2. 对 TuShare 缺失概念，从 THS 详情页提取 `clid` 指数码。
3. 迁移 AData 发现的 THS `blockrank` JSONP 接口，批量获取全部成分，保存 provider 和数量审计信息。
4. 对 API 返回数量、代码格式、去重数量做校验；失败或分页不足保留 partial checkpoint。
5. 回填最新快照，生成概念完整性报告。

验收：361/361 概念 checkpoint 为 `success`，4 个原 partial 概念分别为 641、52、289、53 只；成员来源和 `date_verified=0` 状态可追溯。

状态：已完成本次快照完整性；历史日期仍不支持验证，不能用于历史回测。

## 第四阶段：人工交易结果与复盘闭环

目标：把候选、计划、实际成交和结果统一到可重复的复盘链路。

执行项：

1. 生成当天人工结果 CSV 模板，默认所有行 `review_required`。
2. 导入时校验股票、日期、方向、成交价格和数量；重复导入必须幂等。
3. 写入 `operator_trade_outcome`、更新 `trade_plan`，同时追加 `trade_journal`。
4. 复盘报告按真实结果计算，不用候选分数冒充成交样本。

验收：模板、导入命令和回测报告可用；无人工结果时样本数为 0 且 verdict 为 `insufficient_sample`。

状态：已完成实现；当前数据库尚无真实人工结果，正式绩效结论仍待录入。

## 第五阶段：QLib 特征、影子训练与评估

目标：先建立无泄漏的分析/排序能力，再决定是否进入信号门槛。

执行项：

1. 从 Tushare 日线、日基本面和资金流批量导出宽表特征。
2. 训练集/评估集按日期切分，只用训练集统计量填充缺失。
3. 使用 QLib `LGBModel` 做影子模型，保存模型、预测、注册信息和评估结果。
4. 计算模型候选与策略候选重合度，但保持 `signal_impact=disabled`。
5. 达到足够真实交易样本后，再评估是否开启信号影响。

验收：特征文件、模型、预测和评估报告均可复现；当前影子评估 494 个样本，模型不改变实盘信号。

状态：已完成影子链路；尚未达到自动交易授权条件。

## 五阶段后的持续运行顺序

每日按以下顺序运行：

1. 先同步日线/基本面/资金流并写入数据库；
2. 盘中只补当前候选范围的个股资金流和需要的板块流；
3. 更新 THS 最新概念快照并检查 checkpoint；
4. 生成候选池和人工结果模板；
5. 收盘后导入真实结果、生成复盘，再更新 QLib 影子评估；
6. 最后刷新数据就绪报告和可视化页面。

任何阶段出现 `partial`、`unverified`、`stale` 或覆盖不足，都只允许研究/复核用途，并在报告中保留缺口。

## Remediation execution status (2026-07-14)

- P0 completed: fixed TuShare sector-flow unit semantics, added sector taxonomy/unit columns and a semantic readiness gate, repaired the current 2026-07-14 rows, rebuilt normalized views, and changed the 2026 audit to flag capped 5,000-row dates instead of treating the capped stock-basic table as the universe.
- P1 completed: daily review and dashboard now expose stock inflow/outflow rankings, THS concept inflow/outflow rankings, concept-to-limit-up-stock mapping, coverage metadata, and research-only candidate output.
- P2 implemented: the integrated runner has a `priority` collection profile that avoids duplicate focus/staged API fan-out; PowerShell wrapper performs a pre-run DuckDB backup; an opt-in Windows Task Scheduler registration script is provided.
- P3 implemented: candidate scores carry `is_actionable` and `candidate_status`; same-day K-line, sector, auction, non-fallback, positive-price, and non-penalized-regime evidence are required before a candidate can be executable. Missing evidence remains `research_only`.
- Remaining external inputs: historical THS snapshots are still unverified for backtest use, operator outcomes are still empty, and the current 2026-07-14 close readiness remains blocked until same-day close K-lines are persisted.
