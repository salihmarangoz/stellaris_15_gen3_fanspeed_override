import sys

from shared.fan_control_ipc import (
    BackendClient,
    ensure_backend,
    is_administrator,
    launch_component,
)


def main() -> None:
    arguments = set(sys.argv[1:])
    if "--backend" in arguments:
        if not is_administrator():
            extra_arguments = () if "--no-frontend" not in arguments else ("--no-frontend",)
            launch_component("backend", *extra_arguments)
            return
        from backend.fan_control_backend import run_backend

        run_backend(start_frontend="--no-frontend" not in arguments)
        return

    if "--frontend" in arguments:
        if is_administrator():
            launch_component("frontend")
            return
        if not ensure_backend(start_frontend=False):
            raise RuntimeError("Could not start the fan-control backend")
        from frontend.fan_control_gui import main as run_frontend

        run_frontend()
        return

    client = BackendClient(timeout=0.5)
    try:
        if client.ping():
            client.request("show_frontend")
            return
    except Exception:
        pass
    if not ensure_backend(start_frontend=True):
        raise RuntimeError("Could not start the fan-control backend")


if __name__ == "__main__":
    main()
