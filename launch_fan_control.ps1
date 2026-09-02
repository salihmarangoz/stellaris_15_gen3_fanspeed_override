$ErrorActionPreference = 'Stop'

& (Join-Path $PSScriptRoot 'setup_pawnio.ps1')

$python = Join-Path $PSScriptRoot '.venv\Scripts\pythonw.exe'
$launcher = Join-Path $PSScriptRoot 'stellaris15gen3.py'
Start-Process -FilePath $python -ArgumentList @($launcher) -WorkingDirectory $PSScriptRoot -WindowStyle Hidden
