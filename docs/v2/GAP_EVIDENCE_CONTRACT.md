# 缺口证据入口契约 · v1

后续新增：[第 7 段原生数据源适配](STAGE_NATIVE_20260910.md)，提供独立的同花顺/TuShare 原生响应到本 schema 的可复核转换；下方描述的旧离线导入接口本身不变。真实 7 个现金候选不授予账户应用权限，送股仍保留原始观察待核算。

入口是 `tools/v2/run_gap_evidence.py`。这是**结构化适配器响应的离线归档/研究裁决接口**，不是已接通的同花顺或 TuShare 原生 API 客户端。它按原字节保存传入 JSON，但不会把人工填入的来源声明认证成真实服务响应，也不从任意原生 JSON/公告文本自动抽取事实。来源原生字段适配、独立验真、权益和请求预算仍须后续实现。

## 四个动作

- `audit --portfolio <已完成组合目录> --snapshot <登记过的只读快照> --output <新目录>`：验证父实验，扫描全部潜在持有窗口，按明确身份查日线、因子、备用源和任务状态，保存未知案件。
- `import-response --receipt <收据JSON> --response <结构化响应JSON> --output <新目录>`：校验原字节 SHA、响应行号、严格字段、业务/供应方接收时间；本地 ingested_at/available_at 由程序赋值，CLI 不接受用户倒填。
- `adjudicate --audit <案件目录> --evidence <证据目录> ... --output <新目录> [--mode historical_repair|system_replay] [--asof <带时区时间>]`：保存完整裁决和证据副本，不覆盖案件或原始组合。没有 evidence 时输出 unknown 是有效诊断结果，不是通过。
- `verify --folder <案件或裁决目录>`：核对成员、文件摘要、完成清单和当前实现摘要。实现变化时旧证据失效于当前实现；必须保留旧版本、新建实验，不修改旧清单。

所有输出目录必须全新。失败不生成 completed；半成品目录不得当成功结果或原地重用。所有文件与接口只限研究，不写生产 DuckDB、PaperBook/账户服务、信号或券商。

## 输入与时间

结构化响应必须是 `{"schema_version":1,"rows":[...]}`。每行且仅有 `instrument/date/kind/value`；证券格式 `SH|SZ|BJ.六位代码`，不按前缀推断市场。多行响应由收据 row_index 指明引用行；导入的 claim 必须与归档原字节内该行完全一致。

收据字段仅允许：schema_version、source_family、source_api、origin_family、reference、source_revision、source_event_at、provider_received_at、license_status、row_index、raw_sha256。API、源族、出处和版本必须明确。source_family 为 hithink_official/xiaodefa_tushare/other；license_status 为 unverified/declared_permitted/restricted。这些是声明，不是权限认证。

所有时间必须带时区，source_event_at ≤ provider_received_at ≤ 本地导入时间。最终日线的业务时间不得早于该日期 Shanghai 15:00 的声明收盘边界；这只是一项必要一致性检查，不认证交易所规则或实际 finality。`available_at=ingested_at`，供应方的早期时间不能使新导入记录进入旧时点 system_replay。historical_repair 可以在当前时点查看补收到的历史记录，不代表原来已知。

JSON 最大 4 MiB，重复键拒绝；响应 SHA、行号/内容、成员集合、时间关系和来源声明都有校验。SHA 只证明内容一致，不能证明外部来源真实。

## 支持的事实

| kind | value 必需字段与边界 |
|---|---|
| daily_bar | open/high/low/close（正数、最多两位 CNY 小数且 OHLC 合理）、currency=CNY、adjustment=none、finality=final；不含盘口/容量推断 |
| session_status | status=suspended/traded/unknown；coverage=full_session/intraday；盘中停牌不能证明全天无交易 |
| lifecycle | status=delisted/not_yet_listed/listed；effective_from/to 必须覆盖案件日期 |
| corporate_action | action_type=cash_dividend/split、action_id、ex_date 必须等于案件日；分红需正 gross_cny_per_share，拆股需正 numerator/denominator；不计算实际税/股数/权益 |
| collector_status | outcome=failed/successful_empty/partial/complete，request_id；空返回/任务完成不等于停牌，失败仅支持采集失败事实，不证明缺行的市场原因 |

## 裁决与恢复门槛

来源优先级：同花顺官方 > xiaodefa TuShare > 其他。其他如 akshare 只能最后兜底且须披露稳定性；本段没有调用任何网络源。优先级只排序候选，不能压过冲突。同一响应行经不同渠道重复提供只算一条；声明的 origin_family 不等于经验证的独立证据数，后者始终为 0。

全天停牌与有成交日线/交易状态、退出上市区间与活跃交易、不同原始价格、同一公司行为 ID 不同版本值、修复价与冻结原价矛盾均进入 conflicting_evidence；不自动覆盖或选“更高优先级”忽略另一项。不同公司行为可并存；相同公司行为候选去重，禁止借重复来源生成重复应用建议。

输出分类包括 unknown、conflicting_evidence、suspension_supported_not_authenticated、lifecycle_supported_not_authenticated、raw_bar_candidate_not_authenticated、corporate_action_candidate_not_authenticated、collection_failure_supported_cause_unresolved。“supported”只表示声明内容支持，**不是已独立确认**。

所有分类均保持 can_apply_to_account / can_resume_portfolio / can_forward_fill_mark / execution_ready=false，signal_impact=disabled。确认全天停牌也不能单独允许沿用旧估值或顺延成交；仍需恢复日、定价与时效规则。公司行为仍需持仓权益、税费、股数与成本控制数，以及新的冻结实验。历史补证不改变原测试集已被观察的状态。

## 可运行的合成样例

[响应样例](examples/gap_response_fixture.json) 与 [收据样例](examples/gap_receipt_fixture.json) **完全是合成测试，不声称 SZ.000001 在 2020-01-02 实际停牌**。本轮 CLI 将其导入 `reports/v2-gap-fixture-import-20260910/`；没有将它用于 11 个真实案件。样例响应 SHA 为 `86807d9c93515c8973a5450b0e80ea247d154761a92113707e23775252f03ea8`。编辑任何字节后收据摘要必须重新计算，不能把原样例 SHA 留作真实性证明。
# 增量入口：公告补证与权益子账

第 8 段新增[公告逐案补证与权益金标准](STAGE_ENTITLEMENT_20260910.md)。官方 PDF 的人工转录必须保留原字节 SHA、页码、解释和本次接收时间；不把公告日期伪装成系统当时可用。旧公司行为 schema 未扩展：资本公积转增保留为独立条款候选，不映射 split；三个停牌支持也不授权补 OHLC、前向填充估值或恢复账户。
