# TODO

This is the single list of open technical debt, known problems, investigations, and planned work. Entries are not grouped by category. Every entry starts with exactly one impact priority: `LOW`, `MEDIUM`, `HIGH`, or `CRITICAL`. Remove an entry when it is completed and preserve any lasting decision in `DESIGN.md`; record an actual important failure in `ACCIDENTS.md`.

## HIGH - Validate the 30-second sensor-loss Boost on target hardware

The owner decided on 2026-09-29 that both fans go to 100% when no valid CPU or GPU reading succeeds for 30 seconds; the backend now enables EC Fan Boost and refuses Manual writes until recovery. Only mocked tests cover it. With explicit authorization for live writes, inject a sensor failure (for example a failing `nvidia-smi`) on Linux and on Windows with both OEM MQTT and Direct EC, and confirm Boost within about 35 seconds, the GUI warning, and correct recovery in Automatic and Manual modes.

## HIGH - Re-test Windows after the cross-platform refactor

The 2026-09-29 Linux work changed shared code that has not run on Windows hardware: the controller now selects its service and sensors by platform, imports `msvcrt` conditionally, runs the sensor watchdog, reports capabilities, guards the Auto thread, and authorizes requests through the server class. The loopback client error text changed from "not responding" to "not available" when connecting fails. The GUI gained the grayed GPU power row, RPM/power details, tighter spacing, and a guarded completion handler. Run the Windows CI, build and install the package, and verify startup, tray, Exit at 100%, session shutdown, GCUBridge switching, Manual writes, Boost, and Automatic mode on the laptop.

## HIGH - Run the new shared tests on Windows CI

`tests/test_linux_backend.py` runs on Windows except its Unix-socket and service-settings classes. Confirm that the fake-EC, sensor, controller-safety, and Linux service tests pass on the Windows runner, and fix any path or platform assumption they expose.

## MEDIUM - Show CPU and GPU power draw and fan RPM on Windows

The GUI shows power and RPM details only when the backend reports them. On Windows, add GPU `power.draw` to a separate `nvidia-smi` query (do not change the fail-closed temperature parse), CPU package power through the PawnIO AMD module if it allows the RAPL energy MSR, and fan RPM from EC `0x0464`/`0x046C` in the direct path. Validate each value against the Linux readings on the same laptop.

## MEDIUM - Decide whether Windows should stop writing whole control bytes

Windows direct EC writes the OEM-traced bytes `0x0741=1`, `0x0751` and `0x07C5` equal to the mode, and `0x07C6=5`. `0x07C5` bit 5 is the firmware's WhisperMode flag forwarded to the NVIDIA driver, and Boost clears the split-table bit 7. Linux uses bit-level writes. Compare the OEM Control Center's own writes and decide, with a Windows hardware test, whether Windows should adopt the Linux semantics.

## LOW - Add the GPU power limit on Windows

The cTGP control is Linux-only and grayed out on Windows. A Windows version would write `0x0743`-`0x0746` through the direct path and must refuse while the OEM broker runs, because Control Center manages the same registers.

## HIGH - Validate Linux after removing TUXEDO

The Linux service was validated with `tccd` disabled but `tuxedo-drivers` still loaded. After `sudo apt remove tuxedo-control-center tuxedo-drivers` and a reboot, confirm that `uniwill-laptop` loads, that the service starts in Automatic mode at boot, the boot value of `0x0751`, cTGP through `/sys/bus/platform/devices/INOU0000:*/ctgp_offset`, and that Fn lock and charge limits still work.

## HIGH - Validate Linux suspend, resume, and shutdown

Confirm on the laptop that the resume hook's `SIGUSR1` re-applies the table and cTGP immediately after suspend, that `systemctl stop` and shutdown leave both fans at 100%, and how the mainline driver's shutdown hook (clearing `0x0741` bit 0) affects the final seconds.

## HIGH - Validate the Dynamic Boost checkbox on Linux

The switch clears or sets EC `0x0743` bit 1 like TUXEDO's `db_enable`, but its effect on NVIDIA's enforced limit has not been measured yet. With `nvidia-powerd` running, confirm that turning it off lowers `enforced.power.limit` by the boost room, that the choice survives a service restart, suspend and resume, and that the mainline driver's resume re-enable is corrected.

## MEDIUM - Stress-test higher GPU power limits on Linux

The 2026-09-29 stress test ran at the 115 W base without `nvidia-powerd`. Repeat `scripts/linux/stress_test.py` at +25 W and +50 W and confirm the enforced limit, temperatures, and fan response stay within safe margins.

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
