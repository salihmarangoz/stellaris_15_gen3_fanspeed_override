# Development

Start with [AGENTS.md](../AGENTS.md) for the hardware-safety rules and module boundaries, [DESIGN.md](../DESIGN.md) for the reasoning behind them, and [STRUCTURE.md](../STRUCTURE.md) for the file map.

Routine work never performs live fan writes. Tests use mocks, offscreen Qt, and read-only probes. Installing, starting the application or service, selecting Automatic, and the stress test write to the fans and need the hardware owner's explicit go-ahead.

## Tests

The mocked suites run on both platforms without hardware access.

Ubuntu:

```bash
python3 -m venv .venv && ./.venv/bin/python -m pip install -r requirements.txt
QT_QPA_PLATFORM=offscreen ./.venv/bin/python -m unittest discover -s tests
```

Windows:

```powershell
$env:QT_QPA_PLATFORM = 'offscreen'
.\.venv\Scripts\python.exe -m unittest discover -s tests
```

`sudo python3 -m backend.linux_fan_control probe` is the read-only live check on Ubuntu.

## Build the Windows Executable

```powershell
powershell.exe -ExecutionPolicy Bypass -File .\scripts\build_exe.ps1
```

The script creates the virtual environment when necessary, prepares PawnIO, installs the build requirements, and writes `dist\StellarisFanControl.exe`, which contains the interface, controller, stylesheet, and AMD sensor module. Start it normally and accept its UAC prompt. Generated executables, PyInstaller files, downloaded modules, virtual environments, caches, and fan backups are excluded from Git.

On a machine without the target hardware, use `scripts\build_exe.ps1 -SkipDriverInstall`. This skips only the driver installation; the pinned AMD module is still downloaded and hash-verified. It does not make the application usable without PawnIO on the laptop.

The build temporarily limits the DLL search `PATH` to the virtual environment, the Python runtime, and Windows directories, then restores it. This prevents unrelated tools such as Poppler from contributing an incompatible `icuuc.dll` or OpenSSL DLL.

Before installing a new build, check its bundled Qt DLLs and offscreen platform plugin without starting the controller or touching hardware:

```powershell
$check = Start-Process -FilePath .\dist\StellarisFanControl.exe -ArgumentList '--check-qt' -Verb RunAs -WindowStyle Hidden -Wait -PassThru
if ($check.ExitCode -ne 0) { throw "Packaged Qt check failed: $($check.ExitCode)" }
```

The administrator manifest still asks for UAC. A successful check exits with code 0 and does not enter Automatic mode.

## Install a Local Build

```powershell
powershell.exe -ExecutionPolicy Bypass -File .\scripts\install.ps1
```

Approve the administrator prompt. The installer creates `%ProgramFiles%\StellarisFanControl`, copies `StellarisFanControl.exe` into it, and registers the per-user `Stellaris Fan Control` scheduled task. The task starts the application with highest privileges at the installing user's next sign-in; a sign-in trigger is used instead of system boot because the application needs the interactive desktop for its window and tray icon. Installation does not launch the application, so it never starts Automatic mode or writes the fans as a side effect.

On Ubuntu, `sudo ./scripts/linux/install.sh` installs from the working tree; see the [Ubuntu guide](linux.md#install).

## Publish a Release

The **Release** workflow (`.github/workflows/release.yml`):

- builds on Windows x64 with Python 3.14, runs compilation and the mocked/offscreen tests, and packages the executable, installer, PawnIO setup script, pinned module, README, `docs/`, and third-party notices into `StellarisFanControl-windows-x64.zip` with a SHA-256 checksum;
- runs the same tests on Ubuntu 24.04 and packages the Linux source release `StellarisFanControl-linux.tar.gz` with its checksum;
- never installs PawnIO, touches the EC, or launches the application.

After committing and pushing, push a new version tag:

```bash
git tag v1.2.0
git push origin v1.2.0
```

A successful tag build publishes a GitHub Release with both archives and checksums. Use a new tag for each release; existing releases are not overwritten. **Run workflow** on a branch builds downloadable Actions artifacts without publishing; running it on a `v*` tag also publishes, so use an unreleased tag.

Local packaging uses the same scripts:

```powershell
powershell.exe -ExecutionPolicy Bypass -File .\scripts\build_exe.ps1
powershell.exe -ExecutionPolicy Bypass -File .\scripts\package_release.ps1
```

```bash
./scripts/linux/package_release.sh
```

The archives are written to `dist\`. Automated checks do not replace hardware validation on the supported laptop.
