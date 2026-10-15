# Windows control-loop safety review - 2026-10-16

Reviewed executable/source baseline: commit 2f5a2e584d7053c39ef657cc04c23b7243205930. Scope: Windows packaged single-process mode and Direct EC, with the shared controller, sensor watchdog, Auto failure handling, GUI worker, request deadlines and shutdown path. Shared-controller findings also affect Linux; its service/process boundary differs from the Windows package.

Result: three actionable findings remain. This review cannot establish that the application cannot stall or overheat the laptop. These are reproducible software failure paths, not observations that the laptop overheated. No application behavior was changed by this review.

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
