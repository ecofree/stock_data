# Qlib Shadow Mode

本文件描述历史 SQL 工件。当前研究使用冻结模型和独立产物，不直接参与
`stock_data` 的交易信号、仓位建议或订单执行。旧 SQL 写入器已于 2026-09-20 删除。

## 历史研究表边界（不是当前调度产物）

- `qlib_model_registry`：记录外部模型元信息。
- `qlib_prediction`：保留过去导入的预测分数。
- `qlib_shadow_evaluation`：记录预测与后验收益的验证结果。
- `qlib_candidate_pool`：将 QLib 分数与个股资金、板块资金和规则证据融合，形成研究候选池。
- `v_qlib_shadow_candidate_overlap`：观察 qlib 分数与策略候选的重合。

## 禁止事项

- 不在主流程训练 qlib 模型。
- 不把 Alpha158/Alpha200 二进制数据放入主项目。
- 不把 qlib score 或融合分作为买入/卖出触发。
- 不因 qlib 缺失而影响日常报告。

## 历史诊断与当前研究

使用 `scripts/evaluate_qlib_shadow.py` 按需只读评估已有历史预测；它不创建或更新以上表。
旧 CSV 导入命令、模型注册和预测 SQL 写入实现已删除，不能恢复成当前预测入口。
当前独立研究沿用 `scripts/run_research_daily.ps1 -RefreshResearch` 和现有研究输入合同；
缺少获知时间的旧预测不能改标为前瞻产物，也不能覆盖冻结模型。
七个真实任务此前已完成停用交接。本轮源码变化后仍保持暂停；部署与最终验收状态见唯一关闭清单。
