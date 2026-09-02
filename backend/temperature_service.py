import csv
import ctypes
import struct
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Temperatures:
    cpu_c: float
    gpu_c: float
    cpu_source: str
    gpu_source: str


PAWNIO_DEVICE = r"\\?\GLOBALROOT\Device\PawnIO"
PAWNIO_LOAD_BINARY = (41394 << 16) | (0x821 << 2)
PAWNIO_EXECUTE = (41394 << 16) | (0x841 << 2)
RYZEN_THERMAL_REGISTER = 0x00059800
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value


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


def _pawn_module_path() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys._MEIPASS) / "pawnio" / "AMDFamily17.bin"
    return (
        Path(__file__).resolve().parents[1]
        / "third_party"
        / "pawnio"
        / "AMDFamily17.bin"
    )


def _read_ryzen_smn(offset: int) -> int:
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

        request = b"ioctl_read_smn".ljust(32, b"\0") + struct.pack("<q", offset)
        request_buffer = ctypes.create_string_buffer(request)
        output = ctypes.c_int64()
        try:
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
                    f"PawnIO SMN read failed (Windows error {ctypes.get_last_error()})"
                )
        finally:
            kernel32.ReleaseMutex(mutex)
        if returned.value != ctypes.sizeof(output):
            raise RuntimeError(f"PawnIO returned an unexpected {returned.value}-byte result")
        return output.value & 0xFFFFFFFF
    finally:
        if mutex:
            kernel32.CloseHandle(mutex)
        kernel32.CloseHandle(handle)


def read_ryzen_temperature() -> tuple[float, str]:
    raw = _read_ryzen_smn(RYZEN_THERMAL_REGISTER)
    celsius = (raw >> 21) * 0.125
    if raw & (1 << 19) or (raw & (3 << 16)) == (3 << 16):
        celsius -= 49.0
    if not 0 <= celsius <= 120:
        raise RuntimeError(f"Implausible Ryzen SMN temperature: {celsius:.1f} C")
    return celsius, "PawnIO AMD SMN (Tctl/Tdie)"


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
    cpu_c, cpu_source = read_ryzen_temperature()
    gpu_c, gpu_source = read_nvidia_temperature()
    return Temperatures(cpu_c, gpu_c, cpu_source, gpu_source)
