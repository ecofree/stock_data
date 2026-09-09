# AGENTS.md

## 协作规则（必须遵守）

1. **先方案，后动手**：任何涉及修改项目代码/文件的任务，必须先给出问题原因分析 + 解决方案（含取舍与预期效果），经用户明确同意后才能编辑项目。诊断类只读操作（查库、跑脚本验证、看日志）不受此限。
2. 数据源优先级：同花顺 HiThink 官方 API > xiaodefa TuShare 中继 > 其他（akshare 仅作最后兜底，需说明不稳定性）。
3. 复盘页 `reports/daily_review_latest.html` 是自包含静态页：禁止引入运行时外部数据依赖（sidecar/fetch），所有交互数据必须内联。
4. 涉及 DuckDB 的长任务注意写锁：同一时间只允许一个写进程；批量任务前检查残留 python 进程。
5. **进程强杀纪律**：kill 任何可能持有 `kpl_data.duckdb.pipeline.lock` 的进程后，必须检查并删除该锁文件，否则当日 17:30 收盘流水线会静默失败（启动即退出，无 run 无日志）。close 失败的排查入口：`logs/scheduled_close_<date>.log` 是否存在 + `*.pipeline.lock` 是否残留。
