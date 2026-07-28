# 东方财富 clist 连接重置处理

`push2*/api/qt/clist/get` 是盘中全市场个股资金流的专用入口；它与
`datacenter-web`、`stock/get`、`ulist.np` 不是同一条上游路径，因此其它东方财富接口可用时，clist 仍可能被单独重置。资金流字段缺失时不能用行情接口冒充。

当前实现的保护：

- 每个页面最多轮换 3 个前门，直连最多 3 轮并保留同一前门集合的有界 curl fallback；不再对 14 个前门再做 8 次 curl 扩散重试。
- 首个有效页后记录成功；页面无有效 `data.diff` 时写入 `trade_system/.stock_cache/eastmoney_clist_guard.json`，按 30 秒、60 秒、120 秒、300 秒、900 秒指数冷却。
- 冷却期间调度器快速失败，保留数据库已有快照和分页检查点，不擦除旧数据，也不把上一交易日改标成今天。
- 由于 clist 是动态排序，批次低于 `expected_rows` 时 `--resume` 会重放全部页面；成功条件要求精确达到上游 denominator，不接受 95% 近似覆盖。
- 不自动启动第二次全市场重抓；恢复后再由调度器按检查点重试，避免故障时产生流量尖峰。

恢复或人工探测：

```powershell
D:\anaconda\python.exe scripts\collect_intraday_stock_flow_market.py --db kpl_data.duckdb --date 2026-07-15 --resume --pause-seconds 0.8 --out reports\intraday_stock_flow_latest.md
```

仅在确认上游已恢复、需要绕过冷却窗口时临时设置：

```powershell
$env:EASTMONEY_CLIST_BYPASS_GUARD = "1"
```

绕过变量不应写入计划任务。若 clist 仍重置，状态应保持 `partial/error`，并使用上一份可审计快照；Tushare/东方财富历史接口只能补齐已完成交易日，不能替代盘中实时资金流。
