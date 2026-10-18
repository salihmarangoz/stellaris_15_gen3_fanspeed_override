$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$application = Join-Path $root 'dist\StellarisFanControl'
if (-not (Test-Path -LiteralPath (Join-Path $application 'StellarisFanControl.exe') -PathType Leaf)) {
    throw 'Build the executable before packaging a release.'
}

# Use a new staging directory so previous build output cannot enter the ZIP.
$stage = Join-Path $root ('build\release-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path (Join-Path $stage 'scripts') -Force | Out-Null
New-Item -ItemType Directory -Path (Join-Path $stage 'third_party\pawnio') -Force | Out-Null
Copy-Item -LiteralPath $application -Destination $stage -Recurse
# A local test run writes preferences beside the executable; never ship them.
$stagedPreferences = Join-Path $stage 'StellarisFanControl\StellarisFanControl.json'
if (Test-Path -LiteralPath $stagedPreferences) {
    Remove-Item -LiteralPath $stagedPreferences -Force
}
foreach ($name in @('install.ps1', 'setup_pawnio.ps1')) {
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot $name) -Destination (Join-Path $stage 'scripts')
}
foreach ($name in @('README.md', 'THIRD_PARTY_NOTICES.md')) {
    Copy-Item -LiteralPath (Join-Path $root $name) -Destination $stage
}
# README links into docs\ and shows the screenshot; ship both so the links work offline.
Copy-Item -LiteralPath (Join-Path $root 'docs') -Destination $stage -Recurse
New-Item -ItemType Directory -Path (Join-Path $stage 'assets') -Force | Out-Null
Copy-Item -LiteralPath (Join-Path $root 'assets\ss.png') -Destination (Join-Path $stage 'assets')
Copy-Item -LiteralPath (Join-Path $root 'third_party\pawnio\AMDFamily17.bin') -Destination (Join-Path $stage 'third_party\pawnio')
$archive = Join-Path $root 'dist\StellarisFanControl-windows-x64.zip'
Compress-Archive -Path (Join-Path $stage '*') -DestinationPath $archive -Force
$hash = (Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash.ToLowerInvariant()
Set-Content -LiteralPath "$archive.sha256" -Encoding ASCII -Value "$hash  $([IO.Path]::GetFileName($archive))"
Write-Host "Release archive: $archive"
