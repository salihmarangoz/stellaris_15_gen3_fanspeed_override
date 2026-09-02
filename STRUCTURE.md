# Structure

This file is the living map of source files, generated paths, runtime data, and dependency boundaries. Update it whenever a file moves, a new subsystem is introduced, or a generated/runtime path changes.

Last reviewed: 2026-09-02

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
|-- stellaris15gen3.py           packaged/source role launcher
|-- fan_control_common.py        pure automatic-curve constants and calculation
|-- fan_control_ipc.py           local IPC client, endpoint, privilege-aware launches
|-- fan_control_backend.py       elevated backend, scheduler, IPC server, watchdog
|-- fan_control_gui.py           normal-user PySide6 frontend
|-- stellaris15gen3.css          complete Qt stylesheet
|-- temperature_service.py       independent Ryzen and NVIDIA temperature reads
|-- fan_control_service.py       serialized singleton OEM-service owner
|-- fan_control.py               low-level MQTT, curve transforms, backup/restore CLI
|-- fan_control_probe.py         read-only MQTT diagnostic utility
|-- test_fan_control_backend.py  pure/mocked backend, safety, and IPC tests
|-- launch_fan_control.ps1       source setup and normal-user launcher
|-- run_fan_control_gui.cmd      command-shell entry point
|-- setup_pawnio.ps1             pinned PawnIO setup and module verification
|-- build_exe.ps1                reproducible PyInstaller build entry point
|-- requirements.txt             source/runtime Python dependencies
|-- requirements-build.txt       packaging dependencies
`-- .gitignore                   generated and local-only exclusions
```

## Dependency direction

```text
stellaris15gen3.py
|-- frontend role -> fan_control_gui.py -> fan_control_ipc.py
`-- backend role  -> fan_control_backend.py
                    |-- fan_control_common.py
                    |-- fan_control_ipc.py
                    |-- temperature_service.py
                    `-- fan_control_service.py -> fan_control.py
```

`temperature_service.py` must remain independent of `fan_control_service.py` and the OEM Control Center. `fan_control_gui.py` must not import hardware or OEM protocol modules. `fan_control_common.py` stays pure so curve behavior can be tested without Qt, MQTT, PawnIO, NVIDIA, or administrator access.

## Runtime paths

```text
%LOCALAPPDATA%\StellarisFanControl\
|-- backend-endpoint.json    loopback address and random IPC token
|-- backend.lock             process-wide backend singleton lock
`-- fan-backups\             backups created by packaged writes
```

Source-mode backups are written to `fan-backups\` in the repository. The endpoint file is replaced atomically when a backend starts and removed only when that same backend shuts down normally.

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
```

`build_exe.ps1` produces `dist\StellarisFanControl.exe`. PyInstaller embeds `stellaris15gen3.css` and the hash-verified AMD PawnIO module. The PawnIO driver and OEM Control Center remain external system dependencies.
