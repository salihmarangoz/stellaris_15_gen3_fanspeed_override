# Safety Model

What keeps the fans safe, what the application writes to the hardware, and where software protection ends. The binding rules for contributors are in [AGENTS.md](../AGENTS.md); the reasoning behind them is in [DESIGN.md](../DESIGN.md).

## Temperature Sources

The embedded controller's CPU temperature is the failure this project works around, so it is never used for fan control.

| | Windows | Ubuntu |
| --- | --- | --- |
| CPU | Ryzen `Tctl/Tdie` SMN register `0x00059800`, read through the signed [PawnIO](https://github.com/namazso/PawnIO) driver and its restricted AMD module | Kernel `k10temp` driver, `temp1_input` labeled `Tctl` (the same register) |
| GPU | NVIDIA driver (NVML, in a separate helper process) | NVIDIA driver (`nvidia-smi`) |
| Never used | Control Center, EC and MQTT CPU temperatures; Core Temp; ACPI thermal zones | EC temperature registers, `acpitz`, TUXEDO/mainline EC hwmon temperatures |

## Fail Closed

If either temperature is missing, zero, malformed, or implausible, Automatic mode writes no new fan target in that cycle.

The one exception is an NVIDIA GPU the operating system reports powered off: on Ubuntu a PCI runtime status of `suspended`, on Windows the PnP power state D3. It counts as cold and is not woken for a reading. Automatic mode then uses the CPU temperature alone, so both fans follow the CPU. If the power state cannot be read, the GPU is queried as usual and a missing reading still fails closed. The Windows D3 check is not yet validated on the laptop.

## Sensor Watchdog

Every 5 seconds, in every mode, a watchdog checks when the last valid CPU or GPU reading succeeded. If none has succeeded for 30 seconds, it enables EC Fan Boost (100% on both fans) and holds it until readings recover. Meanwhile, Manual changes and turning Boost off are refused. Afterwards Automatic mode resumes, or the last Manual duties are restored. The watchdog never stops on an exception.

## Fan Output Failures

The safety fixes of 2026-10-16 bound temperature waits (now 4 seconds) and EC-operation waits to 8 seconds. The watchdog evaluates freshness before another sensor attempt.

A failed fan update immediately attempts a guarded Boost and records a separate output fault. Lower Manual targets and turning Boost off are blocked until a verified recovery: a fresh successful Automatic update or an explicit successful 100/100 Manual retry. The UI distinguishes a confirmed Boost from unconfirmed fan safety. Evidence and remaining limits: [Windows safety review](../cc_analysis/windows-safety-review-2026-10-16.md).

The Windows package honors request deadlines (15 seconds by default; existing longer explicit deadlines still apply). A timeout releases the UI but does not cancel a driver call. Each hardware lane serializes ordinary work within the caller's deadline, keeps ownership of a timed-out operation, refuses further work while that operation is unfinished, and discards late replies. A request whose wait expires is never executed later. If an EC call returns after its deadline, the same worker attempts a guarded Boost before releasing ownership. The package never starts a replacement controller for a stalled in-process request.

## Automatic Curve Limits

Automatic mode uses `max(CPU, GPU)` and applies one identical target to both fans. It never requests less than 30%, and it requests 100% at 80 C and above regardless of the configured points. Manual values below 30% always need confirmation. The full curve is described in [Using the app](usage.md#automatic-mode).

## Who Can Control the Fans

- **Windows**: the packaged application is one elevated process. In source mode, only the elevated backend reads sensors, talks to Control Center, or touches the EC; the normal-user frontend sends authenticated loopback requests. Only one frontend, one backend and one Control Center client can run at a time.
- **Ubuntu**: a root systemd service owns fan control. Only root and the user IDs listed in `/etc/stellaris-fan-control/config.json` (the installing user) may send commands; the kernel's peer credentials decide. The GUI never runs as root and never starts the service.

## What Is Written to the Hardware

### Windows

While the OEM `GCUBridge` broker runs, fan curves go through Control Center's own MQTT interface. When it is stopped, the backend writes the EC fan tables through the installed OEM ACPI driver library (`ReadEC`/`WriteEC` exports only):

- Direct EC is restricted to the EC project ID and the exact OEM library hashes validated on this laptop; the Control Center 4.9 library additionally requires the exact board, SKU, BIOS and the EC universal-control flag. Unknown libraries are refused.
- While OEM MQTT is available, the backend caches the last complete OEM curve. If the OEM service clears its EC tables while stopping, Direct EC restores that curve, activates the traced OEM application and fan-subsystem state, and then applies the new duties.
- Every write verifies all six 16-byte curve blocks and the control state, and attempts to restore every previous byte if verification fails.
- Before every individual EC write, including rollback, the backend checks that the OEM broker is not running, using a fresh query of the local IPv6 TCP listener table. An unavailable or malformed table refuses access.
- The fan tables are volatile EC RAM, not EEPROM, CMOS, or firmware flash. Matching Uniwill driver source treats the same locations as routinely rewritten fan-table RAM. Writing a wrong address or using incompatible firmware can still cause malfunction, even without flash wear.

### Ubuntu

The service reads and writes EC RAM through the firmware's INOU window at physical address `0xFE200000`, the same byte access the firmware's own `ECRR`/`ECRW` methods perform.

- Access is refused unless the DMI board and SKU, the DSDT SHA-256 of BIOS N.1.61A15, EC project ID `0x10`, and the universal-fan-control flag (`0x078E` bit 6) all match. Every write is also refused while TUXEDO's `tccd` runs.
- Only these EC locations are ever written: the fan enable bits, the fan mode byte (`0xA0` normal, `0x40` Boost), the fan tables, the cTGP bytes, and the lightbar bytes. The exact allowlist is in [AGENTS.md](../AGENTS.md#safety-invariants).
- All 16 zones of each fan table get the same duty, so the unreliable EC temperature cannot change the fan speed.
- Bit fields are changed by read-modify-write, tables are written before enable bits, every write is read back, and a mismatch is rolled back.
- Every 15 seconds the service verifies the EC state and rewrites it if the firmware drifted. After resume, a systemd sleep hook triggers the same check immediately.

### GPU Power Limit and Lightbar

Both platforms preserve unrelated control bits, write data before enable bits, verify readback, and attempt rollback on failure. The GPU power offset is limited to 0-50 W above the 115 W base and to the VBIOS maximum. On Windows, all such writes, including rollback, are refused while the OEM broker runs, and nothing is written until you choose a setting. Fan and WhisperMode bytes are never touched by these features, nor are the unvalidated battery-mode lightbar registers or any private NVIDIA setter.

NVIDIA's software thermal-throttling flag is display information only. It never feeds Automatic control and never changes thermal protections.

### NVIDIA Optimus Firmware Setting

The Optimus window changes one byte: `OemDisplayMode` at offset `0x62` of the exact 180-byte `UniWillVariable` UEFI variable (`0` Hybrid, `1` dGPU). It requires exact platform and firmware checks, two confirmations (the second is the exact text **YES DO IT**), and on Ubuntu the administrator password. It saves a full backup first, rejects concurrent changes and OEM ownership, and verifies the full readback.

Windows uses the firmware API with a temporary process-token privilege; Ubuntu uses efivarfs through the root service. The kernel marks `UniWillVariable` immutable at every boot; after the confirmations and the password, the service lifts that flag for this one write and sets it again right after, so no `chattr` is needed. A read-only efivarfs mount is never changed. The application never retries, periodically reapplies, rolls back a whole variable, reboots, or changes firmware protections on its own.

## Backups

The active fan curve is backed up before a Manual write and once when entering Automatic mode (not on every 15-second update). Restore steps: [Windows](windows.md#recovery), [Ubuntu](linux.md#recovery).

## Known Limits

- A permanently hung EC driver can block every command, including Boost. Software cannot confirm fan safety in that state.
- Boost waits for an EC transaction already in progress, because EC access is serialized.
- Command completion times are not a guarantee of immediate physical fan acceleration.
- Forced termination, power loss, or the OS ending the process before a shutdown write completes cannot be guarded against.
- Manual mode holds its targets even at high temperatures; it does not raise the fans for heat.

If temperatures rise unexpectedly, do not wait for the application to recover. Enable Fan Boost or shut down the laptop.
