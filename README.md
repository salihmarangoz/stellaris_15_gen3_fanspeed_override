# Stellaris 15 Gen3 Fan Speed Override

> [!CAUTION]
> This is experimental, hardware-specific fan-control software. It directly replaces the OEM fan curve through the Control Center service. It has **not been tested thoroughly**, is **not recommended for general use**, and may cause overheating, instability, hardware damage, or data loss. Use it entirely at your own risk. Keep an independent temperature monitor open and be ready to enable the OEM Fan Boost mode or shut the laptop down.

## Why this exists

The embedded controller (EC) in the author's laptop stopped receiving a correct CPU temperature. The OEM curve consequently allowed the machine to overheat. This project provides an alternative manual and temperature-driven way to set the CPU and GPU fan duties.

The project was developed for one Stellaris 15 Gen3 / XMG-Uniwill-style system running OEM Control Center 3.9.42.1. MQTT actions, curve formats, table names, authentication values, and EC behavior may differ on any other machine or Control Center release.

## How it works

- Fan commands are sent to the locally installed OEM Control Center service over its IPv6 loopback MQTT broker.
- CPU temperature is read from the running [Core Temp](https://www.alcpu.com/CoreTemp/) application's shared-memory interface. The broken Control Center CPU temperature is never used.
- GPU temperature is read from the NVIDIA driver with `nvidia-smi`.
- Manual mode provides separate CPU and GPU sliders in 5% steps. Values below 30% require confirmation.
- Auto mode checks temperatures every 15 seconds, uses `max(CPU, GPU)`, and applies one shared duty to both fans.
- Only one GUI and one Control Center communication client can run at a time. Background work is serialized so refreshes cannot build up in a queue or freeze the GUI.

The automatic curve is linearly interpolated and rounded to the nearest 5%:

| Maximum temperature | Fan duty |
| ---: | ---: |
| 40 C or below | 30% |
| 50 C | 40% |
| 60 C | 55% |
| 70 C | 75% |
| 80 C or above | 100% |

Auto mode never requests less than 30%. Selecting the Auto tab starts control immediately and repeats it every 15 seconds. Returning to Manual stops future automatic updates; use **Apply manual speeds** to write the slider values.

## Requirements

- Windows 11
- The compatible OEM Control Center service installed and running
- Python 3.11 or newer for source use
- [Core Temp](https://www.alcpu.com/CoreTemp/) installed and running
- An NVIDIA GPU and working `nvidia-smi.exe`

If either independent temperature source is unavailable or implausible, the Auto cycle fails before writing a new fan target.

## Run from source

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\run_fan_control_gui.cmd
```

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

The script creates `.venv` if necessary, installs the build dependencies, and produces `dist\StellarisFanControl.exe` as a windowed, single-file executable. Core Temp and the OEM Control Center remain external runtime requirements.

## Recovery

Fan curve backups can be restored with the source CLI:

```powershell
.\.venv\Scripts\python.exe .\fan_control.py restore .\fan-backups\M2T1-TIMESTAMP.json
.\.venv\Scripts\python.exe .\fan_control.py restore .\fan-backups\M2T1-TIMESTAMP.json --apply
```

The first command previews the backup. The second writes it. If temperature rises unexpectedly, enable OEM Fan Boost immediately or shut the machine down rather than relying on this software.
