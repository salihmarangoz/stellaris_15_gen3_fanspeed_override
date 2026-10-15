# NVIDIA Optimus / MUX firmware contract

Date: 2026-10-15 (Europe/Istanbul). Platform: TUXEDO Stellaris AMD Gen3 (CZN),
board GMxZGxx, SKU STELLARIS1XA03, BIOS N.1.61A15.

## Confirmed target

This is a UEFI NVRAM field, not an EC RAM address or process-memory address.

| Item | Observed value |
| --- | --- |
| Variable | UniWillVariable |
| Vendor GUID | 9f33f85c-13ca-4fd1-9c4a-96217722c593 |
| Data length | 180 bytes (0xB4) |
| Attributes | 0x00000007: nonvolatile, boot-service, runtime |
| Field | OemDisplayMode |
| Offset in variable data | 0x62 (98), one byte |
| AMD Hybrid / Optimus enabled | 0 |
| AMD NVIDIA dGPU / Optimus disabled | 1 |
| Board SSID, little endian at offset 0 | 0x11141D05 |
| Project ID at offset 6 | 0x10 |
| Linux efivarfs file | /sys/firmware/efi/efivars/UniWillVariable-9f33f85c-13ca-4fd1-9c4a-96217722c593 |
| Offset in Linux file | 0x66: four attribute bytes precede the data |

Changing this field saves the mode for the next boot. It does not establish
which GPU currently drives a screen. A reboot is needed for routing to change.

## Evidence chain

1. Installed official Control Center 4.9 service SHA-256:
   8c490c54a15977f7421ac819a54370cc40e5c9e1dcac94177fbfe1821de94a2f.
   Its methods are encrypted in the on-disk PE. Ordinary ILSpy static output
   reports invalid method headers, so names alone were not treated as proof.
2. With the owner's authorization, the already-running service's executable
   code image was copied using read-only process access. Page-by-page reads
   avoided inaccessible guard/header pages. Original metadata was retained
   while recovered CIL bodies were used for offline analysis. No code was
   injected, patched in the live process, or invoked by this investigation.
3. MyControlCenter.MySettingManager.UserSetDGgpuDirectConnectionSwitch(int)
   (method RVA 0x70660) calls the firmware setter for OemDisplayMode with AMD
   values 1/0. Intel uses 2/4 and is deliberately unsupported by this app.
4. MyControlCenter.NvramVariable serializes NVRAM_STRUCT and calls
   UEFI_Firmware.dll!WriteUefi. The native DLL reads the current named UEFI
   variable, patches a range, writes it with the Windows firmware API, then
   reads it back. The structure places OemDisplayMode at 0x62.
5. Native UEFI_Firmware.dll SHA-256:
   ded0f6263b87e48d75a8806452d412b20ef59c4fbdec6d94ae75f06f01b95931.
   Its literal variable name and GUID match live firmware API reads.
6. The owner manually operated the official Disable NVIDIA Optimus switch.
   Read-only UEFI snapshots recorded on/off/on = 1/0/1. The on/off pair differed
   at exactly one of the 180 data bytes: 0x62. The final on snapshot was
   byte-for-byte identical to the first on snapshot. The original requested
   dGPU selection was restored. No reboot or custom-app firmware write occurred.
7. Full 4 KiB EC snapshots and secondary CMOS snapshots did not establish a
   MUX register. Changing telemetry bytes must not be mistaken for MUX state.

## Application implementation and limits

backend/gpu_mux.py uses Windows firmware APIs or Linux efivarfs independently
of fan transport. It requires the exact board/SKU/firmware DSDT, exact
180-byte variable, attributes, SSID, project ID, and known AMD display value.
The DSDT SHA-256 is
b35bf53709db9ac190ce13489619ffcf7793b78b65c7888d76e25ffc49eaea62.

Only a user selection followed by two confirmations can request a write. The
second requires exactly YES DO IT, including case and spacing; the backend
checks the exact phrase too. Opening the window, app startup, maintenance,
resume and unchanged selections never write this variable.

A write backs up the complete variable, re-reads it to detect concurrent
changes, changes only data byte 0x62, preserves attributes and every other
byte, and verifies complete readback. OEM GCUBridge or tccd ownership blocks
writes. Failed verification does not trigger blind whole-blob rollback or
repeated NVRAM writes. The retained backup is for deliberate recovery.

The UI reports the saved mode, not an inferred active route, and never reboots.
The owner's internal panel is already broken; incorrect display routing could
make pre-boot recovery especially difficult.

Custom-app Windows writes and Linux efivarfs writes/reboot effects have not
been validated on target hardware. Automated write tests use fake transports
or ordinary temporary files. Linux efivarfs may mark this vendor variable
immutable; the application reports that failure and does not remove
immutability, remount efivarfs, or change kernel lockdown policy.

The installed 4.9 ACPI DLL hash is
ee2c506dd414222013e6dbe5c67a774b1fa1387a47ad5d705a82f048c1a945bb.
It differs from the original pinned 3.9 direct-fan DLL. MUX implementation
requires neither DLL. Later, separate direct fan support for this exact 4.9
image was added and target-observed; see control-center-4.9-fans.md.

## Primary API references

- [Windows firmware read API](https://learn.microsoft.com/en-us/windows/win32/api/winbase/nf-winbase-getfirmwareenvironmentvariableexw)
- [Windows firmware write API](https://learn.microsoft.com/en-us/windows/win32/api/winbase/nf-winbase-setfirmwareenvironmentvariableexw)
- [Linux efivarfs layout and protection](https://docs.kernel.org/filesystems/efivarfs.html)
- [ACPICA tools](https://github.com/open-acpica/acpica/releases/tag/R2025_04_04)

Local iASL 20250404 came from that official release. Executable SHA-256:
908e4f3334ffb724fbf63a92dc9dd715b2ba0bdf5d8083ee8d9caf1a3f3bbd75.
It disassembled a saved DSDT whose hash matched the existing firmware pin.

## Recorded comparison timestamps

- enabled1: 2026-10-15T22:31:18.786422 local; OemDisplayMode=1; SHA-256 e0b3953b1a869ac50e7dafc5d5e71f5e5cc392b3c0279c0079c2dc389365453e
- disabled1: 2026-10-15T22:32:16.232211 local; OemDisplayMode=0; SHA-256 dccd030a6f28850d22fa9e115ce9883ee33d6f2ba622458cc38864a33ccc22ac
- enabled2: 2026-10-15T22:32:44.876976 local; OemDisplayMode=1; SHA-256 e0b3953b1a869ac50e7dafc5d5e71f5e5cc392b3c0279c0079c2dc389365453e

## Implementation verification

The complete mocked/offscreen Windows suite passed 130 tests (two Linux-only tests skipped). Source GUI previews confirmed the separate Optimus container to the right of GPU power and both selection/final-warning dialogs. The rebuilt Windows executable passed its hardware-free --check-qt smoke test (exit 0) before installation.

An elevated, read-only call through the new FirmwareMux adapter on the target laptop returned the saved dGPU mode. A token-privilege snapshot was identical before and after the call, confirming restoration for this read path. OEM broker detection correctly disabled writes while Control Center was running. No custom firmware write or reboot was performed. Linux execution and write-path privilege restoration remain unvalidated.
