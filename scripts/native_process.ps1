# Capture the actual process exit code independently of PowerShell error streams.
# A provider logging INFO to stderr must not turn exit 0 into a failed task.
function Invoke-StockDataProcess {
    param([string]$Executable, [string[]]$Arguments, [string]$WorkingDirectory,
          [ValidateRange(1,3600)][int]$TimeoutSeconds=3600,
          [double]$DeadlineEpoch=0,
          [ValidateRange(0,600)][int]$DrainGraceSeconds=30,
          [ValidateRange(50,5000)][int]$PollMilliseconds=1000,
          [string]$ProgressPath='')
    $nowEpoch=[DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()/1000.0
    $effectiveDeadline=$nowEpoch+$TimeoutSeconds
    foreach ($value in @($DeadlineEpoch,$env:STOCKDATA_PHASE_DEADLINE_EPOCH,$env:STOCKDATA_OBSERVATION_WINDOW_DEADLINE_EPOCH)) {
        if ($null -eq $value -or [string]$value -eq '' -or [string]$value -eq '0') {continue}
        $parsed=0.0
        if (-not [double]::TryParse([string]$value,[Globalization.NumberStyles]::Float,[Globalization.CultureInfo]::InvariantCulture,[ref]$parsed) -or
            [double]::IsNaN($parsed) -or [double]::IsInfinity($parsed)) {throw 'Finite cooperative process deadline required'}
        $effectiveDeadline=[Math]::Min($effectiveDeadline,$parsed)
    }
    if ($effectiveDeadline -le $nowEpoch) {throw 'Cooperative process deadline exhausted; no child started'}
    function Write-ProcessProgress($State) {
        if ($ProgressPath) {
            try {
                $target=[IO.Path]::GetFullPath($ProgressPath)
                $temp=$target+'.'+[Guid]::NewGuid().ToString('N')+'.partial'
                [IO.File]::WriteAllText($temp,($State|ConvertTo-Json -Compress),[Text.UTF8Encoding]::new($false))
                if ([IO.File]::Exists($target)) {[IO.File]::Replace($temp,$target,[Management.Automation.Language.NullString]::Value)} else {[IO.File]::Move($temp,$target)}
            } catch {
                [Console]::Error.WriteLine('PROCESS_PROGRESS_WRITE_FAILED; owner continues waiting safely')
                # Only this unique attempted evidence file may be cleaned. No
                # guard, owner metadata, or previous receipt is ever removed.
                if ($temp -and $target -and $temp.StartsWith($target+'.',[StringComparison]::OrdinalIgnoreCase) -and [IO.File]::Exists($temp)) {
                    try {[IO.File]::Delete($temp)} catch {}
                }
            }
        }
    }
    $start = New-Object System.Diagnostics.ProcessStartInfo
    $start.FileName = $Executable
    $start.WorkingDirectory = $WorkingDirectory
    $start.UseShellExecute = $false
    $start.CreateNoWindow = $true
    $start.RedirectStandardOutput = $true
    $start.RedirectStandardError = $true
    $start.StandardOutputEncoding = New-Object System.Text.UTF8Encoding($false)
    $start.StandardErrorEncoding = New-Object System.Text.UTF8Encoding($false)
    # This stops new work only for children that consume the shared deadline.
    # It is never a promise that blocked native/local I/O can be interrupted.
    $start.EnvironmentVariables['STOCKDATA_PHASE_DEADLINE_EPOCH']=$effectiveDeadline.ToString('R',[Globalization.CultureInfo]::InvariantCulture)
    $quoted = foreach ($argument in $Arguments) {
        if ($argument.Contains([char]0)) { throw 'NUL in process argument' }
        $escaped = [regex]::Replace($argument, '(\\*)"', '$1$1\"')
        $escaped = [regex]::Replace($escaped, '(\\+)$', '$1$1')
        '"' + $escaped + '"'
    }
    $start.Arguments = $quoted -join ' '
    $process = New-Object System.Diagnostics.Process
    $process.StartInfo = $start
    try {
        if (-not $process.Start()) { throw 'Process did not start' }
        $stdout = $process.StandardOutput.ReadToEndAsync()
        $stderr = $process.StandardError.ReadToEndAsync()
        $expired=$false; $maintenance=$false; $drainStarted=$null; $priorState=''
        do {
            $nowEpoch=[DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()/1000.0
            if (-not $expired -and $nowEpoch -ge $effectiveDeadline) {$expired=$true;$drainStarted=$nowEpoch}
            $elapsed=if($expired){$nowEpoch-$drainStarted}else{0}
            if ($expired -and $elapsed -ge $DrainGraceSeconds) {$maintenance=$true}
            $state=[ordered]@{pid=$process.Id;status=$(if($maintenance){'maintenance_blocked'}elseif($expired){'draining'}else{'running'});
                observed_at=[DateTimeOffset]::UtcNow.ToString('o');deadline_epoch=$effectiveDeadline;
                deadline_exceeded=$expired;drain_elapsed_seconds=[Math]::Round($elapsed,3);
                new_work_permitted=(-not $expired);exit_not_guaranteed=$true;child_exited=$false;
                manual_maintenance_required=$maintenance}
            Write-ProcessProgress $state
            if ($state.status -ne $priorState -and $expired) {
                [Console]::Error.WriteLine(('PROCESS_{0} pid={1}; no termination; owner/guard not cleared' -f $state.status.ToUpper(),$process.Id))
            }
            $priorState=$state.status
            # Bounded polling makes a never-draining child visible; it does not
            # release the supervising writer or manufacture a completed result.
            $exited=$process.WaitForExit($PollMilliseconds)
        } while (-not $exited)
        $state.status=if($expired){'drained_after_deadline'}else{'completed'}
        $state.child_exited=$true; $state.native_exit_code=$process.ExitCode
        Write-ProcessProgress $state
        [pscustomobject]@{ ExitCode=$(if($expired){-1}else{$process.ExitCode}); NativeExitCode=$process.ExitCode;
            Stdout=$stdout.Result; Stderr=$stderr.Result; DeadlineExceeded=$expired;
            MaintenanceBlockedSeen=$maintenance; DrainResolved=$true }
    } finally {
        $process.Dispose()
    }
}
