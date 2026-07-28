# Qlib Shadow Mode

qlib 只作为旁路验证，不直接参与 `stock_data` 的交易信号、仓位建议或候选股入选。

## 已接入边界

- `qlib_model_registry`：记录外部模型元信息。
- `qlib_prediction`：导入外部预测分数。
- `qlib_shadow_evaluation`：记录预测与后验收益的验证结果。
- `v_qlib_shadow_candidate_overlap`：观察 qlib 分数与策略候选的重合。

## 禁止事项

- 不在主流程训练 qlib 模型。
- 不把 Alpha158/Alpha200 二进制数据放入主项目。
- 不把 qlib score 作为买入/卖出触发。
- 不因 qlib 缺失而影响日常报告。

## 使用方式

1. 外部生成 CSV：`trade_date,symbol,score,rank,horizon`。
2. 使用 `scripts/import_qlib_shadow_predictions.py` 导入。
3. 使用 `scripts/evaluate_qlib_shadow.py` 评估。
4. 连续样本验证有效后，最多作为候选股证据字段，不作为主信号。
