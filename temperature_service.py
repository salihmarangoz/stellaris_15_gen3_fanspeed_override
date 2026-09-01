import csv
import ctypes
import mmap
import subprocess
from dataclasses import dataclass


@dataclass(frozen=True)
class Temperatures:
    cpu_c: float
    gpu_c: float
    cpu_source: str
    gpu_source: str


class _CoreTempData(ctypes.Structure):
    _pack_ = 4
    _fields_ = [
        ("loads", ctypes.c_uint32 * 256),
        ("tjmax", ctypes.c_uint32 * 128),
        ("core_count", ctypes.c_uint32),
        ("cpu_count", ctypes.c_uint32),
        ("temps", ctypes.c_float * 256),
        ("vid", ctypes.c_float),
        ("cpu_speed", ctypes.c_float),
        ("fsb_speed", ctypes.c_float),
        ("multiplier", ctypes.c_float),
        ("cpu_name", ctypes.c_char * 100),
        ("fahrenheit", ctypes.c_ubyte),
        ("delta_to_tjmax", ctypes.c_ubyte),
        ("tdp_supported", ctypes.c_ubyte),
        ("power_supported", ctypes.c_ubyte),
        ("struct_version", ctypes.c_uint32),
        ("tdp", ctypes.c_uint32 * 128),
        ("power", ctypes.c_float * 128),
        ("multipliers", ctypes.c_float * 256),
    ]


def _run_hidden(command: list[str], timeout: int = 8) -> str:
    startupinfo = None
    if hasattr(subprocess, "STARTUPINFO"):
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW

    try:
        result = subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
            timeout=timeout,
            startupinfo=startupinfo,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.CalledProcessError as exc:
        details = (exc.stderr or exc.stdout or str(exc)).strip()
        raise RuntimeError(details) from exc
    return result.stdout.strip()


def read_core_temp_temperature() -> tuple[float, str]:
    size = ctypes.sizeof(_CoreTempData)
    mapping = mmap.mmap(
        -1,
        size,
        tagname="CoreTempMappingObjectEx",
        access=mmap.ACCESS_READ,
    )
    try:
        data = _CoreTempData.from_buffer_copy(mapping[:])
    finally:
        mapping.close()

    reading_count = data.core_count * data.cpu_count
    if not 0 < reading_count <= len(data.temps):
        raise RuntimeError("Core Temp is not running or its shared data is unavailable")

    temperatures = [float(value) for value in data.temps[:reading_count]]
    if data.delta_to_tjmax:
        temperatures = [
            float(data.tjmax[index // data.core_count]) - value
            for index, value in enumerate(temperatures)
        ]
    if data.fahrenheit:
        temperatures = [(value - 32.0) * 5.0 / 9.0 for value in temperatures]

    celsius = max(temperatures)
    if not 0 <= celsius <= 120:
        raise RuntimeError(f"Implausible Core Temp reading: {celsius:.1f} C")
    cpu_name = data.cpu_name.decode(errors="replace").rstrip("\0 ")
    return celsius, f"Core Temp ({cpu_name})"


def read_nvidia_temperature() -> tuple[float, str]:
    output = _run_hidden(
        [
            "nvidia-smi.exe",
            "--query-gpu=name,temperature.gpu",
            "--format=csv,noheader,nounits",
        ]
    )
    row = next(csv.reader(output.splitlines()))
    if len(row) < 2:
        raise RuntimeError(f"Unexpected nvidia-smi output: {output!r}")
    name = row[0].strip()
    celsius = float(row[1].strip())
    if not 0 <= celsius <= 120:
        raise RuntimeError(f"Implausible NVIDIA temperature: {celsius:.1f} C")
    return celsius, f"NVIDIA driver ({name})"


def read_temperatures() -> Temperatures:
    cpu_c, cpu_source = read_core_temp_temperature()
    gpu_c, gpu_source = read_nvidia_temperature()
    return Temperatures(cpu_c, gpu_c, cpu_source, gpu_source)
