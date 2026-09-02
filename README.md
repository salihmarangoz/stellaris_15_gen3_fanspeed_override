# Stellaris 15 Gen3 Fan Control

An experimental fan-control application for my Stellaris 15 Gen3 laptop, designed to work around an unreliable OEM CPU-temperature path.

> [!CAUTION]
> I built this project for my own Stellaris 15 Gen3, and it remains experimental. It directly replaces fan curves through the installed OEM Control Center service. A software defect, invalid sensor value, or incompatible laptop can cause overheating, instability, hardware damage, or data loss. Use it at your own risk, keep an independent temperature monitor visible, and be ready to enable OEM Fan Boost or shut down the laptop.

> [!WARNING]
> This project can interfere with the installed OEM fan-control application or its configuration. After custom curves are written, the Control Center GUI may display unusual curves, incorrect-looking values, broken layouts, or other unexpected behavior. Recovery may require restoring a backup, resetting Control Center, or reinstalling it. This project does not intentionally modify OEM program files, but it does change the fan data consumed by that software.

## Why I Built This

The embedded-controller path on my laptop stopped providing a reliable CPU temperature. As a result, the original fan curve could leave the system undercooled while the CPU temperature increased.

This application works around that failure. It supports separate manual CPU and GPU fan settings, or automatic control driven by independent CPU and GPU temperature sources.

I built and tested it for one Stellaris 15 Gen3 / XMG-Uniwill-style laptop running OEM Control Center 3.9.42.1. MQTT messages, table names, curve formats, authentication, and embedded-controller behavior may differ on other laptops or Control Center versions. Similar hardware is not proof of compatibility.

## How It Works

- Fan commands are sent to the locally installed OEM Control Center service through its IPv6 loopback MQTT broker.
- CPU temperature comes directly from the Ryzen `Tctl/Tdie` SMN register through the signed [PawnIO](https://github.com/namazso/PawnIO) driver and a restricted AMD module.
- The broken Control Center CPU-temperature value is never used in Automatic mode.
- GPU temperature comes from the NVIDIA driver through `nvidia-smi`.
- Manual mode provides separate CPU and GPU fan controls in 5% steps. Any value below 30% requires explicit confirmation.
- Automatic mode checks temperatures every 15 seconds, uses `max(CPU, GPU)`, and sends one shared target to both fans.
- The sensor panel displays CPU and GPU temperature gauges plus reported CPU and GPU fan-duty gauges.
- The elevated backend is the only process allowed to read hardware sensors or communicate with Control Center.
- The normal-user frontend displays state and sends authenticated requests to the backend.
- Only one frontend, one backend, and one Control Center client may run at a time.

If either temperature is missing, zero, malformed, or implausible, Automatic mode fails closed and does not write a new fan target during that cycle.

## Automatic Curve

The two configurable temperature points accept values from 0 to 100 C:

| Default temperature | Fan duty |
| ---: | ---: |
| 40 C or below | 30% |
| 80 C or above | 100% |

The application interpolates linearly between the two points and rounds the result to the nearest 5%. Automatic mode never requests less than 30%. The fixed 80 C safety cap always forces 100%, even when the maximum-temperature slider is configured above 80 C.

Selecting **Automatic** starts an immediate cycle followed by non-overlapping 15-second cycles. Selecting **Manual** stops future automatic cycles. Manual is the default mode at startup.

The inactive control section is disabled and visually dimmed. Sensor gauges and the OEM **Fan Boost 100%** fallback remain available in both modes.

## Frontend and Backend

The application uses separate supervised processes:

- The frontend runs without administrator privileges and contains the PySide6 interface.
- The backend runs with administrator privileges and owns sensor access, automatic scheduling, and fan-control communication.

If the frontend crashes or PySide6 cannot start, the backend keeps Automatic mode running and attempts to restart a non-administrator frontend. If the backend crashes, the frontend requests an elevated replacement and restores the selected mode after reconnection.

Both restart directions use a 60-second cooldown to prevent rapid crash loops. Closing the frontend normally detaches it from the watchdog, while the backend remains active.

The processes communicate through an authenticated loopback connection. The backend endpoint and random token are stored under `%LOCALAPPDATA%\StellarisFanControl` for the current user.

## Requirements

- Windows 11
- Compatible OEM Control Center service installed and running
- Python 3.11 or newer when running from source
- Signed PawnIO driver
- Administrator access for the hardware backend
- NVIDIA GPU with a working `nvidia-smi.exe`

## Run from Source

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\scripts\run_fan_control_gui.cmd
```

The launcher runs `scripts\setup_pawnio.ps1`, installs PawnIO when necessary, downloads the pinned and SHA-256-verified AMD sensor module, and starts the GUI as a normal user. The PawnIO installer may display a separate one-time setup prompt. During normal use, Windows requests administrator access only for the hardware backend. Core Temp is not required.

## Low-Level CLI

Write commands are dry runs unless `--apply` is supplied:

```powershell
.\.venv\Scripts\python.exe .\backend\fan_control.py status
.\.venv\Scripts\python.exe .\backend\fan_control.py rpm
.\.venv\Scripts\python.exe .\backend\fan_control.py fixed 65 --gpu-duty 75
```

Inspect the preview before adding `--apply`. Manual writes create a backup of the active curve first. Source backups are stored in `fan-backups`; packaged builds use `%LOCALAPPDATA%\StellarisFanControl\fan-backups`.

## Build the Executable

```powershell
powershell.exe -ExecutionPolicy Bypass -File .\scripts\build_exe.ps1
```

The script creates the virtual environment when necessary, prepares PawnIO, installs the build requirements, and writes `dist\StellarisFanControl.exe`. The AMD sensor module and `frontend\stellaris15gen3.css` are embedded in the executable. The signed PawnIO driver and OEM Control Center remain separate system dependencies.

The executable has no global administrator manifest, so the frontend remains non-elevated. Windows requests administrator access when the executable starts its backend role. Generated executables, PyInstaller files, downloaded modules, virtual environments, caches, and fan backups are excluded from Git.

## Recovery

Backups can be previewed and restored with:

```powershell
.\.venv\Scripts\python.exe .\backend\fan_control.py restore .\fan-backups\M2T1-TIMESTAMP.json
.\.venv\Scripts\python.exe .\backend\fan_control.py restore .\fan-backups\M2T1-TIMESTAMP.json --apply
```

The first command only displays the proposed restoration. The second command writes it.

If temperatures rise unexpectedly, do not wait for this application to recover. Enable OEM Fan Boost immediately or shut down the laptop.

## Project Documentation

- [Design decisions and ideas](DESIGN.md)
- [File, folder, runtime, and dependency structure](STRUCTURE.md)
- [Important accident records](ACCIDENTS.md)
- [Prioritized technical debt, problems, and planned work](TODO.md)
