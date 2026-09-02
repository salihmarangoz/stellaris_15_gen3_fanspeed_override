$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $PSScriptRoot
$venv = Join-Path $root '.venv'
$python = Join-Path $venv 'Scripts\python.exe'
$pawnModule = Join-Path $root 'third_party\pawnio\AMDFamily17.bin'
$styleSheet = Join-Path $root 'frontend\stellaris15gen3.css'

if (-not (Test-Path -LiteralPath $python)) {
    py -3 -m venv $venv
    if ($LASTEXITCODE -ne 0) {
        throw "Virtual environment creation failed with exit code $LASTEXITCODE"
    }
}

& (Join-Path $PSScriptRoot 'setup_pawnio.ps1')

Push-Location $root
try {
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
        --add-data "$styleSheet;frontend" `
        --name StellarisFanControlFrontend `
        (Join-Path $root 'stellaris15gen3_frontend.py')
    if ($LASTEXITCODE -ne 0) {
        throw "Frontend PyInstaller build failed with exit code $LASTEXITCODE"
    }
    & $python -m PyInstaller `
        --noconfirm `
        --clean `
        --onefile `
        --windowed `
        --uac-admin `
        --add-data "$pawnModule;pawnio" `
        --name StellarisFanControlBackend `
        (Join-Path $root 'stellaris15gen3_backend.py')
    if ($LASTEXITCODE -ne 0) {
        throw "Backend PyInstaller build failed with exit code $LASTEXITCODE"
    }
}
finally {
    Pop-Location
}

Write-Host "Built frontend: $(Join-Path $root 'dist\StellarisFanControlFrontend.exe')"
Write-Host "Built backend:  $(Join-Path $root 'dist\StellarisFanControlBackend.exe')"
