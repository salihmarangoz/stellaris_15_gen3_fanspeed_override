# Design

This is the living design record for Stellaris 15 Gen3 fan control. It explains why the project is shaped this way and records decisions that should survive individual code changes. Safety requirements in `AGENTS.md` take precedence if this document ever falls behind the code.

Last reviewed: 2026-09-29

## Goals

- Keep automatic fan control alive when the GUI crashes or PySide6 cannot start.
- Replace the untrustworthy OEM CPU-temperature input with the validated Ryzen `Tctl/Tdie` path.
- Keep hardware access and fan writes behind a small, elevated backend.
- Make every automatic decision predictable, conservative, and testable without live hardware writes.
- Preserve an immediate OEM Fan Boost fallback and recoverable curve backups.
- Run the same safety logic on Windows 11 and Ubuntu 24.04, with platform adapters only where hardware access, privilege, startup, and packaging differ.

## Non-goals

- Supporting laptops or OEM Control Center versions that have not been validated on the target hardware.
- Replacing or patching OEM program files.
- Reading CPU temperature from the OEM service, EC, MQTT, Core Temp, ACPI thermal zones, WinRing0, or TUXEDO Control Center.
- Depending on TUXEDO Control Center or out-of-tree kernel modules on Linux.
- Providing remote control. IPC is loopback-only.

## Process design

```text
normal-user frontend
        |
        | authenticated loopback JSON requests
        v
elevated backend ----> Ryzen SMN through PawnIO
        |              NVIDIA temperature through nvidia-smi
        v
ControlCenterService --+-> one persistent OEM MQTT client (broker available)
                       `-> validated direct EC client (broker unavailable)
```

The backend owns the safety-critical state, automatic scheduler, temperature reads, and the only `ControlCenterService`. The frontend owns presentation and user confirmation. Backend validation is still authoritative because a UI check alone is not a security or safety boundary.

`ControlCenterService` selects OEM MQTT while the `GCUBridge` broker is listening and direct EC access only while it is unavailable. A method change closes the old client before opening the other. Direct writes use the installed Control Center 3.9.42.1 `ACPIDriverDll.dll` and `UWACPIDriver`, validate the exact DLL SHA-256 and EC project ID, reject malformed tables, write both fans under the existing service lock, and verify readback. They refuse to begin while the broker is present. This preserves the OEM path as the fallback without letting two writers intentionally operate at once.

Stopping `GCUBridge` clears all six RAM Fan 1.5 curve blocks and changes the OEM application/fan-subsystem state. The backend therefore caches the last complete OEM curve after successful OEM reads or writes. Direct activation reproduces the driver-call order observed from Control Center: assert application presence and fan-subsystem state, write the primary and mirrored fan-control bytes, then interleave CPU/GPU up thresholds, down thresholds, and duties. All 96 curve bytes and the control state are read back. An absent cache with cleared EC tables fails closed.

The direct fan locations are volatile EC RAM rather than an EEPROM/firmware update path. This is supported by the matching Uniwill implementation in TUXEDO's hardware driver, which names the same `0x0751`, `0x07C5`, `0x07C6`, and `0x0F00`-`0x0F5F` locations EC RAM and routinely rewrites the fan tables during initialization. The installed OEM DLL also exports distinct `WriteEC` and `WriteCMOS` functions; this application imports only `ReadEC` and `WriteEC`. This substantially reduces write-endurance concerns, but does not make arbitrary EC writes safe: the OEM firmware and Windows driver remain proprietary, so target validation, exact address restrictions, serialization, readback, and rollback remain mandatory.

The packaged `StellarisFanControl.exe` carries an administrator manifest and runs the PySide6 interface and backend controller in one elevated process. The interface uses an in-process client with the same synchronous dispatch boundary, while its one-worker pool keeps controller operations off the GUI thread. Closing the window hides it in the system tray and preserves the controller and Automatic scheduler. Only the dedicated Exit controls perform the confirmed 100% shutdown. The separate source entry points and authenticated loopback transport remain available for development.

## Automatic-control design

An automatic cycle reads both independent sensors, rejects unavailable, zero, malformed, or implausible values, and calculates a single target from `max(cpu_temperature, gpu_temperature)`. The same target is written to both fans.

The default curve is 30% at 35 C and 100% at 75 C. The endpoints can be adjusted from 0 to 100 C, but the hard 80 C safety cap still forces 100%. Targets are rounded to 5% steps and can never fall below 30% in Automatic mode.

Entering Automatic mode creates one backup before the first write. Later 15-second cycles reuse that protection instead of producing a backup every time. Changing to Manual stops future automatic cycles. Fan Boost pauses automatic writes while it is enabled.

## Sensor-loss emergency

The backend records the time of every successful, validated sensor read. A watchdog thread reads the sensors whenever no read was attempted in the last 5 seconds, in every mode. When no read has succeeded for 30 seconds, it enables EC Fan Boost, which reached 100% within about two seconds in the live test, whereas a 100% table ramps at about 1.7% per second. While the emergency lasts, Manual writes and turning Boost off are refused, and the EC Boost state is not mistaken for a user Boost. When readings return, Automatic mode resumes with the next validated target, which leaves Boost, or Manual mode restores the last written duties.

"Stale" means that no valid reading succeeded: errors, timeouts, zero, malformed, or implausible values, or a missing GPU. An unchanged value is not treated as stale, because the NVIDIA driver reports whole degrees that legitimately stay constant at idle, and both sources read live hardware registers rather than the frozen EC copy. A runtime-suspended Linux GPU is powered off and counts as a valid cold reading.

The sensor status has its own lock, which is never held while the state lock is acquired, so the watchdog cannot deadlock with the service-change path that reads sensors while holding the state lock.

## Linux design

```text
normal-user tray GUI (PySide6)
        |
        | JSON over /run/stellaris-fan-control/backend.sock (peer-credential check)
        v
root systemd service ----> k10temp Tctl, nvidia-smi, RAPL package energy
        |
        v
LinuxFanService -> LinuxEcClient -> /dev/mem window 0xFE200000 (INOU EC RAM)
                \-> uniwill-laptop or tuxedo-drivers ctgp_offset sysfs (cTGP)
```

The BIOS N.1.61A15 DSDT defines `\_SB.INOU.ECRR`/`ECRW` as single-byte accesses to `0xFE200000 + address` under an AML mutex, with no mailbox protocol. A root process can therefore perform the same accesses through an `O_SYNC` `/dev/mem` mapping; the Ubuntu kernel enables `STRICT_DEVMEM` but not `IO_STRICT_DEVMEM`, and the window is not RAM. Accesses are paced at 6 ms like the OEM software and the mainline driver. Because each access is one byte, concurrent AML users cannot corrupt a transaction; the only shared state is the bit-level enable registers, which neither the firmware nor the mainline driver writes except `0x0741` bit 0, which both sides set. The DSDT SHA-256 is pinned like the Windows OEM DLL hash, so a BIOS update disables direct control until it is revalidated.

The register meanings match the mainline `uniwill-laptop` register map and TUXEDO's driver: `0x0741` bit 0 enables manual control, `0x0751` is the fan mode (`0x80` user, `0x20` high, `0x40` Boost), `0x07C5` bit 7 splits the CPU and GPU tables, `0x07C6` bit 2 enables the `0x0F00`-`0x0F5F` tables (end temperature, start temperature, duty per zone), `0x075B`/`0x075C` are the live duties, and `0x0464`/`0x046C` hold big-endian fan RPM. `0x07C5` bit 5 (`WHMS`) is forwarded to the NVIDIA driver as WhisperMode by the firmware, so Linux leaves it untouched. The Windows direct path still writes whole OEM-traced bytes there.

Linux writes the TUXEDO zone thresholds (0-115 C, then single-degree zones up to 131 C) with the same duty in all 16 zones, so the stale EC temperature selects a zone but cannot change the duty. A duty of 0% is written as raw 1, because raw 0 makes the EC run the fan at 30% for three minutes first. The live test on 2026-09-29 confirmed that the EC follows these tables with `0x0751` set to `0x00` or `0xA0`, and that `0x40` forces 100% immediately. Tables are written before the enable bits, only changed bytes are written, every write is read back, and a mismatch restores the previous bytes.

The service starts in Automatic mode on every start, persists only the Automatic endpoints and the GPU power offset in `/var/lib/stellaris-fan-control/settings.json`, and re-asserts the last requested fan state and cTGP offset every 15 seconds and on `SIGUSR1` from the resume hook. `SIGTERM` stops the IPC server and writes 100% to both fans before exiting. systemd restarts the service after any failure without a start limit. Writes are refused while `tccd` runs so that two controllers never fight.

When the NVIDIA GPU's PCI runtime status is `suspended`, it is powered off; querying it would wake it, so Automatic mode uses the CPU temperature alone. Otherwise the GPU reading remains mandatory and fails closed.

The GPU power limit uses the cTGP offset register `0x0744` (watts above the 115 W base, capped at 50 W and at the VBIOS maximum reported by `nvidia-smi`). The service writes the kernel driver's `ctgp_offset` attribute when `uniwill-laptop` or `tuxedo-drivers` provides it, and otherwise programs `0x0743`-`0x0746` itself the way those drivers do. Dynamic Boost is `0x0743` bit 1 with its 25 W amount in `0x0746`; neither kernel driver exposes a switch, so the service toggles the bit directly with TUXEDO's `db_enable` semantics (clearing the general-enable bit only when cTGP is off as well) and re-asserts the saved choice every 15 seconds, which also undoes the mainline driver's resume-time re-enable.

The NVIDIA driver applies these firmware limits only while `nvidia-powerd` runs; without it the enforced limit stays at the 115 W default. Measured on 2026-09-29 with `nvidia-powerd` running, `enforced.power.limit` was `min(115 + offset + 25, 165)` W and followed offset changes within 1.5 seconds without restarting the daemon. TUXEDO Control Center displays the same model (cTGP plus a "Dynamic Boost range" of `min(max - default - offset, 25)`) and relies on TUXEDO's driver packages to install and enable `nvidia-powerd` with its D-Bus policy. Ubuntu's own driver packages ship the unit only as documentation, so the installer does the same setup when the daemon is not running.

The lightbar is driven through the same window with the registers tuxedo-drivers uses for this SKU (the mainline driver has the code but does not enable it for `GMxZGxx`, a board name the Stellaris shares with the lightbar-less Polaris). `0x0749`-`0x074B` hold red, green, and blue from 0 to 36, and `0x0748` bit 7 runs the firmware's rainbow animation, which is also its power-on default. Color writes the three levels and then clears bit 7; Off writes 0/0/0 and clears bit 7, as tuxedo-drivers initializes it; Rainbow sets bit 7 only. The other `0x0748` bits and the battery-mode copy at `0x07E2`-`0x07E5` that the mainline driver also writes are left alone until validated. The EC does not keep these bytes across a reboot, so the service stores the choice, including the last color while Off or Rainbow is shown, and the 15-second check re-applies it after boot or resume. That check reads the four bytes and writes only when they differ. Because the lightbar may also forget its state while the bytes still match, the service additionally rewrites them every 15 minutes, at the first check after a service start, and whenever the user picks an option; such a routine refresh is not logged as drift. Until the user picks a mode, the service never touches the lightbar.

The GUI is the Windows GUI with platform switches: it adopts the service's mode instead of forcing Automatic, stores its preferences under `~/.config/stellaris-fan-control`, hides the GCUBridge button, and its **Quit** closes only the GUI. It never starts or elevates the service; while the service is unavailable it shows the reason and retries quietly.

## Concurrency design

- The backend process lock allows one backend per user runtime directory.
- `ControlCenterService` is a process-wide singleton with a serialized operation lock.
- Automatic scheduling runs in the backend and does not depend on the Qt event loop.
- A sensor lock prevents overlapping PawnIO and NVIDIA reads.
- Automatic state is checked again after sensor acquisition so a late Manual-mode or Fan Boost change prevents the pending write.
- The GUI worker pool has one thread and skips timer work while an operation is already active.

## IPC design

The backend binds an ephemeral IPv4 loopback port and writes its host, port, and random token to `%LOCALAPPDATA%\StellarisFanControl\backend-endpoint.json`. Requests and responses are newline-delimited JSON with a 64 KiB limit. Application control does not use MQTT; the OEM MQTT connection is a hardware-specific implementation detail owned exclusively by the backend.

Current commands are `ping`, `load_state`, `read_telemetry`, `apply_manual`, `set_boost`, `set_gpu_power`, `set_dynamic_boost`, `read_lightbar`, `set_lightbar`, `set_oem_service`, `prepare_exit`, `set_mode`, `configure_auto`, `frontend_heartbeat`, `frontend_detach`, and `show_frontend`. The GPU power and lightbar commands fail on Windows, whose capabilities report them unavailable. Service changes and exit preparation are rejected unless the frontend includes the confirmation marker after the user accepts the corresponding modal prompt. A confirmed exit serializes a 100% write to both fans, stops Automatic scheduling only after that write succeeds, and leaves the window open on failure.

The token prevents unauthenticated requests that cannot read the endpoint file. This is local process authentication, not encryption and not a claim that the current-user account is isolated from its own processes. IPC hardening work belongs in `TODO.md`.

## User-interface design

The window is a wide three-column layout: Automatic controls on the left, Manual controls in the middle, and sensor values on the right. A two-state mode toggle selects Automatic or Manual. Manual input commits automatically after a short debounce; its optional mirror toggle copies whichever fan target was changed to the other fan before the shared pair is submitted. A distinct top-right Exit button avoids conflating window hiding with application shutdown, and the adjacent badge always reports OEM MQTT, Direct EC, or the initial detection state. The inactive control section is disabled and covered by a translucent overlay; telemetry, Fan Boost, and the confirmed `GCUBridge` start/stop button remain available in both modes. The service transition is serialized by the backend with fan operations, waits for the broker state to match, and allows the OEM shutdown cleanup interval to finish before direct EC use.

Packaged preferences are written atomically to `StellarisFanControl.json` beside the executable. Only validated Automatic endpoints, Manual CPU/GPU targets, and the mirror toggle are persisted. The control mode is intentionally not persisted: every launch starts Automatic only after the backend has loaded the active curve and both independent temperature paths remain subject to fail-closed validation.

The automatic curve graph visualizes the configured temperature endpoints and the fixed 80 C full-speed cap. CPU and GPU temperature gauges are separate from the reported CPU and GPU fan-duty gauges.

The **Lightbar** button opens a separate window with three options, Color (red, green, and blue sliders), Rainbow animation, and Off, each in its own panel with a checkbox. The checkboxes are exclusive like radio buttons: exactly one is ticked, clicking another switches to it directly, and the color sliders are grayed out unless Color is ticked. Picking an option applies it at once, and color slider changes apply after a 300 ms pause. The window reads the service's state each time it opens. The button is disabled on Windows.

All styling lives in `frontend/stellaris15gen3.css`; Python code supplies structure, state, and custom-widget painting.

The always-available Start minimized checkbox persists a boolean `start_minimized` preference, defaulting to true when missing or invalid while preserving an explicitly saved false value. It only changes initial window visibility on the next launch. With a system tray available the window stays hidden; without one it is minimized on the taskbar so it remains accessible. Tray activation and second-instance activation still restore the window. Automatic scheduling is independent of this preference.

## Recorded decisions

| Timestamp | Decision | Reason and consequence |
| --- | --- | --- |
| 2026-09-02T02:58:04+03:00 | Read Ryzen temperature from SMN register `0x00059800` through PawnIO. | The OEM CPU reading is the failed input. This makes PawnIO and elevation a backend requirement. |
| 2026-09-02T11:22:34+03:00 | Use configurable 30%-to-100% automatic endpoints with a fixed 80 C safety cap. | The UI can tune normal behavior without weakening the full-speed threshold. |
| 2026-09-02T12:13:36+03:00 | Keep presentation styling in one CSS file and use the `stellaris15gen3` identifier. | Visual changes stay separate from application logic and naming stays hardware-specific. |
| 2026-09-02T12:39:47+03:00 | Split the frontend and backend into supervised processes. | Automatic control can continue without PySide6, and either side can recover the other with cooldown protection. |
| 2026-09-02T12:46:54+03:00 | Elevate only the backend. | The GUI has no hardware-access reason to run as administrator; packaged builds therefore have no global admin manifest. |
| 2026-09-02T12:54:38+03:00 | Put frontend, backend, shared code, and tests in explicit packages. | Filesystem boundaries now match process and dependency boundaries; frontend and backend dependencies can be inspected separately. |
| 2026-09-02T12:59:04+03:00 | Keep operational PowerShell and command scripts under `scripts/`. | The repository root stays focused on application entry points, dependency manifests, and project documentation. |
| 2026-09-02 | Package the frontend and backend as separate executables and keep control IPC on authenticated loopback TCP. | Separate manifests make the privilege boundary visible and enforceable; avoiding MQTT keeps UI commands independent from the reverse-engineered OEM broker. |
| 2026-09-02 | Add direct EC fan-table control when the OEM broker is unavailable, with OEM MQTT selected when it is running. | Fan control can survive a stopped `GCUBridge` service while preserving the established OEM route, complete backups, method serialization, target-hardware checks, and readback verification. |
| 2026-09-02 | Supersede separate packaged executables with one role-selecting executable. | Distribution is simpler while frontend and backend remain separate processes; only the `--backend` relaunch receives UAC elevation. |
| 2026-09-02 | Supersede the role-selecting package with one elevated application process. | The requested distribution and runtime model is a single app that asks for UAC at startup; closing its window therefore also stops Automatic control. |
| 2026-09-02 | Install packaged startup through a highest-privilege per-user sign-in task. | The GUI needs an interactive desktop, so a pre-login boot task would hide its window and tray icon in a non-interactive session. Installation itself does not launch Auto mode or write fan targets. |
| 2026-09-29 | Support Ubuntu in the same repository with platform adapters. | One copy of the curve, safety rules, EC table semantics, GUI, and tests serves both systems; Windows behaviour stays as validated. |
| 2026-09-29 | On Linux, access EC RAM through the firmware's INOU `/dev/mem` window instead of TUXEDO or an out-of-tree module. | Needs no DKMS module, matches the firmware's own access, and works after TUXEDO is removed; requires root and no kernel lockdown. |
| 2026-09-29 | Run Linux fan control as a root systemd service with a normal-user tray GUI over a peer-credential Unix socket. | Fan control starts at boot and survives GUI exits; only the installing user may command it. |
| 2026-09-29 | Force EC Fan Boost when no valid CPU or GPU reading succeeds for 30 seconds, on both platforms. | Supersedes the pure fail-closed behaviour during prolonged sensor loss, as decided by the owner. |
| 2026-09-29 | Treat a runtime-suspended NVIDIA GPU as cold on Linux. | A powered-off GPU produces no heat; polling it would keep it awake. |
| 2026-09-29 | Use bit-level writes for `0x0741`, `0x07C5`, and `0x07C6` on Linux only, and `0xA0`/`0x40` for `0x0751`. | Leaves firmware-owned bits such as WhisperMode alone; both mode values were validated live. Windows keeps its OEM-traced byte writes until re-tested. |
| 2026-09-29 | Add a Linux-only GPU power limit (cTGP 115-165 W). | The EC exposes it through documented registers; the Windows GUI shows the control disabled. |
| 2026-09-29 | Add a Linux Dynamic Boost checkbox and enable `nvidia-powerd` from the installer. | NVIDIA ignores cTGP and Dynamic Boost without the daemon; TUXEDO's packages enable it the same way. The GUI shows the sustained limit, the remaining boost room, and NVIDIA's enforced limit separately. |
| 2026-09-29 | Add Linux lightbar control (Color, Rainbow, Off) in a separate window. | The mainline driver does not expose the lightbar on this board; the registers match tuxedo-drivers and were validated live on AC power. One option at a time, switched with a single click, and a forced rewrite every 15 minutes, as the owner specified. |
| 2026-09-29 | Make the Linux installer a single command that offers to disable `tccd` and starts the service. | The owner found the multi-step install too complicated; unlike the Windows installer it starts Automatic control, because the service is the product and has no separate launch step. |
| 2026-09-29 | Distribute Linux as a source archive with `install.sh`, not a `.deb`. | Ubuntu 24.04 does not package PySide6, and a `.deb` would have to bundle Qt or download it during installation. |

## Release distribution

Tagged `v*` pushes build Windows x64 release archives through GitHub Actions. The build job has read-only repository permissions and skips PawnIO driver installation while retaining the pinned module hash check. A separate publish job receives write permission only for tagged releases after the build and mocked tests succeed. Branch dispatches produce artifacts without publishing.

The same installer accepts a local `dist` executable or the executable at the root of an extracted release archive. It prepares PawnIO and installs into Program Files, preserving preferences and registering an interactive, highest-privilege sign-in task. Startup therefore remains independent of source/download folder moves. Neither packaging nor installation launches Automatic mode. Release checksums detect download corruption; the application remains unsigned.

## Ideas under consideration

- Version the IPC contract before incompatible commands are introduced.
- Add structured, redacted, rotating backend logs for field diagnosis.
- Add a read-only diagnostics view that clearly separates sensor failures from OEM MQTT failures.
- Show backend privilege and connection state without exposing the IPC token.

Ideas are not commitments. Actionable work and priorities are tracked in `TODO.md`.

Windows session shutdown uses a direct Qt `commitDataRequest` connection. Qt 6 does not expose the old `setFallbackSessionManagementEnabled` API; startup must not call it. A subprocess regression test creates the real QApplication and window and enters the event loop with hardware initialization mocked. The handler suppresses frontend callbacks, queues exit preparation on the existing single-worker pool, and waits up to 45 seconds before acknowledging shutdown. No sensor read is required. On failure or timeout it requests session cancellation; on success it closes the frontend, leaving both targets at 100%. Forced termination and OEM service teardown ordering still require target-hardware validation.

User-initiated mode changes ask for confirmation in the toggle before its checked state changes. Declining leaves the current UI and backend mode intact. Programmatic startup and backend-state synchronization do not prompt.
