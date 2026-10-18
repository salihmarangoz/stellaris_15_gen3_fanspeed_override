param([switch]$SkipDriverInstall)

$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $PSScriptRoot
$venv = Join-Path $root '.venv'
$python = Join-Path $venv 'Scripts\python.exe'
$pawnModule = Join-Path $root 'third_party\pawnio\AMDFamily17.bin'
$styleSheet = Join-Path $root 'frontend\stellaris15gen3.css'
$appIcon = Join-Path $root 'icon\stellaris-fan-control.ico'
$appIconPng = Join-Path $root 'icon\stellaris-fan-control.png'
$legacyExecutables = @(
    (Join-Path $root 'dist\StellarisFanControlFrontend.exe'),
    (Join-Path $root 'dist\StellarisFanControlBackend.exe'),
    (Join-Path $root 'dist\StellarisFanControl.exe')
)

if (-not (Test-Path -LiteralPath $python)) {
    py -3 -m venv $venv
    if ($LASTEXITCODE -ne 0) {
        throw "Virtual environment creation failed with exit code $LASTEXITCODE"
    }
}

& (Join-Path $PSScriptRoot 'setup_pawnio.ps1') -SkipDriverInstall:$SkipDriverInstall

$originalBuildPath = $env:Path
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
    # Resolve DLLs only from Python, its packages, and Windows. Other PATH
    # tools can ship an incompatible icuuc.dll or OpenSSL with the same name.
    $pythonBaseDirectory = & $python -c 'import sys; print(sys.base_prefix)'
    if ($LASTEXITCODE -ne 0) {
        throw 'Could not determine the Python runtime directory.'
    }
    $env:Path = @(
        (Split-Path -Parent $python),
        $pythonBaseDirectory,
        (Join-Path $pythonBaseDirectory 'DLLs'),
        (Join-Path $env:SystemRoot 'System32'),
        $env:SystemRoot
    ) -join ';'
    # One-folder build: a one-file build unpacks into %TEMP%\_MEI* on every
    # launch, and temp cleaners can delete the sensor module while it runs.
    & $python -m PyInstaller `
        --noconfirm `
        --clean `
        --onedir `
        --windowed `
        --uac-admin `
        --icon "$appIcon" `
        --add-data "$styleSheet;frontend" `
        --add-data "$(Join-Path $root 'frontend\checkmark.svg');frontend" `
        --add-data "$appIconPng;icon" `
        --add-data "$pawnModule;pawnio" `
        --name StellarisFanControl `
        (Join-Path $root 'stellaris15gen3.py')
    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller build failed with exit code $LASTEXITCODE"
    }
}
finally {
    $env:Path = $originalBuildPath
    Pop-Location
}

Write-Host "Built application: $(Join-Path $root 'dist\StellarisFanControl\StellarisFanControl.exe')"
