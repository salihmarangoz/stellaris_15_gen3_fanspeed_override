import os
import sys
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


def check_qt_startup() -> int:
    """Exercise the packaged Qt DLLs and platform plugin without hardware access."""
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    from frontend.fan_control_gui import QApplication, QTimer, QWidget

    application = QApplication([sys.argv[0]])
    window = QWidget()
    window.show()
    QTimer.singleShot(0, application.quit)
    return application.exec()


def main() -> None:
    if "--check-qt" in sys.argv[1:]:
        raise SystemExit(check_qt_startup())
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
