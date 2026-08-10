from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _read(name: str) -> str:
    return (ROOT / "scripts" / name).read_text(encoding="utf-8")


def test_scheduled_runners_use_absolute_project_paths_and_durable_logs():
    close = _read("run_stock_data_daily.ps1")
    once = _read("run_phase_once.ps1")
    watch = _read("run_phase_watch.ps1")

    assert "$IntegratedRunner = Join-Path $Root" in close
    assert "--reports-dir" in close
    assert "scheduled_close_" in close
    assert "DAILY_RUN_FAILED" in close
    assert "audit_p0_five_day_observation.py" in close
    assert "P0_OBSERVATION_FAILED" in close
    assert "Push-Location $Root" in close

    assert "$IntegratedRunner = Join-Path $Root" in once
    assert "$DbPath = if ([System.IO.Path]::IsPathRooted($Db))" in once
    assert "scheduled_{0}_{1}.log" in once

    assert "$DbPath = if ([System.IO.Path]::IsPathRooted($Db))" in watch
    assert "scheduled_{0}_watch_{1}.log" in watch
    assert "PHASE_WATCH_STOP" in watch
    assert "PHASE_WATCH_DRAIN" in watch
    assert "MinRunWindowSeconds" in watch
    assert "PHASE_WATCH_COMPLETE" in watch


def test_scheduler_installer_requires_elevation_and_system_principal():
    installer = _read("install_stock_data_task.ps1")

    assert "WindowsBuiltInRole]::Administrator" in installer
    assert 'New-ScheduledTaskPrincipal -UserId "SYSTEM"' in installer
    assert "Get-ScheduledTask -TaskName $name" in installer
    assert "principal verification failed" in installer
    assert '"StockData-Auction" "auction" "09:15" 120 "09:27"' in installer
