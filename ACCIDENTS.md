# Accidents

This is the factual log of important failures or harmful behavior encountered by this project or the target system. Every entry must have a timestamp, title, problem, outcomes, and solution. When the actual incident time is unknown, the entry must say so and use the earliest recorded remediation timestamp instead of inventing a time. Design risks and unconfirmed possibilities belong in `TODO.md`.

## 2026-10-18T12:18:00+03:00 - Temp cleanup deletes the running application's sensor module

Actual occurrence time: shortly before 12:16 local time; the exact minute is unknown. The timestamp records diagnosis from a screenshot of the running application.

### Problem

The installed one-file executable unpacked its bundle, including `pawnio\AMDFamily17.bin`, into `%TEMP%\_MEI00000d582` at launch. A user-approved disk cleanup deleted the contents of `%TEMP%` while the application was running. Files that were not locked, including the AMD module, were removed. Temperature reads then failed with `[Errno 2] No such file or directory: ...\_MEI00000d582\pawnio\AMDFamily17.bin`.

### Outcomes

After more than 30 seconds without a valid reading, the sensor watchdog enabled EC Fan Boost, and the interface reported "Boost confirmed at 100%" with CPU and GPU temperatures unavailable. No automatic target was written from invalid data. Restarting the application unpacked a fresh copy and readings recovered.

### Solution

Build a PyInstaller one-folder application and install the executable with its `_internal\` directory under `%ProgramFiles%\StellarisFanControl`, so no runtime file lives in `%TEMP%`. The installer replaces `_internal\` whole, keeps the user's preferences, refuses while the application runs, and verifies every installed file's SHA-256.

The one-folder build passed `--check-qt` with exit code 0 and the offscreen suite (230 tests, seven Linux-only skips). It was installed at 12:33 with every file's SHA-256 verified and the preferences file unchanged, then started through the sign-in task. It created no `%TEMP%\_MEI*` directory and showed 46 C CPU, 45 C GPU and 50% Automatic duty with both fan RPMs. A second launch exited with code 0 and kept the same process.

## 2026-10-11T03:33:27+03:00 - Local Windows update fails to load QtCore

Actual occurrence time: unknown. The timestamp records diagnosis following the user's failed-launch report.

### Problem

PyInstaller resolved `icuuc.dll` from the build environment's unrelated Poppler PATH directory. That ICU 78 library exports version-suffixed symbols, while the bundled Qt6Core requires the Windows ICU API including the unsuffixed `ucnv_open`. Source Qt imports worked, but the installed executable raised an ImportError before creating its controller. Loading the extracted Qt6Core reproduced a Windows entry-point error naming `ucnv_open`.

### Outcomes

The updated application could not start. The failed launch did not construct a controller or issue fan writes. The user's previous executable and settings were backed up, and installation preserved the settings. Hardware fan speed during the interruption was not measured.

### Solution

Restrict DLL discovery to the selected Python runtime, virtual environment, and Windows directories during packaging, then restore the caller's PATH. Add `--check-qt` to exercise actual frozen Qt imports and an offscreen window/event loop without importing hardware backends. Cover the no-backend property with a subprocess regression test, rebuild, check the executable, and replace the installed copy.

The corrected executable passed `--check-qt` with exit code 0 and was installed with matching SHA-256 and unchanged preferences. Its normal window displayed valid temperatures, GPU power, and fan RPM; Automatic updates resumed. A second executable launch exited with code 0 and retained the same original process IDs and window. The offscreen source suite passed 85 tests with two Linux-only skips. No lightbar mode was selected or tested.

## 2026-09-27T01:14:30+03:00 - Release v1.0.0 crashes during Qt startup

Actual occurrence time: unknown. The timestamp records investigation after the user's traceback report.

### Problem

Frontend startup called `QApplication.setFallbackSessionManagementEnabled`, which is absent from the pinned PySide6 6.11.2 Qt 6 API. Compilation and handler-only mocked tests did not execute this startup path.

### Outcomes

The user reported an AttributeError on launch of the packaged application. The failure prevents normal startup. Hardware outcomes were not measured.

### Solution

Remove the obsolete call while retaining the direct `commitDataRequest` shutdown connection. Add a subprocess regression test that creates the real Qt application and window and enters its event loop with hardware initialization mocked. Rebuild installation and publish a corrected patch release.

## 2026-09-02T02:58:04+03:00 - OEM CPU temperature path became untrustworthy

Actual incident time: unknown. The heading uses the timestamp of commit `cce4712`, where the remediation was first recorded in this repository.

### Problem

The target laptop's embedded-controller/OEM Control Center path stopped receiving the correct CPU temperature. The stock fan behavior could therefore make decisions from a stale or incorrect CPU reading.

### Outcomes

- The original fan curve could leave the fans too slow while the CPU was heating up.
- Continuing to use the OEM, EC, or MQTT CPU reading in Automatic mode became unsafe.
- Overheating, instability, shutdown, hardware damage, and data loss became credible failure outcomes.
- On 2026-09-29 a read-only Ubuntu check confirmed the same failure outside Windows: during a 12-second all-core load, `k10temp` Tctl rose from 54 C to 75 C while EC register `0x043E` and the ACPI `acpitz` zone stayed at 55 C, and the EC GPU temperature read 0. TUXEDO Control Center's Linux fan logic reads exactly these EC values, so it was driving both fans from the frozen CPU value.

### Solution

CPU temperature now comes from the Ryzen `Tctl/Tdie` SMN register `0x00059800` through PawnIO's restricted `AMDFamily17` module. GPU temperature comes independently from the NVIDIA driver. Automatic control fails closed when either reading is missing, zero, malformed, or implausible, uses the hotter valid reading, and preserves OEM Fan Boost as the immediate 100% fallback.
