import json
import msvcrt
import secrets
import socketserver
import threading
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

from fan_control_common import (
    AUTO_INTERVAL_SECONDS,
    DEFAULT_MAX_FAN_TEMP,
    DEFAULT_MIN_FAN_TEMP,
    auto_target,
)
from fan_control_ipc import (
    FRONTEND_HEARTBEAT_TIMEOUT_SECONDS,
    MAX_MESSAGE_BYTES,
    RESTART_COOLDOWN_SECONDS,
    endpoint_path,
    launch_component,
    runtime_directory,
)
from fan_control_service import ControlCenterService
from temperature_service import Temperatures, read_temperatures


class BackendController:
    def __init__(self) -> None:
        self._service = ControlCenterService.instance()
        self._state_lock = threading.RLock()
        self._sensor_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._auto_wakeup = threading.Event()
        self._automatic = False
        self._boost_enabled = False
        self._minimum_temp = DEFAULT_MIN_FAN_TEMP
        self._maximum_temp = DEFAULT_MAX_FAN_TEMP
        self._auto_backup_needed = True
        self._next_auto_at: float | None = None
        self._last_temperatures: dict[str, Any] | None = None
        self._last_auto_target: int | None = None
        self._last_auto_hottest: float | None = None
        self._last_auto_error: str | None = None
        self._last_auto_update: float | None = None
        self._frontend_expected = False
        self._last_frontend_heartbeat: float | None = None
        self._last_frontend_restart = 0.0

    def start(self, *, start_frontend: bool) -> None:
        threading.Thread(target=self._auto_loop, name="auto-control", daemon=True).start()
        threading.Thread(target=self._frontend_watchdog, name="frontend-watchdog", daemon=True).start()
        if start_frontend:
            self.show_frontend(force=True)

    def stop(self) -> None:
        self._stop_event.set()
        self._auto_wakeup.set()
        self._service.close(wait=True)

    def dispatch(self, command: str, arguments: dict[str, Any]) -> Any:
        if command not in {
            "ping",
            "frontend_heartbeat",
            "frontend_detach",
            "show_frontend",
        }:
            self.frontend_heartbeat()
        handlers = {
            "ping": lambda: "pong",
            "load_state": self.load_state,
            "read_telemetry": self.read_telemetry,
            "apply_manual": lambda: self.apply_manual(
                int(arguments["cpu"]),
                int(arguments["gpu"]),
                confirmed_low=bool(arguments.get("confirmed_low", False)),
            ),
            "set_boost": lambda: self.set_boost(bool(arguments["enabled"])),
            "set_mode": lambda: self.set_mode(
                bool(arguments["automatic"]),
                int(arguments.get("minimum_temp", self._minimum_temp)),
                int(arguments.get("maximum_temp", self._maximum_temp)),
            ),
            "configure_auto": lambda: self.configure_auto(
                int(arguments["minimum_temp"]), int(arguments["maximum_temp"])
            ),
            "frontend_heartbeat": self.frontend_heartbeat,
            "frontend_detach": self.frontend_detach,
            "show_frontend": lambda: self.show_frontend(force=True),
        }
        if command not in handlers:
            raise ValueError(f"Unknown backend command: {command}")
        return handlers[command]()

    def _snapshot(self) -> dict[str, Any]:
        with self._state_lock:
            return {
                "automatic": self._automatic,
                "boost_enabled": self._boost_enabled,
                "minimum_temp": self._minimum_temp,
                "maximum_temp": self._maximum_temp,
                "temperatures": self._last_temperatures,
                "auto_target": self._last_auto_target,
                "auto_hottest": self._last_auto_hottest,
                "auto_error": self._last_auto_error,
                "auto_updated": self._last_auto_update,
            }

    def _read_temperature_snapshot(self) -> tuple[dict[str, Any] | None, str | None]:
        try:
            temperatures = self._read_validated_temperatures()
            result = asdict(temperatures)
            with self._state_lock:
                self._last_temperatures = result
            return result, None
        except Exception as exc:
            return None, str(exc)

    def _read_validated_temperatures(self) -> Temperatures:
        with self._sensor_lock:
            temperatures = read_temperatures()
        if temperatures.cpu_c <= 0 or temperatures.gpu_c <= 0:
            raise RuntimeError("A temperature source returned zero")
        return temperatures

    def apply_manual(
        self, cpu: int, gpu: int, *, confirmed_low: bool
    ) -> dict[str, Any]:
        if not 0 <= cpu <= 100 or not 0 <= gpu <= 100:
            raise ValueError("Manual fan duty must be between 0 and 100")
        if (cpu < 30 or gpu < 30) and not confirmed_low:
            raise PermissionError("Manual fan duty below 30% was not confirmed")
        return self._service.apply_manual(cpu, gpu)

    def load_state(self) -> dict[str, Any]:
        result = self._service.load_state()
        with self._state_lock:
            self._boost_enabled = str(result["status"]["FanBoostEnable"]) == "1"
        temperatures, temperature_error = self._read_temperature_snapshot()
        result.update(
            {
                "temperatures": temperatures,
                "temperature_error": temperature_error,
                "backend": self._snapshot(),
            }
        )
        return result

    def read_telemetry(self) -> dict[str, Any]:
        telemetry = self._service.read_telemetry()
        temperatures, temperature_error = self._read_temperature_snapshot()
        return {
            "telemetry": telemetry,
            "temperatures": temperatures,
            "temperature_error": temperature_error,
            "backend": self._snapshot(),
        }

    def configure_auto(self, minimum_temp: int, maximum_temp: int) -> dict[str, Any]:
        minimum_temp = max(0, min(100, minimum_temp))
        maximum_temp = max(minimum_temp, min(100, maximum_temp))
        with self._state_lock:
            self._minimum_temp = minimum_temp
            self._maximum_temp = maximum_temp
        return self._snapshot()

    def set_mode(
        self, automatic: bool, minimum_temp: int, maximum_temp: int
    ) -> dict[str, Any]:
        self.configure_auto(minimum_temp, maximum_temp)
        with self._state_lock:
            entering_auto = automatic and not self._automatic
            self._automatic = automatic
            if entering_auto:
                self._auto_backup_needed = True
                self._next_auto_at = time.monotonic()
            elif not automatic:
                self._next_auto_at = None
        self._auto_wakeup.set()
        return self._snapshot()

    def _auto_loop(self) -> None:
        while not self._stop_event.is_set():
            with self._state_lock:
                automatic = self._automatic and not self._boost_enabled
                next_auto_at = self._next_auto_at
            if not automatic or next_auto_at is None:
                timeout = None
            else:
                timeout = max(0.0, next_auto_at - time.monotonic())
            signaled = self._auto_wakeup.wait(timeout)
            self._auto_wakeup.clear()
            if self._stop_event.is_set() or signaled:
                continue
            self._run_auto_cycle()

    def _run_auto_cycle(self) -> None:
        with self._state_lock:
            if not self._automatic:
                return
            minimum_temp = self._minimum_temp
            maximum_temp = self._maximum_temp
            make_backup = self._auto_backup_needed
        try:
            temperatures = self._read_validated_temperatures()
            hottest = max(temperatures.cpu_c, temperatures.gpu_c)
            target = auto_target(hottest, minimum_temp, maximum_temp)
            with self._state_lock:
                if (
                    not self._automatic
                    or self._boost_enabled
                    or minimum_temp != self._minimum_temp
                    or maximum_temp != self._maximum_temp
                ):
                    return
                self._service.apply_manual(
                    target, target, create_backup=make_backup
                )
                self._auto_backup_needed = False
                self._last_temperatures = asdict(temperatures)
                self._last_auto_target = target
                self._last_auto_hottest = hottest
                self._last_auto_error = None
                self._last_auto_update = time.time()
        except Exception as exc:
            with self._state_lock:
                self._last_auto_error = str(exc)
                self._last_auto_update = time.time()
        finally:
            with self._state_lock:
                if self._automatic:
                    self._next_auto_at = time.monotonic() + AUTO_INTERVAL_SECONDS

    def set_boost(self, enabled: bool) -> bool:
        with self._state_lock:
            state = self._service.set_boost(enabled)
            self._boost_enabled = state
            if not state and self._automatic:
                self._next_auto_at = time.monotonic()
        self._auto_wakeup.set()
        return state

    def frontend_heartbeat(self) -> dict[str, Any]:
        with self._state_lock:
            self._frontend_expected = True
            self._last_frontend_heartbeat = time.monotonic()
        return self._snapshot()

    def frontend_detach(self) -> bool:
        with self._state_lock:
            self._frontend_expected = False
            self._last_frontend_heartbeat = None
        return True

    def show_frontend(self, *, force: bool = False) -> bool:
        now = time.monotonic()
        with self._state_lock:
            self._frontend_expected = True
            if not force and now - self._last_frontend_restart < RESTART_COOLDOWN_SECONDS:
                return False
            self._last_frontend_restart = now
            self._last_frontend_heartbeat = now
        launch_component("frontend")
        return True

    def _frontend_watchdog(self) -> None:
        while not self._stop_event.wait(2.0):
            now = time.monotonic()
            with self._state_lock:
                expected = self._frontend_expected
                heartbeat = self._last_frontend_heartbeat
                last_restart = self._last_frontend_restart
            missing = heartbeat is None or now - heartbeat > FRONTEND_HEARTBEAT_TIMEOUT_SECONDS
            cooled_down = now - last_restart >= RESTART_COOLDOWN_SECONDS
            if expected and missing and cooled_down:
                self.show_frontend()


class BackendRequestHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        line = self.rfile.readline(MAX_MESSAGE_BYTES + 1)
        if len(line) > MAX_MESSAGE_BYTES:
            self._reply(False, error="Request is too large")
            return
        try:
            request = json.loads(line)
            if not secrets.compare_digest(str(request.get("token", "")), self.server.token):
                raise PermissionError("Invalid backend token")
            result = self.server.controller.dispatch(
                str(request["command"]), dict(request.get("arguments", {}))
            )
            self._reply(True, result=result)
        except Exception as exc:
            self._reply(False, error=str(exc))

    def _reply(self, ok: bool, **payload: Any) -> None:
        response = json.dumps({"ok": ok, **payload}, separators=(",", ":"))
        self.wfile.write(response.encode("utf-8") + b"\n")


class BackendServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = False
    daemon_threads = True

    def __init__(self, controller: BackendController, token: str) -> None:
        super().__init__(("127.0.0.1", 0), BackendRequestHandler)
        self.controller = controller
        self.token = token


def _acquire_process_lock() -> tuple[Any, Path]:
    directory = runtime_directory()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "backend.lock"
    handle = path.open("a+b")
    if path.stat().st_size == 0:
        handle.write(b"0")
        handle.flush()
    handle.seek(0)
    try:
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        handle.close()
        raise RuntimeError("The fan-control backend is already running")
    return handle, path


def run_backend(*, start_frontend: bool = True) -> None:
    lock_handle, _ = _acquire_process_lock()
    controller = BackendController()
    token = secrets.token_hex(32)
    server = BackendServer(controller, token)
    endpoint = endpoint_path()
    endpoint.parent.mkdir(parents=True, exist_ok=True)
    temporary_endpoint = endpoint.with_suffix(".tmp")
    temporary_endpoint.write_text(
        json.dumps(
            {
                "host": server.server_address[0],
                "port": server.server_address[1],
                "token": token,
            }
        ),
        encoding="utf-8",
    )
    temporary_endpoint.replace(endpoint)
    controller.start(start_frontend=start_frontend)
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        controller.stop()
        server.server_close()
        try:
            current = json.loads(endpoint.read_text(encoding="utf-8"))
            if secrets.compare_digest(str(current.get("token", "")), token):
                endpoint.unlink(missing_ok=True)
        except (OSError, ValueError):
            pass
        lock_handle.seek(0)
        msvcrt.locking(lock_handle.fileno(), msvcrt.LK_UNLCK, 1)
        lock_handle.close()
