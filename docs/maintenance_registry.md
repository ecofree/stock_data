# 维护对象登记与处理边界

`trade_system/maintenance_registry.py` 为脚本和空表提供保守的生命周期分类。它只改变审计信息，不自动移动、删除文件或删除数据库表。

## 脚本生命周期

| 生命周期 | 处理方式 |
|---|---|
| `canonical_production` | 日常生产唯一入口，必须保持稳定 |
| `production_collector` / `production_runner` | 当前生产入口显式调用的任务 |
| `historical_recovery` | 历史回补、缺口修复和同步，按批次运行 |
| `compatibility_collector` / `compatibility_runner` | 保留用于恢复和对照，不得作为第二定时生产写入者 |
| `research_or_experiment` | QLib、回测、策略和研究，不进入收盘主链 |
| `audit_or_gate` | 审计和门禁，不写生产数据 |
| `report_or_build` | 报告和构建任务 |

## 运行时 DDL 边界

生产采集器和报表任务只允许读取结构并写入业务数据，不得执行
`DROP TABLE`、`DROP COLUMN`、整表重建或隐式索引重建。结构修复必须使用
版本化 migration 或带 `--dry-run` 的显式恢复命令，并在写入前持有
`<database>.pipeline.lock`。常规收盘运行不得因为初始化采集器而触发结构迁移。

索引属于 schema 版本的一部分；新增或变更索引使用 migration，
`scripts/ensure_operational_indexes.py` 仅作为显式恢复/核验工具，不进入每日收盘计划。

## 空表生命周期

空表至少观察 30 天，核对静态引用、视图依赖、运行日志、写入记录和人工工作流。

- `retain_manual`：人工输入或运营工作流，保留。
- `retain_recovery_candidate`：接口可用、交易时段专用或可修复，保留并单独回补。
- `retain_optional_dependency`：QLib/财务等外部依赖，保留但不进入生产门禁。
- `orphan_candidate`：暂未发现引用，观察后才可提出归档。
- `safe_to_drop` 默认永远为 `false`；需要单独批准和迁移记录才可改变。
