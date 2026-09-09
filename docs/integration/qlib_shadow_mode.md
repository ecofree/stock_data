# Qlib Shadow Mode

qlib 已接入研究候选融合和复盘展示，但仍不直接参与
`stock_data` 的交易信号、仓位建议或订单执行。

## 已接入边界

- `qlib_model_registry`：记录外部模型元信息。
- `qlib_prediction`：导入外部预测分数。
- `qlib_shadow_evaluation`：记录预测与后验收益的验证结果。
- `qlib_candidate_pool`：将 QLib 分数与个股资金、板块资金和规则证据融合，形成研究候选池。
- `v_qlib_shadow_candidate_overlap`：观察 qlib 分数与策略候选的重合。

## 禁止事项

- 不在主流程训练 qlib 模型。
- 不把 Alpha158/Alpha200 二进制数据放入主项目。
- 不把 qlib score 或融合分作为买入/卖出触发。
- 不因 qlib 缺失而影响日常报告。

## 使用方式

1. 外部生成 CSV：`trade_date,symbol,score,rank,horizon`。
2. 使用 `scripts/import_qlib_shadow_predictions.py` 导入。
3. 使用 `scripts/evaluate_qlib_shadow.py` 评估。
4. `scripts/run_qlib_research_daily.py` 可在收盘认证后刷新特征、当日预测、融合候选和后验评估。
5. 连续样本验证有效、通过模型门禁并完成人工复核后，才讨论模型晋级；在此之前统一保持 `shadow`。
