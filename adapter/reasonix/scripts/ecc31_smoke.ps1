# ECC V3.1 Reasonix shadow adapter - PowerShell entry point.
# Compatible with Windows PowerShell 5.1 and pwsh 7.
# Forwards arguments to ecc31_smoke.py and propagates its exit code
# unchanged via $LASTEXITCODE.
[CmdletBinding()]
param(
    [string]$TestRoot = '',
    [int]$Timeout = 0
)

$ErrorActionPreference = 'Stop'

function Get-Ecc31Python {
    foreach ($candidate in @('python', 'py')) {
        $cmd = Get-Command $candidate -ErrorAction SilentlyContinue
        if ($null -ne $cmd) { return $cmd.Source }
    }
    return $null
}

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$pyScript  = Join-Path $scriptDir 'ecc31_smoke.py'

$python = Get-Ecc31Python
if ($null -eq $python) {
    [Console]::Error.WriteLine('[ECC31] AdapterStartupError: python interpreter not found (fail closed, exit 3)')
    exit 3
}

$pyArgs = @($pyScript)
if ($TestRoot -ne '') {
    $pyArgs += @('--test-root', $TestRoot)
}
if ($Timeout -gt 0) {
    $pyArgs += @('--timeout', [string]$Timeout)
}

& $python @pyArgs
$exitCode = $LASTEXITCODE
exit $exitCode
