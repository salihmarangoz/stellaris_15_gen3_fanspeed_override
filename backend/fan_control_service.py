import ctypes
import os
import threading
import time
from collections.abc import Callable
from typing import Any, TypeVar

from backend.fan_control import ControlCenterClient, fixed_curve, save_backup
from backend.direct_fan_control import (
    DirectEcClient,
    oem_broker_available,
    save_oem_curve_cache,
)


T = TypeVar("T")
GCU_BRIDGE_SERVICE = "GCUBridge"
OEM_SERVICE_TIMEOUT_SECONDS = 20.0
OEM_SHUTDOWN_CLEANUP_SECONDS = 5.0


class ServiceStatus(ctypes.Structure):
    _fields_ = [
        ("service_type", ctypes.c_uint32),
        ("current_state", ctypes.c_uint32),
        ("controls_accepted", ctypes.c_uint32),
        ("win32_exit_code", ctypes.c_uint32),
        ("service_specific_exit_code", ctypes.c_uint32),
        ("checkpoint", ctypes.c_uint32),
        ("wait_hint", ctypes.c_uint32),
    ]


class ServiceStatusProcess(ctypes.Structure):
    _fields_ = ServiceStatus._fields_ + [
        ("process_id", ctypes.c_uint32),
        ("service_flags", ctypes.c_uint32),
    ]


def _set_windows_service_running(enabled: bool) -> None:
    if os.name != "nt":
        raise RuntimeError("GCUBridge service control is available only on Windows")
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    advapi32.OpenSCManagerW.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint32]
    advapi32.OpenSCManagerW.restype = ctypes.c_void_p
    advapi32.OpenServiceW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_uint32]
    advapi32.OpenServiceW.restype = ctypes.c_void_p
    advapi32.QueryServiceStatusEx.argtypes = [
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.POINTER(ctypes.c_ubyte),
        ctypes.c_uint32,
        ctypes.POINTER(ctypes.c_uint32),
    ]
    advapi32.QueryServiceStatusEx.restype = ctypes.c_int
    advapi32.StartServiceW.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p]
    advapi32.StartServiceW.restype = ctypes.c_int
    advapi32.ControlService.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.POINTER(ServiceStatus),
    ]
    advapi32.ControlService.restype = ctypes.c_int
    advapi32.CloseServiceHandle.argtypes = [ctypes.c_void_p]
    advapi32.CloseServiceHandle.restype = ctypes.c_int

    manager = advapi32.OpenSCManagerW(None, None, 0x0001)
    if not manager:
        raise RuntimeError(
            f"Cannot open the Windows service manager (error {ctypes.get_last_error()})"
        )
    service = None
    try:
        service = advapi32.OpenServiceW(manager, GCU_BRIDGE_SERVICE, 0x0034)
        if not service:
            raise RuntimeError(
                f"Cannot open {GCU_BRIDGE_SERVICE} (error {ctypes.get_last_error()})"
            )

        if enabled:
            if not advapi32.StartServiceW(service, 0, None):
                error = ctypes.get_last_error()
                if error != 1056:
                    raise RuntimeError(f"Cannot start {GCU_BRIDGE_SERVICE} (error {error})")
        else:
            status = ServiceStatus()
            if not advapi32.ControlService(service, 1, ctypes.byref(status)):
                error = ctypes.get_last_error()
                if error != 1062:
                    raise RuntimeError(f"Cannot stop {GCU_BRIDGE_SERVICE} (error {error})")

        expected_state = 4 if enabled else 1
        deadline = time.monotonic() + OEM_SERVICE_TIMEOUT_SECONDS
        while True:
            status_process = ServiceStatusProcess()
            needed = ctypes.c_uint32()
            if not advapi32.QueryServiceStatusEx(
                service,
                0,
                ctypes.cast(ctypes.byref(status_process), ctypes.POINTER(ctypes.c_ubyte)),
                ctypes.sizeof(status_process),
                ctypes.byref(needed),
            ):
                raise RuntimeError(
                    f"Cannot query {GCU_BRIDGE_SERVICE} (error {ctypes.get_last_error()})"
                )
            if status_process.current_state == expected_state:
                return
            if time.monotonic() >= deadline:
                action = "start" if enabled else "stop"
                raise TimeoutError(f"Timed out waiting for {GCU_BRIDGE_SERVICE} to {action}")
            time.sleep(0.2)
    finally:
        if service:
            advapi32.CloseServiceHandle(service)
        advapi32.CloseServiceHandle(manager)


def _wait_for_oem_broker(available: bool) -> None:
    deadline = time.monotonic() + OEM_SERVICE_TIMEOUT_SECONDS
    while oem_broker_available() != available:
        if time.monotonic() >= deadline:
            state = "start" if available else "stop"
            raise TimeoutError(f"Timed out waiting for the OEM MQTT broker to {state}")
        time.sleep(0.2)


class ControlCenterService:
    _instance: "ControlCenterService | None" = None
    _instance_lock = threading.Lock()

    def __new__(cls) -> "ControlCenterService":
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self) -> None:
        if getattr(self, "_initialized", False):
            return
        self._initialized = True
        self._operation_lock = threading.RLock()
        self._client: ControlCenterClient | DirectEcClient | None = None
        self._method: str | None = None

    @classmethod
    def instance(cls) -> "ControlCenterService":
        return cls()

    @property
    def method(self) -> str | None:
        return self._method

    def _connected_client(self) -> ControlCenterClient | DirectEcClient:
        use_oem = oem_broker_available()
        expected_method = "oem_mqtt" if use_oem else "direct_ec"
        if self._client is not None and self._method != expected_method:
            self._reset_client()
        if self._client is None:
            self._client = ControlCenterClient() if use_oem else DirectEcClient()
            self._client.connect()
            self._method = expected_method
        return self._client

    def _reset_client(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None
            self._method = None

    def _execute(
        self,
        operation: Callable[[ControlCenterClient | DirectEcClient], T],
        *,
        retry: bool = True,
    ) -> T:
        with self._operation_lock:
            try:
                return operation(self._connected_client())
            except Exception:
                self._reset_client()
                if not retry:
                    raise
                return operation(self._connected_client())

    def load_state(self) -> dict[str, Any]:
        def operation(client: ControlCenterClient | DirectEcClient) -> dict[str, Any]:
            status = client.status()
            curve = client.curve(str(status["FAN_TableName"]))
            telemetry = client.fan_info()
            if client.method_name == "oem_mqtt":
                save_oem_curve_cache(curve)
            return {"status": status, "curve": curve, "telemetry": telemetry}

        return self._execute(operation)

    def read_telemetry(self) -> dict[str, Any]:
        return self._execute(lambda client: client.fan_info())

    def apply_manual(
        self, cpu: int, gpu: int, *, create_backup: bool = True
    ) -> dict[str, Any]:
        def operation(client: ControlCenterClient | DirectEcClient) -> dict[str, Any]:
            status = client.status()
            name = str(status["FAN_TableName"])
            existing = client.curve(name)
            backup = save_backup(existing) if create_backup else None
            updated = fixed_curve(existing, cpu, gpu, include_idle=True)
            if client.method_name == "oem_mqtt":
                save_oem_curve_cache(updated)
            client.set_curve(updated)
            client.set_boost(False)
            return {
                "backup": str(backup) if backup else None,
                "cpu": cpu,
                "gpu": gpu,
                "table": name,
            }

        return self._execute(operation, retry=False)

    def set_boost(self, enabled: bool) -> bool:
        def operation(client: ControlCenterClient | DirectEcClient) -> bool:
            client.set_boost(enabled)
            return enabled

        return self._execute(operation, retry=False)

    def set_oem_service_running(self, enabled: bool) -> bool:
        with self._operation_lock:
            self._reset_client()
            _set_windows_service_running(enabled)
            _wait_for_oem_broker(enabled)
            if not enabled:
                # GCUService clears the EC tables shortly after the Windows service stops.
                time.sleep(OEM_SHUTDOWN_CLEANUP_SECONDS)
            return enabled

    def close(self, wait: bool = True) -> None:
        acquired = self._operation_lock.acquire(blocking=wait)
        if not acquired:
            return
        try:
            self._reset_client()
        finally:
            self._operation_lock.release()
