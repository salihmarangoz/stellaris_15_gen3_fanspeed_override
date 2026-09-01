# Third-party notices

## PawnIO

PawnIO provides the signed kernel driver used for restricted hardware access. It is installed separately through Windows Package Manager and is not committed to this repository.

- Project: <https://github.com/namazso/PawnIO>
- Installer: <https://github.com/namazso/PawnIO.Setup>
- License: GNU General Public License v2.0 or later, with the exception described by the project

## AMD Family 17h module

The build downloads `AMDFamily17.bin` from LibreHardwareMonitor commit `75e0106f1af4fbdc0cb5d95ca32dc15f8ab070d7`. The expected SHA-256 is `DAE74615761B78BDF064DFB3E136252DDCC6FC727D88F14738D0E5800D427A91`. It is bundled into local executable builds but ignored by Git.

- Binary source: <https://github.com/LibreHardwareMonitor/LibreHardwareMonitor/blob/75e0106f1af4fbdc0cb5d95ca32dc15f8ab070d7/LibreHardwareMonitorLib/Resources/PawnIo/AMDFamily17.bin>
- Module source: <https://github.com/namazso/PawnIO.Modules/blob/main/AMDFamily17.p>
- LibreHardwareMonitor license: Mozilla Public License 2.0
- PawnIO module license: see the PawnIO and PawnIO.Modules repositories

The module is restricted to approved AMD MSR and SMN operations. This application calls only its `ioctl_read_smn` operation for the Ryzen thermal register.
