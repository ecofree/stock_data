"""Export task definitions and current ACLs without changing either."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import xml.etree.ElementTree as ET


def powershell(command):
    # Do not inherit PowerShell 7's module search path into Windows PowerShell
    # 5.1: mixed extended type definitions make Get-Acl fail to autoload.
    child_env = {k: v for k, v in os.environ.items() if k.upper() != 'PSMODULEPATH'}
    result=subprocess.run(['powershell.exe','-NoProfile','-Command',
        "$ErrorActionPreference='Stop';[Console]::OutputEncoding=[System.Text.UTF8Encoding]::new();"+command],capture_output=True,encoding='utf-8',timeout=60,env=child_env)
    if result.returncode:raise RuntimeError('read-only Windows snapshot failed')
    return result.stdout


def capture(output):
    output=Path(output).resolve();output.mkdir(parents=True,exist_ok=False)
    command="""@(Get-ScheduledTask -TaskName 'StockData-*' | ForEach-Object {
        $info=$_ | Get-ScheduledTaskInfo
        [pscustomobject]@{Name=$_.TaskName;State=$_.State.ToString();LastResult=$info.LastTaskResult;
        LastRun=$info.LastRunTime.ToString('o');Next=$info.NextRunTime.ToString('o');
        Actions=@($_.Actions | Select-Object Execute,Arguments,WorkingDirectory);
        Principal=$_.Principal | Select-Object UserId,LogonType,RunLevel;
        Triggers=@($_.Triggers | Select-Object StartBoundary,Enabled)}
    }) | ConvertTo-Json -Depth 5"""
    tasks=json.loads(powershell(command))
    (output/'tasks.json').write_text(json.dumps(tasks,ensure_ascii=False,indent=2),encoding='utf-8')
    for task in tasks:
        name=task['Name']
        if not name.startswith('StockData-') or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-' for c in name):
            raise ValueError('safe scoped task identity required')
        xml=powershell("Export-ScheduledTask -TaskName '"+name+"'")
        target=output/(name+'.xml')
        target.write_text(xml,encoding='utf-16')
        ET.parse(target)
    acl=powershell("$acl=Get-Acl -LiteralPath 'D:/accio/stock_data/kpl_data.duckdb'; [pscustomobject]@{Owner=$acl.Owner;Access=@($acl.Access | Select-Object IdentityReference,FileSystemRights,AccessControlType,IsInherited)} | ConvertTo-Json -Depth 5")
    parsed_acl=json.loads(acl)
    if not parsed_acl.get('Owner') or not parsed_acl.get('Access'):
        raise ValueError('ACL owner and access rules unavailable; snapshot is not deployment evidence')
    (output/'source-database-acl.json').write_text(acl,encoding='utf-8')
    return {'tasks_exported':len(tasks),'task_changes':0,'acl_changes':0}


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--output',required=True)
    print(json.dumps(capture(parser.parse_args().output)))
