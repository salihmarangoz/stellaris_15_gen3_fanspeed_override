$ErrorActionPreference = 'Stop'

$root = $PSScriptRoot
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

& (Join-Path $root 'setup_pawnio.ps1')

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
        --add-data "$pawnModule;pawnio" `
        --add-data "$styleSheet;frontend" `
        --name StellarisFanControl `
        (Join-Path $root 'stellaris15gen3.py')
    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller failed with exit code $LASTEXITCODE"
    }
}
finally {
    Pop-Location
}

Write-Host "Built: $(Join-Path $root 'dist\StellarisFanControl.exe')"
