$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $PSScriptRoot
$venv = Join-Path $root '.venv'
$python = Join-Path $venv 'Scripts\python.exe'
$pawnModule = Join-Path $root 'third_party\pawnio\AMDFamily17.bin'
$styleSheet = Join-Path $root 'frontend\stellaris15gen3.css'
$appIcon = Join-Path $root 'assets\stellaris-fan-control.ico'
$appIconPng = Join-Path $root 'assets\stellaris-fan-control.png'
$legacyExecutables = @(
    (Join-Path $root 'dist\StellarisFanControlFrontend.exe'),
    (Join-Path $root 'dist\StellarisFanControlBackend.exe')
)

if (-not (Test-Path -LiteralPath $python)) {
    py -3 -m venv $venv
    if ($LASTEXITCODE -ne 0) {
        throw "Virtual environment creation failed with exit code $LASTEXITCODE"
    }
}

& (Join-Path $PSScriptRoot 'setup_pawnio.ps1')

Push-Location $root
try {
    foreach ($legacyExecutable in $legacyExecutables) {
        if (Test-Path -LiteralPath $legacyExecutable) {
            Remove-Item -LiteralPath $legacyExecutable -Force
        }
    }
    & $python -m pip install --upgrade pip
    if ($LASTEXITCODE -ne 0) {
        throw "pip upgrade failed with exit code $LASTEXITCODE"
    }
    & $python -m pip install -r (Join-Path $root 'requirements-build.txt')
    if ($LASTEXITCODE -ne 0) {
        throw "Dependency installation failed with exit code $LASTEXITCODE"
    }
    & $python -m PyInstaller `
        --noconfirm `
        --clean `
        --onefile `
        --windowed `
        --uac-admin `
        --icon "$appIcon" `
        --add-data "$styleSheet;frontend" `
        --add-data "$appIconPng;assets" `
        --add-data "$pawnModule;pawnio" `
        --name StellarisFanControl `
        (Join-Path $root 'stellaris15gen3.py')
    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller build failed with exit code $LASTEXITCODE"
    }
}
finally {
    Pop-Location
}

Write-Host "Built application: $(Join-Path $root 'dist\StellarisFanControl.exe')"
