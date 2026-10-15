# TODO

## MEDIUM - Profile the remaining Windows Direct EC transaction time

The 2026-10-16 listener-table change removed repeated 201 ms TCP timeouts without weakening per-byte OEM exclusion. Owner-authorized Manual tests measured Boost on/off at 88/69 ms, while a full fan-table update still takes 3.2-3.3 seconds through the OEM DLL with complete backup and readback. Profile driver reads/writes and frontend queue time before further changes. Keep transactions serialized, every ownership guard and complete verification/rollback; do not assume Linux direct-memory performance is attainable through this DLL. Live tests require explicit authorization and must honor the owner's requested fan floor. See cc_analysis/windows-direct-ec-latency.md.

This is the single list of open technical debt, known problems, investigations, and planned work. Entries are not grouped by category. Every entry starts with exactly one impact priority: `LOW`, `MEDIUM`, `HIGH`, or `CRITICAL`. Remove an entry when it is completed and preserve any lasting decision in `DESIGN.md`; record an actual important failure in `ACCIDENTS.md`.

## HIGH - Validate the 30-second sensor-loss Boost on target hardware

The owner decided on 2026-09-29 that both fans go to 100% when no valid CPU or GPU reading succeeds for 30 seconds; the backend now enables EC Fan Boost and refuses Manual writes until recovery. The 2026-10-16 authorized Windows Direct EC check aged the success timestamp, injected a failing reader and confirmed Boost (65.836 ms) plus recovery to saved Manual 60/60 targets, ending at verified 100/100. This validates the fallback action, not the real 30-second timer or a native-driver hang. With explicit authorization for further live writes, inject a sensor failure (for example a failing `nvidia-smi`) on Linux and on Windows with both OEM MQTT and Direct EC, and confirm Boost within about 35 seconds, the GUI warning, and correct recovery in Automatic and Manual modes.

## HIGH - Re-test Windows after the cross-platform refactor

The 2026-09-29 Linux work changed shared code that has not run on Windows hardware: the controller now selects its service and sensors by platform, imports `msvcrt` conditionally, runs the sensor watchdog, reports capabilities, guards the Auto thread, and authorizes requests through the server class. The loopback client error text changed from "not responding" to "not available" when connecting fails. The GUI gained the grayed GPU power row, RPM/power details, tighter spacing, and a guarded completion handler. Run the Windows CI, build and install the package, and verify startup, tray, Exit at 100%, session shutdown, GCUBridge switching, Manual writes, Boost, and Automatic mode on the laptop.

On 2026-10-11 the rebuilt installed application passed its packaged Qt check, opened normally, displayed live temperatures/GPU power/RPM, and resumed Automatic updates. A second executable launch retained the original process IDs and activated its window. This closes the basic launch/display checks only; the remaining mode, recovery, tray, shutdown, and hardware validation listed above is still pending.

The subsequent power-control update also passed its bundled Qt check, installed with unchanged frontend preferences, and displayed live CPU package watts (6.5-6.9 W), an active GPU slider with NVIDIA's 115 W limit, and matching 45% live fan duties after settling. A second launch again retained both existing process IDs. The explicitly authorized 115/130/165 W GPU-limit test is recorded below; no extra fan-mode or shutdown tests were performed at that stage. The subsequent authorized GPU stress result is recorded below.

The subsequent GPU-slider fix passed 14 focused tests, including a real Qt timer/drag regression with a mocked backend. The rebuilt installed executable passed its bundled Qt check and second-launch singleton check, retained the frontend preferences, and displayed live temperatures, watts and matching Automatic fan duties. No GPU stress worker remained. The slider now cancels pending updates on press and refuses application until release.

The v1.1.0 Windows CI build on 2026-10-13 passed all 118 mocked tests, skipping only the two Linux-only service classes (run 38328638470). The on-laptop checks above remain pending.

## HIGH - Validate Windows GPU power, fan RPM, and lightbar on target hardware

Implemented on 2026-10-11 with mocked/offscreen coverage. The installed Windows GUI subsequently displayed plausible GPU power and fan RPM, but these were not independently compared. Compare the optional GPU `power.draw` and Direct EC RPM (`0x0464`/`0x046C`) with independent readings on the laptop. With explicit authorization for live writes, verify Color/Rainbow/Off, unrelated-bit preservation, saved-color restoration after restart/resume, and refusal while GCUBridge runs or returns during a lightbar operation. Test on AC power and battery; the unvalidated battery-mode lightbar copy remains untouched. Confirm lightbar maintenance does not delay Auto, sensor-loss Boost, or full-speed exit. Hardware validation remains pending.

## MEDIUM - Compare Windows CPU package power with Linux RAPL

Implemented on 2026-10-11 with the unchanged pinned PawnIO module, read-only unit/counter MSRs, isolated optional telemetry, and mocked sampling/transport coverage. Six read-only samples on the target laptop accepted units `0xA1003` (energy exponent 16) and reported about 6.5-6.9 W after the initial unavailable sample. Compare the average package power with Linux RAPL on the same laptop under idle and load; verify the display after sleep/resume. Do not change the module pin or add another hardware-access fallback.

## MEDIUM - Decide whether Windows should stop writing whole control bytes

Windows direct EC writes the OEM-traced bytes `0x0741=1`, `0x0751` and `0x07C5` equal to the mode, and `0x07C6=5`. `0x07C5` bit 5 is the firmware's WhisperMode flag forwarded to the NVIDIA driver, and Boost clears the split-table bit 7. Linux uses bit-level writes. Compare the OEM Control Center's own writes and decide, with a Windows hardware test, whether Windows should adopt the Linux semantics.

## HIGH - Validate Windows Dynamic Boost, GPU load, and saved-choice recovery

Implemented on 2026-10-11 using the existing pinned DLL, Linux-validated cTGP encoding, exact board/SKU/BIOS and EC gates, readable VBIOS limits, readback/rollback, OEM refusal before every write, saved user choices, and mocked tests. The owner explicitly authorized 115/130/165 W tests; with Fan Control stopped, NVIDIA followed all three, Dynamic Boost off, no stress load. All four EC bytes were restored to zero and verified; NVIDIA afterwards reported 115 W instead of the pre-test 120 W, so investigate driver-limit restoration separately. The downloaded 4.9.47.13 OEM package confirms register names statically, but protected method bodies leave additional NVIDIA calls unresolved. With authorization for further hardware tests, validate Dynamic Boost on/off, sustained power near the selected ceiling and longer thermal behavior, saved-choice repair after resume/restart, and refusal if OEM ownership returns. Do not change `0x07C5` or invoke private NVIDIA Whisper setters based on this evidence.

An authorized 60-second GPU-only CUDA load on 2026-10-11 maintained 98-99% utilization after startup. While the owner changed the slider, NVIDIA samples showed limits from 115 to 155 W, then 165 W during cooldown. Peak sampled GPU draw was 112.95 W, GPU temperature 63 C, CPU temperature 57 C, and both fan duties reached 75%. The load ended normally without a temperature abort. This workload did not reach the selected power ceiling and does not validate sustained 165 W draw or long-duration thermal stability.

## HIGH - Investigate Windows GPU software thermal capping under load

A subsequent authorized FurMark 2.10.2 OpenGL test on 2026-10-11 ran on the RTX 3080 Laptop GPU for 60007 ms at 1920x1080, ending normally. The owner changed the limit during the test: sampled NVIDIA limits were 145, 160 and 165 W, so this was not a fixed 155 W run. At 160 W the driver reported up to 159.68 W average draw and 161.09 W instantaneous draw with software power capping active; at 145 W it initially drew about 144.5 W. Peak sampled GPU/CPU temperatures were 65/58.75 C, and both fan duties reached 80%. Software thermal capping became active at a reported core temperature of 59 C and later reduced draw to roughly 96-101 W and graphics clocks to about 1065-1080 MHz, even with a 165 W limit. Hardware thermal and external power-brake flags were inactive. Memory temperature and T.Limit were unavailable; the post-test driver reported an 87 C target and 105 C maximum operating temperature. The source of software thermal capping is unresolved, and sustained ceiling power is not validated.

A later run configured for 300 seconds exited normally after 176162 ms; no monitor temperature abort occurred, and the reason for early normal exit was not established. It does not count as a completed five-minute test. Selected limits changed between 115/140/145/165 W; peak sampled draw reached 164.83 W, GPU temperature 72 C and CPU temperature 65.875 C. The final loaded sample drew 139.4 W at a 140 W limit with software power capping active and software/hardware thermal and power-brake flags inactive. Both fan duties reached 100%. This confirms transient 165 W operation and later near-ceiling 140 W operation, without establishing the earlier software thermal-cap cause. Its reports are under `build/qa/furmark-stress-20261011-042915`.

Reports, CSV samples and FurMark logs are under `build/qa/furmark-stress-20261011-042507`. NVIDIA documents software thermal capping as potentially involving GPU or memory temperature; unavailable memory telemetry prevents assigning a cause from the core reading alone. Compare supported read-only thermal telemetry and platform policy under a fixed-limit repeat before changing any OEM/WhisperMode register or protection threshold. Do not bypass thermal protections.

## HIGH - Validate Linux after removing TUXEDO

The Linux service was validated with `tccd` disabled but `tuxedo-drivers` still loaded. After `sudo apt remove tuxedo-control-center tuxedo-drivers` and a reboot, confirm that `uniwill-laptop` loads, that the service starts in Automatic mode at boot, the boot value of `0x0751`, cTGP through `/sys/bus/platform/devices/INOU0000:*/ctgp_offset`, and that Fn lock and charge limits still work.

## HIGH - Validate Linux suspend, resume, and shutdown

Confirm on the laptop that the resume hook's `SIGUSR1` re-applies the table and cTGP immediately after suspend, that `systemctl stop` and shutdown leave both fans at 100%, and how the mainline driver's shutdown hook (clearing `0x0741` bit 0) affects the final seconds.

## HIGH - Validate the Dynamic Boost checkbox on Linux

The switch clears or sets EC `0x0743` bit 1 like TUXEDO's `db_enable`, but its effect on NVIDIA's enforced limit has not been measured yet. With `nvidia-powerd` running, confirm that turning it off lowers `enforced.power.limit` by the boost room, that the choice survives a service restart, suspend and resume, and that the mainline driver's resume re-enable is corrected.

On 2026-10-13, with Dynamic Boost on, a +0 W offset and `nvidia-powerd` running, an authorized 60-second GPU-only `stress_test.py --no-cpu` held `enforced.power.limit` at 140 W (115 W + 25 W boost) for the whole load, with 139-140 W draw, GPU 71 C, CPU 68 C and fans at 85%. At idle the limit returns to 115 W, which is NVIDIA's normal Dynamic Boost behavior. Turning the switch off, and persistence across restart, suspend and resume, remain unvalidated.

## HIGH - Validate the Linux MUX password flow on target hardware

The Linux save asks polkit for an administrator password and lifts the efivarfs immutable flag of UniWillVariable only around its one write. Validated on 2026-10-16 00:19 (owner-run, installed 03c3e23): the GNOME password dialog appeared after YES DO IT, polkit logged a one-shot authorization for this action, and the save to Hybrid changed only data byte 0x62 (1 to 0). The flag was already cleared by hand that boot, so the lift/restore path did not run. Still to check: Cancel and a wrong password leave the variable and backups untouched; after a fresh boot (flag set again) a save works and `lsattr` shows `i` afterwards; the next boot uses the saved mode.

## MEDIUM - Validate the lightbar on battery, after suspend, and after reboot

The lightbar registers (`0x0748` bit 7, `0x0749`-`0x074B`) were validated live only on AC power. The mainline driver also writes a battery-mode copy at `0x07E2`-`0x07E5` (all zero on 2026-09-29), which the service leaves alone. Unplug the charger and confirm whether the chosen color, rainbow, and off stay the same; if the lightbar changes, validate and add the battery copy. Also confirm that the choice returns within about 15 seconds after reboot and immediately after resume, whether the lightbar ever forgets its state without the bytes changing (the reason for the 15-minute rewrite), and what the EC shows while suspended (`0x0748` bit 3 is clear, which the mainline driver describes as the suspend breathing animation).

## LOW - Defer to a kernel lightbar driver if one appears

If a future `uniwill-laptop` enables its lightbar LED (`uniwill:multicolor:status`) for `GMxZGxx`, or `tuxedo-drivers` is loaded again, the kernel and the service would both write the lightbar and the service's 15-second check would override changes made through sysfs. Detect that LED and route the lightbar through it, as cTGP already prefers the kernel attribute.

## MEDIUM - Stress-test higher GPU power limits on Linux

The 2026-09-29 stress test ran at the 115 W base without `nvidia-powerd`; the 2026-10-13 Dynamic Boost run above reached 140 W for 60 s only. Repeat `scripts/linux/stress_test.py` at +25 W and +50 W and confirm the enforced limit, temperatures, and fan response stay within safe margins.

## LOW - Consider a Debian package

Linux ships as a source archive with `install.sh`. A `.deb` would need to bundle PySide6 (Ubuntu 24.04 has no package) or download it during installation. Revisit if Ubuntu packages PySide6 or the GUI moves to an Ubuntu-packaged toolkit.

## HIGH - Smoke-test the packaged privilege split

Build the two applications and verify on Windows that the frontend stays at normal-user integrity, only the backend shows UAC, backend recovery also shows UAC, and the elevated backend restarts the sibling frontend executable without elevation. Also verify that canceling UAC produces a useful frontend state instead of a silent failure. Do not select Automatic or perform a live write during this test.

## HIGH - Test frontend and backend crash recovery end to end

With hardware writes disabled or mocked, terminate each process independently and verify the surviving side restarts it no more than once per 60 seconds. Confirm that a deliberately closed frontend remains detached and that backend recovery restores the selected mode without creating overlapping automatic cycles.

## HIGH - Harden elevated-backend IPC authorization

Review the endpoint-file ACL and token lifecycle on Windows. Ensure another local user cannot read the token or command the elevated backend, stale endpoint files cannot redirect the frontend, comparisons remain constant-time, and malformed or oversized requests cannot create an unbounded workload.

## HIGH - Add regression coverage for every automatic safety invariant

Expand pure and mocked tests to cover implausible high readings, malformed values, minimum/maximum endpoint ordering, 5% rounding boundaries, Fan Boost races, backup failure, service reconnect failure, and the fixed 80 C full-speed cap. Tests must not perform live fan writes.

## MEDIUM - Version and type the IPC contract

Replace loosely shaped request and response dictionaries with a documented protocol version and validated message schemas. Keep backward-incompatible frontend/backend combinations from issuing control commands.

## MEDIUM - Add structured backend diagnostics

Add redacted rotating logs for process starts, privilege state, sensor-source failures, MQTT reconnects, mode changes, automatic targets, watchdog restarts, and shutdowns. Never log the IPC token, MQTT password, or full sensitive payloads.

## MEDIUM - Add automated GUI state tests

Test the mode toggle, whole-panel disabled overlay, curve slider constraints, reset behavior, low-duty confirmation, four gauges, backend-offline state, and mode resynchronization with Qt's offscreen platform and a mocked backend.

## LOW - Add a documentation consistency check

Add a lightweight check that flags tracked source files missing from `STRUCTURE.md`, invalid TODO priority headings, and accident entries missing Problem, Outcomes, or Solution sections.

## LOW - Review gauge accessibility

Check contrast, scaling, keyboard navigation, screen-reader labels, and meaning without color for temperature and fan-duty gauges. Keep the layout usable at the minimum supported window size and Windows display scaling levels.

## HIGH - Validate full-speed exit and Windows shutdown on target hardware

With explicit authorization for live fan writes, verify both fans reach 100% on Exit, shutdown, restart, and sign-out with OEM MQTT and Direct EC. Check pending-operation handling, failure cancellation, and OEM service teardown ordering. Mock tests do not establish that Windows allows enough time or that firmware retains the targets throughout shutdown.

## HIGH - Validate experimental firmware MUX selection on target Windows and Linux

The owner-operated official switch was correlated with only byte 0x62 of the 180-byte UniWillVariable changing 1/0/1 on 2026-10-15. The custom app has a gated adapter and two confirmations, with exact YES DO IT required in the second. Mocked safety/GUI coverage is present. The owner reported one successful custom-app save to dGPU and a boot in dGPU mode on 2026-10-15; nothing else has been validated. With explicit authorization for hardware-changing tests, verify both modes, backup/readback, Windows write-path privilege restoration, the Linux password flow (separate entry below), preservation of other bytes, and external pre-boot recovery with the broken panel. Do not infer success from EC telemetry. The only firmware-protection change allowed is the Linux immutable-flag lift inside a password-authorized save (AGENTS.md).

## HIGH - Complete Control Center 4.9 target validation

The exact 4.9 DLL now passes a separate board/SKU/BIOS/universal-control gate. The owner-authorized 2026-10-15 test stopped the OEM service, started the custom source app, and confirmed direct uniform fan-table writes, preserved subsystem bits, and RPM. Long-duration Auto, recovery, shutdown, and cTGP/lightbar writes on this new image still need validation. See cc_analysis/control-center-4.9-fans.md. Keep unknown DLLs refused and retain the installed driver/DLL when stopping the OEM UI/service.

## MEDIUM - Bound the Linux service's IPC against local resource exhaustion

Review 2026-10-16: the socket is 0666 and `BackendRequestHandler` reads up to 64 KiB with a 30 s timeout before checking SO_PEERCRED, with no cap on handler threads. A local user outside the allow-list held enough connections in a scaled-down repro (64-descriptor limit) to make sysfs sensor reads and the tccd check fail with EMFILE, defeating Auto and the sensor-loss Boost. Check peer credentials right after accept (`verify_request`), cap concurrent handlers, shorten the read timeout, and raise `LimitNOFILE`.

## MEDIUM - Validate 0x07C5 as a bitfield on Control Center 4.9

Review 2026-10-16, unverified on hardware: on the 4.9 image `direct_fan_control.py` sets 0x07C5 to `previous | 0x80`, but the connect gate accepts only the listed fan-mode values. If 0x07C5 was 0x40 or 0x10 before, the result (0xC0, 0x90) fails the next connect, so later Auto, sensor-loss Boost and the exit write would all fail. Check the real values on target hardware, then validate the byte as a bitfield or refuse an activation whose result would fail the gate; add a reconnect-after-write test.

## LOW - Manual-panel inconsistencies in the GUI

Review 2026-10-16: switching Auto to Manual shows slider values that were never applied (the EC keeps the last Auto duty, and dragging back to the shown value sends nothing); and a slider release that happens while a background operation is running is dropped without a retry. Load the sliders from the last written duties on entering Manual, and keep a pending apply that re-runs when the busy state clears, as the lightbar path does.

## LOW - Smaller safety and hardening gaps from the 2026-10-16 review

- A request accepted just before service stop can write after the stop-time 100% write; set a stopping flag that refuses write commands first.
- Windows writes 0x0741 as the whole byte 1 and its connect gate accepts only 0 or 1, so an EC status bit (fan-abnormal 0x20) would lock the app out during a fan fault; preserve and accept the other bits after checking them on hardware.
- `backend.linux_fan_control boost off --apply` writes zone 0's duty to all zones without the 30% floor.
- `install.sh` stops the service first; a later failure (for example `pip` without network) leaves fan control stopped with fans at 100% and no message. Add a trap that restarts it.
- Linux `set_dynamic_boost` accepts non-boolean values (the string "false" counts as true); type-check it like Windows.
- If sensors work but every Auto write fails, the last duty stays with no Boost fallback; consider Boost after N consecutive failed Auto writes.
- Nothing prunes `fan-backups`.

## LOW - MUX display and error-reporting gaps from the 2026-10-16 review

- The "Please reboot" hint comes from an in-memory flag: it stays after switching back to the boot-time mode and is lost after a service or app restart. Compare the saved mode with the mode at boot instead (on Linux the AMD iGPU's presence also shows the active route).
- If the transport write itself raises, there is no re-read and the message has no backup path; efivarfs can report an error after the variable was set. Re-read once (no retry) and report the saved mode and the backup.
- On Windows `conflicting_writer()` only checks the GCUBridge broker port; check whether the Control Center GUI or its service can write UniWillVariable while the broker is stopped.
- Request threads are daemon threads, so a `set_mux` still running at service stop is cut off mid-flow.
