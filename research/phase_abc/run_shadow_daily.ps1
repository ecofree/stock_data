# 影子每日任务：只读生产库，写research ledger；失败不阻塞收盘主链
$ErrorActionPreference = "Continue"
Set-Location -LiteralPath "D:\accio\stock_data"
& "D:\accio\stock_data\.venv-qlib\Scripts\python.exe" research\phase_abc\shadow_daily.py
exit 0
