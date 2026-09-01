$ErrorActionPreference = 'Stop'

$root = $PSScriptRoot
$venv = Join-Path $root '.venv'
$python = Join-Path $venv 'Scripts\python.exe'

if (-not (Test-Path -LiteralPath $python)) {
    py -3 -m venv $venv
}

Push-Location $root
try {
    & $python -m pip install --upgrade pip
    & $python -m pip install -r (Join-Path $root 'requirements-build.txt')
    & $python -m PyInstaller `
        --noconfirm `
        --clean `
        --onefile `
        --windowed `
        --name StellarisFanControl `
        (Join-Path $root 'fan_control_gui.py')
}
finally {
    Pop-Location
}

Write-Host "Built: $(Join-Path $root 'dist\StellarisFanControl.exe')"
