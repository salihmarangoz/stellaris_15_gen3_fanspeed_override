$ErrorActionPreference = 'Stop'

$commit = '75e0106f1af4fbdc0cb5d95ca32dc15f8ab070d7'
$expectedHash = 'DAE74615761B78BDF064DFB3E136252DDCC6FC727D88F14738D0E5800D427A91'
$moduleDirectory = Join-Path $PSScriptRoot 'third_party\pawnio'
$modulePath = Join-Path $moduleDirectory 'AMDFamily17.bin'
$moduleUrl = "https://raw.githubusercontent.com/LibreHardwareMonitor/LibreHardwareMonitor/$commit/LibreHardwareMonitorLib/Resources/PawnIo/AMDFamily17.bin"

$pawnIo = Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\PawnIO' -ErrorAction SilentlyContinue
if ($null -eq $pawnIo) {
    Write-Host 'Installing the signed PawnIO driver. Approve the Windows administrator prompt.'
    winget install --id namazso.PawnIO --exact --accept-package-agreements --accept-source-agreements
    if ($LASTEXITCODE -ne 0) {
        throw "PawnIO installation failed with exit code $LASTEXITCODE"
    }
}
elseif ([version]$pawnIo.DisplayVersion -lt [version]'2.2.0.0') {
    Write-Host 'Updating PawnIO to version 2.2 or newer. Approve the Windows administrator prompt.'
    winget upgrade --id namazso.PawnIO --exact --accept-package-agreements --accept-source-agreements
    if ($LASTEXITCODE -ne 0) {
        throw "PawnIO update failed with exit code $LASTEXITCODE"
    }
}

New-Item -ItemType Directory -Path $moduleDirectory -Force | Out-Null
$download = $true
if (Test-Path -LiteralPath $modulePath) {
    $download = (Get-FileHash -LiteralPath $modulePath -Algorithm SHA256).Hash -ne $expectedHash
}
if ($download) {
    Invoke-WebRequest -Uri $moduleUrl -OutFile $modulePath
}

$actualHash = (Get-FileHash -LiteralPath $modulePath -Algorithm SHA256).Hash
if ($actualHash -ne $expectedHash) {
    Remove-Item -LiteralPath $modulePath -Force
    throw "PawnIO AMD module hash mismatch. Expected $expectedHash, received $actualHash"
}

Write-Host "PawnIO AMD sensor module ready: $modulePath"
