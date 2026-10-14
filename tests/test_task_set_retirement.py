"""Explicit five-task lifecycle; legacy plans retain their safety contract."""
from pathlib import Path
import hashlib
import json
import os
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
ACTIVE = ["Auction", "Intraday", "DailyClose", "SupplementalRetry", "ResearchDaily"]
RETIRED = ["QLibResearch", "MonthlyCompact"]


def ps(script, *args):
    return subprocess.run(
        [r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe", "-NoProfile",
         "-NonInteractive", "-File", str(script), *map(str, args)],
        capture_output=True, timeout=60,
        env={k: v for k, v in os.environ.items() if k.upper() != "PSMODULEPATH"},
    )


def fixture(tmp_path, suffixes):
    baseline = tmp_path / "baseline"
    baseline.mkdir()
    rows = []
    for suffix in suffixes:
        name = "StockData-" + suffix
        rows.append({"Name": name, "State": "Disabled", "Actions": [
            {"Execute": "PowerShell.exe", "Arguments": "old", "WorkingDirectory": None}],
            "Triggers": []})
        (baseline / (name + ".xml")).write_text(
            '<Task xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">'
            '<Principals><Principal><UserId>S-1-5-21-123-1006</UserId>'
            '<LogonType>Password</LogonType></Principal></Principals>'
            '<Settings><Enabled>false</Enabled></Settings><Triggers/>'
            '<Actions><Exec><Command>PowerShell.exe</Command>'
            '<Arguments>old</Arguments></Exec></Actions></Task>', encoding="utf-8")
    inventory = baseline / "tasks.json"
    inventory.write_text(json.dumps(rows), encoding="utf-8")
    contract = tmp_path / "contract.json"
    contract.write_text(json.dumps({"scope": "transitional_market_collection_only",
        "execution_ready": False, "python": sys.executable,
        "database": str(tmp_path / "db.duckdb"), "reports": str(tmp_path / "reports")}), encoding="utf-8")
    release = tmp_path / "release"
    release.mkdir()
    manifest = release / "research-release.json"
    manifest.write_text('{"version":"0.3.51"}', encoding="utf-8")
    env = tmp_path / "synthetic.env"
    env.write_text("# fixture; no real credentials", encoding="utf-8")
    output = tmp_path / "plan.json"
    digest = lambda p: hashlib.sha256(p.read_bytes()).hexdigest()
    args = ["-BaselineDirectory", baseline, "-BaselineInventorySha256", digest(inventory),
        "-CollectorContract", contract, "-CollectorContractSha256", digest(contract),
        "-AdapterPython", sys.executable, "-ResearchPython", sys.executable,
        "-ResearchReleaseDirectory", release, "-ResearchReleaseManifestSha256", digest(manifest),
        "-EnvironmentFile", env, "-Workspace", tmp_path / "workspace", "-Output", output,
        "-ResearchStartBoundary", "2026-10-14T19:30:00+08:00"]
    return args, output, digest


@pytest.mark.skipif(sys.platform != "win32", reason="Windows task compiler")
@pytest.mark.parametrize("case", ["valid", "missing_active", "retired_reappeared", "unknown_task", "legacy_five"])
def test_five_task_profile_is_explicit_and_exact(tmp_path, case):
    suffixes = ACTIVE.copy()
    if case == "missing_active":
        suffixes.remove("SupplementalRetry")
    elif case == "retired_reappeared":
        suffixes.append("QLibResearch")
    elif case == "unknown_task":
        suffixes.append("Unapproved")
    args, output, digest = fixture(tmp_path, suffixes)
    if case != "legacy_five":
        args += ["-TaskSet", "active-five-v1"]
    result = ps(ROOT / "scripts/install_stock_data_task.ps1", *args)
    if case != "valid":
        assert result.returncode != 0 and not output.exists()
        return
    assert result.returncode == 0, (result.stdout, result.stderr)
    plan = json.loads(output.read_text("utf-8"))
    assert plan["TaskSet"] == "active-five-v1"
    assert set(plan["RetiredTaskNames"]) == {"StockData-" + n for n in RETIRED}
    assert {r["Name"] for r in plan["Actions"]} == {"StockData-" + n for n in ACTIVE}
    assert len(plan["Actions"]) == 5 and plan["AbsentTasks"] == []
    assert plan["MissingRequiredTasks"] == [] and plan["ProductionCutover"] is False
    engine = ROOT / "scripts/deploy_current_tasks.ps1"
    checked = ps(engine, "-Mode", "Check", "-Plan", output, "-PlanSha256", digest(output))
    assert checked.returncode == 0, (checked.stdout, checked.stderr)
    rehearsed = ps(engine, "-Mode", "Rehearse", "-Plan", output, "-PlanSha256", digest(output),
                   "-OutputDirectory", tmp_path / "rehearsal")
    assert rehearsed.returncode == 0, (rehearsed.stdout, rehearsed.stderr)
    receipt = json.loads((tmp_path / "rehearsal/receipt.json").read_text("utf-8"))
    assert receipt["passed"] and len(receipt["scenarios"]) == 8
    assert receipt["windows_task_changes"] == 0
    # Loss of the explicit lifecycle profile must fail, not silently allow a
    # missing legacy MonthlyCompact or reintroduce either retired task.
    plan.pop("TaskSet")
    changed = tmp_path / "missing-profile.json"
    changed.write_text(json.dumps(plan), encoding="utf-8")
    assert ps(engine, "-Mode", "Check", "-Plan", changed, "-PlanSha256", digest(changed)).returncode != 0


@pytest.mark.skipif(sys.platform != "win32", reason="Windows snapshot paths")
@pytest.mark.parametrize("extra", [None, "MonthlyCompact", "QLibResearch"])
def test_five_task_capture_reports_retired_reappearance_and_explicit_database(tmp_path, monkeypatch, extra):
    from tools.v2 import capture_windows_cutover as capture
    names = ACTIVE + ([extra] if extra else [])
    commands = []

    def read_only_shell(command):
        commands.append(command)
        if "Get-ScheduledTask -TaskName" in command:
            return json.dumps([{"Name": "StockData-" + n} for n in names])
        if "Export-ScheduledTask" in command:
            return "<Task/>"
        return '{"Owner":"fixture","Access":[{"FileSystemRights":"Read"}]}'

    monkeypatch.setattr(capture, "powershell", read_only_shell)
    database = tmp_path / "current-source.duckdb"
    result = capture.capture(tmp_path / "snapshot", "active-five-v1", database)
    assert result["task_set"] == "active-five-v1"
    assert result["missing_expected_tasks"] == []
    assert result["unexpected_tasks"] == (["StockData-" + extra] if extra else [])
    assert any(str(database) in command for command in commands)
    assert not any("D:/accio/stock_data/kpl_data.duckdb" in command for command in commands)
    with pytest.raises(ValueError, match="explicit current source database"):
        capture.capture(tmp_path / "missing-db", "active-five-v1")
