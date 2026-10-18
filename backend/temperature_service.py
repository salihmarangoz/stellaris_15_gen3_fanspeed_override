import ctypes
import math
import struct
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from backend.nvidia_helper import NvidiaHelper


@dataclass(frozen=True)
class Temperatures:
    cpu_c: float
    gpu_c: float | None
    cpu_source: str
    gpu_source: str
    # Only set when Windows PnP reports the NVIDIA GPU in D3; gpu_c is then None.
    gpu_powered_off: bool = False


PAWNIO_DEVICE = r"\\?\GLOBALROOT\Device\PawnIO"
PAWNIO_LOAD_BINARY = (41394 << 16) | (0x821 << 2)
PAWNIO_EXECUTE = (41394 << 16) | (0x841 << 2)
RYZEN_THERMAL_REGISTER = 0x00059800
RYZEN_POWER_UNIT_MSR = 0xC0010299
RYZEN_PACKAGE_ENERGY_MSR = 0xC001029B
MIN_POWER_INTERVAL_SECONDS = 0.5
MAX_POWER_INTERVAL_SECONDS = 30.0
MAX_PLAUSIBLE_CPU_POWER_WATTS = 150.0
# Below the controller's 4-second sensor deadline, so a stuck query releases
# the temperature lane before the next caller's deadline expires.
NVIDIA_TEMPERATURE_TIMEOUT_SECONDS = 3
NVIDIA_TELEMETRY_TIMEOUT_SECONDS = 2
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value


def _pawn_module_path() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys._MEIPASS) / "pawnio" / "AMDFamily17.bin"
    return (
        Path(__file__).resolve().parents[1]
        / "third_party"
        / "pawnio"
        / "AMDFamily17.bin"
    )


def _read_ryzen_registers(operation: str, offsets: tuple[int, ...]) -> tuple[int, ...]:
    if operation == "ioctl_read_smn":
        allowed = {RYZEN_THERMAL_REGISTER}
        label = "SMN"
    elif operation == "ioctl_read_msr":
        allowed = {RYZEN_POWER_UNIT_MSR, RYZEN_PACKAGE_ENERGY_MSR}
        label = "MSR"
    else:
        raise ValueError("Only the validated PawnIO read operations are allowed")
    if not offsets or any(type(offset) is not int or offset not in allowed for offset in offsets):
        raise ValueError("The requested AMD register is not a validated sensor register")
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
    ]
    kernel32.CreateFileW.restype = ctypes.c_void_p
    kernel32.DeviceIoControl.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.POINTER(ctypes.c_uint32),
        ctypes.c_void_p,
    ]
    kernel32.DeviceIoControl.restype = ctypes.c_int
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    kernel32.WaitForSingleObject.restype = ctypes.c_uint32
    kernel32.ReleaseMutex.argtypes = [ctypes.c_void_p]
    kernel32.ReleaseMutex.restype = ctypes.c_int
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_int

    handle = kernel32.CreateFileW(PAWNIO_DEVICE, 0xC0000000, 3, None, 3, 0, None)
    if handle == INVALID_HANDLE_VALUE:
        error = ctypes.get_last_error()
        raise RuntimeError(
            f"Cannot open PawnIO (Windows error {error}). Install PawnIO and run as administrator."
        )

    mutex = None
    try:
        module = _pawn_module_path().read_bytes()
        module_buffer = ctypes.create_string_buffer(module)
        returned = ctypes.c_uint32()
        if not kernel32.DeviceIoControl(
            handle,
            PAWNIO_LOAD_BINARY,
            module_buffer,
            len(module),
            None,
            0,
            ctypes.byref(returned),
            None,
        ):
            raise RuntimeError(
                f"PawnIO rejected the AMD sensor module (Windows error {ctypes.get_last_error()})"
            )

        mutex = kernel32.CreateMutexW(None, False, "Global\\Access_PCI")
        if not mutex:
            raise RuntimeError(f"Cannot create PCI mutex (Windows error {ctypes.get_last_error()})")
        wait_result = kernel32.WaitForSingleObject(mutex, 1000)
        if wait_result not in (0, 0x80):
            raise TimeoutError("Timed out waiting for exclusive PCI access")

        values = []
        try:
            for offset in offsets:
                request = operation.encode("ascii").ljust(32, b"\0") + struct.pack("<q", offset)
                request_buffer = ctypes.create_string_buffer(request)
                output = ctypes.c_int64()
                returned.value = 0
                if not kernel32.DeviceIoControl(
                    handle,
                    PAWNIO_EXECUTE,
                    request_buffer,
                    len(request),
                    ctypes.byref(output),
                    ctypes.sizeof(output),
                    ctypes.byref(returned),
                    None,
                ):
                    raise RuntimeError(
                        f"PawnIO {label} read failed (Windows error {ctypes.get_last_error()})"
                    )
                if returned.value != ctypes.sizeof(output):
                    raise RuntimeError(f"PawnIO returned an unexpected {returned.value}-byte result")
                values.append(output.value & 0xFFFFFFFFFFFFFFFF)
        finally:
            kernel32.ReleaseMutex(mutex)
        return tuple(values)
    finally:
        if mutex:
            kernel32.CloseHandle(mutex)
        kernel32.CloseHandle(handle)


def _read_ryzen_smn(offset: int) -> int:
    return _read_ryzen_registers("ioctl_read_smn", (offset,))[0] & 0xFFFFFFFF


def _read_ryzen_package_energy() -> tuple[int, int]:
    units, energy = _read_ryzen_registers(
        "ioctl_read_msr", (RYZEN_POWER_UNIT_MSR, RYZEN_PACKAGE_ENERGY_MSR)
    )
    return units, energy & 0xFFFFFFFF


class RyzenPackagePowerMeter:
    """Average package watts from the restricted AMD RAPL energy-counter reads."""

    def __init__(
        self,
        read: Callable[[], tuple[int, int]] = _read_ryzen_package_energy,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._read = read
        self._clock = clock
        self._lock = threading.Lock()
        self._previous: tuple[int, float, int] | None = None
        self._last_power: float | None = None

    def sample(self) -> float | None:
        with self._lock:
            previous = self._previous
            now = self._clock()
            if previous is not None and 0 <= now - previous[1] < MIN_POWER_INTERVAL_SECONDS:
                return self._last_power
            try:
                units, energy = self._read()
                if type(units) is not int or type(energy) is not int:
                    raise ValueError("Malformed AMD energy counter")
                exponent = (units >> 8) & 0x1F
                if units < 0 or units & ~0xF1F0F or exponent == 0 or not 0 <= energy <= 0xFFFFFFFF:
                    raise ValueError("Invalid AMD RAPL energy units or counter")
                now = self._clock()
                if not math.isfinite(now):
                    raise ValueError("Invalid sampling time")
            except (OSError, RuntimeError, ValueError, TypeError, TimeoutError):
                self._previous = None
                self._last_power = None
                return None
            self._previous = (energy, now, exponent)
            self._last_power = None
            if previous is None or exponent != previous[2]:
                return None
            elapsed = now - previous[1]
            if not MIN_POWER_INTERVAL_SECONDS <= elapsed <= MAX_POWER_INTERVAL_SECONDS:
                return None
            consumed = (energy - previous[0]) & 0xFFFFFFFF
            watts = consumed * (2.0 ** -exponent) / elapsed
            if math.isfinite(watts) and 0 <= watts <= MAX_PLAUSIBLE_CPU_POWER_WATTS:
                self._last_power = watts
            return self._last_power


_cpu_power_meter = RyzenPackagePowerMeter()


def read_ryzen_temperature() -> tuple[float, str]:
    raw = _read_ryzen_smn(RYZEN_THERMAL_REGISTER)
    celsius = (raw >> 21) * 0.125
    if raw & (1 << 19) or (raw & (3 << 16)) == (3 << 16):
        celsius -= 49.0
    if not 0 <= celsius <= 120:
        raise RuntimeError(f"Implausible Ryzen SMN temperature: {celsius:.1f} C")
    return celsius, "PawnIO AMD SMN (Tctl/Tdie)"


# One helper per lane: a slow telemetry query must never delay a temperature read.
_temperature_helper = NvidiaHelper("NVIDIA temperature")
_telemetry_helper = NvidiaHelper("NVIDIA telemetry")


def read_nvidia_temperature() -> tuple[float | None, str]:
    """Return None as the temperature only for a GPU that PnP reports powered off."""
    result = _temperature_helper.query("temperature", NVIDIA_TEMPERATURE_TIMEOUT_SECONDS)
    if result.get("powered_off") is True:
        return None, "NVIDIA GPU powered off (D3)"
    celsius = result.get("temperature")
    if type(celsius) is not int:
        raise RuntimeError(f"Unexpected NVIDIA temperature: {celsius!r}")
    if not 0 <= celsius <= 120:
        raise RuntimeError(f"Implausible NVIDIA temperature: {celsius:.1f} C")
    name = str(result.get("name") or "NVIDIA GPU").strip()
    return float(celsius), f"NVIDIA driver ({name})"


def read_temperatures() -> Temperatures:
    cpu_c, cpu_source = read_ryzen_temperature()
    gpu_c, gpu_source = read_nvidia_temperature()
    return Temperatures(cpu_c, gpu_c, cpu_source, gpu_source, gpu_c is None)


def read_cpu_power_draw() -> dict[str, float | None]:
    """Optional display telemetry from the PawnIO energy MSRs."""
    try:
        return {"cpu_power_w": _cpu_power_meter.sample()}
    except (OSError, RuntimeError, ValueError, TypeError, TimeoutError):
        return {"cpu_power_w": None}


def read_gpu_power_draw() -> dict[str, float | bool | None]:
    """Optional display telemetry from the separate NVIDIA telemetry helper."""
    watts = None
    thermal_throttling = None
    try:
        result = _telemetry_helper.query("power", NVIDIA_TELEMETRY_TIMEOUT_SECONDS)
        value = result.get("power_w")
        if type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 300:
            watts = float(value)
        if type(result.get("sw_thermal")) is bool:
            thermal_throttling = result["sw_thermal"]
    except (OSError, RuntimeError, ValueError, TimeoutError):
        pass
    return {"gpu_power_w": watts, "gpu_sw_thermal_throttling": thermal_throttling}


def read_nvidia_power_limits() -> dict[str, float]:
    """Read driver limits without issuing a power-limit or profiling command."""
    result = _telemetry_helper.query("limits", NVIDIA_TELEMETRY_TIMEOUT_SECONDS)
    if result.get("powered_off") is True:
        raise RuntimeError("The NVIDIA GPU is powered off")
    keys = ("base_limit_w", "max_limit_w", "enforced_limit_w")
    values = [result.get(key) for key in keys]
    if any(type(value) not in (int, float) or not math.isfinite(value) or not 1 <= value <= 300
           for value in values):
        raise RuntimeError("NVIDIA reported invalid power limits")
    base, maximum, enforced = (float(value) for value in values)
    if not base <= maximum or enforced > maximum:
        raise RuntimeError("NVIDIA reported inconsistent power limits")
    return dict(zip(keys, (base, maximum, enforced)))
