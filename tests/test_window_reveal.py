import os
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from frontend import fan_control_gui as gui
from frontend.fan_control_gui import FanControlWindow


class WindowRevealTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])

    def make_window(self):
        window = FanControlWindow(backend=Mock())
        window._telemetry_timer.stop()
        window._backend_watchdog_timer.stop()
        return window

    def close_window(self, window):
        window._closing = True
        window._status_timer.stop()
        window.tray_icon.hide()
        window.lightbar_window.stop_pending_apply()
        window.hide()
        window.deleteLater()
        self.application.processEvents()

    def wait_for(self, predicate):
        deadline = time.monotonic() + 2
        while not predicate() and time.monotonic() < deadline:
            QTest.qWait(10)
        self.assertTrue(predicate())

    def test_window_is_cloaked_until_its_first_frame_then_revealed(self):
        with TemporaryDirectory() as directory, \
                patch.object(FanControlWindow, "_load_initial_state"), \
                patch.object(FanControlWindow, "_settings_path", return_value=Path(directory) / "settings.json"), \
                patch.object(gui, "set_window_cloaked", return_value=True) as cloak:
            window = self.make_window()
            try:
                window.show()
                cloak.assert_called_once_with(window, True)
                self.wait_for(lambda: not window._window_cloaked)
                cloak.assert_called_with(window, False)
                self.assertFalse(window._reveal_timer.isActive())
                # Showing again from the tray cloaks and reveals again.
                window.hide()
                cloak.reset_mock()
                window.activate_from_second_instance()
                cloak.assert_called_once_with(window, True)
                self.wait_for(lambda: not window._window_cloaked)
                cloak.assert_called_with(window, False)
            finally:
                self.close_window(window)

    def test_fallback_reveals_window_when_no_frame_arrives(self):
        with TemporaryDirectory() as directory, \
                patch.object(FanControlWindow, "_load_initial_state"), \
                patch.object(FanControlWindow, "_settings_path", return_value=Path(directory) / "settings.json"), \
                patch.object(gui, "set_window_cloaked", return_value=True) as cloak, \
                patch.object(FanControlWindow, "eventFilter", return_value=False):
            window = self.make_window()
            window._reveal_timer.setInterval(50)
            try:
                window.show()
                self.assertTrue(window._window_cloaked)
                self.wait_for(lambda: not window._window_cloaked)
                cloak.assert_called_with(window, False)
            finally:
                self.close_window(window)

    def test_unsupported_platform_never_cloaks(self):
        with TemporaryDirectory() as directory, \
                patch.object(FanControlWindow, "_load_initial_state"), \
                patch.object(FanControlWindow, "_settings_path", return_value=Path(directory) / "settings.json"):
            window = self.make_window()
            try:
                window.show()
                self.assertFalse(window._window_cloaked)
                self.assertFalse(window._reveal_timer.isActive())
            finally:
                self.close_window(window)


if __name__ == "__main__":
    unittest.main()
