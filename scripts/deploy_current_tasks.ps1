# Captured-task transaction. Live staging preserves the production pause.
[CmdletBinding()]
param(
    [ValidateSet('Check','Rehearse','Stage','Apply','Rollback','Library')][string]$Mode='Check',
    [string]$Plan, [string]$PlanSha256, [string]$OutputDirectory,
    [string]$WindowStart, [string]$WindowEnd, [pscredential]$Credential
)
$ErrorActionPreference='Stop'
$script:CurrentInstaller=$PSCommandPath
$script:TaskNames=@('StockData-Auction','StockData-Intraday','StockData-DailyClose','StockData-SupplementalRetry','StockData-QLibResearch','StockData-ResearchDaily','StockData-MonthlyCompact')

function Task-Xml([string]$Text) {
    # Normalize defaults and schema ordering with the scheduler's own parser.
    # NewTask creates only an in-memory definition; it does not register a task.
    if (-not $script:XmlScheduler) {$script:XmlScheduler=New-Object -ComObject Schedule.Service;$script:XmlScheduler.Connect()}
    $definition=$script:XmlScheduler.NewTask(0)
    try {$definition.XmlText=$Text;$xml=[xml]$definition.XmlText}
    finally {[void][Runtime.InteropServices.Marshal]::ReleaseComObject($definition)}
    return $xml.DocumentElement.OuterXml
}
function Xml-Child($Parent,[string]$Name,[string]$Value) {
    $child=$Parent.SelectSingleNode('*[local-name()="'+$Name+'"]')
    if (-not $child) {$child=$Parent.OwnerDocument.CreateElement($Name,$Parent.NamespaceURI);[void]$Parent.AppendChild($child)}
    $child.InnerText=$Value
}
function Replacement-Xml($Row,[string]$ResearchStartBoundary) {
    if ($Row.Disposition -eq 'preserve_disabled') {return [string]$Row.BeforeXml}
    $xml=[xml]$Row.BeforeXml; $task=$xml.DocumentElement
    $exec=$task.SelectSingleNode('*[local-name()="Actions"]/*[local-name()="Exec"]')
    foreach ($field in @(@('Command','Execute'),@('Arguments','Arguments'),@('WorkingDirectory','WorkingDirectory'))) {
        Xml-Child $exec $field[0] ([string]$Row.($field[1]))
    }
    $settings=$task.SelectSingleNode('*[local-name()="Settings"]')
    Xml-Child $settings 'Enabled' 'true'
    # Registering an old trigger must not replay a missed close while committing.
    Xml-Child $settings 'StartWhenAvailable' 'false'
    if ($Row.Name -in @('StockData-ResearchDaily','StockData-QLibResearch')) {
        $principal=$task.SelectSingleNode('*[local-name()="Principals"]/*[local-name()="Principal"]')
        Xml-Child $principal 'UserId' $Row.Principal.UserId
        Xml-Child $principal 'LogonType' 'Password'
        Xml-Child $principal 'RunLevel' 'LeastPrivilege'
    }
    if ($Row.Name -eq 'StockData-ResearchDaily') {
        if ($ResearchStartBoundary -notmatch '^\d{4}-\d{2}-\d{2}T19:30:00\+08:00$') {throw 'Explicit proposed 19:30 China start boundary required'}
        [void][DateTimeOffset]::Parse($ResearchStartBoundary)
        $triggers=$task.SelectSingleNode('*[local-name()="Triggers"]');$triggers.RemoveAll()
        $trigger=$xml.CreateElement('CalendarTrigger',$task.NamespaceURI);[void]$triggers.AppendChild($trigger)
        Xml-Child $trigger 'StartBoundary' $ResearchStartBoundary
        $week=$xml.CreateElement('ScheduleByWeek',$task.NamespaceURI);[void]$trigger.AppendChild($week)
        Xml-Child $week 'WeeksInterval' '1'
        $days=$xml.CreateElement('DaysOfWeek',$task.NamespaceURI);[void]$week.AppendChild($days)
        foreach ($day in @('Monday','Tuesday','Wednesday','Thursday','Friday')) { [void]$days.AppendChild($xml.CreateElement($day,$task.NamespaceURI)) }
        Xml-Child $settings 'AllowStartOnDemand' 'true'
        Xml-Child $settings 'DisallowStartIfOnBatteries' 'false'
        Xml-Child $settings 'StopIfGoingOnBatteries' 'false'
    }
    return $xml.OuterXml
}
function Disabled-Xml([string]$Text) {
    $xml=[xml]$Text;Xml-Child $xml.Task.Settings 'Enabled' 'false';return $xml.OuterXml
}
function Read-TaskXml([string]$Name) {return [string](Export-ScheduledTask -TaskName $Name -TaskPath '\' -ErrorAction Stop)}
function Task-State([string]$Name) {return [string](Get-ScheduledTask -TaskName $Name -TaskPath '\' -ErrorAction Stop).State}
function Task-Identity([string]$Text,$Supplied) {
    $xml=[xml]$Text;$principal=$xml.Task.Principals.Principal
    if ($xml.Task.Settings.Enabled -ne 'false' -or @($xml.Task.Principals.Principal).Count -ne 1) {throw 'Live backend accepts only disabled tasks with one identity'}
    $parameters=@{User=[string]$principal.UserId}
    $logon=[string]$principal.LogonType
    if (-not $logon -and $parameters.User -eq 'S-1-5-18') {$logon='ServiceAccount'}
    switch ($logon) {
        'Password' {
            if (-not $Supplied) {throw 'Local PSCredential required; never infer or reset a password'}
            $sid=([Security.Principal.NTAccount]::new($Supplied.UserName)).Translate([Security.Principal.SecurityIdentifier]).Value
            $expected=if ($parameters.User -like 'S-1-*') {$parameters.User} else {([Security.Principal.NTAccount]::new($parameters.User)).Translate([Security.Principal.SecurityIdentifier]).Value}
            if ($sid -ne $expected) {throw 'Credential does not match captured task identity'}
            $parameters.Password=$Supplied.GetNetworkCredential().Password
        }
        'ServiceAccount' {if ($parameters.User -notin @('SYSTEM','S-1-5-18')) {throw 'Unsupported service identity'}}
        'InteractiveToken' {if (-not $parameters.User) {throw 'Missing interactive identity'}}
        default {throw 'Unsupported task logon type'}
    }
    return $parameters
}
function Write-TaskXml([string]$Name,[string]$Text,$Supplied) {
    if ($Name -notin $script:TaskNames -or $Name -eq 'StockData-MonthlyCompact') {throw 'Task outside replacement scope'}
    if ((Task-State $Name) -ne 'Disabled') {throw 'Live task must remain disabled; no forced stop'}
    $parameters=Task-Identity $Text $Supplied
    try {Register-ScheduledTask -TaskName $Name -TaskPath '\' -Xml $Text @parameters -Force -ErrorAction Stop | Out-Null}
    finally {$parameters.Clear()}
}
function Assert-WindowBounds([string]$Start,[string]$End,[DateTimeOffset]$Now=[DateTimeOffset]::UtcNow) {
    if ($Start -notmatch '\+08:00$' -or $End -notmatch '\+08:00$') {throw 'Explicit China offsets required'}
    $begin=[DateTimeOffset]::Parse($Start);$finish=[DateTimeOffset]::Parse($End)
    if ($begin.Offset -ne [TimeSpan]::FromHours(8) -or $finish.Offset -ne [TimeSpan]::FromHours(8) -or
        ($finish-$begin).TotalMinutes -le 0 -or ($finish-$begin).TotalMinutes -gt 60 -or $Now -lt $begin -or $Now -ge $finish) {throw 'Outside explicit new China maintenance window; no renewal permitted'}
}
function Assert-EngineeringPlan($Candidate) {
    if ($Candidate.Schema -ne 3 -or $Candidate.Scope -ne 'task_handover_engineering_only' -or $Candidate.Version -notmatch '^0\.3\.\d+$' -or
        $Candidate.ProductionCutover -ne $false -or
        @($Candidate.Actions.Name | Select-Object -Unique).Count -ne @($Candidate.Actions).Count -or
        @($Candidate.Actions.Name | Where-Object {$_ -notin $script:TaskNames}).Count -or
        @(@('StockData-Auction','StockData-Intraday','StockData-DailyClose','StockData-MonthlyCompact') | Where-Object {$_ -notin $Candidate.Actions.Name}).Count) {throw 'Complete captured-task engineering plan required'}
    foreach ($row in $Candidate.Actions) {
        $monthly=$row.Name -eq 'StockData-MonthlyCompact'
        if ($monthly -ne ($row.Disposition -eq 'preserve_disabled') -or
            (-not $monthly -and $row.Disposition -ne 'replace_responsibility')) {throw 'Task disposition mismatch'}
        if ($monthly -and (([xml]$row.BeforeXml).Task.Settings.Enabled -ne 'false' -or $row.AfterXml -cne $row.BeforeXml)) {throw 'Monthly compact must be byte-for-byte preserved disabled'}
        if (-not $row.AfterXml -or (Task-Xml $row.AfterXml) -cne (Task-Xml (Replacement-Xml $row $Candidate.ResearchStartBoundary))) {throw 'Compiled task XML mismatch'}
    }
}
function Save-Transaction($Journal,[string]$Path) {
    $temp=$Path+'.new'
    $bytes=[Text.UTF8Encoding]::new($false).GetBytes(($Journal | ConvertTo-Json -Depth 12))
    $stream=[IO.File]::Open($temp,[IO.FileMode]::CreateNew,[IO.FileAccess]::Write,[IO.FileShare]::None)
    try {$stream.Write($bytes,0,$bytes.Length);$stream.Flush($true)} finally {$stream.Dispose()}
    if ([IO.File]::Exists($Path)) {[IO.File]::Replace($temp,$Path,[NullString]::Value)} else {[IO.File]::Move($temp,$Path)}
}
function Restore-Tasks($Candidate,$Journal,[string]$JournalPath,$Supplied) {
    if (-not $Supplied -and $Journal.scope -ne 'windows_disabled_task_stage') {throw 'Explicit credential required; never reset or infer a password'}
    if ($Journal.scope -notin @('offline_seven_task_transaction','windows_disabled_task_stage') -or $Journal.plan_sha256 -ne $Candidate.InputHash -or
        @($Journal.touched | Where-Object {$_ -notin $script:TaskNames -or $_ -eq 'StockData-MonthlyCompact'}).Count) {throw 'Unrelated rollback journal'}
    $failures=@()
    foreach ($row in @($Candidate.Actions | Where-Object { $_.Name -in $Journal.touched } | Sort-Object Name -Descending)) {
        try {
            $current=Task-Xml (Read-TaskXml $row.Name)
            if ($current -ceq (Task-Xml $row.BeforeXml)) {continue}
            if ($current -cne (Task-Xml $row.AfterXml) -and $current -cne (Task-Xml (Disabled-Xml $row.AfterXml))) {throw 'Independent task change; refusing overwrite'}
            if ((Task-State $row.Name) -eq 'Running') {throw 'Task still running; no forced stop'}
            Write-TaskXml $row.Name $row.BeforeXml $Supplied
            if ((Task-Xml (Read-TaskXml $row.Name)) -cne (Task-Xml $row.BeforeXml)) {throw 'Restored XML differs'}
        } catch {$failures+=($row.Name+': '+$_.Exception.GetType().Name)}
    }
    $Journal.rollback_errors=$failures
    $Journal.status=if ($failures.Count) {'rollback_incomplete'} else {'rolled_back'}
    Save-Transaction $Journal $JournalPath
    if ($failures.Count) {throw 'Rollback incomplete; retained journal requires inspection; do not rerun Apply'}
}
function Install-Tasks($Candidate,[string]$JournalPath,$Supplied,[scriptblock]$CheckWindow,[switch]$StageOnly) {
    Assert-EngineeringPlan $Candidate
    if (-not $Supplied -and -not $StageOnly) {throw 'Explicit credential required before any change; never reset or infer a password'}
    if (Test-Path -LiteralPath $JournalPath) {throw 'Transaction exists; inspect rather than overwrite or retry'}
    & $CheckWindow
    foreach ($row in $Candidate.Actions) {
        if ((Task-Xml (Read-TaskXml $row.Name)) -cne (Task-Xml $row.BeforeXml) -or (Task-State $row.Name) -eq 'Running') {throw 'Baseline drift or running task; no mutation'}
    }
    $journal=[ordered]@{schema=1;scope=$(if($StageOnly){'windows_disabled_task_stage'}else{'offline_seven_task_transaction'});status='prepared';touched=@();rollback_errors=@();
        plan_sha256=$Candidate.InputHash;installer_sha256=(Get-FileHash -LiteralPath $script:CurrentInstaller -Algorithm SHA256).Hash}
    Save-Transaction $journal $JournalPath
    try {
        foreach ($row in $Candidate.Actions) {
            if ($row.Disposition -eq 'preserve_disabled') {continue}
            & $CheckWindow
            if ((Task-Xml (Read-TaskXml $row.Name)) -cne (Task-Xml $row.BeforeXml) -or (Task-State $row.Name) -eq 'Running') {throw 'Task drift before replacement'}
            $journal.touched+=($row.Name);Save-Transaction $journal $JournalPath
            $disabled=Disabled-Xml $row.AfterXml
            Write-TaskXml $row.Name $disabled $Supplied
            if ((Task-Xml (Read-TaskXml $row.Name)) -cne (Task-Xml $disabled)) {throw 'Staged XML differs'}
        }
        if ($StageOnly) {$journal.status='staged_disabled_not_production';Save-Transaction $journal $JournalPath;return $journal}
        # Every action/principal is installed disabled before any trigger is enabled.
        foreach ($row in $Candidate.Actions) {
            if ($row.Disposition -eq 'preserve_disabled') {continue}
            & $CheckWindow
            if ((Task-Xml (Read-TaskXml $row.Name)) -cne (Task-Xml (Disabled-Xml $row.AfterXml)) -or (Task-State $row.Name) -eq 'Running') {throw 'Staged task drift before activation'}
            Write-TaskXml $row.Name $row.AfterXml $Supplied
            if ((Task-Xml (Read-TaskXml $row.Name)) -cne (Task-Xml $row.AfterXml)) {throw 'Activated XML differs'}
        }
        $journal.status='offline_installed_not_windows_acceptance';Save-Transaction $journal $JournalPath
    } catch {
        $journal.failure_type=$_.Exception.GetType().Name;Save-Transaction $journal $JournalPath
        Restore-Tasks $Candidate $journal $JournalPath $Supplied
        throw 'Task installation failed and was rolled back'
    }
    return $journal
}

function Invoke-OfflineRehearsal($Candidate,[string]$Directory) {
    if (Test-Path -LiteralPath $Directory) {throw 'Fresh offline output directory required'}
    [void](New-Item -ItemType Directory -Path $Directory)
    $results=@()
    $activeCount=@($Candidate.Actions | Where-Object Disposition -ne 'preserve_disabled').Count
    foreach ($scenario in @('roundtrip','stage_failure','activation_failure','window_expiry','rollback_auth_failure','independent_drift','running_before','missing_credential')) {
        $case=[ordered]@{xml=@{};writes=0;checks=0;password_writes=0;failed=$false;rolling_back=$false}
        foreach ($row in $Candidate.Actions) {$case.xml[$row.Name]=$row.BeforeXml}
        $credential=[pscredential]::new('SYNTHETIC\fixture',(ConvertTo-SecureString 'fixture-not-real' -AsPlainText -Force))
        function Read-TaskXml([string]$Name) {return $case.xml[$Name]}
        function Task-State([string]$Name) {if ($scenario -eq 'running_before' -and $Name -eq 'StockData-Intraday') {return 'Running'};return 'Ready'}
        function Write-TaskXml([string]$Name,[string]$Text,$Supplied) {
            if (-not [object]::ReferenceEquals($credential,$Supplied)) {throw 'Credential lost during update or rollback'}
            if ($Name -eq 'StockData-MonthlyCompact') {throw 'Monthly task touched'}
            $saved=[IO.File]::ReadAllText($journalPath) | ConvertFrom-Json
            if ($Name -notin $saved.touched) {throw 'Missing write-ahead record'}
            $case.writes++
            if (([xml]$Text).Task.Principals.Principal.LogonType -eq 'Password') {
                $case.password_writes++
            }
            if ($scenario -eq 'rollback_auth_failure' -and $case.rolling_back) {throw 'Synthetic rollback authentication rejection'}
            $case.xml[$Name]=$Text
            $failAt=if ($scenario -eq 'stage_failure') {3} elseif ($scenario -in @('activation_failure','rollback_auth_failure','independent_drift')) {$activeCount+2} else {-1}
            if (-not $case.failed -and $case.writes -eq $failAt) {
                $case.failed=$true;$case.rolling_back=$true
                if ($scenario -eq 'independent_drift') {$case.xml[$Name]=$Text.Replace('</Task>','<Data>independent</Data></Task>')}
                throw 'Synthetic failure after mutation'
            }
        }
        $journalPath=Join-Path $Directory ($scenario+'.json')
        $caught=$false
        try {
            $supplied=if ($scenario -eq 'missing_credential') {$null} else {$credential}
            $journal=Install-Tasks $Candidate $journalPath $supplied -StageOnly:($scenario -eq 'roundtrip') -CheckWindow {
                $case.checks++;if ($scenario -eq 'window_expiry' -and $case.checks -eq 4) {throw 'Synthetic window expired'}
            }
            if ($scenario -eq 'roundtrip') {if (@($Candidate.Actions | Where-Object Disposition -ne 'preserve_disabled' | Where-Object {([xml]$case.xml[$_.Name]).Task.Settings.Enabled -ne 'false'}).Count) {throw 'Staging enabled a task'};Restore-Tasks $Candidate $journal $journalPath $credential}
        } catch {$caught=$true}
        $restored=@($Candidate.Actions | Where-Object {(Task-Xml $case.xml[$_.Name]) -cne (Task-Xml $_.BeforeXml)}).Count -eq 0
        $state=if (Test-Path -LiteralPath $journalPath) {([IO.File]::ReadAllText($journalPath) | ConvertFrom-Json).status} else {'not_started'}
        $incomplete=$scenario -in @('rollback_auth_failure','independent_drift')
        $expectedState=if ($incomplete) {'rollback_incomplete'} elseif ($scenario -in @('running_before','missing_credential')) {'not_started'} else {'rolled_back'}
        if ($state -ne $expectedState -or $caught -ne ($scenario -ne 'roundtrip') -or $restored -eq $incomplete -or
            $case.xml['StockData-MonthlyCompact'] -cne @($Candidate.Actions | Where-Object Name -eq 'StockData-MonthlyCompact')[0].BeforeXml) {throw ('Offline scenario failed: '+$scenario)}
        $results+=@{scenario=$scenario;passed=$true;status=$state;fixture_writes=$case.writes;password_parameter_fixture_writes=$case.password_writes}
    }
    $receipt=[ordered]@{schema=1;scope='offline_task_transaction_fault_injection';plan_sha256=$Candidate.InputHash;
        installer_sha256=(Get-FileHash -LiteralPath $script:CurrentInstaller -Algorithm SHA256).Hash;
        passed=$true;real_authentication_verified=$false;windows_task_changes=0;account_changes=0;acl_changes=0;production_cutover=$false;scenarios=$results}
    Save-Transaction $receipt (Join-Path $Directory 'receipt.json')
    return $receipt
}
if ($Mode -eq 'Library') {return}
if ($Mode -eq 'Apply') {throw 'LIVE_DISABLED: activation requires completed production data, account and recovery handover. Stage preserves disabled tasks only.'}
if ($Mode -in @('Stage','Rollback')) {
    if (-not $Plan -or -not $OutputDirectory -or -not ([Security.Principal.WindowsPrincipal]::new([Security.Principal.WindowsIdentity]::GetCurrent())).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {throw 'LIVE_DISABLED: explicit plan, transaction directory and elevated local session required; no system changes'}
    Assert-WindowBounds $WindowStart $WindowEnd
}
function Assert-SealedFiles([string]$ManifestPath,[string]$ExpectedHash,[string]$Root) {
    if ($ExpectedHash -notmatch '^[a-fA-F0-9]{64}$' -or (Get-FileHash -LiteralPath $ManifestPath -Algorithm SHA256).Hash -ne $ExpectedHash) {throw 'Release manifest changed'}
    $manifest=[IO.File]::ReadAllText($ManifestPath) | ConvertFrom-Json
    if (-not $Root) {$Root=[string]$manifest.source_root}
    $rootPath=[IO.Path]::GetFullPath($Root).TrimEnd('\')+'\'
    if (-not @($manifest.files.PSObject.Properties).Count) {throw 'Empty release manifest'}
    foreach ($entry in $manifest.files.PSObject.Properties) {
        $path=[IO.Path]::GetFullPath((Join-Path $rootPath $entry.Name))
        if (-not $path.StartsWith($rootPath,[StringComparison]::OrdinalIgnoreCase) -or
            (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash -ne $entry.Value) {throw 'Release member changed or escaped root'}
    }
}
if ($PlanSha256 -notmatch '^[a-fA-F0-9]{64}$' -or (Get-FileHash -LiteralPath $Plan -Algorithm SHA256).Hash -ne $PlanSha256) {throw 'Explicit matching engineering plan SHA256 required'}
$raw=[IO.File]::ReadAllText($Plan)
# PowerShell 7.5+ otherwise silently coerces ISO strings to DateTime; preserve
# the exact explicitly proposed offset. Windows PowerShell 5.1 keeps strings.
if ((Get-Command ConvertFrom-Json).Parameters.ContainsKey('DateKind')) {$candidate=$raw | ConvertFrom-Json -DateKind String}
elseif ($PSVersionTable.PSVersion.Major -eq 5) {$candidate=$raw | ConvertFrom-Json}
else {throw 'Use Windows PowerShell 5.1 or PowerShell 7.5+ for exact JSON date preservation'}
$candidate | Add-Member NoteProperty InputHash $PlanSha256
Assert-EngineeringPlan $candidate
if ($Mode -eq 'Check') {Write-Output 'ENGINEERING_XML_CHECK_PASS: changes=0; not live baseline or deployment acceptance.';exit 0}
if ($Mode -in @('Stage','Rollback')) {
    $live=@(Get-ScheduledTask | Where-Object TaskName -Like 'StockData-*')
    if (@($live | Where-Object {$_.TaskPath -ne '\' -or $_.State -ne 'Disabled'}).Count -or
        @(Compare-Object @($live.TaskName | Sort-Object) @($candidate.Actions.Name | Sort-Object)).Count) {throw 'Live task inventory differs or is not fully paused'}
    foreach ($row in $candidate.Actions | Where-Object Disposition -ne 'preserve_disabled') {
        foreach ($xml in @($row.BeforeXml,(Disabled-Xml $row.AfterXml))) {$identity=Task-Identity $xml $Credential;$identity.Clear()}
    }
    $journalPath=Join-Path $OutputDirectory 'transaction.json'
    if ($Mode -eq 'Rollback') {
        $journal=[IO.File]::ReadAllText($journalPath) | ConvertFrom-Json
        if ($journal.scope -ne 'windows_disabled_task_stage') {throw 'Only a captured disabled staging transaction can be restored'}
        Restore-Tasks $candidate $journal $journalPath $Credential
    } else {
        Assert-SealedFiles $candidate.CollectorContract $candidate.CollectionContractSha256 ''
        Assert-SealedFiles (Join-Path $candidate.ResearchReleaseDirectory 'research-release.json') $candidate.ResearchManifestSha256 $candidate.ResearchReleaseDirectory
        foreach ($entry in $candidate.AdapterFiles.PSObject.Properties) {
            $path=[IO.Path]::GetFullPath((Join-Path (Split-Path $PSScriptRoot -Parent) $entry.Name))
            if (-not $path.StartsWith((Split-Path $PSScriptRoot -Parent)+'\',[StringComparison]::OrdinalIgnoreCase) -or
                (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash -ne $entry.Value) {throw 'Candidate adapter source changed'}
        }
        if (Test-Path -LiteralPath $OutputDirectory) {throw 'Fresh transaction directory required'}
        [void](New-Item -ItemType Directory -Path $OutputDirectory)
        $null=Install-Tasks $candidate $journalPath $Credential -StageOnly -CheckWindow {Assert-WindowBounds $WindowStart $WindowEnd}
    }
    Write-Output 'WINDOWS_DISABLED_TRANSACTION_COMPLETE: production_cutover=false; no task started or enabled.';exit 0
}
if (-not $OutputDirectory) {throw 'Explicit fresh offline output directory required'}
$result=Invoke-OfflineRehearsal $candidate $OutputDirectory
Write-Output ('OFFLINE_REHEARSAL_COMPLETE: '+$result.scenarios.Count+' scenarios; real_authentication_verified=false; system changes=0.')
