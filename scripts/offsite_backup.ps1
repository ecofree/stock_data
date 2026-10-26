# Retired: this entrypoint cannot establish qualified, restorable backup identity.
# Keep a failing stub so old callers cannot copy a live database or unqualified gzip.
param(
    [Parameter(Mandatory = $true)]
    [string]$Destination,
    [switch]$Rclone,
    [switch]$Execute,
    [switch]$IncludeMainDb
)
[Console]::Error.WriteLine("RETIRED: legacy offsite backup is disabled. No files were read or copied. A qualified backup/restore and transfer workflow is required.")
exit 2
