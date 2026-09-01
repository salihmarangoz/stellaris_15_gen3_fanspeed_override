$ErrorActionPreference = 'Stop'

& (Join-Path $PSScriptRoot 'setup_pawnio.ps1')

$python = Join-Path $PSScriptRoot '.venv\Scripts\pythonw.exe'
$gui = Join-Path $PSScriptRoot 'fan_control_gui.py'
$isAdmin = ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator
)

if ($isAdmin) {
    Start-Process -FilePath $python -ArgumentList @($gui) -WorkingDirectory $PSScriptRoot -WindowStyle Hidden
}
else {
    Start-Process -FilePath $python -ArgumentList @($gui) -WorkingDirectory $PSScriptRoot -Verb RunAs
}
