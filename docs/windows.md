# Windows Guide

## Requirements

- Windows 11 on the [validated laptop](../README.md#supported-hardware-and-software)
- OEM Control Center 3.9.42.1 or 4.9.47.13 installed; its service may be running or stopped
- The signed PawnIO driver (the installer sets it up)
- Administrator access
- The NVIDIA driver, with its `nvml.dll` in `System32`
- Python 3.11 or newer, only when running from source

## Install from a Release

No Python is needed.

1. Download `StellarisFanControl-windows-x64.zip` and its `.sha256` file from the [latest release](https://github.com/salihmarangoz/stellaris_15_gen3_fanspeed_override/releases/latest). The GitHub-generated source archives do not contain the executable.
2. Optionally compare `Get-FileHash .\StellarisFanControl-windows-x64.zip -Algorithm SHA256` with the checksum, then extract the entire ZIP to a folder.
3. Open PowerShell in the extracted folder and run:

   ```powershell
   powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\install.ps1
   ```

Approve the administrator prompt with your own administrator account. The script installs or updates PawnIO when needed (this step needs an Internet connection and Windows Package Manager), verifies the pinned AMD module, copies the application into `%ProgramFiles%\StellarisFanControl`, and registers the **Stellaris Fan Control** startup task. Control Center and the NVIDIA driver must already be installed.

The task starts the application with highest privileges whenever you sign in, including after a reboot. It uses your interactive sign-in instead of system boot, so the window and tray icon are on your desktop. Installation does not launch the application, so it never writes fan targets as a side effect. Launch `%ProgramFiles%\StellarisFanControl\StellarisFanControl.exe` yourself, or sign out and back in. Every launch starts Automatic mode and can write fan targets.

**Updating**: exit the running application with its confirmed **Exit** action, then run the installer from the new release. Your `StellarisFanControl.json` is preserved and the startup task is replaced. The extracted folder can be moved afterwards without breaking startup.

**Autostart**: disable **Stellaris Fan Control** in Task Scheduler to stop it starting at sign-in. Enable the task or rerun the installer to turn it back on.

Release builds are not code-signed, so Windows may show an unknown-publisher warning.

## Run from Source

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\scripts\run_fan_control_gui.cmd
```

The launcher runs `scripts\setup_pawnio.ps1`, which installs PawnIO when necessary and downloads the pinned, SHA-256-verified AMD sensor module, then starts the GUI as a normal user. The PawnIO installer may show its own one-time setup prompt. Afterwards, Windows asks for administrator access only for the hardware backend. Core Temp is not required.

## How Fan Control Works on Windows

- **OEM MQTT**: while the OEM `GCUBridge` broker runs, fan commands use Control Center's existing MQTT interface. This path is validated with Control Center 3.9.42.1.
- **Direct EC**: when the broker is stopped or unavailable, the backend writes the EC fan tables through the installed, hash-validated Uniwill ACPI driver library. It switches back to OEM MQTT when the broker returns.
- The **Start/Stop GCUBridge** button in the sensor panel switches between the two after a confirmation. The badge next to **Exit** shows which one is active.
- CPU temperature is read from the Ryzen SMN register through PawnIO. GPU temperature, power draw and power limits come from NVIDIA's NVML library, queried in a small helper process that the app kills and restarts if a query hangs. Control Center's CPU temperature is never used.
- While Windows reports the NVIDIA GPU powered off (device power state D3), it is not queried, so it is not woken. Automatic mode then uses the CPU temperature alone, so both fans follow the CPU.

Details of the Direct EC checks and verification are in the [safety model](safety.md#windows).

### Control Center 4.9

The exact 4.9.47.13 ACPI library is supported for Direct EC on the validated board, SKU, BIOS and universal EC platform. Its inactive fan subsystem can be 0 or 3; activation preserves unrelated bits instead of replacing the whole subsystem byte. To use Direct EC, stop the Control Center GUI and `GCUBridge` but keep the OEM driver and DLL installed: uninstalling Control Center can remove them. [Analysis and observed state](../cc_analysis/control-center-4.9-fans.md).

## Application Process

The packaged application runs as one elevated process. Windows asks for UAC approval as soon as `StellarisFanControl.exe` starts. The interface, the Automatic scheduler, sensor access and fan control share that process; internal requests still pass through the same serialized controller boundary.

Source mode keeps a separate normal-user frontend and an elevated backend that talk over authenticated loopback IPC. The packaged application dispatches requests inside the process and does not launch a companion executable.

## GPU Power Limit and Lightbar on Windows

Both are available only in Direct EC mode, while `GCUBridge` is stopped.

**GPU power limit** (115-165 W, capped by the VBIOS) and **Dynamic Boost** require the exact validated board, SKU, BIOS N.1.61A15, EC project and support flag, OEM DLL, readable NVIDIA limits, and no OEM broker. Until you choose a setting, the app only reads these registers. Your choice is saved in `%LOCALAPPDATA%\StellarisFanControl\gpu-power.json` and restored after drift, restart, or resume by the 15-second maintenance check. The displayed NVIDIA limit is the one the driver actually enforces.

**Lightbar** offers Color, Rainbow, and Off. The choice is saved in `%LOCALAPPDATA%\StellarisFanControl\lightbar.json` and restored within the next 15-second check after startup or resume. A refresh is forced every 15 minutes, only while Direct EC is available. All lightbar writes stop while the OEM broker runs.

## Shutdown and Exit

Closing the window hides it in the tray; Automatic mode keeps running.

The **Exit** button and the tray's **Exit** action ask for confirmation. Then the controller stops Automatic scheduling, turns Fan Boost off, writes 100% to both fans, and exits only if that write succeeds.

Shutdown, restart and sign-out also request 100% on both fans before acknowledging, without an extra confirmation. The request waits behind any active frontend operation; Automatic scheduling stops after a successful write. If the request fails or times out, the app asks Windows to cancel the shutdown. If another application cancels shutdown after the write succeeded, Fan Control still exits with both fans at 100%. Forced termination, power loss, or Windows ending the process before the write completes cannot be guarded against.

## Low-Level CLI

Write commands are dry runs unless `--apply` is given:

```powershell
.\.venv\Scripts\python.exe .\backend\fan_control.py status
.\.venv\Scripts\python.exe .\backend\fan_control.py rpm
.\.venv\Scripts\python.exe .\backend\fan_control.py fixed 65 --gpu-duty 75
```

Inspect the preview before adding `--apply`. Manual writes back up the active curve first. Source runs keep backups in `fan-backups`; the packaged application uses `%LOCALAPPDATA%\StellarisFanControl\fan-backups`.

## Recovery

If temperatures rise unexpectedly, do not wait for this application to recover. Enable Fan Boost immediately or shut down the laptop.

Preview and restore an OEM MQTT backup:

```powershell
.\.venv\Scripts\python.exe .\backend\fan_control.py restore .\fan-backups\M2T1-TIMESTAMP.json
.\.venv\Scripts\python.exe .\backend\fan_control.py restore .\fan-backups\M2T1-TIMESTAMP.json --apply
```

The first command only shows the proposed restoration; the second writes it.

A `DIRECT_EC-*.json` backup must use the direct path while the OEM broker is stopped:

```powershell
.\.venv\Scripts\python.exe .\backend\fan_control.py restore .\fan-backups\DIRECT_EC-TIMESTAMP.json --direct
.\.venv\Scripts\python.exe .\backend\fan_control.py restore .\fan-backups\DIRECT_EC-TIMESTAMP.json --direct --apply
```

If Control Center's own GUI looks broken after custom curves, restore a backup, reset Control Center, or reinstall it.

## Validation Status

All tests below were owner-authorized and ran on the one validated laptop.

- **Direct EC fans and RPM** (2026-10-15, Control Center 4.9): Direct EC fan-table writes and both RPM readings confirmed with the OEM service stopped. Sustained Automatic operation and further feature tests are pending.
- **OEM ownership checks** (2026-10-16): with the fresh listener-table check, Boost on/off took 88/69 ms and full verified fan updates 3.2-3.3 seconds in Manual mode. These are command-completion times, not physical fan acceleration. [Latency analysis](../cc_analysis/windows-direct-ec-latency.md).
- **Sensor-loss Boost** (2026-10-16, Direct EC): with an aged success timestamp and an injected failing reader, Boost engaged in 66 ms and recovery restored the saved Manual 60/60 targets, ending at a verified 100/100. This validates the fallback action, not the real 30-second timer or a hung native driver. [Safety review](../cc_analysis/windows-safety-review-2026-10-16.md).
- **GPU power limit** (2026-10-11): NVIDIA enforced 115, 130 and 165 W with Dynamic Boost off. A 60-second GPU-only CUDA load kept 98-99% utilization while the slider moved the limit from 115 to 155 W (165 W during cooldown); peak draw was 112.95 W, GPU 63 C, CPU 57 C, both fans 75%, and it ended without a temperature abort. That load never reached the selected ceiling.
- **FurMark**: a later 60-second run reached about 159.7 W at a 160 W limit and 144.5 W at 145 W. NVIDIA software thermal capping later reduced draw to about 100 W while the GPU core read 59-65 C; the cause is unresolved. This confirms transient, not sustained, full-ceiling operation. The limit was changed during the run, so it does not establish a fixed 155 W result.
- **Not yet validated**: sustained 165 W draw and long-duration thermal stability, Dynamic Boost switching, restoring saved GPU settings after restart or resume, lightbar writes on Windows, Control Center 4.9 cTGP/lightbar writes, and reboot/recovery behavior with Control Center 4.9.

The downloaded Control Center 4.9 package was inspected statically, without installation; its protected setter bodies and any additional NVIDIA calls could not be recovered.
