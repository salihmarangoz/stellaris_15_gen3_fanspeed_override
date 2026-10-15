# Stellaris 15 Gen3 Fan Control

An experimental fan-control application for my Stellaris 15 Gen3 laptop on Windows 11 and Ubuntu 24.04, designed to work around an unreliable OEM CPU-temperature path.

> [!CAUTION]
> I built this project for my own Stellaris 15 Gen3, and it remains experimental. It replaces fan curves through the installed OEM Control Center service or the validated OEM EC driver interface on Windows, and through the firmware's EC memory window on Ubuntu. A software defect, invalid sensor value, or incompatible laptop can cause overheating, instability, hardware damage, or data loss. Use it at your own risk, keep an independent temperature monitor visible, and be ready to enable Fan Boost or shut down the laptop.

> [!WARNING]
> This project can interfere with the installed OEM fan-control application or its configuration. After custom curves are written, the Control Center GUI may display unusual curves, incorrect-looking values, broken layouts, or other unexpected behavior. Recovery may require restoring a backup, resetting Control Center, or reinstalling it. This project does not intentionally modify OEM program files, but it does change the fan data consumed by that software.

![Stellaris 15 Gen3 Fan Control on Ubuntu in Automatic mode, with temperature, fan, and power gauges, the GPU power limit, and the Lightbar and NVIDIA Optimus buttons](assets/ss.png)

## Why I Built This

The embedded controller (EC) on my laptop stopped providing a reliable CPU temperature. As a result, the original fan curve could leave the system undercooled while the CPU temperature increased.

This application works around that failure. It never uses the EC's CPU temperature. It reads the CPU temperature directly from the Ryzen processor and the GPU temperature from the NVIDIA driver, and drives both fans from them.

## Features

- **Automatic mode**: both fans follow the hotter of CPU and GPU, from 30% to 100% between two temperatures you choose. 80 C and above always means 100%.
- **Manual mode**: separate CPU and GPU fan targets, optionally mirrored. Values below 30% need confirmation.
- **Fan Boost 100%**: the OEM's immediate full-speed fallback, always one click away.
- **Sensor panel**: CPU/GPU temperatures, fan duties and RPM, and CPU/GPU power draw.
- **GPU power limit**: raise the NVIDIA sustained power limit from 115 W up to 165 W, with a Dynamic Boost switch.
- **Lightbar**: solid color, rainbow, or off.
- **NVIDIA Optimus** (experimental): choose Hybrid or dGPU-only display mode for the next boot.
- **Fail-safe**: an invalid sensor reading never produces a new automatic target; if neither temperature can be read for 30 seconds, both fans go to 100% until readings recover.
- Runs in the system tray and starts in Automatic mode.

How each feature behaves is described in [Using the app](docs/usage.md). What protects your laptop, and what doesn't, is in the [safety model](docs/safety.md).

## Supported Hardware and Software

Only one laptop has been validated: a Stellaris 15 Gen3 (XMG/Uniwill board `GMxZGxx`, SKU `STELLARIS1XA03`) with an AMD Ryzen CPU, an NVIDIA GPU, and BIOS N.1.61A15. Direct EC access is refused unless platform checks for this exact laptop pass. MQTT messages, table names, curve formats, authentication, and EC behavior may differ on other laptops or Control Center versions; similar hardware is not proof of compatibility.

| | Windows | Ubuntu |
| --- | --- | --- |
| OS | Windows 11 | Ubuntu 24.04, Secure Boot kernel lockdown off |
| Fan access | OEM Control Center 3.9.42.1 (OEM MQTT or direct EC) or 4.9.47.13 (direct EC only) | Direct EC through the firmware's memory window; no TUXEDO software or extra kernel modules |
| CPU temperature | Ryzen `Tctl` through the signed PawnIO driver | Kernel `k10temp` driver |
| GPU temperature | NVIDIA driver (`nvidia-smi`) | NVIDIA driver (`nvidia-smi`) |

## Install

**Windows**: download `StellarisFanControl-windows-x64.zip` from the [latest release](https://github.com/salihmarangoz/stellaris_15_gen3_fanspeed_override/releases/latest), extract it, and run in PowerShell:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\install.ps1
```

Requirements, updating, running from source, and the low-level CLI: [Windows guide](docs/windows.md).

**Ubuntu**: stop TUXEDO Control Center's daemon if it is installed, then from an extracted `StellarisFanControl-linux.tar.gz` release or a clone of this repository run:

```bash
sudo ./scripts/linux/install.sh
```

Requirements, uninstalling, the service, and the stress test: [Ubuntu guide](docs/linux.md).

## If Something Goes Wrong

If temperatures rise unexpectedly, do not wait for this application to recover. Enable Fan Boost immediately or shut down the laptop. The application backs up the active fan curve before Manual writes and when it enters Automatic mode; restore steps are in the [Windows](docs/windows.md#recovery) and [Ubuntu](docs/linux.md#recovery) guides.

## Documentation

For users:

- [Using the app](docs/usage.md): modes, the automatic curve, sliders, sensors, GPU power, lightbar, NVIDIA Optimus, settings
- [Safety model](docs/safety.md): temperature sources, fail-closed rules, the sensor watchdog, what is written to the EC, known limits
- [Windows guide](docs/windows.md): requirements, install, OEM MQTT and Direct EC, recovery, validation status
- [Ubuntu guide](docs/linux.md): requirements, install, the service, recovery, stress test

For developers:

- [Development](docs/development.md): tests, building, packaging, publishing a release
- [Design decisions](DESIGN.md), [file and dependency structure](STRUCTURE.md), [open work](TODO.md), [accident records](ACCIDENTS.md)
- [OEM Control Center analysis](cc_analysis/README.md)
- [Third-party notices](THIRD_PARTY_NOTICES.md)
