import os
import threading
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from frontend.fan_control_gui import FanControlWindow
from stellaris15gen3 import InProcessBackendClient


class DeadlineGuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])

    def wait_for(self, predicate):
        deadline = time.monotonic() + 2
        while not predicate() and time.monotonic() < deadline:
            QTest.qWait(10)
        self.assertTrue(predicate())

    def test_deadline_reenables_ui_without_restarting_or_overlapping_controller(self):
        entered, release = threading.Event(), threading.Event()
        def dispatch(command, arguments):
            entered.set()
            release.wait(3)
            return True
        controller = Mock()
        controller.dispatch.side_effect = dispatch
        client = InProcessBackendClient(controller)
        with TemporaryDirectory() as directory, \
                patch.object(FanControlWindow, "_load_initial_state"), \
                patch.object(FanControlWindow, "_settings_path", return_value=Path(directory) / "settings.json"), \
                patch("frontend.fan_control_gui.launch_component") as launch:
            window = FanControlWindow(backend=client)
            # The in-process backend is the Windows model; Linux talks to a root service.
            window._linux = False
            window._telemetry_timer.stop()
            window._backend_watchdog_timer.stop()
            try:
                window._run(
                    lambda: client.request("apply_manual", request_timeout=0.02, cpu=50, gpu=50),
                    lambda result: self.fail("A late request must not report success"),
                    "Testing deadline", show_error=False,
                )
                self.assertTrue(entered.wait(1))
                self.wait_for(lambda: not window._busy)
                self.assertTrue(window.boost_button.isEnabled())
                self.assertTrue(window.exit_button.isEnabled())
                window.check_backend()
                self.wait_for(lambda: not window._backend_check_inflight)
                launch.assert_not_called()
                controller.dispatch.assert_called_once()
                self.assertIn("unconfirmed", window.connection_label.text())
            finally:
                release.set()
                window._pool.waitForDone(2000)
                window._closing = True
                window._status_timer.stop()
                window.tray_icon.hide()
                window.lightbar_window.stop_pending_apply()
                window.deleteLater()
                self.application.processEvents()

    def test_warning_distinguishes_confirmed_boost_from_unknown_output(self):
        with TemporaryDirectory() as directory, \
                patch.object(FanControlWindow, "_load_initial_state"), \
                patch.object(FanControlWindow, "_settings_path", return_value=Path(directory) / "settings.json"):
            window = FanControlWindow(backend=Mock())
            window._telemetry_timer.stop()
            window._backend_watchdog_timer.stop()
            try:
                state = {"output_error": "readback failed", "automatic": True,
                         "emergency_boost_confirmed": False}
                window._show_backend_state(state)
                self.assertIn("unconfirmed", window.connection_label.text())
                state["emergency_boost_confirmed"] = True
                window._show_backend_state(state)
                self.assertIn("confirmed at 100%", window.connection_label.text())
            finally:
                window._closing = True
                window._status_timer.stop()
                window.tray_icon.hide()
                window.lightbar_window.stop_pending_apply()
                window.deleteLater()
                self.application.processEvents()


if __name__ == "__main__":
    unittest.main()
