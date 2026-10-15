# Windows control-loop safety review - 2026-10-16

Reviewed executable/source baseline: commit 2f5a2e584d7053c39ef657cc04c23b7243205930. Scope: Windows packaged single-process mode and Direct EC, with the shared controller, sensor watchdog, Auto failure handling, GUI worker, request deadlines and shutdown path. Shared-controller findings also affect Linux; its service/process boundary differs from the Windows package.

Original review result: three actionable findings on the baseline below. The subsequent implementation addresses their software failure paths; see the remediation section. No review or test establishes that the laptop cannot overheat or that a hung kernel driver can be recovered. The findings below preserve the original baseline evidence; they are not claims about the updated code.

## P1 - Sensor and EC stalls can block emergency Boost

Locations: backend/fan_control_backend.py:237-246, 277-299, 467-477; backend/temperature_service.py:121-160; backend/direct_fan_control.py:203-220.

The watchdog calls _read_validated_temperatures before checking freshness. That read acquires the same sensor lock held across the native CPU read and NVIDIA query. A read that never returns therefore keeps the watchdog waiting before it reaches _update_sensor_emergency. Even a direct freshness check must acquire the state lock, which is held across hardware fan writes; an EC transaction that never returns blocks that route too.

The CPU transport uses synchronous DeviceIoControl calls without an application operation deadline. The one-second Access_PCI mutex wait bounds only acquisition of that mutex, not the subsequent driver request. [Microsoft documents that a non-overlapped DeviceIoControl waits for operation completion or an error](https://learn.microsoft.com/en-us/windows/win32/api/ioapiset/nf-ioapiset-deviceiocontrol).

Reproduction: an injected stalled sensor held the sensor lock while the last successful sample was set to 60 seconds old. The watchdog remained blocked and requested no Boost until the fake read was released. A separately injected blocked fan write held the state lock and likewise prevented stale-sensor Boost until released. Each artificial wait was bounded and released during the test; no real driver was stalled.

Consequence: the documented 30-second sensor-loss fallback does not cover an indefinitely blocked read/write. Previous cooling targets can remain active while the safety thread is stuck.

Required direction: freshness evaluation must not depend on obtaining a new sensor sample or waiting indefinitely for controller locks. Bound or isolate hardware operations, retain a single EC owner, and explicitly handle a stuck actuator. Do not launch competing EC writes from another thread or forcibly kill a thread inside a hardware call. A watchdog using the same hung driver cannot promise hardware recovery; any independent fallback requires its own validated recovery design.

## P1 - Failed hot Auto writes do not attempt emergency Boost

Location: backend/fan_control_backend.py:487-494.

An exception during a fan update sets last_auto_error and schedules the next attempt 15 seconds later. It does not attempt the separate Boost path. The sensor watchdog responds to missing/stale readings, so fresh high readings do not activate it merely because actuator writes fail.

Reproduction: with fake CPU/GPU readings of 95/85 C, the fan-table operation raised a simulated readback failure. The controller recorded the error and scheduled a retry in 15 seconds; the independently functioning fake Boost operation was never called. A successful hot-cycle control test did request 100/100 on both fake fans.

Consequence: after a failed 100% update or rollback, an older low target can remain despite fresh high temperatures. This is distinct from a sensor-loss emergency.

Required direction: make control-output failures a separate safety condition. Attempt guarded Boost when a required Auto write fails, expose and retry that failure, and prevent later low manual/Auto writes from silently clearing the condition until recovery is verified. Preserve OEM ownership refusal and report failure when Boost itself cannot be delivered. Regression cases must cover hot readings, write failure, rollback, failed Boost and recovery.

## P2 - Packaged request deadlines are ignored, leaving controls busy

Locations: stellaris15gen3.py:10-20; frontend/fan_control_gui.py:1348-1360, 1472-1514, 2242-2250, 2334-2342.

InProcessBackendClient.request discards request_timeout and directly calls controller.dispatch. A native operation that never returns consequently holds the one GUI worker indefinitely. Boost, mode switching and Exit stay disabled while busy, and the backend check skips busy/in-flight operations. Repainting can continue, but the user cannot complete these actions. Windows session shutdown has an outer 45-second cancellation wait; that does not cancel the underlying hardware call or make normal Exit bounded.

Reproduction: a fake in-process request given a 10 ms timeout was still running after 100 ms. In an offscreen real Qt window it remained busy after 150 ms, Boost and Exit were disabled, and check_backend issued no heartbeat. Releasing the fake driver allowed the UI to recover. No real fan controller was constructed for this GUI test.

Required direction: add observable operation deadlines and a defined stalled-operation state to the package. Keep the interface informative and responsive without allowing unsafe overlapping writers or spawning a second hardware owner. A frontend timeout alone must not be mistaken for cancellation of the hardware operation.

## Checks that passed and test limits

- 31 existing selected safety, sensor-recovery and Qt shutdown tests passed. The normal hot Auto path requests both fake fans at 100%; ordinary sensor exceptions eventually activate Boost, and recovery resumes the intended mode.
- The earlier listener-table fix retains per-write ownership checks and its mocked fail-closed coverage. It removes the measured TCP delays; it does not resolve the failure paths above.
- All new review tests used fake hardware. No live fan target, GPU power, lightbar, firmware or service setting was changed. The real application remained closed. The owner allowed Manual testing at a 50% minimum and later offered a stop of at most 30 seconds; stopping the real fans was unnecessary and was not performed.
- Manual mode intentionally holds the selected duty. Valid hot readings alone do not make Manual 50% automatically increase; the simulated Manual 50%/95 C case confirmed that behavior. Manual targets are not a substitute for Auto thermal regulation.
- No sustained load, actual overheating, native-driver hang, forced termination, reboot or hardware thermal-protection behavior was tested. Current integrity checks and the previous short latency run are not proof against these failure cases.

Raw fault reproductions and scripts are ignored under build/qa/windows-safety-review-faults.json, build/qa/windows-safety-review-ui.json, build/qa/review-safety-failure-paths.py and build/qa/review-safety-ui-stall.py.

## Remediation - 2026-10-16

- Sensor waits now have a 2 s caller deadline and a separate single in-flight lane. Freshness is checked before attempting a read; timed-out/late reads cannot refresh it. The watchdog can attempt Boost while a fake sensor remains stuck.
- EC calls have an 8 s caller deadline, retaining exclusive ownership until the actual operation returns. A late operation is followed by a guarded Boost attempt on its same worker before releasing ownership. No second EC operation starts while it remains stuck; ordinary contention is serialized only within the caller deadline, and expired waiters are never run later. Controller state locks are released when the bounded caller returns.
- Failed fan targets, including simulated readback/rollback errors, immediately attempt Boost. Output faults and Boost errors/confirmation are separately visible. Lower Manual writes and Boost-off are blocked. Fresh sensors or successful Boost alone cannot clear an output fault; only a verified fresh Auto target or explicit verified Manual 100/100 recovery can.
- Packaged request_timeout is honored (15 s default). Real offscreen Qt tests show busy clears and Boost/Exit controls become available after a deadline. Further requests are refused while the previous request is actually unfinished. The frontend does not spawn a replacement controller for this in-process condition.
- The GUI distinguishes confirmed emergency Boost from unconfirmed hardware safety. Exit still refuses to complete unless both 100% targets were successfully written. Optional power telemetry, peripheral maintenance and service close use bounded serialized operations too.

Tracked regression coverage: tests/test_operation_safety.py and tests/test_operation_safety_gui.py. They use fake hardware with bounded Event waits, always released during cleanup. Existing sensor recovery, shutdown, OEM ownership, verification and rollback suites remain applicable. Raw per-module results are ignored under build/qa/safety-fix-module-tests.json.

Remaining physical limit: a permanently hung EC driver also prevents its Boost command. This implementation keeps control/UI waits bounded and reports unconfirmed safety; it cannot cancel arbitrary kernel calls or guarantee cooling through an unavailable actuator. Actual kernel hangs, forced termination, sustained-load thermals and Linux target-hardware recovery have not been validated by these tests.

### Authorized Windows live check of the fixed controller

The app stayed closed; no Auto scheduler or peripheral-maintenance thread was started. Both complete fan tables were initially 100%. A per-byte test guard refused duty-table values below 50%, and recorded zero such attempts. The test wrote Manual 60/60, accepted real PawnIO/NVIDIA temperatures (about 40/38 C), then simulated stale failed sensors by aging the timestamp and substituting a failing reader. It did not wait for a real hardware failure or stall the native driver.

The controller enabled and confirmed EC Boost in 65.836 ms; a separate EC status read reported FanBoostEnable=1. Restoring real readings returned to all 16 entries of both saved 60% tables in 3289.531 ms, with the emergency cleared. Final exit preparation wrote and verified all 32 duties at 100% in 3217.917 ms. Immediate fan telemetry was still ramping (63.5%); the table target was 100%, not a claim that physical speed had already settled. No heat load or stopped-fan test was performed. Raw result: build/qa/windows-safety-fix-live.json. The subsequent ordinary-contention adjustment is covered by mocked serialization/deadline tests; it changes waiting behavior, not the EC protocol.

Final software validation: 195 tests across 15 individually isolated modules, 7 platform-specific skips and no failures (188 executed successfully). Separate module processes avoid the existing Qt test-state interference in combined discovery. Compilation and diff whitespace checks also passed.

### Installed build

Code commit 33902a3 was built locally, passed the packaged --check-qt test (exit 0) and installed to C:/Program Files/StellarisFanControl/StellarisFanControl.exe. Source and installed SHA-256 both equal 5E94581DDB4ABF62D186FD8225D5918C98BF82C539F8F91464348DEC76A89E21. Existing frontend preferences were preserved byte-for-byte. Previous EXE/settings backup: C:/Users/salih/Desktop/Apps/stellaris_15_gen3_fanspeed_override/build/install-backups/20261016-022649. The highest-privilege sign-in task still points to the installed executable. The normal hardware-controlling GUI was left closed; real FanControlWindow responsiveness/startup and deadline recovery were exercised with mocked backends/offscreen Qt, while the packaged check exercised its bundled Qt. No normal packaged-window/singleton hardware smoke was performed in this run, preserving the authorized live-test floor. Installation evidence: ignored build/qa/install-safety-fix-status.json.
