# Design

This is the living design record for Stellaris 15 Gen3 fan control. It explains why the project is shaped this way and records decisions that should survive individual code changes. Safety requirements in `AGENTS.md` take precedence if this document ever falls behind the code.

Last reviewed: 2026-09-02

## Goals

- Keep automatic fan control alive when the GUI crashes or PySide6 cannot start.
- Replace the untrustworthy OEM CPU-temperature input with the validated Ryzen `Tctl/Tdie` path.
- Keep hardware access and fan writes behind a small, elevated backend.
- Make every automatic decision predictable, conservative, and testable without live hardware writes.
- Preserve an immediate OEM Fan Boost fallback and recoverable curve backups.

## Non-goals

- Supporting laptops or OEM Control Center versions that have not been validated on the target hardware.
- Replacing or patching OEM program files.
- Reading CPU temperature from the OEM service, EC, MQTT, Core Temp, ACPI thermal zones, or WinRing0.
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

The packaged `StellarisFanControl.exe` carries an administrator manifest and runs the PySide6 interface and backend controller in one elevated process. The interface uses an in-process client with the same synchronous dispatch boundary, while its one-worker pool keeps controller operations off the GUI thread. Closing the window hides it in the system tray and preserves the controller and Automatic scheduler. Only the dedicated Exit controls perform the confirmed 80% shutdown. The separate source entry points and authenticated loopback transport remain available for development.

## Automatic-control design

An automatic cycle reads both independent sensors, rejects unavailable, zero, malformed, or implausible values, and calculates a single target from `max(cpu_temperature, gpu_temperature)`. The same target is written to both fans.

The default curve is 30% at 35 C and 100% at 75 C. The endpoints can be adjusted from 0 to 100 C, but the hard 80 C safety cap still forces 100%. Targets are rounded to 5% steps and can never fall below 30% in Automatic mode.

Entering Automatic mode creates one backup before the first write. Later 15-second cycles reuse that protection instead of producing a backup every time. Changing to Manual stops future automatic cycles. Fan Boost pauses automatic writes while it is enabled.

## Concurrency design

- The backend process lock allows one backend per user runtime directory.
- `ControlCenterService` is a process-wide singleton with a serialized operation lock.
- Automatic scheduling runs in the backend and does not depend on the Qt event loop.
- A sensor lock prevents overlapping PawnIO and NVIDIA reads.
- Automatic state is checked again after sensor acquisition so a late Manual-mode or Fan Boost change prevents the pending write.
- The GUI worker pool has one thread and skips timer work while an operation is already active.

## IPC design

The backend binds an ephemeral IPv4 loopback port and writes its host, port, and random token to `%LOCALAPPDATA%\StellarisFanControl\backend-endpoint.json`. Requests and responses are newline-delimited JSON with a 64 KiB limit. Application control does not use MQTT; the OEM MQTT connection is a hardware-specific implementation detail owned exclusively by the backend.

Current commands are `ping`, `load_state`, `read_telemetry`, `apply_manual`, `set_boost`, `set_oem_service`, `prepare_exit`, `set_mode`, `configure_auto`, `frontend_heartbeat`, `frontend_detach`, and `show_frontend`. Service changes and exit preparation are rejected unless the frontend includes the confirmation marker after the user accepts the corresponding modal prompt. A confirmed exit serializes an 80% write to both fans, stops Automatic scheduling only after that write succeeds, and leaves the window open on failure.

The token prevents unauthenticated requests that cannot read the endpoint file. This is local process authentication, not encryption and not a claim that the current-user account is isolated from its own processes. IPC hardening work belongs in `TODO.md`.

## User-interface design

The window is a wide three-column layout: Automatic controls on the left, Manual controls in the middle, and sensor values on the right. A two-state mode toggle selects Automatic or Manual. Manual input commits automatically after a short debounce; its optional mirror toggle copies whichever fan target was changed to the other fan before the shared pair is submitted. A distinct top-right Exit button avoids conflating window hiding with application shutdown, and the adjacent badge always reports OEM MQTT, Direct EC, or the initial detection state. The inactive control section is disabled and covered by a translucent overlay; telemetry, Fan Boost, and the confirmed `GCUBridge` start/stop button remain available in both modes. The service transition is serialized by the backend with fan operations, waits for the broker state to match, and allows the OEM shutdown cleanup interval to finish before direct EC use.

Packaged preferences are written atomically to `StellarisFanControl.json` beside the executable. Only validated Automatic endpoints, Manual CPU/GPU targets, and the mirror toggle are persisted. The control mode is intentionally not persisted: every launch starts Automatic only after the backend has loaded the active curve and both independent temperature paths remain subject to fail-closed validation.

The automatic curve graph visualizes the configured temperature endpoints and the fixed 80 C full-speed cap. CPU and GPU temperature gauges are separate from the reported CPU and GPU fan-duty gauges.

All styling lives in `frontend/stellaris15gen3.css`; Python code supplies structure, state, and custom-widget painting.

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

## Ideas under consideration

- Version the IPC contract before incompatible commands are introduced.
- Add structured, redacted, rotating backend logs for field diagnosis.
- Add a read-only diagnostics view that clearly separates sensor failures from OEM MQTT failures.
- Show backend privilege and connection state without exposing the IPC token.

Ideas are not commitments. Actionable work and priorities are tracked in `TODO.md`.
