import json
import os
import threading
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any, TypeVar

from backend.linux_ec import PROC_DIRECTORY, MAX_CTGP_OFFSET_WATTS, LinuxEcClient
from backend.linux_sensors import NvidiaLimits, last_nvidia_limits, last_nvidia_limits_age


T = TypeVar("T")
DEFAULT_STATE_DIRECTORY = Path("/var/lib/stellaris-fan-control")
TABLE_NAME = "LINUX_EC"
MAINLINE_CTGP_GLOB = "bus/platform/devices/INOU0000:*/ctgp_offset"
TUXEDO_CTGP_PATH = "devices/platform/tuxedo_nvidia_power_ctrl/ctgp_offset"


def state_directory() -> Path:
    override = os.environ.get("STELLARIS15GEN3_STATE_DIR")
    return Path(override) if override else DEFAULT_STATE_DIRECTORY


def save_backup(snapshot: dict[str, Any]) -> Path:
    directory = state_directory() / "fan-backups"
    directory.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    path = directory / f"{TABLE_NAME}-{timestamp}.json"
    path.write_text(json.dumps(snapshot, indent=2) + "\n", encoding="utf-8")
    return path


def nvidia_powerd_running(proc_directory: Path = PROC_DIRECTORY) -> bool:
    """NVIDIA applies cTGP and Dynamic Boost only while nvidia-powerd runs."""
    for entry in proc_directory.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            if (entry / "comm").read_text(encoding="ascii").strip() == "nvidia-powerd":
                return True
        except OSError:
            continue
    return False


def find_ctgp_sysfs(sysfs: Path = Path("/sys")) -> tuple[Path, str] | None:
    for path in sorted(sysfs.glob(MAINLINE_CTGP_GLOB)):
        return path, "uniwill-laptop sysfs"
    tuxedo = sysfs / TUXEDO_CTGP_PATH
    if tuxedo.exists():
        return tuxedo, "tuxedo-drivers sysfs"
    return None


class LinuxFanService:
    """Serialized owner of the one Linux EC client, mirroring ControlCenterService."""

    _instance: "LinuxFanService | None" = None
    _instance_lock = threading.Lock()

    def __new__(cls) -> "LinuxFanService":
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self) -> None:
        if getattr(self, "_initialized", False):
            return
        self._initialized = True
        self._operation_lock = threading.RLock()
        self._client: LinuxEcClient | None = None
        self._client_factory: Callable[[], LinuxEcClient] = LinuxEcClient
        self._limits: Callable[[], NvidiaLimits | None] = last_nvidia_limits
        self._limits_age: Callable[[], float | None] = last_nvidia_limits_age
        self._ctgp_sysfs: Callable[[], tuple[Path, str] | None] = find_ctgp_sysfs
        self._powerd_running: Callable[[], bool] = nvidia_powerd_running
        self._desired_duties: tuple[int, int] | None = None
        self._desired_boost = False
        self._desired_gpu_offset: int | None = None
        self._desired_dynamic_boost: bool | None = None

    @classmethod
    def instance(cls) -> "LinuxFanService":
        return cls()

    @property
    def method(self) -> str | None:
        return LinuxEcClient.method_name if self._client is not None else None

    @staticmethod
    def capabilities() -> dict[str, Any]:
        return {
            "platform": "linux",
            "gpu_power_limit": True,
            "oem_service": False,
            "exit_stops_control": False,
        }

    def _connected_client(self) -> LinuxEcClient:
        if self._client is None:
            client = self._client_factory()
            client.connect()
            self._client = client
        return self._client

    def _reset_client(self) -> None:
        if self._client is not None:
            try:
                self._client.close()
            finally:
                self._client = None

    def _execute(self, operation: Callable[[LinuxEcClient], T], *, retry: bool = True) -> T:
        with self._operation_lock:
            try:
                return operation(self._connected_client())
            except Exception:
                self._reset_client()
                if not retry:
                    raise
                return operation(self._connected_client())

    def load_state(self) -> dict[str, Any]:
        def operation(client: LinuxEcClient) -> dict[str, Any]:
            status = {
                "FAN_TableName": TABLE_NAME,
                "FanBoostEnable": "1" if client.boost_enabled() else "0",
            }
            return {"status": status, "curve": client.curve(), "telemetry": client.fan_info()}

        return self._execute(operation)

    def read_telemetry(self) -> dict[str, Any]:
        return self._execute(lambda client: client.fan_info())

    def apply_manual(
        self, cpu: int, gpu: int, *, create_backup: bool = True
    ) -> dict[str, Any]:
        def operation(client: LinuxEcClient) -> dict[str, Any]:
            backup = save_backup(client.snapshot()) if create_backup else None
            client.apply_duties(cpu, gpu)
            self._desired_duties = (cpu, gpu)
            self._desired_boost = False
            return {
                "backup": str(backup) if backup else None,
                "cpu": cpu,
                "gpu": gpu,
                "table": TABLE_NAME,
            }

        return self._execute(operation, retry=False)

    def set_boost(self, enabled: bool) -> bool:
        def operation(client: LinuxEcClient) -> bool:
            if enabled:
                client.set_boost(True)
            else:
                # Leaving Boost hands control back to the tables, so rewrite
                # them first; with no known target, fall back to full speed.
                cpu, gpu = self._desired_duties or (100, 100)
                client.apply_duties(cpu, gpu)
                self._desired_duties = (cpu, gpu)
            self._desired_boost = enabled
            return enabled

        return self._execute(operation, retry=False)

    def set_oem_service_running(self, enabled: bool) -> bool:
        del enabled
        raise RuntimeError("GCUBridge is a Windows OEM service and is not used on Linux")

    def maintain(self) -> list[str]:
        """Re-assert the last requested state if the EC drifted. Returns corrections."""
        with self._operation_lock:
            if (
                self._desired_duties is None
                and not self._desired_boost
                and self._desired_gpu_offset is None
                and self._desired_dynamic_boost is None
            ):
                return []

            def operation(client: LinuxEcClient) -> list[str]:
                corrections: list[str] = []
                if self._desired_boost:
                    if client.set_boost(True):
                        corrections.append("Fan Boost")
                elif self._desired_duties is not None and client.apply_duties(*self._desired_duties):
                    corrections.append("fan table")
                if (
                    self._desired_gpu_offset is not None
                    and client.read_ctgp_offset() != self._desired_gpu_offset
                ):
                    self._write_gpu_offset(client, self._desired_gpu_offset)
                    corrections.append("GPU power limit")
                if (
                    self._desired_dynamic_boost is not None
                    and client.dynamic_boost_enabled() != self._desired_dynamic_boost
                ):
                    client.write_dynamic_boost(self._desired_dynamic_boost)
                    corrections.append("Dynamic Boost")
                return corrections

            return self._execute(operation, retry=False)

    def set_desired_dynamic_boost(self, enabled: bool | None) -> None:
        with self._operation_lock:
            self._desired_dynamic_boost = enabled

    def set_dynamic_boost(self, enabled: bool) -> dict[str, Any]:
        def operation(client: LinuxEcClient) -> None:
            client.write_dynamic_boost(enabled)
            self._desired_dynamic_boost = enabled

        self._execute(operation, retry=False)
        return self.gpu_power_state()

    def set_desired_gpu_offset(self, watts: int | None) -> None:
        with self._operation_lock:
            self._desired_gpu_offset = None if watts is None else self._validate_offset(watts)

    def _validate_offset(self, watts: Any) -> int:
        if isinstance(watts, bool):
            raise ValueError("The GPU power offset must be an integer number of watts")
        value = int(watts)
        if value != watts or not 0 <= value <= MAX_CTGP_OFFSET_WATTS:
            raise ValueError(f"The GPU power offset must be from 0 to {MAX_CTGP_OFFSET_WATTS} W")
        limits = self._limits()
        if limits and limits.default_limit_w and limits.max_limit_w:
            headroom = limits.max_limit_w - limits.default_limit_w
            if value > headroom:
                raise ValueError(f"The GPU allows at most {headroom:.0f} W above its base limit")
        return value

    def _write_gpu_offset(self, client: LinuxEcClient, watts: int) -> None:
        sysfs = self._ctgp_sysfs()
        if sysfs is None:
            client.write_ctgp_offset(watts, dynamic_boost=self._desired_dynamic_boost)
            return
        client.refuse_conflicting_writer()
        sysfs[0].write_text(f"{watts}\n", encoding="ascii")
        if client.read_ctgp_offset() != watts:
            raise RuntimeError("The cTGP offset readback did not match the requested value")

    def set_gpu_power_offset(self, watts: int) -> dict[str, Any]:
        value = self._validate_offset(watts)

        def operation(client: LinuxEcClient) -> None:
            self._write_gpu_offset(client, value)
            self._desired_gpu_offset = value

        self._execute(operation, retry=False)
        return self.gpu_power_state()

    def gpu_power_state(self) -> dict[str, Any]:
        limits = self._limits()
        sysfs = self._ctgp_sysfs()
        state: dict[str, Any] = {
            "available": True,
            "offset": None,
            "requested_offset": self._desired_gpu_offset,
            "dynamic_boost": None,
            "dynamic_boost_w": None,
            "powerd_running": self._powerd_running(),
            "max_offset": MAX_CTGP_OFFSET_WATTS,
            "base_limit_w": limits.default_limit_w if limits else None,
            "max_limit_w": limits.max_limit_w if limits else None,
            "enforced_limit_w": limits.enforced_limit_w if limits else None,
            # Seconds since nvidia-smi reported these limits; lets the GUI tell a
            # reading taken before a change from one taken after it.
            "limits_age_s": self._limits_age(),
            "method": sysfs[1] if sysfs else "direct EC",
            "error": None,
        }
        if limits and limits.default_limit_w and limits.max_limit_w:
            state["max_offset"] = int(
                min(MAX_CTGP_OFFSET_WATTS, limits.max_limit_w - limits.default_limit_w)
            )
        try:
            ctgp = self._execute(lambda client: client.read_ctgp_state())
            state["offset"] = ctgp["offset"]
            state["dynamic_boost"] = ctgp["dynamic_boost"]
            state["dynamic_boost_w"] = ctgp["dynamic_boost_w"]
        except Exception as exc:
            state["available"] = False
            state["error"] = str(exc)
        return state

    def close(self, wait: bool = True) -> None:
        acquired = self._operation_lock.acquire(blocking=wait)
        if not acquired:
            return
        try:
            self._reset_client()
        finally:
            self._operation_lock.release()
