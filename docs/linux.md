# Ubuntu Guide

The Linux version runs on Ubuntu 24.04 without TUXEDO Control Center, without extra kernel modules, and without PawnIO.

## Requirements

- The validated Stellaris 15 Gen3 with BIOS N.1.61A15 (see [supported hardware](../README.md#supported-hardware-and-software))
- Ubuntu 24.04 with Python 3.11 or newer and `python3-venv`
- The NVIDIA driver with `nvidia-smi`
- Secure Boot kernel lockdown disabled: lockdown blocks `/dev/mem` access to the EC window

## How It Works

- A root systemd service, `stellaris-fan-control`, owns fan control. It starts in Automatic mode at every boot and keeps running when no GUI is open.
- The PySide6 GUI runs as your normal user in the tray and talks to the service over a Unix socket. Only root and the user who ran the installer may send commands; the kernel's peer credentials decide.
- CPU temperature comes from the kernel `k10temp` driver, which reads the same Ryzen `Tctl` register as the Windows PawnIO path. GPU temperature and power come from `nvidia-smi`, CPU package power from RAPL.
- Fans are written directly in EC RAM through the firmware's INOU memory window, after strict platform checks. Every write is read back and rolled back on mismatch. Details: [safety model](safety.md#ubuntu).
- All 16 zones of each EC fan table get the same duty, so the unreliable EC temperature cannot change the fan speed.
- Every 15 seconds the service verifies the EC state and rewrites it if the firmware drifted. After resume, a systemd sleep hook triggers the same check immediately.
- When the NVIDIA GPU is powered off (PCI runtime status `suspended`), it counts as cold: Automatic mode uses the CPU temperature alone and the GPU is not woken.
- GPU power limit and Dynamic Boost use the kernel driver's cTGP attribute when present and the EC otherwise, and are restored after every service start and resume.
- Stopping the service, including shutdown and reboot, writes 100% to both fans. The GUI's **Quit** only closes the GUI.

## Install

1. Stop TUXEDO Control Center's daemon, or remove TUXEDO entirely. The service refuses every fan write while `tccd` runs:

   ```bash
   sudo systemctl disable --now tccd
   # or: sudo apt remove tuxedo-control-center tuxedo-drivers && sudo reboot
   ```

   After `tuxedo-drivers` is removed, the kernel's own `uniwill-laptop` driver loads at the next boot and provides Fn lock, battery charge limits, and the cTGP attribute.

2. From an extracted `StellarisFanControl-linux.tar.gz` release or a clone of this repository, run the installer from your normal account. This one command installs, updates, and starts fan control:

   ```bash
   sudo ./scripts/linux/install.sh
   ```

   If `tccd` is enabled, the installer asks to disable it (add `--yes` to accept without asking).

3. Open **Stellaris Fan Control** from the application menu, or log in again.

The installer:

- checks the laptop and copies the application to `/opt/stellaris-fan-control`;
- creates a PySide6 environment for the GUI (downloaded from PyPI);
- allows only your user to control the service (`/etc/stellaris-fan-control/config.json`);
- installs and enables the systemd unit and the resume hook;
- installs the polkit rule that asks for a password before an NVIDIA Optimus change;
- adds GUI autostart for your login;
- enables `nvidia-powerd` if it is not running, installing its unit and D-Bus policy when Ubuntu's driver package lacks them (NVIDIA applies cTGP and Dynamic Boost only while it runs; TUXEDO's driver packages set it up the same way);
- starts the service in Automatic mode.

**Updating**: quit the GUI, then rerun the installer from the new version so the new GUI opens next time. Both fans run at 100% while the service is stopped for the update.

**Uninstalling**: run `sudo ./scripts/linux/uninstall.sh`. Add `--purge` to also delete settings and fan backups in `/var/lib/stellaris-fan-control`. Both fans stay at 100% until the next reboot.

## Service and Logs

```bash
systemctl status stellaris-fan-control
journalctl -u stellaris-fan-control
```

The journal records failed Automatic cycles, EC drift repairs, and the start and end of a sensor-loss Boost.

The service keeps its settings in `/var/lib/stellaris-fan-control/settings.json` and fan backups in `/var/lib/stellaris-fan-control/fan-backups`. The GUI keeps its own preferences in `~/.config/stellaris-fan-control/StellarisFanControl.json`.

## Low-Level CLI

```bash
cd /opt/stellaris-fan-control
sudo python3 -m backend.linux_fan_control probe        # read-only report
sudo systemctl stop stellaris-fan-control             # the CLI refuses writes while the service runs
sudo python3 -m backend.linux_fan_control fixed 65 --gpu-duty 75          # dry run
sudo python3 -m backend.linux_fan_control fixed 65 --gpu-duty 75 --apply
```

Every CLI write first saves a `LINUX_EC-*.json` backup in `/var/lib/stellaris-fan-control/fan-backups`.

## Recovery

If temperatures rise unexpectedly, do not wait for this application to recover. Enable Fan Boost immediately or shut down the laptop. Stopping the service also puts both fans at 100%:

```bash
sudo systemctl stop stellaris-fan-control
```

To restore a fan backup with the service stopped:

```bash
cd /opt/stellaris-fan-control
sudo python3 -m backend.linux_fan_control restore /var/lib/stellaris-fan-control/fan-backups/LINUX_EC-TIMESTAMP.json --apply
```

## Stress Test

`scripts/linux/stress_test.py` loads every CPU core and the NVIDIA GPU for 180 seconds, samples the running service every two seconds, writes a CSV report, and stops the load if the CPU reaches 96 C or the GPU 90 C. Add `--no-cpu` or `--no-gpu` to load only one of them. It drives real hardware: run it only when you intend to. Run it as your user with the GUI environment:

```bash
/opt/stellaris-fan-control/.venv/bin/python scripts/linux/stress_test.py
```

## Validation Status

All tests below ran on the one validated laptop.

- **Fan tables**: a live test confirmed that the EC follows the tables written by the service and that Boost reaches 100% within seconds.
- **Dynamic Boost** (2026-10-13): a GPU-only stress test held 140 W with no cTGP offset. Turning it off (2026-10-18) dropped the limit to 115 W within about 2 seconds and held it under load.
- **Higher GPU power** (2026-10-18, Dynamic Boost off, 180-second GPU-only loads): +25 W held a 140 W limit with up to 139.9 W draw, GPU 77 C, fans 95%. +50 W held 165 W with 163 W average draw, GPU 86 C and still rising about 1 C per 30 seconds at the end, fans 100%; only the power cap limited the GPU. A 300-second +50 W run with Dynamic Boost on held 165 W (164-165 W draw) and reached 87 C after about 290 seconds, with CPU 78 C from the GPU's heat.
- **Tray icon** (2026-10-18, GNOME on X11, during the 300-second +50 W run): the icon turned yellow above 70 C and red above 85 C, alternated between its two reds, and showed one "GPU is at 86 C, above 85 C" notification.
- **NVIDIA Optimus** (2026-10-16, owner-run): the password dialog appeared after **YES DO IT**, polkit granted a one-shot authorization, and the save changed only byte `0x62`. Still to check: Cancel and a wrong password leave everything untouched, the immutable-flag path after a fresh boot, and that the next boot uses the saved mode.
- **Sensor-loss Boost** (2026-10-18): a test-only `nvidia-smi` wrapper failed on demand. In Automatic and in Manual 60/60, Boost reached 100% (about 4340/4500 RPM) 32-34 seconds after the last good reading. The GUI showed the sensor warning, and Manual writes and turning Boost off were refused. Within 3 seconds of readings returning, Automatic resumed or Manual 60/60 was restored.
