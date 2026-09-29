# AGENTS.md

## Project scope

This repository contains experimental, hardware-specific fan control for a Stellaris 15 Gen3 / XMG-Uniwill-style laptop (DMI board `GMxZGxx`, SKU `STELLARIS1XA03`). On Windows it uses OEM Control Center 3.9.42.1; on Ubuntu it talks to the EC directly through the firmware's INOU memory window from a root systemd service. Treat every fan write and low-level sensor read as hardware-sensitive. Read `README.md` and `THIRD_PARTY_NOTICES.md` before changing behavior or dependencies.

## Safety invariants

- Never use the OEM Control Center, EC, or MQTT CPU temperature in Auto mode. That reading is the failure this project works around.
- CPU temperature must come from the validated Ryzen `Tctl/Tdie` SMN register (PawnIO on Windows, `k10temp` on Linux). GPU temperature must come from the NVIDIA driver.
- Fail closed: if either temperature is unavailable, zero, malformed, or implausible, do not write a new automatic fan target. The only exception is a Linux NVIDIA GPU whose PCI runtime status is `suspended`: it is powered off, counts as cold, and must not be woken for a reading.
- If no valid CPU or GPU reading has succeeded for 30 seconds, enable EC Fan Boost (100%) on both fans until readings recover. Meanwhile refuse Manual writes and turning Boost off. On recovery resume Automatic or restore the last Manual duties. The sensor watchdog runs every 5 seconds in every mode and must never stop on an exception.
- Auto mode uses `max(cpu_temperature, gpu_temperature)` and applies one identical target to both fans. Do not calculate separate automatic duties.
- Auto mode must never request less than 30%. It must request 100% at 80 C and above.
- Manual values below 30% must require explicit confirmation for either fan.
- Back up the active OEM curve before a manual write and once when entering Auto mode. Do not create a backup on every 15-second Auto update.
- Do not perform live fan writes as part of routine tests. A user must explicitly authorize a hardware-changing test. Prefer pure curve tests, mocks, and read-only probes.
- Preserve the OEM Fan Boost control as the immediate 100% fallback.
- Linux direct EC access must stay gated by the DMI board/SKU, the pinned DSDT SHA-256, EC project ID `0x10`, and `0x078E` bit 6. It may write only `0x0741` bit 0, `0x0751` (`0xA0` normal, `0x40` Boost), `0x07C5` bit 7, `0x07C6` bit 2, the tables `0x0F00`-`0x0F5F`, cTGP `0x0743`-`0x0746`, and the lightbar (`0x0748` bit 7, colors `0x0749`-`0x074B` at 0-36 each). Keep read-modify-write for bit fields, tables-before-enable-bits order, readback verification, rollback, and the refusal while `tccd` runs.
- Linux writes the same duty to all 16 zones of each table so the unreliable EC temperature cannot change the fan speed.
- The GPU power limit (cTGP offset) and the Dynamic Boost switch (`0x0743` bit 1) are Linux-only; the offset is limited to 0-50 W above the 115 W base and to the VBIOS maximum. The Windows GUI shows them disabled. NVIDIA applies them only while `nvidia-powerd` runs.
- Lightbar control is Linux-only and grayed out on Windows. The service writes it only when the user chose a mode, leaves the other `0x0748` bits alone, and never touches the unvalidated battery-mode copy at `0x07E2`-`0x07E5`.

## Architecture

- `backend/fan_control.py`: low-level MQTT protocol, curve transformation, backup/restore CLI, and dry-run write commands.
- `backend/fan_control_service.py`: backend-process singleton that owns the one persistent Control Center client and serializes synchronous operations.
- `backend/temperature_service.py`: independent CPU/GPU temperature acquisition. It must not import or query the Control Center service.
- `backend/fan_control_backend.py`: PySide-free backend process, automatic control scheduler, IPC server, and frontend watchdog.
- `shared/fan_control_ipc.py`: authenticated loopback IPC client plus process launch and 60-second restart-cooldown helpers.
- `shared/fan_control_common.py`: pure automatic-curve constants and calculation shared by the two processes.
- `frontend/fan_control_gui.py`: PySide6 frontend, display timers, worker lifecycle, backend watchdog, and single-GUI enforcement.
- `frontend/stellaris15gen3.css`: the complete frontend stylesheet.
- `stellaris15gen3.py`: elevated single-process packaged entry point with an in-process controller client.
- `stellaris15gen3_frontend.py`: source normal-user frontend entry point.
- `stellaris15gen3_backend.py`: source elevated background-backend entry point.
- `backend/fan_control_probe.py`: read-only MQTT diagnostic utility.
- `scripts/setup_pawnio.ps1`: installs PawnIO and downloads the pinned, hash-verified AMD module.
- `scripts/build_exe.ps1`: reproducible PyInstaller entry point.
- `scripts/launch_fan_control.ps1` and `scripts/run_fan_control_gui.cmd`: source launchers.
- `backend/linux_ec.py`: Linux EC window transport, platform gates, fan-table, cTGP, and lightbar encoding, verified writes with rollback.
- `backend/linux_sensors.py`: Linux `k10temp`, NVIDIA (with runtime-suspend detection), and RAPL CPU power readings.
- `backend/linux_fan_service.py`: Linux counterpart of `ControlCenterService`, drift repair, backups, cTGP selection (kernel sysfs first, direct EC otherwise), and the lightbar choice.
- `backend/linux_backend.py`: root systemd service runtime, persistent settings, maintenance thread, peer-credential Unix-socket server, and signal handling.
- `backend/linux_fan_control.py`: Linux low-level CLI with read-only `probe` and dry-run writes.
- `stellaris15gen3_linux_service.py`: Linux service entry point.
- `scripts/linux/`: installer, uninstaller, systemd unit, resume hook, desktop entry, release packaging, and the CPU+GPU stress test.

Keep protocol, service ownership, sensor acquisition, and presentation in these existing boundaries unless a change clearly requires otherwise.

## Concurrency and modes

- Only one GUI instance may run. Preserve the `QLocalServer` guard.
- Only backend modules and the packaged composition root may import `ControlCenterService`; frontend modules must access the controller through the injected client.
- Only one application controller, one `ControlCenterService`, and one MQTT client may run for the current user.
- Backend requests are synchronous but must run off the GUI thread.
- Keep the GUI worker pool serialized with at most one operation running. Timer callbacks must skip while busy; they must not enqueue an unbounded backlog.
- Selecting Auto tells the backend to start an immediate cycle and a non-overlapping 15-second schedule. Selecting Manual stops future Auto cycles in the backend.
- In the packaged single-process application, the window close control hides to the system tray and leaves Auto running. The dedicated confirmed Exit path and Windows session shutdown write both fans to 100% before exit. Windows session shutdown does not require another confirmation. Separate source-mode processes retain their existing watchdog behavior.
- Telemetry refreshes must not overwrite Auto status or block interaction.
- Relative status age, such as `(last updated 7 seconds ago)`, is a display-only timer and must not trigger sensor or MQTT reads.
- On Linux the root service owns fan control and starts in Automatic on every start. The GUI adopts the service's mode, its Quit only closes the GUI, and it must never run as root or start the service. Stopping the service (including shutdown) writes 100% to both fans. `SIGUSR1` (sent by the resume hook) re-verifies the EC state immediately.
- Only root and the UIDs in `/etc/stellaris-fan-control/config.json` may command the Linux service; authorization uses kernel peer credentials.

## Temperature access

- Ryzen temperature is read from SMN register `0x00059800` through PawnIO's restricted `AMDFamily17` module.
- Decode bits 31:21 in 0.125 C units and apply the 49 C range adjustment when the range/Tj selector flags indicate it.
- Use the global `Access_PCI` mutex around the SMN operation.
- PawnIO access requires administrator rights. The packaged single-process application starts with UAC and runs elevated. In separate source mode, only the backend is elevated and it must launch frontend replacements through the non-elevated Windows shell.
- The AMD module URL, commit, and SHA-256 in `scripts/setup_pawnio.ps1` are a supply-chain boundary. Do not update them without validating the new module on the target laptop and updating `THIRD_PARTY_NOTICES.md`.
- Do not reintroduce Core Temp, ACPI thermal-zone fallback, WinRing0, or a Control Center temperature fallback without explicit user direction and hardware validation.
- On Linux, read `k10temp` `temp1_input` only when its label is `Tctl`. Never use `acpitz`, the EC temperature registers, or the TUXEDO/mainline hwmon EC temperatures for control.

## Development and verification

Use the existing virtual environment when available:

```powershell
.\.venv\Scripts\python.exe -m py_compile backend\fan_control.py backend\fan_control_backend.py backend\fan_control_service.py backend\temperature_service.py frontend\fan_control_gui.py shared\fan_control_common.py shared\fan_control_ipc.py stellaris15gen3.py stellaris15gen3_frontend.py stellaris15gen3_backend.py
```

For GUI-only checks, use Qt's offscreen platform and avoid selecting Auto. Read-only live temperature checks require an elevated process and PawnIO. Clearly report when a check was not run because elevation or the target hardware was unavailable.

Build locally with:

```powershell
powershell.exe -ExecutionPolicy Bypass -File .\scripts\build_exe.ps1
```

After packaging, verify that the window opens, remains responsive, and a second launch activates the existing instance. Do not select Auto during packaging smoke tests unless a live fan write was explicitly authorized.

On Linux, use the ignored `.venv` in the repository and run the same mocked suites:

```bash
python3 -m venv .venv && ./.venv/bin/python -m pip install -r requirements.txt
QT_QPA_PLATFORM=offscreen ./.venv/bin/python -m unittest discover -s tests
```

`sudo python3 -m backend.linux_fan_control probe` is the read-only live check. Installing, starting the service, and running `scripts/linux/stress_test.py` perform live fan writes and require explicit user authorization. Agents must never run `sudo` themselves; hand the exact command to the user.

## Repository hygiene

- Work on `main` unless instructed otherwise.
- Do not commit `.venv`, `build`, `dist`, `*.spec`, `*.exe`, `fan-backups`, caches, stress-test CSV reports, DSDT dumps, or the downloaded `AMDFamily17.bin` module.
- Keep `.gitignore` aligned with packaging and generated output.
- Use ASCII for source and scripts unless an existing file requires otherwise.
- Keep changes narrowly scoped. Do not rewrite protocol constants or reverse-engineered payloads without verifying them against the installed OEM service.
- Update the README when prerequisites, safety behavior, Auto curve semantics, or build steps change.

## Documentation maintenance

- Keep `DESIGN.md` synchronized with architecture decisions, safety-relevant rationale, and design ideas.
- Keep `STRUCTURE.md` synchronized with tracked source files, dependency boundaries, and runtime/generated paths.
- Add only factual important incidents to `ACCIDENTS.md`. Each entry needs a timestamp and nested Problem, Outcomes, and Solution sections; explicitly identify unknown occurrence times.
- Keep all open debt, problems, and planned work in `TODO.md`. Every entry heading must use `PRIORITY - Title` with one of `LOW`, `MEDIUM`, `HIGH`, or `CRITICAL`, without priority-group sections.
- Update the affected living documents in the same change as the code or operational discovery that makes them stale.
