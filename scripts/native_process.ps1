# Capture the actual process exit code independently of PowerShell error streams.
# A provider logging INFO to stderr must not turn exit 0 into a failed task.
function Invoke-StockDataProcess {
    param([string]$Executable, [string[]]$Arguments, [string]$WorkingDirectory)
    $start = New-Object System.Diagnostics.ProcessStartInfo
    $start.FileName = $Executable
    $start.WorkingDirectory = $WorkingDirectory
    $start.UseShellExecute = $false
    $start.CreateNoWindow = $true
    $start.RedirectStandardOutput = $true
    $start.RedirectStandardError = $true
    $start.StandardOutputEncoding = New-Object System.Text.UTF8Encoding($false)
    $start.StandardErrorEncoding = New-Object System.Text.UTF8Encoding($false)
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
        $process.WaitForExit()
        [pscustomobject]@{ ExitCode=$process.ExitCode; Stdout=$stdout.Result; Stderr=$stderr.Result }
    } finally {
        $process.Dispose()
    }
}
