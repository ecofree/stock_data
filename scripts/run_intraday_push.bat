@echo off
cd /d D:\accio\stock_data
"D:\anaconda\python.exe" scripts\intraday_watcher.py --db kpl_data.duckdb
