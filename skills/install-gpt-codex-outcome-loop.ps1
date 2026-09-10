#Requires -Version 7.0

[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$remoteBase = 'https://raw.githubusercontent.com/cuiqinggang/ecc-v3.1.1/main/skills/gpt-codex-outcome-loop'
$skillRoot = Join-Path $env:USERPROFILE '.codex\skills'
$skillDestination = Join-Path $skillRoot 'gpt-codex-outcome-loop'
$backupRoot = Join-Path $skillRoot '_backups'
$timestamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$backupDestination = Join-Path $backupRoot "gpt-codex-outcome-loop-$timestamp"
$temporaryRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("gpt-codex-outcome-loop-" + [guid]::NewGuid().ToString('N'))
$sourceSkill = Join-Path $temporaryRoot 'gpt-codex-outcome-loop'
$sourceReferences = Join-Path $sourceSkill 'references'
$backupCreated = $false

$files = @(
    @{ RelativePath = 'SKILL.md'; Sha256 = '0F8A1EC3EFD2EFAB205347408F2AEA2503D943FE71903162854F6087C13BEAE7' },
    @{ RelativePath = 'references/skill-value-map.md'; Sha256 = '90BD8FF76F383EC33507D7AA854A579DEF67D6C9656E86687325E1BCDD1B5A28' },
    @{ RelativePath = 'references/playwright-gpt-bridge.md'; Sha256 = '515A0C1BDA39003CE8C64F850E3BD715A295D3EBD5EB7786CEEDE050EBE70772' }
)

try {
    New-Item -ItemType Directory -Path $temporaryRoot, $sourceSkill, $sourceReferences, $skillRoot, $backupRoot -Force | Out-Null
    foreach ($file in $files) {
        $localRelativePath = $file.RelativePath.Replace('/', [System.IO.Path]::DirectorySeparatorChar)
        $downloadPath = Join-Path $sourceSkill $localRelativePath
        Invoke-WebRequest -Uri "$remoteBase/$($file.RelativePath)" -OutFile $downloadPath
        $actualSha256 = (Get-FileHash -LiteralPath $downloadPath -Algorithm SHA256).Hash
        if ($actualSha256 -ne $file.Sha256) {
            throw "SHA-256 mismatch for $($file.RelativePath). Expected $($file.Sha256), got $actualSha256."
        }
    }

    $sourceSkillFile = Join-Path $sourceSkill 'SKILL.md'
    if (-not (Test-Path -LiteralPath $sourceSkillFile -PathType Leaf)) {
        throw 'Package does not contain gpt-codex-outcome-loop\SKILL.md.'
    }

    if (Test-Path -LiteralPath $skillDestination) {
        Move-Item -LiteralPath $skillDestination -Destination $backupDestination
        $backupCreated = $true
    }

    Copy-Item -LiteralPath $sourceSkill -Destination $skillDestination -Recurse

    $installedSkillFile = Join-Path $skillDestination 'SKILL.md'
    $installedBridgeFile = Join-Path $skillDestination 'references\playwright-gpt-bridge.md'
    if (-not (Test-Path -LiteralPath $installedSkillFile -PathType Leaf) -or
        -not (Test-Path -LiteralPath $installedBridgeFile -PathType Leaf)) {
        throw 'Installed skill failed read-back verification.'
    }

    Write-Output 'DEPLOYMENT=PASS'
    Write-Output "SKILL_PATH=$installedSkillFile"
    Write-Output 'VERSION=1.1.0'
    Write-Output 'GPT_BRIDGE=playwright-native-ai-browser'
    if ($backupCreated) {
        Write-Output "BACKUP_PATH=$backupDestination"
    }
}
catch {
    if ($backupCreated) {
        if (Test-Path -LiteralPath $skillDestination) {
            Remove-Item -LiteralPath $skillDestination -Recurse -Force
        }
        Move-Item -LiteralPath $backupDestination -Destination $skillDestination
    }
    throw
}
finally {
    if (Test-Path -LiteralPath $temporaryRoot) {
        Remove-Item -LiteralPath $temporaryRoot -Recurse -Force
    }
}
