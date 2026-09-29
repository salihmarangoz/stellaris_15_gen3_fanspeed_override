import fcntl
import json
import logging
import os
import signal
import socket
import socketserver
import struct
import threading
import time
from pathlib import Path
from typing import Any

from shared.fan_control_common import (
    AUTO_INTERVAL_SECONDS,
    DEFAULT_MAX_FAN_TEMP,
    DEFAULT_MIN_FAN_TEMP,
)
from shared.fan_control_ipc import linux_socket_path
from backend.fan_control_backend import BackendController, BackendRequestHandler
from backend.linux_ec import validate_lightbar
from backend.linux_fan_service import state_directory


LOGGER = logging.getLogger("stellaris15gen3")
DEFAULT_CONFIG_PATH = Path("/etc/stellaris-fan-control/config.json")
SETTINGS_FILENAME = "settings.json"
LOCK_FILENAME = "backend.lock"
MAINTENANCE_INTERVAL_SECONDS = AUTO_INTERVAL_SECONDS
REQUEST_SOCKET_TIMEOUT_SECONDS = 30.0


def config_path() -> Path:
    override = os.environ.get("STELLARIS15GEN3_CONFIG")
    return Path(override) if override else DEFAULT_CONFIG_PATH


def load_allowed_uids(path: Path) -> frozenset[int]:
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
        uids = config["allowed_uids"]
        if not isinstance(uids, list) or not all(
            isinstance(uid, int) and not isinstance(uid, bool) and uid >= 0 for uid in uids
        ):
            raise ValueError("allowed_uids must be a list of user IDs")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        LOGGER.warning("No valid %s (%s); only root may control the service", path, exc)
        return frozenset({0})
    return frozenset({0, *uids})


def load_settings(path: Path) -> dict[str, Any]:
    settings: dict[str, Any] = {
        "minimum_temp": DEFAULT_MIN_FAN_TEMP,
        "maximum_temp": DEFAULT_MAX_FAN_TEMP,
        "gpu_power_offset": None,
        "dynamic_boost": None,
        "lightbar_mode": None,
        "lightbar_color": None,
    }
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return settings
    if not isinstance(loaded, dict):
        return settings
    minimum = loaded.get("minimum_temp")
    maximum = loaded.get("maximum_temp")
    if (
        isinstance(minimum, int)
        and isinstance(maximum, int)
        and not isinstance(minimum, bool)
        and not isinstance(maximum, bool)
        and 0 <= minimum <= maximum <= 100
    ):
        settings["minimum_temp"] = minimum
        settings["maximum_temp"] = maximum
    offset = loaded.get("gpu_power_offset")
    if isinstance(offset, int) and not isinstance(offset, bool) and offset >= 0:
        settings["gpu_power_offset"] = offset
    if isinstance(loaded.get("dynamic_boost"), bool):
        settings["dynamic_boost"] = loaded["dynamic_boost"]
    try:
        mode, color = validate_lightbar(loaded.get("lightbar_mode"), loaded.get("lightbar_color"))
    except ValueError:
        pass
    else:
        settings["lightbar_mode"] = mode
        settings["lightbar_color"] = list(color)
    return settings


class LinuxBackendController(BackendController):
    """Root-service controller: persistent settings, drift repair, no GUI launching."""

    def __init__(self, settings_path: Path) -> None:
        super().__init__()
        self._settings_path = settings_path
        self._maintenance_wakeup = threading.Event()
        self._resync_requested = threading.Event()
        self._logged_auto_error: str | None = None
        self._logged_maintenance_error: str | None = None
        self._gpu_power_offset: int | None = None
        self._dynamic_boost: bool | None = None
        self._lightbar: tuple[str, list[int]] | None = None
        settings = load_settings(settings_path)
        BackendController.configure_auto(
            self, settings["minimum_temp"], settings["maximum_temp"]
        )
        if settings["gpu_power_offset"] is not None:
            try:
                self._service.set_desired_gpu_offset(settings["gpu_power_offset"])
                self._gpu_power_offset = settings["gpu_power_offset"]
            except ValueError as exc:
                LOGGER.warning("Ignoring saved GPU power offset: %s", exc)
        if settings["dynamic_boost"] is not None:
            self._service.set_desired_dynamic_boost(settings["dynamic_boost"])
            self._dynamic_boost = settings["dynamic_boost"]
        if settings["lightbar_mode"] is not None:
            # The EC shows its rainbow after power-on; maintenance restores the choice.
            self._service.set_desired_lightbar(settings["lightbar_mode"], settings["lightbar_color"])
            self._lightbar = (settings["lightbar_mode"], settings["lightbar_color"])

    def start(self, *, start_frontend: bool = False, monitor_frontend: bool = False) -> None:
        del start_frontend, monitor_frontend
        super().start(start_frontend=False, monitor_frontend=False)
        threading.Thread(target=self._maintenance_loop, name="ec-maintenance", daemon=True).start()
        with self._state_lock:
            minimum_temp, maximum_temp = self._minimum_temp, self._maximum_temp
        # Every service start begins in Automatic mode, like every Windows launch.
        self.set_mode(True, minimum_temp, maximum_temp)

    def stop(self) -> None:
        self._stop_event.set()
        self._maintenance_wakeup.set()
        super().stop()

    def show_frontend(self, *, force: bool = False) -> bool:
        del force
        # A root service must never start the GUI; the user session autostarts it.
        return False

    def configure_auto(self, minimum_temp: int, maximum_temp: int) -> dict[str, Any]:
        result = super().configure_auto(minimum_temp, maximum_temp)
        self._save_settings()
        return result

    def set_gpu_power(self, offset: int) -> dict[str, Any]:
        result = super().set_gpu_power(offset)
        with self._state_lock:
            self._gpu_power_offset = int(offset)
        self._save_settings()
        return result

    def set_dynamic_boost(self, enabled: bool) -> dict[str, Any]:
        result = super().set_dynamic_boost(enabled)
        with self._state_lock:
            self._dynamic_boost = bool(enabled)
        self._save_settings()
        return result

    def set_lightbar(self, mode: str, color: tuple[Any, Any, Any]) -> dict[str, Any]:
        result = super().set_lightbar(mode, color)
        with self._state_lock:
            self._lightbar = (mode, list(color))
        self._save_settings()
        return result

    def request_resync(self) -> None:
        # Safe from a signal handler: only sets events.
        self._resync_requested.set()
        self._maintenance_wakeup.set()

    def _save_settings(self) -> None:
        with self._state_lock:
            settings = {
                "minimum_temp": self._minimum_temp,
                "maximum_temp": self._maximum_temp,
                "gpu_power_offset": self._gpu_power_offset,
                "dynamic_boost": self._dynamic_boost,
                "lightbar_mode": self._lightbar[0] if self._lightbar else None,
                "lightbar_color": self._lightbar[1] if self._lightbar else None,
            }
        try:
            self._settings_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self._settings_path.with_suffix(".tmp")
            temporary.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
            temporary.replace(self._settings_path)
        except OSError as exc:
            LOGGER.error("Could not save service settings: %s", exc)

    def _run_auto_cycle(self) -> None:
        super()._run_auto_cycle()
        with self._state_lock:
            error = self._last_auto_error
            automatic = self._automatic
        if error != self._logged_auto_error and automatic:
            if error:
                LOGGER.error("Automatic cycle failed closed: %s", error)
            elif self._logged_auto_error:
                LOGGER.info("Automatic control recovered")
            self._logged_auto_error = error

    def _maintenance_loop(self) -> None:
        while not self._stop_event.is_set():
            self._maintenance_wakeup.wait(MAINTENANCE_INTERVAL_SECONDS)
            self._maintenance_wakeup.clear()
            if self._stop_event.is_set():
                return
            if self._resync_requested.is_set():
                self._resync_requested.clear()
                LOGGER.info("Resume or resync requested; re-verifying EC state")
                with self._state_lock:
                    if self._automatic and not self._boost_enabled:
                        self._next_auto_at = time.monotonic()
                self._auto_wakeup.set()
            try:
                corrections = self._service.maintain()
            except Exception as exc:
                message = str(exc)
                if message != self._logged_maintenance_error:
                    LOGGER.error("EC state verification failed: %s", message)
                    self._logged_maintenance_error = message
                continue
            if self._logged_maintenance_error:
                LOGGER.info("EC state verification recovered")
                self._logged_maintenance_error = None
            if corrections:
                LOGGER.warning("Re-applied drifted EC state: %s", ", ".join(corrections))


class PeerCredentialRequestHandler(BackendRequestHandler):
    timeout = REQUEST_SOCKET_TIMEOUT_SECONDS


class PeerCredentialServer(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True

    def __init__(self, path: Path, controller: Any, allowed_uids: frozenset[int]) -> None:
        self.controller = controller
        self.allowed_uids = allowed_uids
        path.unlink(missing_ok=True)
        super().__init__(str(path), PeerCredentialRequestHandler)
        # Any local user may connect; the kernel-reported peer UID decides access.
        os.chmod(path, 0o666)

    def authorize(self, connection: socket.socket, request: dict[str, Any]) -> None:
        del request
        credentials = connection.getsockopt(
            socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")
        )
        _pid, uid, _gid = struct.unpack("3i", credentials)
        if uid not in self.allowed_uids:
            raise PermissionError("This user is not allowed to control the fan service")


def acquire_process_lock(directory: Path) -> Any:
    directory.mkdir(parents=True, exist_ok=True)
    handle = (directory / LOCK_FILENAME).open("a+b")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        raise RuntimeError("The fan-control service is already running") from None
    return handle


def run_linux_backend() -> None:
    if os.geteuid() != 0:
        raise PermissionError("The fan-control service must run as root")
    socket_path = linux_socket_path()
    lock_handle = acquire_process_lock(socket_path.parent)
    stop_requested = threading.Event()
    controller = LinuxBackendController(state_directory() / SETTINGS_FILENAME)

    def request_stop(signal_number: int, frame: Any) -> None:
        del signal_number, frame
        stop_requested.set()

    def request_resync(signal_number: int, frame: Any) -> None:
        del signal_number, frame
        controller.request_resync()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGUSR1, request_resync)

    server = PeerCredentialServer(socket_path, controller, load_allowed_uids(config_path()))
    server_thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.5}, name="ipc", daemon=True
    )
    server_thread.start()
    controller.start()
    LOGGER.info("Fan-control service started; listening on %s", socket_path)
    try:
        while not stop_requested.wait(1.0):
            pass
    finally:
        LOGGER.info("Stopping: setting both fans to 100%")
        server.shutdown()
        server.server_close()
        socket_path.unlink(missing_ok=True)
        try:
            controller.prepare_exit(confirmed=True)
            LOGGER.info("Both fans set to 100%")
        except Exception as exc:
            LOGGER.error("Could not set both fans to 100%% before stopping: %s", exc)
        controller.stop()
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
        lock_handle.close()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    try:
        run_linux_backend()
    except Exception:
        LOGGER.exception("The fan-control service stopped because of an error")
        raise SystemExit(1) from None
