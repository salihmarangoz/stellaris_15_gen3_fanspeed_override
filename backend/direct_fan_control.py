import ctypes
import copy
import hashlib
import json
import os
import socket
import sys
import time
from pathlib import Path
from typing import Any, Callable

from shared.fan_control_common import validate_lightbar


OEM_HOST = "::1"
OEM_PORT = 13688
OEM_CONTROL_CENTER_DIRECTORY = Path(
    os.environ.get("ProgramFiles", r"C:\Program Files")
) / "OEM" / "Control Center"
ACPI_LIBRARY_PATH = (
    OEM_CONTROL_CENTER_DIRECTORY
    / "UniwillService"
    / "MyControlCenter"
    / "ACPIDriverDll.dll"
)

# Control Center 3.9.42.1, validated on the target Stellaris 15 Gen3.
ACPI_LIBRARY_SHA256 = "345cff34994351e126c4a7ef9ffc8e09fde005951f1a63af50d1945f28961a33"
ACPI_LIBRARY_49_SHA256 = "ee2c506dd414222013e6dbe5c67a774b1fa1387a47ad5d705a82f048c1a945bb"
SUPPORTED_ACPI_LIBRARIES = {ACPI_LIBRARY_SHA256, ACPI_LIBRARY_49_SHA256}
# The library's ReadEC sends a 4 MiB request, and the driver runs an empty loop
# once per input byte (~8 ms). For this exact driver image the same read IOCTL
# is sent directly with a small buffer; see cc_analysis/windows-ec-driver-busy-wait.md.
# Writes still use the library until the direct write is validated on the target.
UWACPI_DRIVER_PATH = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "drivers" / "UWACPIDriver.sys"
UWACPI_DRIVER_SHA256 = "e9840521975e9073881a488f9e978a18c93aed44c57fe2382b1d513fd27126e2"
UWACPI_DEVICE = "\\\\.\\ACPIDriver"
IOCTL_READ_EC = 0x9C40A488
EC_REQUEST_SIZE = 64
EXPECTED_PROJECT_ID = 0x10

PROJECT_ID_ADDRESS = 0x0740
AP_EXISTS_ADDRESS = 0x0741
FAN_CONTROL_ADDRESS = 0x0751
FAN_CONTROL_MIRROR_ADDRESS = 0x07C5
FAN_SUBSYSTEM_STATE_ADDRESS = 0x07C6
CPU_DUTY_ADDRESS = 0x075B
GPU_DUTY_ADDRESS = 0x075C
CPU_FAN_RPM_ADDRESS = 0x0464
GPU_FAN_RPM_ADDRESS = 0x046C
MAX_PLAUSIBLE_RPM = 10000
# cTGP on the exact Linux-validated board/BIOS. OEM 4.9 metadata names the same
# four registers, but aliases them to fan PWM on other platforms.
CTGP_CONTROL_ADDRESS = 0x0743
CTGP_OFFSET_ADDRESS = 0x0744
CTGP_TPP_ADDRESS = 0x0745
CTGP_DB_ADDRESS = 0x0746
CTGP_ADDRESSES = (CTGP_OFFSET_ADDRESS, CTGP_TPP_ADDRESS, CTGP_DB_ADDRESS, CTGP_CONTROL_ADDRESS)
CTGP_GENERAL_ENABLE = 0x01
CTGP_DB_ENABLE = 0x02
CTGP_OFFSET_ENABLE = 0x04
MAX_CTGP_OFFSET_WATTS = 50
# Same laptop registers validated by the Linux implementation on AC power.
LIGHTBAR_CONTROL_ADDRESS = 0x0748
LIGHTBAR_RAINBOW = 0x80
LIGHTBAR_COLOR_ADDRESSES = (0x0749, 0x074A, 0x074B)
LIGHTBAR_ADDRESSES = LIGHTBAR_COLOR_ADDRESSES + (LIGHTBAR_CONTROL_ADDRESS,)
CPU_TEMP_UP_ADDRESS = 0x0F00
CPU_TEMP_DOWN_ADDRESS = 0x0F10
CPU_TABLE_DUTY_ADDRESS = 0x0F20
GPU_TEMP_UP_ADDRESS = 0x0F30
GPU_TEMP_DOWN_ADDRESS = 0x0F40
GPU_TABLE_DUTY_ADDRESS = 0x0F50
TABLE_POINT_COUNT = 16
# curve() hands its table read to the write that immediately follows it in the
# same operation. Any EC write, or this much time, discards it.
TABLE_SNAPSHOT_SECONDS = 1.0
# Between complete table checks, an unchanged Auto target only re-reads the 32
# duty and 4 mode bytes. With one duty in all 16 zones the 64 temperature
# thresholds cannot change the fan speed.
FULL_TABLE_CHECK_SECONDS = 300.0

FAN_CONTROL_BOOST = 0x40
FAN_CONTROL_USER_HIGH = 0xA0
KNOWN_FAN_CONTROL_VALUES = {
    0x00,
    0x10,
    FAN_CONTROL_BOOST,
    0x80,
    0x81,
    0x82,
    0x83,
    0x84,
    0x85,
    FAN_CONTROL_USER_HIGH,
}


def oem_curve_cache_path() -> Path:
    if getattr(sys, "frozen", False):
        root = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "StellarisFanControl"
    else:
        root = Path(__file__).resolve().parents[1] / "fan-backups"
    return root / "last-oem-curve.json"


def save_oem_curve_cache(curve: dict[str, Any]) -> Path:
    path = oem_curve_cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(curve, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)
    return path


def load_oem_curve_cache() -> dict[str, Any] | None:
    try:
        value = json.loads(oem_curve_cache_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def oem_broker_available(timeout: float = 0.2) -> bool:
    if os.name == "nt":
        from backend.windows_broker import broker_listening
        return broker_listening(OEM_HOST, OEM_PORT)
    try:
        with socket.create_connection((OEM_HOST, OEM_PORT), timeout=timeout):
            return True
    except OSError:
        return False


def validate_ctgp_platform() -> None:
    import winreg

    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\BIOS") as key:
        values = tuple(winreg.QueryValueEx(key, name)[0] for name in
                       ("BaseBoardProduct", "SystemSKU", "BIOSVersion"))
    if values != ("GMxZGxx", "STELLARIS1XA03", "N.1.61A15"):
        raise RuntimeError("GPU power control requires the validated Stellaris board, SKU, and BIOS")


class DriverEcReader:
    """EC byte reads through UWACPIDriver's ECRR IOCTL on one persistent handle."""

    def __init__(self) -> None:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateFileW.argtypes = [
            ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
            ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
        ]
        kernel32.CreateFileW.restype = ctypes.c_void_p
        kernel32.DeviceIoControl.argtypes = [
            ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_uint32,
            ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32), ctypes.c_void_p,
        ]
        kernel32.DeviceIoControl.restype = ctypes.c_int
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        self._kernel32 = kernel32
        # The same open the library performs: read/write access, shared, existing device.
        handle = kernel32.CreateFileW(UWACPI_DEVICE, 0xC0000000, 3, None, 3, 0, None)
        if handle in (None, ctypes.c_void_p(-1).value):
            raise OSError(f"Cannot open the OEM ACPI driver (Windows error {ctypes.get_last_error()})")
        self._handle = handle

    def read(self, address: int) -> int:
        if self._handle is None:
            raise RuntimeError("The OEM ACPI driver handle is closed")
        buffer = (ctypes.c_ubyte * EC_REQUEST_SIZE)()
        ctypes.c_uint32.from_buffer(buffer).value = address
        returned = ctypes.c_uint32()
        if not self._kernel32.DeviceIoControl(
            self._handle, IOCTL_READ_EC, buffer, EC_REQUEST_SIZE,
            buffer, EC_REQUEST_SIZE, ctypes.byref(returned), None,
        ):
            raise RuntimeError(
                f"EC read at 0x{address:04X} failed (Windows error {ctypes.get_last_error()})"
            )
        # ECRR returns one byte as an ACPI integer; anything wider is malformed.
        if returned.value < 4 or any(buffer[1:4]):
            raise RuntimeError(f"Malformed EC read result at 0x{address:04X}")
        return buffer[0]

    def close(self) -> None:
        handle, self._handle = self._handle, None
        if handle is not None:
            self._kernel32.CloseHandle(handle)


def open_driver_ec_reader() -> DriverEcReader | None:
    """Return a direct reader only for the exact analyzed driver image."""
    try:
        digest = hashlib.sha256(UWACPI_DRIVER_PATH.read_bytes()).hexdigest()
    except OSError:
        return None
    if digest != UWACPI_DRIVER_SHA256:
        return None
    try:
        return DriverEcReader()
    except OSError:
        # The library path still works; it is only slower.
        return None


class DirectEcClient:
    """Direct fan-table access through the installed, validated Uniwill driver DLL."""

    method_name = "direct_ec"

    def __init__(
        self,
        *,
        library_path: Path = ACPI_LIBRARY_PATH,
        library_loader: Callable[[str], Any] | None = None,
        broker_available: Callable[[], bool] = oem_broker_available,
        fallback_curve: dict[str, Any] | None = None,
        driver_reader: Callable[[], Any | None] | None = None,
    ) -> None:
        self._library_path = library_path
        self._library_loader = library_loader
        # A substituted library (tests) never reaches the real driver by default.
        self._driver_reader_factory = driver_reader or (
            open_driver_ec_reader if library_loader is None else (lambda: None)
        )
        self._driver_reader: Any | None = None
        self.read_transport: str | None = None
        self._broker_available = broker_available
        self._fallback_curve = copy.deepcopy(fallback_curve or load_oem_curve_cache())
        self._library_sha256: str | None = None
        self._library: Any | None = None
        self._read_ec: Callable[[int], int] | None = None
        self._write_ec: Callable[[int, int], None] | None = None
        self._table_snapshot: tuple[float, dict[str, list[int]]] | None = None
        # (time, complete table, mode bytes) last verified as the active user curve.
        self._verified: tuple[float, dict[str, list[int]], tuple[int, int, int, int]] | None = None

    def connect(self) -> None:
        if self._library is not None:
            return
        if os.name != "nt":
            raise RuntimeError("Direct EC fan control is available only on Windows")
        try:
            digest = hashlib.sha256(self._library_path.read_bytes()).hexdigest()
        except OSError as exc:
            raise RuntimeError(
                f"Cannot read the OEM ACPI library at {self._library_path}"
            ) from exc
        if digest.lower() not in SUPPORTED_ACPI_LIBRARIES:
            raise RuntimeError(
                "The installed OEM ACPI library is not the validated Control Center "
                "3.9.42.1 or 4.9.47.13 build; direct fan control is disabled"
            )

        if digest == ACPI_LIBRARY_49_SHA256:
            validate_ctgp_platform()
        self._library_sha256 = digest
        loader = self._library_loader or ctypes.WinDLL
        try:
            library = loader(str(self._library_path))
            read_ec = library.ReadEC
            write_ec = library.WriteEC
            read_ec.argtypes = [ctypes.c_uint32]
            read_ec.restype = ctypes.c_ubyte
            write_ec.argtypes = [ctypes.c_uint32, ctypes.c_uint32]
            write_ec.restype = None
        except (AttributeError, OSError) as exc:
            raise RuntimeError("Cannot load the validated OEM EC interface") from exc

        self._library = library
        self._read_ec = read_ec
        self._write_ec = write_ec
        self.read_transport = "library"
        reader = self._driver_reader_factory()
        if reader is not None:
            self._driver_reader = reader
            self._read_ec = reader.read
            self.read_transport = "driver_ioctl"
        try:
            self._validate_hardware()
        except Exception:
            self.close()
            raise

    def close(self) -> None:
        self._table_snapshot = None
        self._verified = None
        reader, self._driver_reader = self._driver_reader, None
        if reader is not None:
            reader.close()
        self.read_transport = None
        self._read_ec = None
        self._write_ec = None
        self._library = None
        self._library_sha256 = None

    def __enter__(self) -> "DirectEcClient":
        self.connect()
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()

    def _read(self, address: int) -> int:
        if self._read_ec is None:
            raise RuntimeError("Direct EC client is not connected")
        value = int(self._read_ec(address))
        if not 0 <= value <= 0xFF:
            raise RuntimeError(f"Invalid EC byte at 0x{address:04X}: {value}")
        return value

    def _write(self, address: int, value: int) -> None:
        if self._write_ec is None:
            raise RuntimeError("Direct EC client is not connected")
        if self._broker_available():
            raise RuntimeError("OEM broker became available before the direct EC write")
        if not 0 <= value <= 0xFF:
            raise ValueError(f"Invalid EC byte for 0x{address:04X}: {value}")
        self._table_snapshot = None
        self._verified = None
        self._write_ec(address, value)

    def _read_range(self, address: int, count: int = TABLE_POINT_COUNT) -> list[int]:
        return [self._read(address + offset) for offset in range(count)]

    def _validate_hardware(self) -> None:
        project_id = self._read(PROJECT_ID_ADDRESS)
        if project_id != EXPECTED_PROJECT_ID:
            raise RuntimeError(
                f"Unexpected EC project ID 0x{project_id:02X}; direct fan control is disabled"
            )
        control = self._read(FAN_CONTROL_ADDRESS)
        if control not in KNOWN_FAN_CONTROL_VALUES:
            raise RuntimeError(
                f"Unexpected fan-control byte 0x{control:02X}; direct fan control is disabled"
            )
        mirror = self._read(FAN_CONTROL_MIRROR_ADDRESS)
        if mirror not in KNOWN_FAN_CONTROL_VALUES:
            raise RuntimeError(
                f"Unexpected mirrored fan-control byte 0x{mirror:02X}; "
                "direct fan control is disabled"
            )
        if self._read(AP_EXISTS_ADDRESS) not in (0, 1):
            raise RuntimeError("Unexpected OEM application-presence byte")
        states = (1, 5)
        if self._library_sha256 == ACPI_LIBRARY_49_SHA256:
            if not self._read(0x078E) & 0x40:
                raise RuntimeError("The EC does not support validated universal fan control")
            # Observed on this exact board with 4.9 before custom fan activation.
            states = (0, 1, 3, 4, 5, 7)
        if self._read(FAN_SUBSYSTEM_STATE_ADDRESS) not in states:
            raise RuntimeError("Unexpected fan-subsystem state byte")
        table = self._read_raw_table()
        for key in ("cpu_up", "cpu_down", "gpu_up", "gpu_down"):
            values = table[key]
            if any(value > 120 and value != 0xFF for value in values):
                raise RuntimeError("The EC fan table contains implausible temperatures")
        self._decode_duties(table["cpu_duty"])
        self._decode_duties(table["gpu_duty"])
        if self._table_is_empty(table):
            if self._fallback_curve is None:
                raise RuntimeError(
                    "The OEM service cleared the EC fan table and no cached OEM curve is available"
                )
            self._encode_curve(self._fallback_curve)

    @staticmethod
    def _decode_duties(values: list[int]) -> list[int]:
        if any(value > 200 or value % 2 for value in values):
            raise RuntimeError("The EC fan table contains malformed duty values")
        return [value // 2 for value in values]

    @staticmethod
    def _encode_duty(value: Any) -> int:
        if isinstance(value, bool):
            raise ValueError("Fan duty must be an integer percentage")
        duty = int(value)
        if duty != value or not 0 <= duty <= 100:
            raise ValueError("Fan duty must be an integer from 0 to 100")
        return duty * 2

    @staticmethod
    def _encode_temperature(value: Any) -> int:
        if isinstance(value, bool):
            raise ValueError("Fan temperature must be an integer")
        temperature = int(value)
        if temperature != value or (
            not 0 <= temperature <= 120 and temperature != 0xFF
        ):
            raise ValueError("Fan temperature must be from 0 to 120, or the 255 sentinel")
        return temperature

    @classmethod
    def _encode_curve(cls, curve: dict[str, Any]) -> dict[str, list[int]]:
        result: dict[str, list[int]] = {}
        for name, prefix in (("CPU", "cpu"), ("GPU", "gpu")):
            points = curve.get(name)
            if not isinstance(points, list) or len(points) != TABLE_POINT_COUNT:
                raise ValueError("A direct fan curve must contain exactly 16 points per fan")
            if [point.get("ID") for point in points] != list(range(TABLE_POINT_COUNT)):
                raise ValueError("Direct fan-curve point IDs must be ordered from 0 through 15")
            up = [cls._encode_temperature(point["UpT"]) for point in points]
            down = [cls._encode_temperature(point["DownT"]) for point in points]
            result[f"{prefix}_up"] = up[1:] + [0xFF]
            result[f"{prefix}_down"] = [0] + down[:15]
            result[f"{prefix}_duty"] = [cls._encode_duty(point["Duty"]) for point in points]
        return result

    def _read_raw_table(self) -> dict[str, list[int]]:
        return {
            "cpu_up": self._read_range(CPU_TEMP_UP_ADDRESS),
            "cpu_down": self._read_range(CPU_TEMP_DOWN_ADDRESS),
            "cpu_duty": self._read_range(CPU_TABLE_DUTY_ADDRESS),
            "gpu_up": self._read_range(GPU_TEMP_UP_ADDRESS),
            "gpu_down": self._read_range(GPU_TEMP_DOWN_ADDRESS),
            "gpu_duty": self._read_range(GPU_TABLE_DUTY_ADDRESS),
        }

    @staticmethod
    def _table_is_empty(table: dict[str, list[int]]) -> bool:
        return all(value == 0 for values in table.values() for value in values)

    def status(self) -> dict[str, Any]:
        control = self._read(FAN_CONTROL_ADDRESS)
        return {
            "FAN_TableName": "DIRECT_EC",
            "FanBoostEnable": "1" if control == FAN_CONTROL_BOOST else "0",
            "ControlMethod": self.method_name,
        }

    def _take_table_snapshot(self) -> dict[str, list[int]] | None:
        snapshot, self._table_snapshot = self._table_snapshot, None
        if snapshot is None or not 0 <= time.monotonic() - snapshot[0] <= TABLE_SNAPSHOT_SECONDS:
            return None
        return snapshot[1]

    def curve(self, name: str) -> dict[str, Any]:
        del name
        table = self._read_raw_table()
        self._table_snapshot = (time.monotonic(), copy.deepcopy(table))
        if self._table_is_empty(table):
            if self._fallback_curve is None:
                raise RuntimeError("No complete fan curve is available for direct control")
            result = copy.deepcopy(self._fallback_curve)
            result["Name"] = "DIRECT_EC"
            result["DirectControl"] = {
                "project_id": self._read(PROJECT_ID_ADDRESS),
                "fan_control": self._read(FAN_CONTROL_ADDRESS),
                "fan_control_mirror": self._read(FAN_CONTROL_MIRROR_ADDRESS),
                "ap_exists": self._read(AP_EXISTS_ADDRESS),
                "fan_subsystem_state": self._read(FAN_SUBSYSTEM_STATE_ADDRESS),
                "library_sha256": self._library_sha256,
                "source": "last_oem_curve_cache",
            }
            return result

        cpu_up = [0] + table["cpu_up"][:15]
        cpu_down = table["cpu_down"][1:] + [0xFF]
        gpu_up = [0] + table["gpu_up"][:15]
        gpu_down = table["gpu_down"][1:] + [0xFF]
        cpu_duty = self._decode_duties(table["cpu_duty"])
        gpu_duty = self._decode_duties(table["gpu_duty"])

        def points(up: list[int], down: list[int], duty: list[int]) -> list[dict[str, int]]:
            return [
                {"ID": index, "UpT": up[index], "DownT": down[index], "Duty": duty[index]}
                for index in range(TABLE_POINT_COUNT)
            ]

        return {
            "Activated": True,
            "Name": "DIRECT_EC",
            "FanControlRespective": True,
            "CPU": points(cpu_up, cpu_down, cpu_duty),
            "GPU": points(gpu_up, gpu_down, gpu_duty),
            "DirectControl": {
                "project_id": self._read(PROJECT_ID_ADDRESS),
                "fan_control": self._read(FAN_CONTROL_ADDRESS),
                "fan_control_mirror": self._read(FAN_CONTROL_MIRROR_ADDRESS),
                "ap_exists": self._read(AP_EXISTS_ADDRESS),
                "fan_subsystem_state": self._read(FAN_SUBSYSTEM_STATE_ADDRESS),
                "library_sha256": self._library_sha256,
            },
        }

    def fan_info(self) -> dict[str, Any]:
        cpu = self._read(CPU_DUTY_ADDRESS)
        gpu = self._read(GPU_DUTY_ADDRESS)
        if cpu > 200 or gpu > 200:
            raise RuntimeError("The EC reported an implausible live fan duty")
        return {
            # Live ramp values use the same half-percent unit but can be odd.
            "CpuFanDuty": cpu / 2,
            "GpuFanDuty": gpu / 2,
            "CpuFanRpm": self._read_rpm(CPU_FAN_RPM_ADDRESS),
            "GpuFanRpm": self._read_rpm(GPU_FAN_RPM_ADDRESS),
            "ControlMethod": self.method_name,
        }

    def _read_rpm(self, address: int) -> int | None:
        try:
            rpm = (self._read(address) << 8) | self._read(address + 1)
        except (OSError, RuntimeError):
            return None
        return rpm if rpm <= MAX_PLAUSIBLE_RPM else None

    def _validate_ctgp_hardware(self) -> None:
        validate_ctgp_platform()
        if self._read(PROJECT_ID_ADDRESS) != EXPECTED_PROJECT_ID or not self._read(0x078E) & 0x40:
            raise RuntimeError("The EC does not match the validated universal-control platform")

    def read_ctgp_state(self) -> dict[str, Any]:
        self._validate_ctgp_hardware()
        control = self._read(CTGP_CONTROL_ADDRESS)
        offset = self._read(CTGP_OFFSET_ADDRESS)
        boost = self._read(CTGP_DB_ADDRESS)
        if offset > MAX_CTGP_OFFSET_WATTS or boost > 25:
            raise RuntimeError("The EC reported an unsupported GPU power configuration")
        return {
            "offset": offset if control & CTGP_GENERAL_ENABLE and control & CTGP_OFFSET_ENABLE else 0,
            "dynamic_boost": bool(control & CTGP_GENERAL_ENABLE and control & CTGP_DB_ENABLE),
            "dynamic_boost_w": boost,
            "configured": bool(control & CTGP_GENERAL_ENABLE and control & CTGP_OFFSET_ENABLE
                               and self._read(CTGP_TPP_ADDRESS) == 0xFF and boost == 25),
        }

    def write_ctgp(self, offset: int, dynamic_boost: bool) -> None:
        if type(offset) is not int or not 0 <= offset <= MAX_CTGP_OFFSET_WATTS:
            raise ValueError("The GPU power offset must be an integer from 0 to 50 W")
        if type(dynamic_boost) is not bool:
            raise ValueError("Dynamic Boost must be a boolean")
        self._validate_ctgp_hardware()
        if self._broker_available():
            raise RuntimeError("Stop GCUBridge before changing GPU power")
        self.read_ctgp_state()
        previous = {address: self._read(address) for address in CTGP_ADDRESSES}
        control = previous[CTGP_CONTROL_ADDRESS] | CTGP_GENERAL_ENABLE | CTGP_OFFSET_ENABLE
        control = control | CTGP_DB_ENABLE if dynamic_boost else control & ~CTGP_DB_ENABLE
        desired = dict(zip(CTGP_ADDRESSES, (offset, 0xFF, 25, control)))
        written = []

        def write(address: int, value: int) -> None:
            if self._broker_available():
                raise RuntimeError("OEM broker became available before the GPU power write")
            self._write(address, value)

        try:
            # Data first, enable bits last. Do not touch fan/WhisperMode bytes.
            for address, value in desired.items():
                if previous[address] != value:
                    written.append(address)
                    write(address, value)
            if any(self._read(address) != value for address, value in desired.items()):
                raise RuntimeError("GPU power readback did not match the write")
        except Exception as exc:
            try:
                for address in written:
                    write(address, previous[address])
            except Exception as rollback_error:
                raise RuntimeError(f"{exc}; GPU power rollback could not complete: {rollback_error}") from exc
            raise

    def read_lightbar(self) -> dict[str, Any]:
        control = self._read(LIGHTBAR_CONTROL_ADDRESS)
        color = [self._read(address) for address in LIGHTBAR_COLOR_ADDRESSES]
        mode = "rainbow" if control & LIGHTBAR_RAINBOW else "color" if any(color) else "off"
        validate_lightbar(mode, color)
        return {"mode": mode, "color": color}

    def _write_lightbar_byte(self, address: int, value: int) -> None:
        if self._broker_available():
            raise RuntimeError("OEM broker became available before the lightbar write")
        self._write(address, value)

    def write_lightbar(
        self, mode: str, color: tuple[int, int, int], *, force: bool = False
    ) -> bool:
        mode, color = validate_lightbar(mode, color)
        if self._broker_available():
            raise RuntimeError("Stop GCUBridge before changing the lightbar")
        previous = {address: self._read(address) for address in LIGHTBAR_ADDRESSES}
        validate_lightbar("color", [previous[address] for address in LIGHTBAR_COLOR_ADDRESSES])
        control = previous[LIGHTBAR_CONTROL_ADDRESS]
        if mode == "rainbow":
            desired = {LIGHTBAR_CONTROL_ADDRESS: control | LIGHTBAR_RAINBOW}
        else:
            desired = dict(zip(LIGHTBAR_COLOR_ADDRESSES, color if mode == "color" else (0, 0, 0)))
            desired[LIGHTBAR_CONTROL_ADDRESS] = control & ~LIGHTBAR_RAINBOW
        changed = any(previous[address] != value for address, value in desired.items())
        written: list[int] = []
        try:
            for address, value in desired.items():
                if force or previous[address] != value:
                    written.append(address)
                    self._write_lightbar_byte(address, value)
            if any(self._read(address) != value for address, value in desired.items()):
                raise RuntimeError("Lightbar readback did not match the write")
        except Exception as exc:
            # If OEM ownership returned, stop all writes, including rollback.
            # Otherwise restore colors before the animation bit.
            try:
                for address in written:
                    self._write_lightbar_byte(address, previous[address])
            except Exception as rollback_error:
                raise RuntimeError(
                    f"{exc}; lightbar rollback could not complete: {rollback_error}"
                ) from exc
            raise
        return changed

    def _mode_bytes(self) -> tuple[int, int, int, int]:
        return (
            self._read(FAN_CONTROL_ADDRESS),
            self._read(FAN_CONTROL_MIRROR_ADDRESS),
            self._read(AP_EXISTS_ADDRESS),
            self._read(FAN_SUBSYSTEM_STATE_ADDRESS),
        )

    def uniform_duty_verified(self, cpu: int, gpu: int) -> bool:
        """Confirm an unchanged uniform target with 36 reads instead of a full check.

        True only within FULL_TABLE_CHECK_SECONDS of a complete verification
        with no EC write since, for the same duties, and when the live duty and
        mode bytes still match. Anything else needs the complete path.
        """
        verified = self._verified
        if verified is None or not 0 <= time.monotonic() - verified[0] <= FULL_TABLE_CHECK_SECONDS:
            return False
        _, table, modes = verified
        expected = {"cpu_duty": [self._encode_duty(cpu)] * TABLE_POINT_COUNT,
                    "gpu_duty": [self._encode_duty(gpu)] * TABLE_POINT_COUNT}
        if any(table[key] != values for key, values in expected.items()):
            return False
        live = {
            "cpu_duty": self._read_range(CPU_TABLE_DUTY_ADDRESS),
            "gpu_duty": self._read_range(GPU_TABLE_DUTY_ADDRESS),
        }
        if live != expected or self._mode_bytes() != modes:
            self._verified = None
            return False
        return True

    def set_curve(self, curve: dict[str, Any]) -> bool:
        return self._write_curve(curve, FAN_CONTROL_USER_HIGH)

    def restore_curve(self, curve: dict[str, Any]) -> None:
        metadata = curve.get("DirectControl")
        if not isinstance(metadata, dict):
            raise ValueError("This is not a direct-EC backup")
        control = int(metadata.get("fan_control", -1))
        if control not in KNOWN_FAN_CONTROL_VALUES:
            raise ValueError("The direct-EC backup has an invalid fan-control byte")
        self._write_curve(curve, control)

    def _write_curve(self, curve: dict[str, Any], control: int) -> bool:
        """Write the table and mode bytes unless the EC already holds them."""
        snapshot = self._take_table_snapshot()
        if self._broker_available():
            raise RuntimeError("OEM broker became available before the direct fan write")
        encoded = self._encode_curve(curve)

        previous = snapshot if snapshot is not None else self._read_raw_table()
        previous_control = self._read(FAN_CONTROL_ADDRESS)
        previous_mirror = self._read(FAN_CONTROL_MIRROR_ADDRESS)
        previous_ap_exists = self._read(AP_EXISTS_ADDRESS)
        previous_subsystem_state = self._read(FAN_SUBSYSTEM_STATE_ADDRESS)
        addresses = {
            "cpu_up": CPU_TEMP_UP_ADDRESS,
            "cpu_down": CPU_TEMP_DOWN_ADDRESS,
            "cpu_duty": CPU_TABLE_DUTY_ADDRESS,
            "gpu_up": GPU_TEMP_UP_ADDRESS,
            "gpu_down": GPU_TEMP_DOWN_ADDRESS,
            "gpu_duty": GPU_TABLE_DUTY_ADDRESS,
        }
        subsystem = previous_subsystem_state | 0x04 if self._library_sha256 == ACPI_LIBRARY_49_SHA256 else 5
        mirror = previous_mirror | 0x80 if self._library_sha256 == ACPI_LIBRARY_49_SHA256 else control
        if (
            previous == encoded
            and (previous_control, previous_mirror, previous_ap_exists, previous_subsystem_state)
            == (control, mirror, 1, subsystem)
        ):
            # The fresh reads verify the state. Rewriting it would cost about
            # 200 more busy-waiting driver transactions every Auto cycle.
            self._remember_verified(control, previous, (control, mirror, 1, subsystem))
            return False
        try:
            self._write(AP_EXISTS_ADDRESS, 1)
            self._write(FAN_SUBSYSTEM_STATE_ADDRESS, subsystem)
            self._write(FAN_CONTROL_ADDRESS, control)
            self._write(FAN_CONTROL_MIRROR_ADDRESS, mirror)
            order = (
                ("gpu_up", GPU_TEMP_UP_ADDRESS),
                ("cpu_up", CPU_TEMP_UP_ADDRESS),
                ("gpu_down", GPU_TEMP_DOWN_ADDRESS),
                ("cpu_down", CPU_TEMP_DOWN_ADDRESS),
                ("gpu_duty", GPU_TABLE_DUTY_ADDRESS),
                ("cpu_duty", CPU_TABLE_DUTY_ADDRESS),
            )
            for offset in range(TABLE_POINT_COUNT):
                for key, address in order:
                    self._write(address + offset, encoded[key][offset])
            if self._read_raw_table() != encoded:
                raise RuntimeError("Complete fan-table readback did not match the direct write")
            if self._read(FAN_CONTROL_ADDRESS) != control:
                raise RuntimeError("Fan-control mode readback did not match the direct write")
            if self._read(FAN_CONTROL_MIRROR_ADDRESS) != mirror:
                raise RuntimeError("Mirrored fan-control mode did not match the direct write")
            if self._read(AP_EXISTS_ADDRESS) != 1:
                raise RuntimeError("OEM application-presence readback did not match the direct write")
            if self._read(FAN_SUBSYSTEM_STATE_ADDRESS) != subsystem:
                raise RuntimeError("Fan-subsystem state did not match the direct write")
        except Exception:
            for key, address in addresses.items():
                for offset, value in enumerate(previous[key]):
                    self._write(address + offset, value)
            self._write(FAN_CONTROL_ADDRESS, previous_control)
            self._write(FAN_CONTROL_MIRROR_ADDRESS, previous_mirror)
            self._write(AP_EXISTS_ADDRESS, previous_ap_exists)
            self._write(FAN_SUBSYSTEM_STATE_ADDRESS, previous_subsystem_state)
            raise
        self._remember_verified(control, encoded, (control, mirror, 1, subsystem))
        return True

    def _remember_verified(
        self, control: int, table: dict[str, list[int]], modes: tuple[int, int, int, int]
    ) -> None:
        # Only the user curve qualifies; a restored OEM mode is never short-checked.
        if control == FAN_CONTROL_USER_HIGH:
            self._verified = (time.monotonic(), copy.deepcopy(table), modes)

    def set_boost(self, enabled: bool) -> None:
        if self._broker_available():
            raise RuntimeError("OEM broker became available before the direct boost write")
        expected = FAN_CONTROL_BOOST if enabled else FAN_CONTROL_USER_HIGH
        current = (
            self._read(AP_EXISTS_ADDRESS),
            self._read(FAN_SUBSYSTEM_STATE_ADDRESS),
            self._read(FAN_CONTROL_ADDRESS),
            self._read(FAN_CONTROL_MIRROR_ADDRESS),
        )
        subsystem = current[1] | 0x04 if self._library_sha256 == ACPI_LIBRARY_49_SHA256 else 5
        mirror = current[3] | 0x80 if self._library_sha256 == ACPI_LIBRARY_49_SHA256 else expected
        if current == (1, subsystem, expected, mirror):
            return
        self._write(AP_EXISTS_ADDRESS, 1)
        self._write(FAN_SUBSYSTEM_STATE_ADDRESS, subsystem)
        self._write(FAN_CONTROL_ADDRESS, expected)
        self._write(FAN_CONTROL_MIRROR_ADDRESS, mirror)
        if (
            self._read(FAN_CONTROL_ADDRESS) != expected
            or self._read(FAN_CONTROL_MIRROR_ADDRESS) != mirror
        ):
            raise RuntimeError("Fan Boost readback did not match the direct write")
