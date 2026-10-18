"""NVIDIA queries in a separate process that can be killed when the driver hangs.

The helper keeps NVML initialized, so a query costs a few driver calls instead
of an nvidia-smi launch. It must run standalone with only the standard library:
source mode starts it as ``python -I nvidia_helper.py`` and the packaged
executable starts itself with ``--nvidia-helper``.
"""

import ctypes
import json
import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable


HELPER_ARGUMENT = "--nvidia-helper"
# A helper that has not reported ready by then is treated as hung.
STARTUP_LIMIT_SECONDS = 15.0

NVML_SUCCESS = 0
NVML_ERROR_NOT_SUPPORTED = 3
NVML_TEMPERATURE_GPU = 0
NVML_CLOCKS_EVENT_SW_THERMAL_SLOWDOWN = 0x20

DIGCF_PRESENT = 0x02
ERROR_NO_MORE_ITEMS = 259
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
POWER_DEVICE_D3 = 4
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS = 9


class _Guid(ctypes.Structure):
    _fields_ = [
        ("data1", ctypes.c_uint32),
        ("data2", ctypes.c_uint16),
        ("data3", ctypes.c_uint16),
        ("data4", ctypes.c_ubyte * 8),
    ]

    @classmethod
    def parse(cls, text: str) -> "_Guid":
        parts = text.split("-")
        tail = bytes.fromhex(parts[3] + parts[4])
        return cls(int(parts[0], 16), int(parts[1], 16), int(parts[2], 16),
                   (ctypes.c_ubyte * 8)(*tail))


class _DevPropKey(ctypes.Structure):
    _fields_ = [("fmtid", _Guid), ("pid", ctypes.c_uint32)]


class _DevInfoData(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.c_uint32),
        ("ClassGuid", _Guid),
        ("DevInst", ctypes.c_uint32),
        ("Reserved", ctypes.c_void_p),
    ]


class _PowerData(ctypes.Structure):
    _fields_ = [
        ("PD_Size", ctypes.c_uint32),
        ("PD_MostRecentPowerState", ctypes.c_int),
        ("PD_Capabilities", ctypes.c_uint32),
        ("PD_D1Latency", ctypes.c_uint32),
        ("PD_D2Latency", ctypes.c_uint32),
        ("PD_D3Latency", ctypes.c_uint32),
        ("PD_PowerStateMapping", ctypes.c_int * 7),
        ("PD_DeepestSystemWake", ctypes.c_int),
    ]


GUID_DEVCLASS_DISPLAY = "4d36e968-e325-11ce-bfc1-08002be10318"
DEVPKEY_DEVICE_POWER_DATA = ("a45c254e-df1c-4efd-8020-67d146a850e0", 32)


def nvidia_gpu_powered_off() -> bool | None:
    """Return the PnP power state of the single NVIDIA display device.

    The PnP manager answers from its own records, so this never wakes the GPU.
    True means D3 (powered off), False any working state, None unknown.
    """
    if os.name != "nt":
        return None
    setupapi = ctypes.WinDLL("setupapi", use_last_error=True)
    setupapi.SetupDiGetClassDevsW.argtypes = [
        ctypes.POINTER(_Guid), ctypes.c_wchar_p, ctypes.c_void_p, ctypes.c_uint32,
    ]
    setupapi.SetupDiGetClassDevsW.restype = ctypes.c_void_p
    setupapi.SetupDiEnumDeviceInfo.argtypes = [
        ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(_DevInfoData),
    ]
    setupapi.SetupDiEnumDeviceInfo.restype = ctypes.c_int
    setupapi.SetupDiGetDeviceInstanceIdW.argtypes = [
        ctypes.c_void_p, ctypes.POINTER(_DevInfoData), ctypes.c_wchar_p,
        ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32),
    ]
    setupapi.SetupDiGetDeviceInstanceIdW.restype = ctypes.c_int
    setupapi.SetupDiGetDevicePropertyW.argtypes = [
        ctypes.c_void_p, ctypes.POINTER(_DevInfoData), ctypes.POINTER(_DevPropKey),
        ctypes.POINTER(ctypes.c_uint32), ctypes.c_void_p, ctypes.c_uint32,
        ctypes.POINTER(ctypes.c_uint32), ctypes.c_uint32,
    ]
    setupapi.SetupDiGetDevicePropertyW.restype = ctypes.c_int
    setupapi.SetupDiDestroyDeviceInfoList.argtypes = [ctypes.c_void_p]
    setupapi.SetupDiDestroyDeviceInfoList.restype = ctypes.c_int

    display = _Guid.parse(GUID_DEVCLASS_DISPLAY)
    key = _DevPropKey(_Guid.parse(DEVPKEY_DEVICE_POWER_DATA[0]), DEVPKEY_DEVICE_POWER_DATA[1])
    devices = setupapi.SetupDiGetClassDevsW(ctypes.byref(display), None, None, DIGCF_PRESENT)
    if not devices or devices == INVALID_HANDLE_VALUE:
        return None
    states = []
    try:
        index = 0
        while True:
            data = _DevInfoData()
            data.cbSize = ctypes.sizeof(_DevInfoData)
            if not setupapi.SetupDiEnumDeviceInfo(devices, index, ctypes.byref(data)):
                if ctypes.get_last_error() != ERROR_NO_MORE_ITEMS:
                    return None
                break
            index += 1
            instance = ctypes.create_unicode_buffer(512)
            if not setupapi.SetupDiGetDeviceInstanceIdW(
                devices, ctypes.byref(data), instance, len(instance), None
            ):
                return None
            if not instance.value.upper().startswith("PCI\\VEN_10DE&"):
                continue
            power = _PowerData()
            property_type = ctypes.c_uint32()
            if not setupapi.SetupDiGetDevicePropertyW(
                devices, ctypes.byref(data), ctypes.byref(key), ctypes.byref(property_type),
                ctypes.byref(power), ctypes.sizeof(power), None, 0,
            ):
                return None
            if power.PD_Size != ctypes.sizeof(power):
                return None
            states.append(power.PD_MostRecentPowerState)
    finally:
        setupapi.SetupDiDestroyDeviceInfoList(devices)
    # Only one clear answer for exactly one NVIDIA GPU; anything else is unknown.
    if len(states) != 1 or not 1 <= states[0] <= POWER_DEVICE_D3:
        return None
    return states[0] == POWER_DEVICE_D3


class Nvml:
    """Minimal read-only NVML binding for the single laptop GPU."""

    def __init__(self, loader: Callable[[str], Any] | None = None) -> None:
        self._loader = loader
        self._library: Any | None = None
        self._device = ctypes.c_void_p()

    def _call(self, name: str, *arguments: Any) -> int:
        return getattr(self._library, name)(*arguments)

    def _check(self, name: str, *arguments: Any) -> None:
        result = self._call(name, *arguments)
        if result != NVML_SUCCESS:
            raise RuntimeError(f"NVML {name} failed with error {result}")

    def _ensure(self) -> None:
        if self._library is not None:
            return
        if self._loader is not None:
            library = self._loader("nvml.dll")
        else:
            system32 = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32"
            library = ctypes.CDLL(str(system32 / "nvml.dll"))
        self._library = library
        try:
            self._check("nvmlInit_v2")
        except Exception:
            self._library = None
            raise
        try:
            count = ctypes.c_uint()
            self._check("nvmlDeviceGetCount_v2", ctypes.byref(count))
            if count.value != 1:
                raise RuntimeError(f"Expected one NVIDIA GPU, NVML reported {count.value}")
            self._check("nvmlDeviceGetHandleByIndex_v2", 0, ctypes.byref(self._device))
        except Exception:
            self.reset()
            raise

    def reset(self) -> None:
        """Drop the session so the next query initializes again, e.g. after GPU loss."""
        library, self._library = self._library, None
        if library is not None:
            try:
                library.nvmlShutdown()
            except Exception:
                pass

    def temperature(self) -> dict[str, Any]:
        self._ensure()
        name = ctypes.create_string_buffer(96)
        self._check("nvmlDeviceGetName", self._device, name, len(name))
        value = ctypes.c_uint()
        self._check("nvmlDeviceGetTemperature", self._device, NVML_TEMPERATURE_GPU, ctypes.byref(value))
        return {"name": name.value.decode("ascii", "replace"), "temperature": value.value}

    def power(self) -> dict[str, Any]:
        self._ensure()
        milliwatts = ctypes.c_uint()
        result = self._call("nvmlDeviceGetPowerUsage", self._device, ctypes.byref(milliwatts))
        if result not in (NVML_SUCCESS, NVML_ERROR_NOT_SUPPORTED):
            raise RuntimeError(f"NVML nvmlDeviceGetPowerUsage failed with error {result}")
        reasons = ctypes.c_ulonglong()
        try:
            reason_result = self._call(
                "nvmlDeviceGetCurrentClocksEventReasons", self._device, ctypes.byref(reasons)
            )
        except AttributeError:
            reason_result = self._call(
                "nvmlDeviceGetCurrentClocksThrottleReasons", self._device, ctypes.byref(reasons)
            )
        return {
            "power_w": milliwatts.value / 1000 if result == NVML_SUCCESS else None,
            "sw_thermal": (bool(reasons.value & NVML_CLOCKS_EVENT_SW_THERMAL_SLOWDOWN)
                           if reason_result == NVML_SUCCESS else None),
        }

    def limits(self) -> dict[str, Any]:
        self._ensure()
        default = ctypes.c_uint()
        minimum = ctypes.c_uint()
        maximum = ctypes.c_uint()
        enforced = ctypes.c_uint()
        self._check("nvmlDeviceGetPowerManagementDefaultLimit", self._device, ctypes.byref(default))
        self._check("nvmlDeviceGetPowerManagementLimitConstraints", self._device,
                    ctypes.byref(minimum), ctypes.byref(maximum))
        self._check("nvmlDeviceGetEnforcedPowerLimit", self._device, ctypes.byref(enforced))
        return {"base_limit_w": default.value / 1000, "max_limit_w": maximum.value / 1000,
                "enforced_limit_w": enforced.value / 1000}


def handle_request(
    nvml: Nvml, request: dict[str, Any], powered_off: Callable[[], bool | None] = nvidia_gpu_powered_off,
) -> dict[str, Any]:
    operation = request.get("op")
    queries = {"temperature": nvml.temperature, "power": nvml.power, "limits": nvml.limits}
    if operation not in queries:
        raise ValueError("Unknown NVIDIA helper operation")
    try:
        off = powered_off()
    except Exception:
        off = None
    if off is True:
        # Any NVML call would wake the GPU; a powered-off GPU produces no heat.
        return {"powered_off": True}
    try:
        return queries[operation]()
    except Exception:
        nvml.reset()
        raise


def serve(input_stream: Any = None, output_stream: Any = None, nvml: Nvml | None = None) -> int:
    """Answer one JSON request per line until the parent closes the pipe."""
    if input_stream is None:
        input_stream = sys.stdin.buffer if sys.stdin is not None else os.fdopen(0, "rb")
    if output_stream is None:
        output_stream = sys.stdout.buffer if sys.stdout is not None else os.fdopen(1, "wb")
    nvml = nvml or Nvml()

    def send(message: dict[str, Any]) -> None:
        output_stream.write(json.dumps(message).encode("ascii") + b"\n")
        output_stream.flush()

    send({"ready": True})
    for line in input_stream:
        request_id = None
        try:
            request = json.loads(line)
            request_id = request.get("id")
            send({"id": request_id, "ok": True, "result": handle_request(nvml, request)})
        except Exception as exc:
            send({"id": request_id, "ok": False, "error": str(exc) or type(exc).__name__})
    nvml.reset()
    return 0


def helper_command() -> list[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable, HELPER_ARGUMENT]
    return [sys.executable, "-I", str(Path(__file__).resolve())]


class _KillJob:
    """Windows job object that kills the helper, and anything it starts, at once."""

    def __init__(self, process: subprocess.Popen) -> None:
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
        self._kernel32.CreateJobObjectW.restype = ctypes.c_void_p
        self._kernel32.SetInformationJobObject.argtypes = [
            ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32,
        ]
        self._kernel32.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        self._kernel32.TerminateJobObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        self._kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        self._job = self._kernel32.CreateJobObjectW(None, None)
        if not self._job:
            raise OSError(f"Cannot create a job object (Windows error {ctypes.get_last_error()})")
        # JOBOBJECT_EXTENDED_LIMIT_INFORMATION is 144 bytes on 64-bit Windows;
        # LimitFlags is at offset 16.
        information = (ctypes.c_ubyte * 144)()
        ctypes.c_uint32.from_buffer(information, 16).value = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self._kernel32.SetInformationJobObject(
            self._job, JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS, information, len(information)
        ) or not self._kernel32.AssignProcessToJobObject(self._job, int(process._handle)):
            error = ctypes.get_last_error()
            self.close()
            raise OSError(f"Cannot assign the NVIDIA helper to a job (Windows error {error})")

    def close(self) -> None:
        job, self._job = self._job, None
        if job:
            self._kernel32.TerminateJobObject(job, 1)
            self._kernel32.CloseHandle(job)


class NvidiaHelper:
    """Serialized, deadline-bounded client for one helper process.

    A query that misses its deadline kills the helper, so a hung driver call
    never holds the caller or this lane; the next query starts a new helper.
    """

    def __init__(
        self,
        name: str,
        command: Callable[[], list[str]] = helper_command,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._name = name
        self._command = command
        self._clock = clock
        self._lock = threading.Lock()
        self._process: subprocess.Popen | None = None
        self._job: _KillJob | None = None
        self._messages: queue.Queue | None = None
        self._ready = False
        self._started_at = 0.0
        self._next_id = 0

    def _start(self) -> None:
        startupinfo = None
        if hasattr(subprocess, "STARTUPINFO"):
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        process = subprocess.Popen(
            self._command(),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            startupinfo=startupinfo,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        try:
            self._job = _KillJob(process) if os.name == "nt" else None
        except OSError:
            process.kill()
            raise
        messages: queue.Queue = queue.Queue()

        def read() -> None:
            try:
                for line in process.stdout:
                    try:
                        messages.put(json.loads(line))
                    except ValueError:
                        pass
            except (OSError, ValueError):
                pass
            messages.put(None)

        threading.Thread(target=read, name=f"{self._name} reader", daemon=True).start()
        self._process = process
        self._messages = messages
        self._ready = False
        self._started_at = self._clock()

    def _kill(self) -> None:
        process, self._process = self._process, None
        job, self._job = self._job, None
        self._messages = None
        self._ready = False
        if job is not None:
            job.close()
        if process is not None:
            try:
                process.kill()
                process.wait(timeout=1)
            except (OSError, subprocess.TimeoutExpired):
                pass
            for stream in (process.stdin, process.stdout):
                try:
                    stream.close()
                except (OSError, AttributeError):
                    pass

    def _next(self, deadline: float) -> Any:
        remaining = deadline - self._clock()
        if remaining <= 0:
            raise queue.Empty
        message = self._messages.get(timeout=remaining)
        if message is None:
            self._kill()
            raise RuntimeError(f"{self._name} helper exited")
        return message

    def query(self, operation: str, timeout: float) -> dict[str, Any]:
        deadline = self._clock() + timeout
        if not self._lock.acquire(timeout=max(0.0, timeout)):
            raise TimeoutError(f"{self._name} is busy")
        try:
            if self._process is None:
                self._start()
            while not self._ready:
                try:
                    message = self._next(deadline)
                except queue.Empty:
                    # Startup is not a hang until it exceeds its own limit.
                    if self._clock() - self._started_at >= STARTUP_LIMIT_SECONDS:
                        self._kill()
                    raise TimeoutError(f"{self._name} helper is still starting") from None
                self._ready = message.get("ready") is True
            self._next_id += 1
            request_id = self._next_id
            try:
                self._process.stdin.write(
                    json.dumps({"id": request_id, "op": operation}).encode("ascii") + b"\n"
                )
                self._process.stdin.flush()
            except OSError as exc:
                self._kill()
                raise RuntimeError(f"{self._name} helper is not available") from exc
            while True:
                try:
                    message = self._next(deadline)
                except queue.Empty:
                    self._kill()
                    raise TimeoutError(f"{self._name} query timed out") from None
                if message.get("id") != request_id:
                    continue
                if not message.get("ok"):
                    raise RuntimeError(str(message.get("error") or "NVIDIA query failed"))
                result = message.get("result")
                if not isinstance(result, dict):
                    raise RuntimeError("Malformed NVIDIA helper response")
                return result
        finally:
            self._lock.release()

    def close(self) -> None:
        with self._lock:
            self._kill()


if __name__ == "__main__":
    raise SystemExit(serve())
