import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


RESTART_COOLDOWN_SECONDS = 60.0
FRONTEND_HEARTBEAT_TIMEOUT_SECONDS = 45.0
MAX_MESSAGE_BYTES = 64 * 1024
APP_DIRECTORY_NAME = "StellarisFanControl"
ENDPOINT_FILENAME = "backend-endpoint.json"


class BackendUnavailable(RuntimeError):
    pass


def runtime_directory() -> Path:
    override = os.environ.get("STELLARIS15GEN3_RUNTIME_DIR")
    if override:
        return Path(override)
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data) / APP_DIRECTORY_NAME
    return Path.home() / ".stellaris15gen3"


def endpoint_path() -> Path:
    return runtime_directory() / ENDPOINT_FILENAME


def component_command(role: str, *extra_arguments: str) -> list[str]:
    role_argument = f"--{role}"
    if getattr(sys, "frozen", False):
        return [sys.executable, role_argument, *extra_arguments]

    python = Path(sys.executable)
    if os.name == "nt":
        pythonw = python.with_name("pythonw.exe")
        if pythonw.exists():
            python = pythonw
    launcher = Path(__file__).resolve().with_name("stellaris15gen3.py")
    return [str(python), str(launcher), role_argument, *extra_arguments]


def launch_component(role: str, *extra_arguments: str) -> subprocess.Popen[bytes]:
    creation_flags = 0
    if os.name == "nt" and role == "backend":
        creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return subprocess.Popen(
        component_command(role, *extra_arguments),
        cwd=Path(__file__).resolve().parent,
        close_fds=True,
        creationflags=creation_flags,
    )


class BackendClient:
    def __init__(self, timeout: float = 15.0) -> None:
        self.timeout = timeout

    def request(self, command: str, **arguments: Any) -> Any:
        try:
            endpoint = json.loads(endpoint_path().read_text(encoding="utf-8"))
            host = str(endpoint["host"])
            port = int(endpoint["port"])
            token = str(endpoint["token"])
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise BackendUnavailable("Fan-control backend is not available") from exc

        payload = json.dumps(
            {"token": token, "command": command, "arguments": arguments},
            separators=(",", ":"),
        ).encode("utf-8") + b"\n"
        try:
            with socket.create_connection((host, port), timeout=self.timeout) as connection:
                connection.settimeout(self.timeout)
                connection.sendall(payload)
                received = bytearray()
                while b"\n" not in received:
                    chunk = connection.recv(4096)
                    if not chunk:
                        break
                    received.extend(chunk)
                    if len(received) > MAX_MESSAGE_BYTES:
                        raise BackendUnavailable("Backend response is too large")
        except (OSError, TimeoutError) as exc:
            raise BackendUnavailable("Fan-control backend is not responding") from exc

        try:
            response = json.loads(bytes(received).split(b"\n", 1)[0])
        except (ValueError, IndexError) as exc:
            raise BackendUnavailable("Backend returned an invalid response") from exc
        if not response.get("ok"):
            raise RuntimeError(str(response.get("error", "Backend request failed")))
        return response.get("result")

    def ping(self) -> bool:
        return self.request("ping") == "pong"


def wait_for_backend(timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    client = BackendClient(timeout=0.5)
    while time.monotonic() < deadline:
        try:
            if client.ping():
                return True
        except Exception:
            time.sleep(0.2)
    return False


def ensure_backend(*, start_frontend: bool) -> bool:
    try:
        if BackendClient(timeout=0.5).ping():
            return True
    except Exception:
        pass
    arguments = () if start_frontend else ("--no-frontend",)
    launch_component("backend", *arguments)
    return wait_for_backend()
