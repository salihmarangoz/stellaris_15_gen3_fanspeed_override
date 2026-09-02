# AGENTS.md

## Project scope

This repository contains experimental, hardware-specific fan control for a Stellaris 15 Gen3 / XMG-Uniwill-style laptop using OEM Control Center 3.9.42.1. Treat every fan write and low-level sensor read as hardware-sensitive. Read `README.md` and `THIRD_PARTY_NOTICES.md` before changing behavior or dependencies.

## Safety invariants

- Never use the OEM Control Center, EC, or MQTT CPU temperature in Auto mode. That reading is the failure this project works around.
- CPU temperature must come from the validated Ryzen `Tctl/Tdie` SMN path. GPU temperature must come from the NVIDIA driver.
- Fail closed: if either temperature is unavailable, zero, malformed, or implausible, do not write a new automatic fan target.
- Auto mode uses `max(cpu_temperature, gpu_temperature)` and applies one identical target to both fans. Do not calculate separate automatic duties.
- Auto mode must never request less than 30%. It must request 100% at 80 C and above.
- Manual values below 30% must require explicit confirmation for either fan.
- Back up the active OEM curve before a manual write and once when entering Auto mode. Do not create a backup on every 15-second Auto update.
- Do not perform live fan writes as part of routine tests. A user must explicitly authorize a hardware-changing test. Prefer pure curve tests, mocks, and read-only probes.
- Preserve the OEM Fan Boost control as the immediate 100% fallback.

## Architecture

- `fan_control.py`: low-level MQTT protocol, curve transformation, backup/restore CLI, and dry-run write commands.
- `fan_control_service.py`: backend-process singleton that owns the one persistent Control Center client and serializes synchronous operations.
- `temperature_service.py`: independent CPU/GPU temperature acquisition. It must not import or query the Control Center service.
- `fan_control_backend.py`: PySide-free backend process, automatic control scheduler, IPC server, and frontend watchdog.
- `fan_control_ipc.py`: authenticated loopback IPC client plus process launch and 60-second restart-cooldown helpers.
- `fan_control_common.py`: pure automatic-curve constants and calculation shared by the two processes.
- `fan_control_gui.py`: PySide6 frontend, display timers, worker lifecycle, backend watchdog, and single-GUI enforcement.
- `stellaris15gen3.py`: source and packaged process-role launcher.
- `fan_control_probe.py`: read-only MQTT diagnostic utility.
- `setup_pawnio.ps1`: installs PawnIO and downloads the pinned, hash-verified AMD module.
- `build_exe.ps1`: reproducible PyInstaller entry point.

Keep protocol, service ownership, sensor acquisition, and presentation in these existing boundaries unless a change clearly requires otherwise.

## Concurrency and modes

- Only one GUI instance may run. Preserve the `QLocalServer` guard.
- Only the backend may import `ControlCenterService`, read hardware temperatures, or perform fan writes.
- Only one backend, one `ControlCenterService`, and one MQTT client may run for the current user.
- Backend requests are synchronous but must run off the GUI thread.
- Keep the GUI worker pool serialized with at most one operation running. Timer callbacks must skip while busy; they must not enqueue an unbounded backlog.
- Selecting Auto tells the backend to start an immediate cycle and a non-overlapping 15-second schedule. Selecting Manual stops future Auto cycles in the backend.
- The backend must keep Auto mode running if the frontend exits unexpectedly. Each process may attempt to restart the other no more than once per 60 seconds.
- An intentional frontend close must detach from the backend so the watchdog does not reopen it.
- Telemetry refreshes must not overwrite Auto status or block interaction.
- Relative status age, such as `(last updated 7 seconds ago)`, is a display-only timer and must not trigger sensor or MQTT reads.

## Temperature access

- Ryzen temperature is read from SMN register `0x00059800` through PawnIO's restricted `AMDFamily17` module.
- Decode bits 31:21 in 0.125 C units and apply the 49 C range adjustment when the range/Tj selector flags indicate it.
- Use the global `Access_PCI` mutex around the SMN operation.
- PawnIO access requires administrator rights. Source and packaged launchers must preserve elevation behavior.
- The AMD module URL, commit, and SHA-256 in `setup_pawnio.ps1` are a supply-chain boundary. Do not update them without validating the new module on the target laptop and updating `THIRD_PARTY_NOTICES.md`.
- Do not reintroduce Core Temp, ACPI thermal-zone fallback, WinRing0, or a Control Center temperature fallback without explicit user direction and hardware validation.

## Development and verification

Use the existing virtual environment when available:

```powershell
.\.venv\Scripts\python.exe -m py_compile fan_control.py fan_control_common.py fan_control_ipc.py fan_control_backend.py fan_control_gui.py fan_control_service.py stellaris15gen3.py temperature_service.py
```

For GUI-only checks, use Qt's offscreen platform and avoid selecting Auto. Read-only live temperature checks require an elevated process and PawnIO. Clearly report when a check was not run because elevation or the target hardware was unavailable.

Build locally with:

```powershell
powershell.exe -ExecutionPolicy Bypass -File .\build_exe.ps1
```

After packaging, verify that the window opens, remains responsive, and a second launch activates the existing instance. Do not select Auto during packaging smoke tests unless a live fan write was explicitly authorized.

## Repository hygiene

- Work on `main` unless instructed otherwise.
- Do not commit `.venv`, `build`, `dist`, `*.spec`, `*.exe`, `fan-backups`, caches, or the downloaded `AMDFamily17.bin` module.
- Keep `.gitignore` aligned with packaging and generated output.
- Use ASCII for source and scripts unless an existing file requires otherwise.
- Keep changes narrowly scoped. Do not rewrite protocol constants or reverse-engineered payloads without verifying them against the installed OEM service.
- Update the README when prerequisites, safety behavior, Auto curve semantics, or build steps change.
