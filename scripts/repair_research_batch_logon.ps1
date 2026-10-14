# The 20260917 one-off account preparation is historical, not a reusable repair.
# Original source and receipts remain in Git and the private recovery archive.
[CmdletBinding()]
param([string]$Mode='Check')
$ErrorActionPreference='Stop'
throw 'RETIRED_ENTRYPOINT: historical account preparation cannot be replayed. No account, password, policy, ACL or task changes were made. Use a newly reviewed fixed-scope identity probe; do not reset an account or infer a password.'
