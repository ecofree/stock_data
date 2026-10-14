# Explicit engineering provisioning; does not receive materials or import reviews.
[CmdletBinding()]
param(
 [Parameter(Mandatory=$true)][string]$RuntimeRoot,
 [Parameter(Mandatory=$true)][string]$StateSha256,
 [Parameter(Mandatory=$true)][string]$EvidenceDirectory,
 [Parameter(Mandatory=$true)][string]$WindowStart,
 [Parameter(Mandatory=$true)][string]$WindowEnd
)
$ErrorActionPreference='Stop'
if(-not ([Security.Principal.WindowsPrincipal]::new([Security.Principal.WindowsIdentity]::GetCurrent())).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)){throw 'Elevated explicit provisioning required'}
$begin=[DateTimeOffset]::Parse($WindowStart);$end=[DateTimeOffset]::Parse($WindowEnd)
if($begin.Offset -ne [TimeSpan]::FromHours(8) -or $end.Offset -ne [TimeSpan]::FromHours(8) -or ($end-$begin).TotalMinutes -le 0 -or ($end-$begin).TotalMinutes -gt 60 -or [DateTimeOffset]::Now -lt $begin -or [DateTimeOffset]::Now -ge $end){throw 'New fixed China-offset maintenance window required'}
$root=[IO.Path]::GetFullPath($RuntimeRoot).TrimEnd('\');$statePath=Join-Path $root 'state\current.json'
if((Get-FileHash -LiteralPath $statePath -Algorithm SHA256).Hash -ne $StateSha256){throw 'Exact current state SHA required'}
$state=[IO.File]::ReadAllText($statePath)|ConvertFrom-Json
if($state.runtime_root -ne $root -or $state.status -ne 'paused' -or $state.activation_accepted){throw 'Paused exact runtime binding required'}
$expected=@('StockData-Auction','StockData-DailyClose','StockData-Intraday','StockData-ResearchDaily','StockData-SupplementalRetry');$tasks=@(Get-ScheduledTask|Where-Object TaskName -like 'StockData-*')
if($tasks.Count -ne 5 -or @(Compare-Object @($tasks.TaskName|Sort-Object) $expected).Count -or @($tasks|Where-Object {$_.State -ne 'Disabled' -or $_.Settings.Enabled}).Count){throw 'Exact five-task production pause required'}
if(Test-Path -LiteralPath $EvidenceDirectory){throw 'New provisioning evidence required'}
$intake=Join-Path $root 'workspace\valuation-intake';$accountInbox=Join-Path $root 'workspace\acceptance-inputs'
if((Test-Path -LiteralPath $intake) -or (Test-Path -LiteralPath $accountInbox)){throw 'Existing intake directories require inspection; never reset or overwrite them'}
$evidence=Join-Path $root 'evidence';$objects=Join-Path $evidence 'objects';$workspace=Join-Path $root 'workspace'
foreach($path in @($root,$workspace,$evidence,$objects)){if((Get-Item -LiteralPath $path -Force).Attributes -band [IO.FileAttributes]::ReparsePoint){throw 'Linked runtime root rejected'}}
if(@(Get-ChildItem -LiteralPath $evidence -Recurse -Force -Attributes ReparsePoint).Count){throw 'Linked evidence member rejected'}
$files=@(Get-ChildItem -LiteralPath $evidence -Recurse -Force -File|ForEach-Object {[pscustomobject]@{Path=$_.FullName;Bytes=$_.Length;LastWriteTicks=$_.LastWriteTimeUtc.Ticks}})
$unchanged=@{};foreach($path in @($statePath,(Join-Path $root 'market\kpl_data.duckdb'),(Join-Path $root 'config\collector.env'),(Join-Path $root 'state\acquisition\resilient.db'),$workspace)){$unchanged[$path]=(Get-Acl -LiteralPath $path).Sddl}
[void](New-Item -ItemType Directory -Path $EvidenceDirectory)
$owner=[Security.Principal.WindowsIdentity]::GetCurrent().User;$publisher='S-1-5-21-3027070730-734606845-1610825463-1006'
function Controlled-Acl([bool]$PublicRead){
 $acl=[Security.AccessControl.DirectorySecurity]::new();$acl.SetOwner($owner);$acl.SetAccessRuleProtection($true,$false)
 foreach($sid in @($owner.Value,'S-1-5-18','S-1-5-32-544')){$acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new([Security.Principal.SecurityIdentifier]::new($sid),'FullControl','ContainerInherit,ObjectInherit','None','Allow'))}
 if($PublicRead){$acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new([Security.Principal.SecurityIdentifier]::new($publisher),'ReadAndExecute','ContainerInherit,ObjectInherit','None','Allow'))}
 return $acl
}
Set-Acl -LiteralPath $EvidenceDirectory -AclObject (Controlled-Acl $false)
& icacls.exe $evidence /save (Join-Path $EvidenceDirectory 'evidence-acls-before.txt') /t /q > (Join-Path $EvidenceDirectory 'acl-save.log');if($LASTEXITCODE -ne 0){throw 'Original evidence ACL backup failed'}
[IO.File]::WriteAllText((Join-Path $EvidenceDirectory 'before.json'),(@{at=[DateTimeOffset]::Now.ToString('o');evidence_root_sddl=(Get-Acl -LiteralPath $evidence).Sddl;objects_root_sddl=(Get-Acl -LiteralPath $objects).Sddl;source_files=$files;protected_other_objects=$unchanged;window_start=$WindowStart;window_end=$WindowEnd}|ConvertTo-Json -Depth 10),[Text.UTF8Encoding]::new($false))
Set-Acl -LiteralPath $evidence -AclObject (Controlled-Acl $true)
& icacls.exe (Join-Path $evidence '*') /reset /t /q > (Join-Path $EvidenceDirectory 'descendant-reset.log');if($LASTEXITCODE -ne 0){throw 'Evidence descendant protection failed'}
foreach($path in @($intake,$accountInbox)){[void](New-Item -ItemType Directory -Path $path);Set-Acl -LiteralPath $path -AclObject (Controlled-Acl $false)}
[void](New-Item -ItemType Directory -Path (Join-Path $intake '.staging'))
foreach($row in $files){$now=Get-Item -LiteralPath $row.Path;if($now.Length -ne $row.Bytes -or $now.LastWriteTimeUtc.Ticks -ne $row.LastWriteTicks){throw 'Evidence file content metadata changed'}}
foreach($path in $unchanged.Keys){if((Get-Acl -LiteralPath $path).Sddl -ne $unchanged[$path]){throw 'Unrelated production object ACL changed'}}
[IO.File]::WriteAllText((Join-Path $EvidenceDirectory 'provisioning.json'),(@{at=[DateTimeOffset]::Now.ToString('o');passed=$true;intake=$intake;private_account_inbox=$accountInbox;evidence=$evidence;source_file_count=$files.Count;file_sizes_and_mtimes_preserved=$true;business_database_writes=0;task_changes=0;new_financial_qualification=$false;new_materials_received=0;real_publisher_probe_pending=$true;full_original_sha_readback_pending=$true;system_is_privileged_not_claimed_unable_to_write=$true;after=@{evidence=(Get-Acl -LiteralPath $evidence).Sddl;intake=(Get-Acl -LiteralPath $intake).Sddl;account_inbox=(Get-Acl -LiteralPath $accountInbox).Sddl}}|ConvertTo-Json -Depth 10),[Text.UTF8Encoding]::new($false))
