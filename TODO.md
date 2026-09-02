# TODO

This is the single list of open technical debt, known problems, investigations, and planned work. Entries are not grouped by category. Every entry starts with exactly one impact priority: `LOW`, `MEDIUM`, `HIGH`, or `CRITICAL`. Remove an entry when it is completed and preserve any lasting decision in `DESIGN.md`; record an actual important failure in `ACCIDENTS.md`.

## CRITICAL - Define and validate the sensor-loss emergency behavior

Automatic mode currently fails closed and performs no new write when either temperature source fails. Determine on the target laptop whether retaining the previous OEM curve is always safe during a prolonged sensor outage, and whether a separately validated emergency action is needed. Do not change the current fail-closed rule or trigger Fan Boost automatically without explicit hardware testing and a documented safety decision.

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
