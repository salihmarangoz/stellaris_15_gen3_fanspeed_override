# AGENTS.md

## Project scope

This repository contains experimental, hardware-specific fan control for a Stellaris 15 Gen3 / XMG-Uniwill-style laptop (DMI board `GMxZGxx`, SKU `STELLARIS1XA03`). On Windows it uses the hash-pinned OEM Control Center 3.9.42.1 or 4.9.47.13 EC library; on Ubuntu it talks to the EC directly through the firmware's INOU memory window from a root systemd service. Treat every fan write and low-level sensor read as hardware-sensitive. Read `README.md` and `THIRD_PARTY_NOTICES.md` before changing behavior or dependencies.

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
- The GPU power limit (cTGP offset) and Dynamic Boost switch (`0x0743` bit 1) are limited to 0-50 W above the validated 115 W base and to the VBIOS maximum. Linux requires `nvidia-powerd`. Windows Direct EC additionally requires the exact board/SKU/BIOS, EC project and universal-control flag, the pinned OEM DLL, readable valid NVIDIA limits, and no OEM broker. Preserve other control bits, write data before enable bits, verify readback, and refuse every write/rollback byte while the broker runs. No Windows GPU writes happen until a user has chosen a setting; persist that choice and repair drift every 15 seconds. Do not touch fan/WhisperMode bytes or use private NVIDIA setters. Authorized live tests on 2026-10-11 confirmed NVIDIA enforced 115, 130, and 165 W with Dynamic Boost off. A later authorized 60-second GPU-only load reached 98-99% utilization, 112.95 W and 63 C. A subsequent 60-second FurMark run reached 159.68 W at a 160 W limit, but later NVIDIA software thermal capping reduced draw to about 100 W while the core read 59-65 C; the cause remains unresolved and must not be bypassed. Dynamic Boost switching, sustained full-ceiling load, and saved-choice recovery remain unvalidated; routine tests must remain mocked.
- Lightbar control uses `0x0748` bit 7 and `0x0749`-`0x074B` on Linux and Windows Direct EC. On Windows it must refuse while the OEM broker runs, including before every write and rollback byte. The service writes it only when the user chose a mode, leaves the other `0x0748` bits alone, verifies writes and rolls back failures, and never touches the unvalidated battery-mode copy at `0x07E2`-`0x07E5`. Windows RPM and lightbar behavior still require target-hardware validation.
- NVIDIA software thermal throttling is optional display telemetry, read together with GPU watts. Show the header warning only for an explicit active flag, and clear it on inactive/unknown/failed telemetry. Never use this flag as an Auto temperature input or change thermal protections because of it.
- Windows power draw is optional display telemetry. GPU watts use a separate, bounded NVIDIA query; CPU package watts use energy deltas from the unchanged pinned PawnIO module's read-only MSRs `0xC0010299` and `0xC001029B`. Neither may invalidate temperatures or participate in Automatic control. Keep the first CPU sample unavailable, serialize sampling, handle 32-bit wrap, and reset after failures or long gaps. Never call a PawnIO write operation.

## Architecture

- `backend/fan_control.py`: low-level MQTT protocol, curve transformation, backup/restore CLI, and dry-run write commands.
- `backend/fan_control_service.py`: backend-process singleton that owns the one persistent Control Center client and serializes synchronous operations.
- `backend/temperature_service.py`: independent CPU/GPU temperature acquisition. It must not import or query the Control Center service.
- `backend/fan_control_backend.py`: PySide-free backend process, automatic control scheduler, IPC server, and frontend watchdog.
- `shared/fan_control_ipc.py`: authenticated loopback IPC client plus process launch and 60-second restart-cooldown helpers.
- `shared/fan_control_common.py`: pure automatic-curve calculation and lightbar validation shared by both platforms.
- `backend/direct_fan_control.py`: validated Windows OEM DLL access, fan tables, optional RPM telemetry, and gated, verified cTGP/lightbar writes.
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

Run the packaged `--check-qt` switch before installing or launching a rebuilt executable. It exercises the Qt DLLs and offscreen plugin without importing hardware backends. Keep the build script's restricted DLL-discovery PATH; unrelated tool directories can contribute incompatible ICU/OpenSSL binaries.

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

## Experimental MUX firmware boundary

- MUX selection is separate from the EC write allowlist. backend/gpu_mux.py may change only data byte 0x62 of the exact 180-byte UniWillVariable / GUID 9f33f85c-13ca-4fd1-9c4a-96217722c593, AMD values 0 (Hybrid) or 1 (dGPU). Preserve all other bytes and attributes 7; require exact DMI/BIOS/DSDT, SSID 0x11141D05 and project ID 0x10.
- Require two user confirmations. The final dialog must require exactly YES DO IT, without trimming or case conversion; enforce it in the backend too. Cancel is the safe default of the first dialog. In the final dialog Enter saves only once the exact phrase is typed and otherwise does nothing; Esc and Cancel cancel (owner decision 2026-10-16).
- On Linux also require the polkit administrator password (action `io.github.salihmarangoz.stellarisfancontrol.set-gpu-mux`, `auth_admin`, never kept) after YES DO IT and before the backup, for the kernel-reported requesting process. Ask only when the mode changes; fail closed on denial, cancel, timeout, or an unknown requester.
- Back up before writes, reject concurrent changes/OEM ownership, verify full readback. Never retry, periodically reapply, blindly roll back a whole variable, reboot, or change firmware protections automatically. The one exception (owner decision 2026-10-15): inside a confirmed, password-authorized Linux save, the service may clear the efivarfs immutable flag of UniWillVariable for that single write and must restore it right after. Never remount efivarfs or touch other variables' flags. Report saved mode without claiming it is the active route.
- Routine MUX tests must use fake transports or ordinary temporary files. The owner authorized operating the official switch for read-only comparison, not arbitrary custom firmware-write tests. Those and Linux/reboot validation remain pending.
- Store tracked OEM findings in cc_analysis/; keep raw proprietary binaries, decrypted code and firmware/memory dumps in ignored build/oem-analysis/.

## Control Center 4.9 fan compatibility

- The additional exact DLL hash ee2c506dd414222013e6dbe5c67a774b1fa1387a47ad5d705a82f048c1a945bb requires the exact Windows board/SKU/BIOS and EC universal-control bit. Keep unknown libraries refused.
- On that image subsystem states 0/1/3/4/5/7 are accepted; set only bit 2 of 0x07C6 and bit 7 of 0x07C5 while preserving other bits. The owner-authorized target check observed 3 becoming 7, uniform raw 90 fan tables and both RPM readings with the OEM service stopped. Sustained Auto and additional feature checks remain pending.
- Check OEM broker ownership before every individual direct EC write, including rollback. Keep OEM driver/DLL installed when stopping its UI/service. Details: cc_analysis/control-center-4.9-fans.md.
- Windows ownership checks use the fresh IPv6 listener table in backend/windows_broker.py. Cache only the System32 API binding, never absence; query/parsing errors must refuse writes. Do not reintroduce timed socket connects per byte or weaken exclusion to improve performance. Authorized Manual latency results and the owner's 50% test floor: cc_analysis/windows-direct-ec-latency.md.

## Slider commit behavior

- Every slider drag must preview locally and send no setting until release. Cancel a pending timer on press and guard queued apply callbacks while the relevant slider group is down. Preserve keyboard/wheel debounce, number editing and low-duty confirmation. Temperature spin-box focus-out must not start a synchronous write as another slider is pressed. Cover this with mocked/offscreen tests, without live hardware writes.
