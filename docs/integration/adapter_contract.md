# Adapter Contract

所有外部能力进入 `stock_data` 前必须先实现 adapter 契约。

## 必填元数据

- `source_id`：稳定数据源标识。
- `data_domain`：auction、sector_capital、kline、research、strategy、ml_shadow 等。
- `frequency`：daily、intraday、batch、on_demand。
- `auth_type`：none、env、header、query 或 env_optional。
- `requires_key`：是否需要密钥。
- `enabled`：默认是否进入主流程。
- `risk_level`：low、medium、high。

## 安全约束

- 不读取或输出密钥。
- 不把 `.env`、settings、token、cookie 写入报告。
- 不自动下单。
- 不直接改写 raw 数据。
- 外部数据先进入 adapter 或新增表，再经视图进入信号层。

## 验证要求

- 每个 adapter 必须有测试。
- 每个字段映射必须能解释来源。
- 失败必须降级，不得中断主日跑。
- 进入信号前必须能回测或至少统计样本。
