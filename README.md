# Stellaris 15 Gen3 Fan Speed Override

> [!CAUTION]
> This is experimental, hardware-specific fan-control software. It directly replaces the OEM fan curve through the Control Center service. It has **not been tested thoroughly**, is **not recommended for general use**, and may cause overheating, instability, hardware damage, or data loss. Use it entirely at your own risk. Keep an independent temperature monitor open and be ready to enable the OEM Fan Boost mode or shut the laptop down.

## Why this exists

The embedded controller (EC) in the author's laptop stopped receiving a correct CPU temperature. The OEM curve consequently allowed the machine to overheat. This project provides an alternative manual and temperature-driven way to set the CPU and GPU fan duties.

The project was developed for one Stellaris 15 Gen3 / XMG-Uniwill-style system running OEM Control Center 3.9.42.1. MQTT actions, curve formats, table names, authentication values, and EC behavior may differ on any other machine or Control Center release.

## How it works

- Fan commands are sent to the locally installed OEM Control Center service over its IPv6 loopback MQTT broker.
- CPU temperature is read directly from the Ryzen thermal SMN register through the signed [PawnIO](https://github.com/namazso/PawnIO) driver and a restricted AMD read module. The broken Control Center CPU temperature is never used.
- GPU temperature is read from the NVIDIA driver with `nvidia-smi`.
- Manual mode provides separate CPU and GPU sliders in 5% steps. Values below 30% require confirmation.
- Auto mode checks temperatures every 15 seconds, uses `max(CPU, GPU)`, and applies one shared duty to both fans. Its 30% and 100% temperature endpoints are adjustable from 0-100 C and default to 40 C and 80 C.
- Only one GUI and one Control Center communication client can run at a time. Background work is serialized so refreshes cannot build up in a queue or freeze the GUI.

The automatic curve is linearly interpolated between two adjustable endpoints and rounded to the nearest 5%:

| Default temperature endpoint | Fan duty |
| ---: | ---: |
| 40 C or below | 30% |
| 80 C or above | 100% |

Auto mode never requests less than 30%. Selecting **Automatic** starts control immediately and repeats it every 15 seconds. Selecting **Manual** stops future automatic updates; use **Apply manual speeds** to write the slider values. The inactive control section is disabled, while sensor values and the OEM Fan Boost fallback remain available.

## Requirements

- Windows 11
- The compatible OEM Control Center service installed and running
- Python 3.11 or newer for source use
- The signed PawnIO driver (the included setup script installs it through `winget`)
- Administrator access when the application starts
- An NVIDIA GPU and working `nvidia-smi.exe`

If either independent temperature source is unavailable or implausible, the Auto cycle fails before writing a new fan target.

## Run from source

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\run_fan_control_gui.cmd
```

The launcher runs `setup_pawnio.ps1`, which installs PawnIO once and downloads a pinned, SHA-256-verified 10 KB AMD sensor module. It then requests administrator access and starts the GUI. Core Temp is not required.

The command-line tool defaults to dry-run behavior for writes:

```powershell
.\.venv\Scripts\python.exe .\fan_control.py status
.\.venv\Scripts\python.exe .\fan_control.py rpm
.\.venv\Scripts\python.exe .\fan_control.py fixed 65 --gpu-duty 75
```

Only add `--apply` after inspecting the preview. Existing fan curves are backed up before manual writes. Source-run backups are stored in `fan-backups`; packaged builds use `%LOCALAPPDATA%\StellarisFanControl\fan-backups`.

## Build the executable

Run PowerShell from the repository root:

```powershell
powershell.exe -ExecutionPolicy Bypass -File .\build_exe.ps1
```

The script creates `.venv` if necessary, prepares PawnIO, installs the build dependencies, and produces `dist\StellarisFanControl.exe` as a windowed, single-file executable. The AMD sensor module is bundled, but the signed PawnIO driver and OEM Control Center remain external runtime requirements. The executable contains an administrator manifest and displays one UAC prompt at launch.

The executable, PyInstaller work directories, downloaded AMD module, virtual environment, and generated fan backups are ignored by Git and must not be committed.

## Recovery

Fan curve backups can be restored with the source CLI:

```powershell
.\.venv\Scripts\python.exe .\fan_control.py restore .\fan-backups\M2T1-TIMESTAMP.json
.\.venv\Scripts\python.exe .\fan_control.py restore .\fan-backups\M2T1-TIMESTAMP.json --apply
```

The first command previews the backup. The second writes it. If temperature rises unexpectedly, enable OEM Fan Boost immediately or shut the machine down rather than relying on this software.
