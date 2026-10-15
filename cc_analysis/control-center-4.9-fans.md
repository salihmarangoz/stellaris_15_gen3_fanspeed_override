# Control Center 4.9 direct fan compatibility

Date: 2026-10-15 (Europe/Istanbul). Exact target: GMxZGxx / STELLARIS1XA03,
BIOS N.1.61A15; same firmware DSDT as the MUX analysis.

## Transport evidence

Installed ACPIDriverDll.dll SHA-256:
ee2c506dd414222013e6dbe5c67a774b1fa1387a47ad5d705a82f048c1a945bb.
The existing 3.9 hash remains supported; unknown images remain refused.
The 4.9 image additionally requires the exact Windows DMI board/SKU/BIOS
and EC universal-control bit 0x078E bit 6, as well as the existing project,
fan-mode, table-format and duty checks.

Static analysis of the locally installed native exports established:

- ReadEC at RVA 0x3410: uint32 address, byte result; DeviceIoControl 0x9c40a488.
- WriteEC at RVA 0x34f0: uint32 address/value; DeviceIoControl 0x9c40a48c.
- Both use the ACPIDriver device and the same 4 MiB in/out buffer.
- The read request stores the address at offset 0; the write request stores
  address at 0 and value at 4. Raw OEM code/binaries stay in ignored build.

## Observed state transition and correction

Before custom fan activation, read-only calls returned project 0x10,
application present 1, control 0, mirror 0x80 and subsystem byte 0x00.
After an authorized OEM shutdown, the state was application present 0,
control 0xA0, mirror 0xA0 and subsystem byte 0x03. The old 1/5 subsystem
allowlist therefore refused the direct connection, even with a matching ABI.
On failure the investigation restored the OEM service before continuing.

For this exact 4.9 image, accepted subsystem states are 0, 1, 3, 4, 5, 7.
Activation preserves the existing low bits and sets bit 2, as on the validated
Linux implementation: 0x03 becomes 0x07. Mirror activation preserves other
bits and sets bit 7. Boost changes the mode byte without replacing these
bitfields. The legacy 3.9 behavior is retained. Backups record the actual
loaded library hash. Every individual direct write now rechecks OEM broker
ownership, including fan writes and rollback attempts.

## Authorized target check

The owner explicitly asked to stop Control Center and open the custom app
for testing. Its GUI and GCUBridge/GCUService processes were stopped; the
installed UWACPIDriver was retained. The updated source application was
started elevated and activated through its normal single-instance channel.
A subsequent read-only probe confirmed Direct EC, all 16 CPU and GPU table
entries equal to raw 90 (45%), subsystem 0x07, and independent temperatures
44.25 C CPU / 43 C GPU. At that observation Fan Boost was on: actual duties
were 100%, RPM 4347 CPU / 4549 GPU. Uniform table readback does not imply
that those duties are active while Boost overrides them.

No MUX setting or reboot was performed. The earlier standalone 100%-table
attempt was refused before direct writes by the old subsystem-state check;
it must not be reported as a successful 100%-table test. Long-duration Auto,
new-image cTGP/lightbar writes and reboot/recovery validation remain pending.

The final Windows package passed --check-qt (exit 0) and was installed with matching source/installed SHA-256 bbea10d293dad83b125b76cb6ccc6b7eaaa1190a2cd6dcec97539398129de23b. A read-only MUX query now reports saved dGPU with writes available because the OEM broker is stopped; token privileges were restored. The current running source GUI responded to its activation channel and appeared as Fan Control. The complete mocked/offscreen suite passed 136 tests, with two Linux-only cases skipped.
