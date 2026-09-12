@echo off
powershell.exe -NoProfile -File "%~dp0scripts\start_research_workbench.ps1" -Background %*
if errorlevel 1 pause
