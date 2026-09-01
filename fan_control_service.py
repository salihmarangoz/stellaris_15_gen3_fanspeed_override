import threading
from collections.abc import Callable
from typing import Any, TypeVar

from fan_control import ControlCenterClient, fixed_curve, save_backup


T = TypeVar("T")


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
        self._client: ControlCenterClient | None = None

    @classmethod
    def instance(cls) -> "ControlCenterService":
        return cls()

    def _connected_client(self) -> ControlCenterClient:
        if self._client is None:
            self._client = ControlCenterClient()
            self._client.connect()
        return self._client

    def _reset_client(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    def _execute(self, operation: Callable[[ControlCenterClient], T]) -> T:
        with self._operation_lock:
            try:
                return operation(self._connected_client())
            except Exception:
                self._reset_client()
                return operation(self._connected_client())

    def load_state(self) -> dict[str, Any]:
        def operation(client: ControlCenterClient) -> dict[str, Any]:
            status = client.status()
            curve = client.curve(str(status["FAN_TableName"]))
            telemetry = client.fan_info()
            return {"status": status, "curve": curve, "telemetry": telemetry}

        return self._execute(operation)

    def read_telemetry(self) -> dict[str, Any]:
        return self._execute(lambda client: client.fan_info())

    def apply_manual(
        self, cpu: int, gpu: int, *, create_backup: bool = True
    ) -> dict[str, Any]:
        def operation(client: ControlCenterClient) -> dict[str, Any]:
            status = client.status()
            name = str(status["FAN_TableName"])
            existing = client.curve(name)
            backup = save_backup(existing) if create_backup else None
            updated = fixed_curve(existing, cpu, gpu, include_idle=True)
            client.set_curve(updated)
            client.set_boost(False)
            return {
                "backup": str(backup) if backup else None,
                "cpu": cpu,
                "gpu": gpu,
                "table": name,
            }

        return self._execute(operation)

    def set_boost(self, enabled: bool) -> bool:
        def operation(client: ControlCenterClient) -> bool:
            client.set_boost(enabled)
            return enabled

        return self._execute(operation)

    def close(self, wait: bool = True) -> None:
        acquired = self._operation_lock.acquire(blocking=wait)
        if not acquired:
            return
        try:
            self._reset_client()
        finally:
            self._operation_lock.release()
