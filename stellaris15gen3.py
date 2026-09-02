from typing import Any

from shared.fan_control_ipc import is_administrator


class InProcessBackendClient:
    def __init__(self, controller: Any) -> None:
        self._controller = controller

    def request(
        self,
        command: str,
        *,
        request_timeout: float | None = None,
        **arguments: Any,
    ) -> Any:
        del request_timeout
        return self._controller.dispatch(command, arguments)


def main() -> None:
    if not is_administrator():
        raise PermissionError("Stellaris Fan Control requires administrator access")

    from backend.fan_control_backend import BackendController
    from frontend.fan_control_gui import main as run_frontend

    controller = BackendController()
    controller.start(start_frontend=False, monitor_frontend=False)
    try:
        run_frontend(InProcessBackendClient(controller))
    finally:
        controller.stop()


if __name__ == "__main__":
    main()
