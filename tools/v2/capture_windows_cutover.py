"""Export task definitions and current ACLs without changing either."""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import xml.etree.ElementTree as ET


def powershell(command):
    # Do not inherit PowerShell 7's module search path into Windows PowerShell
    # 5.1: mixed extended type definitions make Get-Acl fail to autoload.
    child_env = {k: v for k, v in os.environ.items() if k.upper() != 'PSMODULEPATH'}
    # Export identifiers, never exception messages/source lines that may contain
    # arguments or credentials. Keep the useful error instead of a generic exit.
    wrapper = ("$ErrorActionPreference='Stop';[Console]::OutputEncoding=[System.Text.UTF8Encoding]::new();try {\n"+
        command+"\n} catch { $errorFields=@{error_id=$_.FullyQualifiedErrorId;category=[string]$_.CategoryInfo.Category};"+
        "[Console]::Error.WriteLine(($errorFields | ConvertTo-Json -Compress));exit 1 }")
    result=subprocess.run(['powershell.exe','-NoProfile','-Command',
        wrapper],capture_output=True,encoding='utf-8',timeout=60,env=child_env)
    if result.returncode:
        try: fields=json.loads(getattr(result,'stderr',''))
        except (ValueError,TypeError): fields={}
        safe=[str(fields.get(key,'')) for key in ('error_id','category')] if isinstance(fields,dict) else []
        safe=[value for value in safe if re.fullmatch(r'[A-Za-z0-9_.,:-]{1,160}',value)]
        raise RuntimeError('read-only Windows snapshot failed (exit '+str(result.returncode)+
                           ('; '+', '.join(safe) if safe else '; diagnostic unavailable')+')')
    return result.stdout


def capture_probe(output):
    """Sanitized retained-probe evidence; no credentials, task changes or ACL writes."""
    command=r"""
    $root='D:\accio\stock-data-runtime'
    $attempt=Join-Path $root 'recovery-probe-0312-once'
    $j=Get-Content -LiteralPath (Join-Path $attempt 'journal.json') -Raw | ConvertFrom-Json
    $t=Get-ScheduledTask -TaskName 'StockData-ResearchDaily' -TaskPath '\'
    $a=Get-LocalUser -Name 'StockDataResearch'
    $paths=@($root,(Join-Path $root 'deployment-20260913'),$attempt)
    $acls=@(foreach($path in $paths) {
        $acl=Get-Acl -LiteralPath $path
        [pscustomobject]@{Path=$path;Protected=$acl.AreAccessRulesProtected;
        Rules=@($acl.GetAccessRules($true,$true,[Security.Principal.SecurityIdentifier]) |
            Select-Object @{n='SID';e={$_.IdentityReference.Value}},FileSystemRights,AccessControlType,InheritanceFlags,IsInherited)}
    })
    [pscustomobject]@{SystemChanges=0;CapturedAt=[DateTimeOffset]::UtcNow.ToString('o');
        Status=$j.status;Verified=$j.probe_verified;Failure=$j.failure_reason;RollbackError=$j.rollback_error;
        SavedAclPaths=@($j.acls.PSObject.Properties.Name);
        PendingJournalWrite=(Test-Path -LiteralPath (Join-Path $attempt 'journal.json.new'));
        AccountEnabled=$a.Enabled;AccountSid=$a.SID.Value;
        TaskState=[string]$t.State;TaskEnabled=$t.Settings.Enabled;TaskTriggers=@($t.Triggers | Where-Object {$null -ne $_}).Count;
        Actions=@($t.Actions | Select-Object Execute,Arguments,WorkingDirectory);
        ProbeProcesses=@(Get-CimInstance Win32_Process | Where-Object {
            $_.Name -match '^(powershell|pwsh|python)\.exe$' -and
            $_.ProcessId -ne $PID -and $_.CommandLine -match 'recover_research_probe|deployment_probe.py|identity-probe.ps1'
        } | Select-Object ProcessId,ParentProcessId,Name,CreationDate);
        Directories=$acls} | ConvertTo-Json -Depth 8
    """
    value=json.loads(powershell(command))
    output=Path(output).resolve()
    output.parent.mkdir(parents=True,exist_ok=True)
    with output.open('x',encoding='utf-8') as handle:
        json.dump(value,handle,ensure_ascii=False,indent=2)
    return {'diagnostic_saved':str(output),'system_changes':0}


def capture(output):
    output=Path(output).resolve();output.mkdir(parents=True,exist_ok=False)
    command="""@(Get-ScheduledTask -TaskName 'StockData-*' | ForEach-Object {
        $info=$_ | Get-ScheduledTaskInfo
        [pscustomobject]@{Name=$_.TaskName;State=$_.State.ToString();LastResult=$info.LastTaskResult;
            LastRun=$(if($null -ne $info.LastRunTime){$info.LastRunTime.ToString('o')}else{$null});
            Next=$(if($null -ne $info.NextRunTime){$info.NextRunTime.ToString('o')}else{$null});
        Actions=@($_.Actions | Select-Object Execute,Arguments,WorkingDirectory);
        Principal=$_.Principal | Select-Object UserId,LogonType,RunLevel;
            Triggers=@($_.Triggers | Where-Object {$null -ne $_} | Select-Object StartBoundary,Enabled)}
    }) | ConvertTo-Json -Depth 5"""
    try:
        tasks=json.loads(powershell(command))
        if tasks is None:tasks=[]
        if isinstance(tasks,dict):tasks=[tasks]
        if not isinstance(tasks,list) or any(not isinstance(task,dict) for task in tasks):
            raise ValueError('invalid task snapshot')
    except (RuntimeError,ValueError,subprocess.TimeoutExpired) as exc:
        detail=str(exc) if isinstance(exc,RuntimeError) else type(exc).__name__
        (output/'error.json').write_text(json.dumps(
            {'status':'incomplete','system_changes':0,'error':detail}),encoding='utf-8')
        raise
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
    expected={'StockData-Auction','StockData-Intraday','StockData-DailyClose',
              'StockData-ResearchDaily','StockData-MonthlyCompact'}
    return {'tasks_exported':len(tasks),'task_changes':0,'acl_changes':0,
            'missing_expected_tasks':sorted(expected-{task['Name'] for task in tasks})}


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--output',required=True)
    parser.add_argument('--retained-probe',action='store_true')
    args=parser.parse_args()
    print(json.dumps((capture_probe if args.retained_probe else capture)(args.output)))
