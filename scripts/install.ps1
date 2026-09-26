$ErrorActionPreference = 'Stop'

$applicationName = 'StellarisFanControl'
$taskName = 'Stellaris Fan Control'
$root = Split-Path -Parent $PSScriptRoot
$sourceExecutable = Join-Path $root "dist\$applicationName.exe"
if (-not (Test-Path -LiteralPath $sourceExecutable -PathType Leaf)) {
    $sourceExecutable = Join-Path $root "$applicationName.exe"
}

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = [Security.Principal.WindowsPrincipal]::new($identity)
$isAdministrator = $principal.IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator
)

if (-not $isAdministrator) {
    Write-Host 'Administrator access is required to install the application and register its startup task.'
    $elevated = Start-Process `
        -FilePath 'powershell.exe' `
        -Verb RunAs `
        -WindowStyle Hidden `
        -Wait `
        -PassThru `
        -ArgumentList @(
            '-NoProfile',
            '-ExecutionPolicy', 'Bypass',
            '-File', "`"$PSCommandPath`""
        )
    exit $elevated.ExitCode
}

if (-not (Test-Path -LiteralPath $sourceExecutable -PathType Leaf)) {
    throw 'Packaged executable not found. Extract the complete release ZIP or run scripts\build_exe.ps1 first.'
}

& (Join-Path $PSScriptRoot 'setup_pawnio.ps1')

$installDirectory = Join-Path $env:ProgramFiles $applicationName
$installedExecutable = Join-Path $installDirectory "$applicationName.exe"

New-Item -ItemType Directory -Path $installDirectory -Force | Out-Null
Copy-Item -LiteralPath $sourceExecutable -Destination $installedExecutable -Force

$action = New-ScheduledTaskAction `
    -Execute $installedExecutable `
    -WorkingDirectory $installDirectory
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $identity.Name
$taskPrincipal = New-ScheduledTaskPrincipal `
    -UserId $identity.Name `
    -LogonType Interactive `
    -RunLevel Highest
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -MultipleInstances IgnoreNew `
    -StartWhenAvailable

$task = New-ScheduledTask `
    -Action $action `
    -Trigger $trigger `
    -Principal $taskPrincipal `
    -Settings $settings
Register-ScheduledTask `
    -TaskName $taskName `
    -InputObject $task `
    -Force | Out-Null

$sourceHash = (Get-FileHash -LiteralPath $sourceExecutable -Algorithm SHA256).Hash
$installedHash = (Get-FileHash -LiteralPath $installedExecutable -Algorithm SHA256).Hash
if ($installedHash -ne $sourceHash) {
    throw 'The installed executable failed SHA-256 verification.'
}

Write-Host "Installed application: $installedExecutable"
Write-Host "Registered highest-privilege sign-in task: $taskName"
Write-Host 'The application will start automatically at the next sign-in.'
