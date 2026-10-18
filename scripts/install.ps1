$ErrorActionPreference = 'Stop'

$applicationName = 'StellarisFanControl'
$taskName = 'Stellaris Fan Control'
$root = Split-Path -Parent $PSScriptRoot
$sourceDirectory = Join-Path $root "dist\$applicationName"
if (-not (Test-Path -LiteralPath (Join-Path $sourceDirectory "$applicationName.exe") -PathType Leaf)) {
    $sourceDirectory = Join-Path $root $applicationName
}
$sourceExecutable = Join-Path $sourceDirectory "$applicationName.exe"
$preferencesName = "$applicationName.json"

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

$runningInstances = Get-CimInstance Win32_Process -Filter "Name = '$applicationName.exe'" |
    Where-Object {
        $_.ExecutablePath -and
        $_.ExecutablePath.StartsWith("$installDirectory\", [StringComparison]::OrdinalIgnoreCase)
    }
if ($runningInstances) {
    throw 'The installed application is running. Exit it with its confirmed Exit action, then run the installer again.'
}

# The one-folder build keeps its runtime in _internal next to the executable.
# Replace it whole so no file from an older build is left behind, but keep the
# user's preferences file.
$sourceDirectory = (Resolve-Path -LiteralPath $sourceDirectory).ProviderPath
New-Item -ItemType Directory -Path $installDirectory -Force | Out-Null
$installedRuntime = Join-Path $installDirectory '_internal'
if (Test-Path -LiteralPath $installedRuntime) {
    Remove-Item -LiteralPath $installedRuntime -Recurse -Force
}
Get-ChildItem -LiteralPath $sourceDirectory |
    Where-Object { $_.Name -ne $preferencesName } |
    Copy-Item -Destination $installDirectory -Recurse -Force

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

$sourceFiles = Get-ChildItem -LiteralPath $sourceDirectory -Recurse -File |
    Where-Object { $_.FullName -ne (Join-Path $sourceDirectory $preferencesName) }
foreach ($sourceFile in $sourceFiles) {
    $relativePath = $sourceFile.FullName.Substring($sourceDirectory.Length + 1)
    $installedFile = Join-Path $installDirectory $relativePath
    $sourceHash = (Get-FileHash -LiteralPath $sourceFile.FullName -Algorithm SHA256).Hash
    if (-not (Test-Path -LiteralPath $installedFile -PathType Leaf) -or
        (Get-FileHash -LiteralPath $installedFile -Algorithm SHA256).Hash -ne $sourceHash) {
        throw "The installed file failed SHA-256 verification: $relativePath"
    }
}

Write-Host "Installed application: $installedExecutable"
Write-Host "Registered highest-privilege sign-in task: $taskName"
Write-Host 'The application will start automatically at the next sign-in.'
