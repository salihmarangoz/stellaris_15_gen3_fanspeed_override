# Stellaris 15 Gen3 fan control

> [!CAUTION]
> i made this for my own Stellaris 15 Gen3 laptop and it is still experimental. it directly replaces fan curves through the installed OEM Control Center service. a bug, a bad sensor value, or an incompatible laptop can cause overheating, instability, hardware damage, or data loss. use it at your own risk. keep another temperature monitor open and be ready to use the OEM Fan Boost button or shut the laptop down.

> [!WARNING]
> this project can also mess up the already installed OEM fan-control app or its configuration. the Control Center GUI may start showing weird fan curves, wrong-looking values, broken layouts, or other strange behavior after custom curves are written. in the worst case you may need to restore a backup, reset Control Center, or reinstall it. this project does not intentionally patch the OEM program files, but it does change the fan data that program uses.

## why i made this

the embedded controller on my laptop stopped getting the correct CPU temperature. because of that, the original fan curve could let the laptop overheat.

this app works around that problem. it can set the CPU and GPU fans manually, or control both fans from independent CPU and GPU temperature readings.

i built and tested it for one Stellaris 15 Gen3 / XMG-Uniwill-style laptop with OEM Control Center 3.9.42.1. the MQTT messages, table names, curve format, authentication, and EC behavior may be different on another laptop or Control Center version. do not assume it is compatible just because the laptop looks similar.

## how it works

- fan commands go to the locally installed OEM Control Center service through its IPv6 loopback MQTT broker.
- CPU temperature comes directly from the Ryzen `Tctl/Tdie` SMN register through the signed [PawnIO](https://github.com/namazso/PawnIO) driver and a restricted AMD module.
- the broken Control Center CPU temperature is never used for Auto mode.
- GPU temperature comes from the NVIDIA driver through `nvidia-smi`.
- Manual mode has separate CPU and GPU fan controls in 5% steps. anything below 30% needs confirmation.
- Automatic mode checks every 15 seconds, uses the hotter value from `max(CPU, GPU)`, and sends one shared target to both fans.
- the sensor panel shows CPU/GPU temperature and reported CPU/GPU fan duty gauges.
- only one GUI and one Control Center client are allowed at a time.

if either temperature is missing, zero, malformed, or not believable, Auto mode stops that cycle before writing a fan target.

## automatic curve

the two temperature points are adjustable from 0 to 100 C:

| default temperature | fan duty |
| ---: | ---: |
| 40 C or below | 30% |
| 80 C or above | 100% |

the app fills in the values between those points with a straight line and rounds the result to the nearest 5%. Auto mode never asks for less than 30%, and it always asks for 100% at 80 C or above with the default settings.

selecting **Automatic** starts a cycle immediately and then repeats every 15 seconds. selecting **Manual** stops future automatic cycles. Manual is the default when the app opens.

the inactive section is grayed out. the sensor gauges and OEM **Fan Boost 100%** fallback stay available in both modes.

## requirements

- Windows 11
- the compatible OEM Control Center service installed and running
- Python 3.11 or newer when running from source
- the signed PawnIO driver
- administrator access
- an NVIDIA GPU with a working `nvidia-smi.exe`

## run it from source

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\run_fan_control_gui.cmd
```

the launcher runs `setup_pawnio.ps1`. it installs PawnIO if needed, downloads the pinned and SHA-256-checked AMD sensor module, asks for administrator access, and then starts the GUI. Core Temp is not needed.

the command-line write commands are dry runs unless `--apply` is added:

```powershell
.\.venv\Scripts\python.exe .\fan_control.py status
.\.venv\Scripts\python.exe .\fan_control.py rpm
.\.venv\Scripts\python.exe .\fan_control.py fixed 65 --gpu-duty 75
```

inspect the preview before adding `--apply`. manual writes create a backup of the active curve first. source backups go in `fan-backups`; packaged builds use `%LOCALAPPDATA%\StellarisFanControl\fan-backups`.

## build the exe

```powershell
powershell.exe -ExecutionPolicy Bypass -File .\build_exe.ps1
```

the script creates the virtual environment if needed, prepares PawnIO, installs the build requirements, and writes `dist\StellarisFanControl.exe`. the AMD sensor module and `stellaris15gen3.css` are bundled into the exe. the signed PawnIO driver and OEM Control Center still have to be installed separately.

the exe asks for administrator access when it starts. generated executables, PyInstaller files, downloaded modules, virtual environments, caches, and fan backups are ignored by git.

## recovery

backups can be previewed and restored with:

```powershell
.\.venv\Scripts\python.exe .\fan_control.py restore .\fan-backups\M2T1-TIMESTAMP.json
.\.venv\Scripts\python.exe .\fan_control.py restore .\fan-backups\M2T1-TIMESTAMP.json --apply
```

the first command only shows what would be restored. the second one writes it.

if temperatures climb unexpectedly, do not wait for this app to fix itself. turn on OEM Fan Boost immediately or shut the laptop down.
