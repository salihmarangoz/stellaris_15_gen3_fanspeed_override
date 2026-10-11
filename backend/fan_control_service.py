import ctypes
import json
import os
from pathlib import Path
import threading
import time
from collections.abc import Callable
from typing import Any, TypeVar

from backend.fan_control import ControlCenterClient, fixed_curve, save_backup
from backend.direct_fan_control import (
    DirectEcClient,
    MAX_CTGP_OFFSET_WATTS,
    oem_broker_available,
    save_oem_curve_cache,
)
from backend.temperature_service import read_nvidia_power_limits
from shared.fan_control_common import LIGHTBAR_REFRESH_SECONDS, MAX_LIGHTBAR_LEVEL, validate_lightbar


T = TypeVar("T")
GCU_BRIDGE_SERVICE = "GCUBridge"
OEM_SERVICE_TIMEOUT_SECONDS = 20.0
OEM_SHUTDOWN_CLEANUP_SECONDS = 5.0


def lightbar_settings_path() -> Path:
    return Path(os.environ.get("LOCALAPPDATA", Path.home())) / "StellarisFanControl" / "lightbar.json"


def load_lightbar_choice() -> tuple[str, tuple[int, int, int]] | None:
    try:
        settings = json.loads(lightbar_settings_path().read_text(encoding="utf-8"))
        return validate_lightbar(settings["mode"], settings["color"])
    except (OSError, ValueError, TypeError, KeyError):
        return None


def gpu_power_settings_path() -> Path:
    return Path(os.environ.get("LOCALAPPDATA", Path.home())) / "StellarisFanControl" / "gpu-power.json"


def load_gpu_power_choice() -> tuple[int, bool] | None:
    try:
        value = json.loads(gpu_power_settings_path().read_text(encoding="utf-8"))
        offset, boost = value["offset"], value["dynamic_boost"]
        if type(offset) is int and 0 <= offset <= MAX_CTGP_OFFSET_WATTS and type(boost) is bool:
            return offset, boost
    except (OSError, ValueError, TypeError, KeyError):
        pass
    return None


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
        self._desired_lightbar = load_lightbar_choice()
        self._lightbar_written_at: float | None = None
        self._desired_gpu_power = load_gpu_power_choice()
        self._gpu_limits: dict[str, float] | None = None
        self._gpu_limits_at = 0.0

    @classmethod
    def instance(cls) -> "ControlCenterService":
        return cls()

    @property
    def method(self) -> str | None:
        return self._method

    def capabilities(self) -> dict[str, Any]:
        return {
            "platform": "windows",
            "gpu_power_limit": self._method == "direct_ec",
            "lightbar": self._method == "direct_ec",
            "oem_service": True,
            "exit_stops_control": True,
        }

    def _power_limits(self, *, force: bool = False) -> dict[str, float]:
        now = time.monotonic()
        if force or self._gpu_limits is None or now - self._gpu_limits_at >= 2:
            self._gpu_limits = None
            limits = read_nvidia_power_limits()
            # Exact laptop/VBIOS model validated under Linux. Refuse other bases.
            if limits["base_limit_w"] != 115 or not 115 <= limits["max_limit_w"] <= 165:
                raise RuntimeError("GPU power control requires the validated 115 W base and VBIOS limits")
            self._gpu_limits = limits
            self._gpu_limits_at = time.monotonic()
        return dict(self._gpu_limits)

    @staticmethod
    def _require_gpu_client(client: Any) -> None:
        if client.method_name != "direct_ec":
            raise RuntimeError("Stop GCUBridge to enable GPU power control")

    def gpu_power_state(self) -> dict[str, Any]:
        state: dict[str, Any] = {
            "available": False, "offset": None, "dynamic_boost": None,
            "dynamic_boost_w": None, "max_offset": 0, "method": "direct EC",
            "base_limit_w": None, "max_limit_w": None, "enforced_limit_w": None,
            "limits_age_s": None, "error": None,
        }
        with self._operation_lock:
            try:
                state.update(self._power_limits())
                state["limits_age_s"] = max(0.0, time.monotonic() - self._gpu_limits_at)
                state["max_offset"] = min(MAX_CTGP_OFFSET_WATTS, int(state["max_limit_w"] - 115))
                if oem_broker_available():
                    raise RuntimeError("Stop GCUBridge to enable GPU power control")
                def operation(client: Any) -> dict[str, Any]:
                    self._require_gpu_client(client)
                    return client.read_ctgp_state()
                state.update(self._execute(operation, retry=False))
                if state["offset"] > state["max_offset"]:
                    raise RuntimeError("The EC GPU offset exceeds the current VBIOS maximum")
                state["available"] = True
            except Exception as exc:
                state["error"] = str(exc)
        return state

    def _write_gpu_power(self, offset: int, boost: bool) -> None:
        if oem_broker_available():
            raise RuntimeError("Stop GCUBridge before changing GPU power")
        limits = self._power_limits(force=True)
        if type(offset) is not int or not 0 <= offset <= min(MAX_CTGP_OFFSET_WATTS, limits["max_limit_w"] - 115):
            raise ValueError("The GPU power offset exceeds the validated VBIOS range")
        if type(boost) is not bool:
            raise ValueError("Dynamic Boost must be a boolean")
        def operation(client: Any) -> None:
            self._require_gpu_client(client)
            client.write_ctgp(offset, boost)
        self._execute(operation, retry=False)

    def _save_gpu_power(self, offset: int, boost: bool) -> None:
        self._desired_gpu_power = (offset, boost)
        path = gpu_power_settings_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix(".tmp")
            temporary.write_text(json.dumps({"offset": offset, "dynamic_boost": boost}) + "\n", encoding="utf-8")
            os.replace(temporary, path)
        except OSError as exc:
            raise RuntimeError(f"GPU power applied, but the choice could not be saved: {exc}") from exc
        self._gpu_limits = None

    def set_gpu_power_offset(self, watts: int) -> dict[str, Any]:
        if type(watts) is not int:
            raise ValueError("The GPU power offset must be an integer")
        with self._operation_lock:
            state = self.gpu_power_state()
            if not state["available"]:
                raise RuntimeError(state["error"])
            boost = state["dynamic_boost"]
            self._write_gpu_power(watts, boost)
            self._save_gpu_power(watts, boost)
            return self.gpu_power_state()

    def set_dynamic_boost(self, enabled: bool) -> dict[str, Any]:
        if type(enabled) is not bool:
            raise ValueError("Dynamic Boost must be a boolean")
        with self._operation_lock:
            state = self.gpu_power_state()
            if not state["available"]:
                raise RuntimeError(state["error"])
            offset = state["offset"]
            self._write_gpu_power(offset, enabled)
            self._save_gpu_power(offset, enabled)
            return self.gpu_power_state()

    def maintain_gpu_power(self) -> None:
        with self._operation_lock:
            if self._desired_gpu_power is None or oem_broker_available():
                return
            state = self.gpu_power_state()
            if not state["available"]:
                raise RuntimeError(state["error"])
            desired = self._desired_gpu_power
            if not state.get("configured") or (state["offset"], state["dynamic_boost"]) != desired:
                self._write_gpu_power(*desired)

    @staticmethod
    def _require_lightbar_client(client: Any) -> None:
        if client.method_name != "direct_ec":
            raise RuntimeError("Stop GCUBridge to enable lightbar control")

    def lightbar_state(self) -> dict[str, Any]:
        state: dict[str, Any] = {
            "available": False, "mode": None, "color": None,
            "max_level": MAX_LIGHTBAR_LEVEL, "error": None,
        }
        with self._operation_lock:
            if oem_broker_available():
                state["error"] = "Stop GCUBridge to enable lightbar control"
                return state
            try:
                def operation(client: Any) -> dict[str, Any]:
                    self._require_lightbar_client(client)
                    return client.read_lightbar()
                shown = self._execute(operation, retry=False)
            except Exception as exc:
                state["error"] = str(exc)
                return state
            state["available"] = True
            desired = self._desired_lightbar
            state["mode"] = shown["mode"] if desired is None else desired[0]
            state["color"] = shown["color"] if desired is None else list(desired[1])
        return state

    def set_lightbar(self, mode: str, color: tuple[int, int, int]) -> dict[str, Any]:
        desired = validate_lightbar(mode, color)
        with self._operation_lock:
            if oem_broker_available():
                raise RuntimeError("Stop GCUBridge before changing the lightbar")
            def operation(client: Any) -> None:
                self._require_lightbar_client(client)
                client.write_lightbar(*desired, force=True)
            self._execute(operation, retry=False)
            self._desired_lightbar = desired
            self._lightbar_written_at = time.monotonic()
            path = lightbar_settings_path()
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                temporary = path.with_suffix(".tmp")
                temporary.write_text(
                    json.dumps({"mode": desired[0], "color": list(desired[1])}) + "\n",
                    encoding="utf-8",
                )
                os.replace(temporary, path)
            except OSError as exc:
                raise RuntimeError(f"Lightbar applied, but the choice could not be saved: {exc}") from exc
            return {
                "available": True, "mode": desired[0], "color": list(desired[1]),
                "max_level": MAX_LIGHTBAR_LEVEL, "error": None,
            }

    def maintain_lightbar(self) -> None:
        with self._operation_lock:
            if self._desired_lightbar is None:
                return
            if oem_broker_available():
                self._lightbar_written_at = None
                return
            now = time.monotonic()
            force = self._lightbar_written_at is None or now - self._lightbar_written_at >= LIGHTBAR_REFRESH_SECONDS
            def operation(client: Any) -> None:
                self._require_lightbar_client(client)
                client.write_lightbar(*self._desired_lightbar, force=force)
            self._execute(operation, retry=False)
            self._lightbar_written_at = now if force else self._lightbar_written_at

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
