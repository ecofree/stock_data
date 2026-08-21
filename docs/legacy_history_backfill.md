# 老日期历史补齐

TuShare 中转的老日期 `daily/daily_basic` 批量接口不稳定，项目现在提供
`scripts/backfill_legacy_baostock.py` 作为独立备源。它只补全指定日期范围内
数据库缺少的股票，不覆盖已有行，并将每只股票的实际来源写入
`baostock_history_checkpoint`。

```powershell
D:\anaconda\python.exe scripts\backfill_legacy_baostock.py `
  --db kpl_data.duckdb `
  --start-date 20250101 --end-date 20251231 `
  --offset 0 --max-stocks 20
```

批次按 `--offset` 递增恢复。`daily` 的成交量和成交额按 TuShare 的万股/万元
口径归一化；`daily_basic` 仅能从 BaoStock取得换手率、PE(TTM)、PB(MRQ)，
市值和量比保持空值，不能冒充完整估值快照。

QLib 导出会自动排除低于全市场覆盖阈值的日期；在 2024/2025 尚未完成全市场
补齐前，数据契约报告会保持 `research_only`，模型门禁不会放行。
