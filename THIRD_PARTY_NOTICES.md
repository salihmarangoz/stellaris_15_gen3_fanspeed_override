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

The module is restricted to approved AMD MSR and SMN operations. This application calls `ioctl_read_smn` only for the Ryzen thermal register and `ioctl_read_msr` only for package-energy units (`0xC0010299`) and the package-energy counter (`0xC001029B`). It never calls a write operation. The pin and binary are unchanged.

The CPU package-power calculation follows AMD RAPL's energy-counter delta, unit exponent, and unsigned 32-bit wrap, cross-checked against [LibreHardwareMonitor's AMD implementation](https://github.com/LibreHardwareMonitor/LibreHardwareMonitor/blob/75e0106f1af4fbdc0cb5d95ca32dc15f8ab070d7/LibreHardwareMonitorLib/Hardware/Cpu/Amd17Cpu.cs). No source code from that implementation is copied. Read-only sampling on the target laptop on 2026-10-11 confirmed the pinned module accepts both reads.

## OEM Control Center direct interface

When the OEM MQTT broker is unavailable, the application loads the locally installed Control Center 3.9.42.1 `ACPIDriverDll.dll` and uses its `ReadEC` and `WriteEC` exports with the installed `UWACPIDriver`. These OEM files are not copied into or distributed with this repository.

The validated `ACPIDriverDll.dll` SHA-256 is `345CFF34994351E126C4A7EF9FFC8E09FDE005951F1A63AF50D1945F28961A33`. A different library build disables direct control until it is separately analyzed and validated on the target laptop.

The Windows RPM and lightbar implementation reuses the register meanings cross-checked for the Linux implementation below. It uses the existing OEM DLL only, with no new driver or third-party binary. Those registers have been validated on the laptop under Linux; live Windows RPM/lightbar validation remains pending.

Windows cTGP uses the same Linux-validated encoding at `0x0743`-`0x0746`, with additional exact board/SKU/BIOS gates. Static extraction of the user's Control Center 4.9.47.13 ZIP on 2026-10-11 confirmed matching cTGP/TPP/Dynamic Boost address names in service metadata; the same addresses also have fan-PWM aliases on other platforms. Protected method bodies prevented verification of OEM ordering, bit masks, or extra NVIDIA calls. The package and its different ACPI DLL were not installed, executed, or substituted for the pinned library. No OEM code or binary is redistributed. Subsequent authorized Windows tests confirmed NVIDIA enforced 115, 130, and 165 W with Dynamic Boost off through the existing driver; A later authorized 60-second GPU CUDA load reached 98-99% utilization, 112.95 W draw and 63 C, while owner-selected NVIDIA limits changed; Dynamic Boost switching and sustained full-ceiling behavior remain pending. The local diagnostic uses the installed NVIDIA CUDA Driver API and original embedded PTX, without installing or redistributing CUDA tools. Local analysis tools (ILSpy and innoextract) are ignored build artifacts, not application dependencies.

## Local FurMark validation tool

FurMark 2.10.2 is a local portable diagnostic downloaded from <https://geeks3d.com/dl/get/830>, verified against the official download page SHA-256 `27AB2E723E2E65DF720BCAFEA681D2104744EDA4A1E0A0374D7E61EAA820E63B`. The package, executables and generated reports remain ignored under `build/qa`; it is not installed, bundled or redistributed with the application. Its GPU-monitor plugin was disabled during validation.

## Linux EC interface

The Linux version uses no third-party binaries or kernel modules. It maps the EC RAM window that the laptop's own ACPI firmware declares (`\_SB.INOU`, `0xFE200000`, 4 KiB) through `/dev/mem`, gated by the SHA-256 of the BIOS N.1.61A15 DSDT (`B35BF53709DB9AC190CE13489619FFCF7793B78B65C7888D76E25FFC49EAEA62`).

Register meanings and the fan-table zone thresholds were cross-checked against the GPL-licensed mainline Linux `uniwill-laptop` driver (`drivers/platform/x86/uniwill/`) and TUXEDO Computers' `tuxedo-drivers`. No source code from either project is copied into this repository; only register addresses and numeric values used for interoperability are reused.

## PySide6

The GUI uses PySide6 (Qt for Python), installed from PyPI into the application's own virtual environment on Linux and bundled into the Windows executable. PySide6 and Qt are available under the GNU LGPL v3 (and other licenses from The Qt Company).

- Project: <https://pypi.org/project/PySide6/>
