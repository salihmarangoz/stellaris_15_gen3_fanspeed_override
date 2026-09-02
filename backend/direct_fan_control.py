import ctypes
import copy
import hashlib
import json
import os
import socket
import sys
from pathlib import Path
from typing import Any, Callable


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
EXPECTED_PROJECT_ID = 0x10

PROJECT_ID_ADDRESS = 0x0740
AP_EXISTS_ADDRESS = 0x0741
FAN_CONTROL_ADDRESS = 0x0751
FAN_CONTROL_MIRROR_ADDRESS = 0x07C5
FAN_SUBSYSTEM_STATE_ADDRESS = 0x07C6
CPU_DUTY_ADDRESS = 0x075B
GPU_DUTY_ADDRESS = 0x075C
CPU_TEMP_UP_ADDRESS = 0x0F00
CPU_TEMP_DOWN_ADDRESS = 0x0F10
CPU_TABLE_DUTY_ADDRESS = 0x0F20
GPU_TEMP_UP_ADDRESS = 0x0F30
GPU_TEMP_DOWN_ADDRESS = 0x0F40
GPU_TABLE_DUTY_ADDRESS = 0x0F50
TABLE_POINT_COUNT = 16

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
    try:
        with socket.create_connection((OEM_HOST, OEM_PORT), timeout=timeout):
            return True
    except OSError:
        return False


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
    ) -> None:
        self._library_path = library_path
        self._library_loader = library_loader
        self._broker_available = broker_available
        self._fallback_curve = copy.deepcopy(fallback_curve or load_oem_curve_cache())
        self._library: Any | None = None
        self._read_ec: Callable[[int], int] | None = None
        self._write_ec: Callable[[int, int], None] | None = None

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
        if digest.lower() != ACPI_LIBRARY_SHA256:
            raise RuntimeError(
                "The installed OEM ACPI library is not the validated Control Center "
                "3.9.42.1 build; direct fan control is disabled"
            )

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
        try:
            self._validate_hardware()
        except Exception:
            self.close()
            raise

    def close(self) -> None:
        self._read_ec = None
        self._write_ec = None
        self._library = None

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
        if not 0 <= value <= 0xFF:
            raise ValueError(f"Invalid EC byte for 0x{address:04X}: {value}")
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
        if self._read(FAN_SUBSYSTEM_STATE_ADDRESS) not in (1, 5):
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

    def curve(self, name: str) -> dict[str, Any]:
        del name
        table = self._read_raw_table()
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
                "library_sha256": ACPI_LIBRARY_SHA256,
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
                "library_sha256": ACPI_LIBRARY_SHA256,
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
            "ControlMethod": self.method_name,
        }

    def set_curve(self, curve: dict[str, Any]) -> None:
        self._write_curve(curve, FAN_CONTROL_USER_HIGH)

    def restore_curve(self, curve: dict[str, Any]) -> None:
        metadata = curve.get("DirectControl")
        if not isinstance(metadata, dict):
            raise ValueError("This is not a direct-EC backup")
        control = int(metadata.get("fan_control", -1))
        if control not in KNOWN_FAN_CONTROL_VALUES:
            raise ValueError("The direct-EC backup has an invalid fan-control byte")
        self._write_curve(curve, control)

    def _write_curve(self, curve: dict[str, Any], control: int) -> None:
        if self._broker_available():
            raise RuntimeError("OEM broker became available before the direct fan write")
        encoded = self._encode_curve(curve)

        previous = self._read_raw_table()
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
        try:
            self._write(AP_EXISTS_ADDRESS, 1)
            self._write(FAN_SUBSYSTEM_STATE_ADDRESS, 5)
            self._write(FAN_CONTROL_ADDRESS, control)
            self._read(FAN_CONTROL_MIRROR_ADDRESS)
            self._write(FAN_CONTROL_MIRROR_ADDRESS, control)
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
            if self._read(FAN_CONTROL_MIRROR_ADDRESS) != control:
                raise RuntimeError("Mirrored fan-control mode did not match the direct write")
            if self._read(AP_EXISTS_ADDRESS) != 1:
                raise RuntimeError("OEM application-presence readback did not match the direct write")
            if self._read(FAN_SUBSYSTEM_STATE_ADDRESS) != 5:
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

    def set_boost(self, enabled: bool) -> None:
        if self._broker_available():
            raise RuntimeError("OEM broker became available before the direct boost write")
        expected = FAN_CONTROL_BOOST if enabled else FAN_CONTROL_USER_HIGH
        self._write(AP_EXISTS_ADDRESS, 1)
        self._write(FAN_SUBSYSTEM_STATE_ADDRESS, 5)
        self._write(FAN_CONTROL_ADDRESS, expected)
        self._write(FAN_CONTROL_MIRROR_ADDRESS, expected)
        if (
            self._read(FAN_CONTROL_ADDRESS) != expected
            or self._read(FAN_CONTROL_MIRROR_ADDRESS) != expected
        ):
            raise RuntimeError("Fan Boost readback did not match the direct write")
