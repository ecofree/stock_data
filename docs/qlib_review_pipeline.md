# QLib 与智能复盘实施规范

## 数据分层

原始采集表保留每个来源的完整结果；`multi_source_stock_flow` 和
`multi_source_sector_flow` 只作为多源审计面；以下两张表是研究和复盘的统一输入：

- `qlib_stock_flow_features`
- `qlib_sector_flow_features`

它们按相同的来源优先级每天选一条有效记录，并按交易日顺序计算 1、3、5、10、20 日累计资金、正流入天数、观察天数、资金加速度和资金/成交额等指标。缺失日期不会被填成零，`observed_days_20d` 用来暴露实际样本长度。

## QLib 影子流程

```text
收盘数据与质量门禁
  -> build_flow_features
  -> export_qlib_features
  -> 滚动训练/下一交易日预测
  -> qlib_prediction
  -> 逐日横截面 IC、RankIC、分位组合、回撤
  -> 复盘展示（shadow / disabled）
```

当前预测仍然不能进入执行信号。`qlib_shadow_evaluation` 的 Top/Bottom 指标按每天的横截面分位计算，而不是把所有日期混在一起排序。QLib训练必须使用时间切分，不能随机打乱；模型样本不足、数据门禁失败或人工成交结果不足时，保持 `shadow`。

## AI 复盘接口

`scripts/generate_ai_review_snapshot.py` 生成 `reports/ai_review_facts_latest.json`。该文件只包含已经过数据门禁的结构化事实、来源、覆盖率、资金流排名、持续性、QLib影子结果和候选池，不调用网络模型，也不保存密钥。

后续接入本地或远程模型时，模型只能：

1. 总结资金流变化和板块轮动；
2. 解释个股与所属板块的资金/价格背离；
3. 标注异常、风险和次日观察条件；
4. 输出带事实引用的自然语言复盘。

模型不能补造缺失数据、修改 `analytics_ready`/`execution_ready`、把影子分数变成订单，或在没有人工成交结果时声称真实收益。

## 验收条件

- 每个数值结论可回溯到 `trade_date`、`source_date`、`provider`、覆盖率和原始表；
- 特征不使用 `label_next_ret`、未来价格或未来板块成员关系；
- 训练、验证和测试按时间滚动切分；
- 至少比较规则基线、QLib模型和 Top-Bottom 组合；
- 交易成本、T+1、涨跌停、停牌和不可成交状态单独建模；
- 人工成交结果达到可评估数量前，所有模型保持 `shadow`，执行门禁保持独立。
