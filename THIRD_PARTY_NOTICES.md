# Third-party notices

## PawnIO

PawnIO provides the signed kernel driver used for restricted hardware access. It is installed separately through Windows Package Manager and is not committed to this repository.

- Project: <https://github.com/namazso/PawnIO>
- Installer: <https://github.com/namazso/PawnIO.Setup>
- License: GNU General Public License v2.0 or later, with the exception described by the project

## AMD Family 17h module

The build setup in `scripts/setup_pawnio.ps1` downloads `AMDFamily17.bin` from LibreHardwareMonitor commit `75e0106f1af4fbdc0cb5d95ca32dc15f8ab070d7`. The expected SHA-256 is `DAE74615761B78BDF064DFB3E136252DDCC6FC727D88F14738D0E5800D427A91`. It is bundled into local executable builds but ignored by Git.

- Binary source: <https://github.com/LibreHardwareMonitor/LibreHardwareMonitor/blob/75e0106f1af4fbdc0cb5d95ca32dc15f8ab070d7/LibreHardwareMonitorLib/Resources/PawnIo/AMDFamily17.bin>
- Module source: <https://github.com/namazso/PawnIO.Modules/blob/main/AMDFamily17.p>
- LibreHardwareMonitor license: Mozilla Public License 2.0
- PawnIO module license: see the PawnIO and PawnIO.Modules repositories

The module is restricted to approved AMD MSR and SMN operations. This application calls only its `ioctl_read_smn` operation for the Ryzen thermal register.

## OEM Control Center direct interface

When the OEM MQTT broker is unavailable, the application loads the locally installed Control Center 3.9.42.1 `ACPIDriverDll.dll` and uses its `ReadEC` and `WriteEC` exports with the installed `UWACPIDriver`. These OEM files are not copied into or distributed with this repository.

The validated `ACPIDriverDll.dll` SHA-256 is `345CFF34994351E126C4A7EF9FFC8E09FDE005951F1A63AF50D1945F28961A33`. A different library build disables direct control until it is separately analyzed and validated on the target laptop.
