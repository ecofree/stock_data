# Current seven-task transaction ENGINEERING ONLY. No live mutation backend.
[CmdletBinding()]
param(
    [ValidateSet('Check','Rehearse','Apply','Rollback','Library')][string]$Mode='Check',
    [string]$Plan, [string]$PlanSha256, [string]$OutputDirectory
)
$ErrorActionPreference='Stop'
$script:CurrentInstaller=$PSCommandPath
$script:TaskNames=@('StockData-Auction','StockData-Intraday','StockData-DailyClose','StockData-SupplementalRetry','StockData-QLibResearch','StockData-ResearchDaily','StockData-MonthlyCompact')

function Task-Xml([string]$Text) {
    $xml=[xml]$Text
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
function Read-TaskXml([string]$Name) {throw 'Live backend not authorized or implemented'}
function Write-TaskXml([string]$Name,[string]$Text,$Supplied) {throw 'Live backend not authorized or implemented'}
function Task-State([string]$Name) {throw 'Live backend not authorized or implemented'}
function Assert-WindowBounds([string]$Start,[string]$End,[DateTimeOffset]$Now=[DateTimeOffset]::UtcNow) {
    if ($Start -notmatch '\+08:00$' -or $End -notmatch '\+08:00$') {throw 'Explicit China offsets required'}
    $begin=[DateTimeOffset]::Parse($Start);$finish=[DateTimeOffset]::Parse($End)
    if ($begin.Offset -ne [TimeSpan]::FromHours(8) -or $finish.Offset -ne [TimeSpan]::FromHours(8) -or
        ($finish-$begin).TotalMinutes -le 0 -or ($finish-$begin).TotalMinutes -gt 60 -or $Now -lt $begin -or $Now -ge $finish) {throw 'Outside explicit new China maintenance window; no renewal permitted'}
}
function Assert-EngineeringPlan($Candidate) {
    if ($Candidate.Schema -ne 3 -or $Candidate.Scope -ne 'task_handover_engineering_only' -or $Candidate.Version -ne '0.3.15' -or
        $Candidate.ProductionCutover -ne $false -or @($Candidate.Actions).Count -ne 7 -or
        @($Candidate.Actions.Name | Select-Object -Unique).Count -ne 7 -or
        @(Compare-Object $script:TaskNames @($Candidate.Actions.Name)).Count) {throw 'Complete engineering-only seven-task plan required'}
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
    if (-not $Supplied) {throw 'Explicit credential required; never reset or infer a password'}
    if ($Journal.scope -ne 'offline_seven_task_transaction' -or $Journal.plan_sha256 -ne $Candidate.InputHash -or
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
function Install-Tasks($Candidate,[string]$JournalPath,$Supplied,[scriptblock]$CheckWindow) {
    Assert-EngineeringPlan $Candidate
    if (-not $Supplied) {throw 'Explicit credential required before any change; never reset or infer a password'}
    if (Test-Path -LiteralPath $JournalPath) {throw 'Transaction exists; inspect rather than overwrite or retry'}
    & $CheckWindow
    foreach ($row in $Candidate.Actions) {
        if ((Task-Xml (Read-TaskXml $row.Name)) -cne (Task-Xml $row.BeforeXml) -or (Task-State $row.Name) -eq 'Running') {throw 'Baseline drift or running task; no mutation'}
    }
    $journal=[ordered]@{schema=1;scope='offline_seven_task_transaction';status='prepared';touched=@();rollback_errors=@();
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
        throw 'Offline installation failed and was rolled back'
    }
    return $journal
}

function Invoke-OfflineRehearsal($Candidate,[string]$Directory) {
    if (Test-Path -LiteralPath $Directory) {throw 'Fresh offline output directory required'}
    [void](New-Item -ItemType Directory -Path $Directory)
    $results=@()
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
                if ($scenario -eq 'rollback_auth_failure' -and $case.rolling_back) {throw 'Synthetic rollback authentication rejection'}
            }
            $case.xml[$Name]=$Text
            $failAt=if ($scenario -eq 'stage_failure') {3} elseif ($scenario -in @('activation_failure','rollback_auth_failure','independent_drift')) {8} else {-1}
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
            $journal=Install-Tasks $Candidate $journalPath $supplied {
                $case.checks++;if ($scenario -eq 'window_expiry' -and $case.checks -eq 4) {throw 'Synthetic window expired'}
            }
            if ($scenario -eq 'roundtrip') {Restore-Tasks $Candidate $journal $journalPath $credential}
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
if ($Mode -in @('Apply','Rollback')) {throw 'LIVE_DISABLED: engineering preparation only; unknown retained password, protected staging, real authentication/rollback and a separately approved new window are unresolved. No system changes.'}
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
if (-not $OutputDirectory) {throw 'Explicit fresh offline output directory required'}
$result=Invoke-OfflineRehearsal $candidate $OutputDirectory
Write-Output ('OFFLINE_REHEARSAL_COMPLETE: '+$result.scenarios.Count+' scenarios; real_authentication_verified=false; system changes=0.')
