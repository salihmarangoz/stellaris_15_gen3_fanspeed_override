# Structure

This file is the living map of source files, generated paths, runtime data, and dependency boundaries. Update it whenever a file moves, a new subsystem is introduced, or a generated/runtime path changes.

Last reviewed: 2026-09-29

## Repository files

```text
.
|-- AGENTS.md                    development and hardware-safety rules
|-- README.md                    user-facing overview, setup, build, and recovery
|-- DESIGN.md                    architecture decisions and design ideas
|-- STRUCTURE.md                 this repository and runtime-path map
|-- ACCIDENTS.md                 factual important-incident records
|-- TODO.md                      prioritized open debt, problems, and work
|-- THIRD_PARTY_NOTICES.md       pinned dependency provenance and licenses
|-- .github\workflows\release.yml Windows and Linux builds, mocked checks, tagged releases
|-- assets\
|   |-- stellaris-fan-control.png transparent application/tray icon source
|   |-- stellaris-fan-control.ico Windows executable icon
|   `-- ss.png                    README interface screenshot
|-- stellaris15gen3.py           elevated single-process packaged entry point
|-- stellaris15gen3_frontend.py  normal-user frontend application entry point
|-- stellaris15gen3_backend.py   elevated backend application entry point
|-- stellaris15gen3_linux_service.py
|                                root systemd service entry point (Linux)
|-- frontend\
|   |-- __init__.py              frontend package marker
|   |-- fan_control_gui.py       normal-user PySide6 frontend
|   |-- stellaris15gen3.css      complete Qt stylesheet
|   |-- checkmark.svg           high-contrast startup checkbox indicator
|   `-- requirements.txt         frontend-only runtime dependencies
|-- backend\
|   |-- __init__.py              backend package marker
|   |-- fan_control_backend.py   elevated scheduler, IPC server, watchdog
|   |-- temperature_service.py   independent Ryzen and NVIDIA temperature reads
|   |-- fan_control_service.py   serialized singleton and control-method selector
|   |-- fan_control.py           low-level MQTT, curves, backup/restore CLI
|   |-- direct_fan_control.py    validated direct Uniwill EC fan-table access
|   |-- fan_control_probe.py     read-only MQTT diagnostic utility
|   |-- linux_ec.py              Linux EC window, platform gates, verified table/cTGP/lightbar writes
|   |-- linux_sensors.py         Linux k10temp, NVIDIA, and RAPL readings
|   |-- linux_fan_service.py     Linux service singleton, drift repair, backups, cTGP, lightbar
|   |-- linux_backend.py         Linux root service runtime and peer-credential socket
|   |-- linux_fan_control.py     Linux low-level CLI (read-only probe, dry-run writes)
|   `-- requirements.txt         Windows backend runtime dependencies
|-- shared\
|   |-- __init__.py              shared package marker
|   |-- fan_control_common.py    pure automatic-curve constants and calculation
|   `-- fan_control_ipc.py       IPC, endpoint, and privilege-aware launches
|-- tests\
|   |-- __init__.py              test package marker
|   |-- test_fan_control_backend.py
|                                pure/mocked backend, safety, and IPC tests
|   |-- test_session_shutdown.py real Qt startup, mocked shutdown/mode, and lightbar window tests
|   `-- test_linux_backend.py    fake-EC/sysfs Linux client, sensor, safety, and IPC tests
|-- scripts\
|   |-- launch_fan_control.ps1   source setup and normal-user launcher
|   |-- run_fan_control_gui.cmd  command-shell entry point
|   |-- setup_pawnio.ps1         pinned PawnIO setup and module verification
|   |-- build_exe.ps1            reproducible PyInstaller build entry point
|   |-- package_release.ps1      release ZIP and SHA-256 checksum generation
|   |-- install.ps1              elevated packaged-app install and startup-task setup
|   `-- linux\
|       |-- install.sh           Ubuntu install/update into /opt and systemd setup
|       |-- uninstall.sh         Ubuntu removal (--purge also deletes state)
|       |-- stellaris-fan-control.service   root systemd unit
|       |-- stellaris-fan-control-sleep     resume hook (sends SIGUSR1)
|       |-- stellaris-fan-control.desktop   menu entry and GUI autostart
|       |-- package_release.sh   Linux source archive and SHA-256 checksum
|       |-- nvidia-powerd.service  NVIDIA daemon unit installed when Ubuntu lacks it
|       |-- nvidia-dbus.conf     D-Bus policy for nvidia-powerd
|       `-- stress_test.py       180 s CPU+GPU load with live service monitoring
|-- requirements.txt             source/runtime Python dependencies
|-- requirements-build.txt       packaging dependencies
`-- .gitignore                   generated and local-only exclusions
```

## Dependency direction

```text
stellaris15gen3.py          -> frontend/fan_control_gui.py
                           `-> backend/fan_control_backend.py

stellaris15gen3_frontend.py -> frontend/fan_control_gui.py
                              `-- shared/{fan_control_common,fan_control_ipc}.py

stellaris15gen3_backend.py  -> backend/fan_control_backend.py
                              |-- shared/{fan_control_common,fan_control_ipc}.py
                              |-- backend/temperature_service.py
                              `-- backend/fan_control_service.py
                                   |-- backend/fan_control.py
                                   `-- backend/direct_fan_control.py
```

```text
stellaris15gen3_linux_service.py -> backend/linux_backend.py
                                   |-- backend/fan_control_backend.py (shared controller)
                                   |-- backend/linux_fan_service.py
                                   |    |-- backend/linux_ec.py
                                   |    `-- backend/linux_sensors.py (NVIDIA limits)
                                   `-- shared/{fan_control_common,fan_control_ipc}.py
```

`backend/fan_control_backend.py` selects `ControlCenterService` and `temperature_service` on Windows and `LinuxFanService` and `linux_sensors` elsewhere at import time. The Linux service imports only the Python standard library, so it runs with the system Python; PySide6 is needed only by the GUI environment. Linux modules must not import `backend/fan_control.py` (paho-mqtt) or any Windows-only module.

`backend/temperature_service.py` must remain independent of `backend/fan_control_service.py` and both fan-control methods. `frontend/fan_control_gui.py` must not import the `backend` package. `shared/fan_control_common.py` stays pure so curve behavior can be tested without Qt, MQTT, PawnIO, NVIDIA, or administrator access. Shared code must not import either process package.

## Runtime paths

```text
%LOCALAPPDATA%\StellarisFanControl\
|-- backend-endpoint.json    loopback address and random IPC token
|-- backend.lock             process-wide backend singleton lock
|-- last-oem-curve.json      last complete OEM curve for direct failover
`-- fan-backups\             backups created by packaged writes
```

Source-mode backups and `last-oem-curve.json` are written to `fan-backups\` in the repository. The endpoint file is replaced atomically when a backend starts and removed only when that same backend shuts down normally.

Packaged user selections are stored as `StellarisFanControl.json` beside `StellarisFanControl.exe`. Source mode uses the ignored repository-root file of the same name.

The settings file also stores `start_minimized` (true by default). Frontend startup applies it to tray/taskbar visibility; the startup task needs no additional arguments.

Linux runtime paths:

```text
/opt/stellaris-fan-control/          installed application (root-owned)
`-- .venv/                           PySide6 environment for the GUI
/etc/stellaris-fan-control/config.json   allowed_uids for the service socket
/etc/systemd/system/stellaris-fan-control.service
/usr/lib/systemd/system-sleep/stellaris-fan-control
/usr/share/applications/stellaris-fan-control.desktop
~/.config/autostart/stellaris-fan-control.desktop
~/.config/stellaris-fan-control/StellarisFanControl.json   GUI preferences
/run/stellaris-fan-control/
|-- backend.sock                     service socket (0666, peer UID checked)
`-- backend.lock                     single-writer lock (service and CLI)
/etc/systemd/system/nvidia-powerd.service   only when the driver package lacks it
/etc/dbus-1/system.d/nvidia-dbus.conf       only when no nvidia-powerd policy exists
/var/lib/stellaris-fan-control/
|-- settings.json                    Automatic endpoints, GPU power offset, Dynamic Boost, lightbar
`-- fan-backups/LINUX_EC-*.json      register snapshots before writes
```

`STELLARIS15GEN3_SOCKET`, `STELLARIS15GEN3_STATE_DIR`, and `STELLARIS15GEN3_CONFIG` override these paths for tests and source runs.

## Generated and local-only paths

The following are ignored and must not be committed:

```text
.venv\
__pycache__\
*.pyc
fan-backups\
build\
dist\
*.spec
third_party\pawnio\AMDFamily17.bin
StellarisFanControl.json
stress-*.csv
*.dat, *.dsl                  ACPI table dumps
```

`scripts\build_exe.ps1` produces one `dist\StellarisFanControl.exe` containing the interface, controller, stylesheet, tray icon, and the hash-verified AMD PawnIO module. The generated ICO is embedded as the executable icon. The executable carries an administrator manifest and runs as one process after UAC approval. The PawnIO driver and OEM Control Center remain external system dependencies.

`scripts\install.ps1` copies that executable to `%ProgramFiles%\StellarisFanControl\StellarisFanControl.exe` and registers the per-user `Stellaris Fan Control` scheduled task. The highest-privilege task starts at interactive sign-in, not pre-login system boot, so the window and tray icon are available on the user's desktop. The script verifies that the source and installed executable SHA-256 hashes match.

The installer also accepts `StellarisFanControl.exe` at the root of an extracted release ZIP and invokes PawnIO setup before copying. Existing installed preferences are preserved. `scripts\package_release.ps1` stages files under an isolated `build\release-<id>` directory and writes `dist\StellarisFanControl-windows-x64.zip` plus `.zip.sha256`. The ZIP contains the executable, README, third-party notices, `scripts\install.ps1`, `scripts\setup_pawnio.ps1`, and `third_party\pawnio\AMDFamily17.bin`.

`.github\workflows\release.yml` builds and runs mocked/offscreen tests on Windows without driver installation or application launch. Tag pushes publish the archive and checksum; manual branch runs retain them as Actions artifacts. Publication runs separately with repository-content write permission.
