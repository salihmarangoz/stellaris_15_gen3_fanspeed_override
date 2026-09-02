$ErrorActionPreference = 'Stop'

& (Join-Path $PSScriptRoot 'setup_pawnio.ps1')

$python = Join-Path $PSScriptRoot '.venv\Scripts\pythonw.exe'
$launcher = Join-Path $PSScriptRoot 'stellaris15gen3.py'
$isAdmin = ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator
)

if ($isAdmin) {
    Start-Process -FilePath $python -ArgumentList @($launcher) -WorkingDirectory $PSScriptRoot -WindowStyle Hidden
}
else {
    Start-Process -FilePath $python -ArgumentList @($launcher) -WorkingDirectory $PSScriptRoot -Verb RunAs
}
