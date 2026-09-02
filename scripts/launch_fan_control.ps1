$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $PSScriptRoot

& (Join-Path $PSScriptRoot 'setup_pawnio.ps1')

$python = Join-Path $root '.venv\Scripts\pythonw.exe'
$launcher = Join-Path $root 'stellaris15gen3.py'
Start-Process -FilePath $python -ArgumentList @($launcher) -WorkingDirectory $root -WindowStyle Hidden
