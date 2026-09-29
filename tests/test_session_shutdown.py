import os
import subprocess
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QThreadPool

from PySide6.QtWidgets import QApplication, QMessageBox

from frontend.fan_control_gui import FanControlWindow, ModeToggle, Worker


class FrontendStartupTests(unittest.TestCase):
    def test_real_qt_startup_reaches_event_loop_without_hardware(self):
        # Exercise real QApplication/window startup in a separate process.
        script = '''
import json
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QSystemTrayIcon
from frontend import fan_control_gui as gui

backend = Mock()
original_window = gui.FanControlWindow
minimized = sys.argv[1] != "False"
tray_available = sys.argv[2] == "True"
def make_window(*args, **kwargs):
    window = original_window(*args, **kwargs)
    def check_window():
        expected_visible = not minimized or not tray_available
        valid = window.isVisible() == expected_visible
        if minimized and not tray_available:
            valid = valid and window.isMinimized()
        window.activate_from_second_instance()
        valid = valid and window.isVisible() and not window.isMinimized()
        window.start_minimized_checkbox.setChecked(not minimized)
        saved = json.loads(settings.read_text())
        valid = valid and saved["start_minimized"] == (not minimized)
        QApplication.instance().exit(0 if valid else 3)
    QTimer.singleShot(100, check_window)
    return window

with (
    TemporaryDirectory() as directory,
    patch.object(gui, "notify_existing_instance", return_value=False),
    patch.object(gui, "create_instance_server"),
    patch.object(original_window, "_load_initial_state"),
    patch.object(gui, "FanControlWindow", side_effect=make_window),
    patch.object(QSystemTrayIcon, "isSystemTrayAvailable", return_value=tray_available),
    patch.object(original_window, "_settings_path", return_value=Path(directory) / "settings.json"),
):
    settings = Path(directory) / "settings.json"
    preference = {"start_minimized": minimized}
    if sys.argv[1] == "missing":
        preference = {}
    elif sys.argv[1] == "invalid":
        preference = {"start_minimized": "false"}
    settings.write_text(json.dumps(preference))
    try:
        gui.main(backend)
    except SystemExit:
        backend.request.assert_not_called()
        raise
'''
        for minimized, tray_available in (
            (False, True), (True, True), (True, False), ("missing", True), ("invalid", True)
        ):
            with self.subTest(minimized=minimized, tray_available=tray_available):
                result = subprocess.run(
                    [sys.executable, "-c", script, str(minimized), str(tray_available)],
                    cwd=Path(__file__).resolve().parents[1],
                    env={**os.environ, "QT_QPA_PLATFORM": "offscreen"},
                    capture_output=True, text=True, timeout=20,
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


class SessionShutdownTests(unittest.TestCase):
    def make_window(self):
        pool = QThreadPool()
        pool.setMaxThreadCount(1)
        return SimpleNamespace(
            _pool=pool, _backend=Mock(), _exit_prepared=False,
            _closing=False, _manual_apply_timer=Mock(),
            _set_status=Mock(), close=Mock(),
        )

    def test_shutdown_waits_for_existing_operation_then_full_speed(self):
        window = self.make_window()
        manager = Mock()
        calls = []
        window._pool.start(Worker(lambda: calls.append("pending operation")))
        window._backend.request.side_effect = lambda *a, **kw: calls.append(a[0])
        window.close.side_effect = lambda: calls.append("close")
        FanControlWindow.prepare_session_shutdown(window, manager)
        self.assertEqual(calls, ["pending operation", "prepare_exit", "close"])
        window._backend.request.assert_called_once_with(
            "prepare_exit", confirmed=True, request_timeout=40.0
        )
        self.assertTrue(window._exit_prepared)
        self.assertTrue(window._closing)
        manager.cancel.assert_not_called()

    def test_shutdown_write_failure_requests_cancellation(self):
        window = self.make_window()
        manager = Mock()
        window._backend.request.side_effect = RuntimeError("write failed")
        FanControlWindow.prepare_session_shutdown(window, manager)
        manager.cancel.assert_called_once()
        window.close.assert_not_called()
        self.assertFalse(window._exit_prepared)
        self.assertFalse(window._closing)

    def test_shutdown_timeout_requests_cancellation(self):
        window = self.make_window()
        window._pool = Mock()
        window._pool.waitForDone.return_value = False
        manager = Mock()
        FanControlWindow.prepare_session_shutdown(window, manager)
        manager.cancel.assert_called_once()
        window.close.assert_not_called()

    def test_already_prepared_exit_does_not_write_again(self):
        window = self.make_window()
        window._exit_prepared = True
        FanControlWindow.prepare_session_shutdown(window, Mock())
        window._backend.request.assert_not_called()


class ModeConfirmationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_confirmation_in_both_directions(self):
        for manual in (False, True):
            for accepted in (False, True):
                with self.subTest(manual=manual, accepted=accepted):
                    toggle = ModeToggle()
                    toggle.setChecked(manual)
                    changed = Mock()
                    toggle.toggled.connect(changed)
                    answer = (QMessageBox.StandardButton.Yes if accepted
                              else QMessageBox.StandardButton.No)
                    with patch.object(QMessageBox, "question", return_value=answer) as ask:
                        toggle.click()
                    ask.assert_called_once()
                    self.assertEqual(toggle.is_manual(), not manual if accepted else manual)
                    if accepted:
                        changed.assert_called_once_with(not manual)
                    else:
                        changed.assert_not_called()

    def test_programmatic_sync_does_not_prompt(self):
        toggle = ModeToggle()
        with patch.object(QMessageBox, "question") as ask:
            toggle.setChecked(True)
            toggle.setChecked(False)
        ask.assert_not_called()


class LinuxQuitTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_linux_quit_asks_and_never_writes_fans(self):
        from tempfile import TemporaryDirectory

        with (
            TemporaryDirectory() as directory,
            patch.object(FanControlWindow, "_load_initial_state"),
            patch.object(
                FanControlWindow, "_settings_path",
                return_value=Path(directory) / "settings.json",
            ),
        ):
            backend = Mock()
            window = FanControlWindow(backend=backend)
            window._linux = True
            with patch.object(
                QMessageBox, "question", return_value=QMessageBox.StandardButton.No
            ):
                window.request_exit()
            self.assertFalse(window._exit_prepared)
            with (
                patch.object(
                    QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes
                ),
                patch.object(window, "close") as close,
            ):
                window.request_exit()
            close.assert_called_once()
            self.assertTrue(window._exit_prepared)
            self.assertNotIn(
                "prepare_exit", [call.args[0] for call in backend.request.call_args_list]
            )
            window._closing = True
            window.tray_icon.hide()


class TrayMenuTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_about_follows_show_and_opens_the_project_page(self):
        from tempfile import TemporaryDirectory

        from frontend import fan_control_gui as gui

        with (
            TemporaryDirectory() as directory,
            patch.object(FanControlWindow, "_load_initial_state"),
            patch.object(
                FanControlWindow, "_settings_path",
                return_value=Path(directory) / "settings.json",
            ),
        ):
            window = FanControlWindow(backend=Mock())
            actions = [a.text() for a in window.tray_icon.contextMenu().actions() if a.text()]
            self.assertEqual(actions[:2], ["Show Fan Control", "About"])
            with patch.object(gui.QDesktopServices, "openUrl", return_value=True) as open_url:
                window.open_project_website()
            self.assertEqual(open_url.call_args.args[0].toString(), gui.PROJECT_URL)
            window._closing = True
            window.tray_icon.hide()


class LightbarWindowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_one_option_at_a_time_with_live_apply(self):
        from tempfile import TemporaryDirectory

        with (
            TemporaryDirectory() as directory,
            patch.object(FanControlWindow, "_load_initial_state"),
            patch.object(
                FanControlWindow, "_settings_path",
                return_value=Path(directory) / "settings.json",
            ),
        ):
            backend = Mock()
            window = FanControlWindow(backend=backend)
            lightbar = window.lightbar_window
            panels = lightbar._panels
            self.assertFalse(window.lightbar_button.isEnabled())
            window._show_backend_state({"capabilities": {"lightbar": True}})
            self.assertTrue(window.lightbar_button.isEnabled())
            self.assertFalse(any(panel.isEnabled() for panel in panels.values()))

            lightbar.show_state(
                {"available": True, "mode": "rainbow", "color": [36, 20, 0], "max_level": 36}
            )
            self.assertTrue(lightbar.rainbow_checkbox.isChecked())
            self.assertTrue(all(panel.isEnabled() for panel in panels.values()))
            self.assertFalse(lightbar.color_sliders[0].isEnabled())
            with patch.object(window, "_run") as run:
                # Clicking the ticked option keeps it; nothing is written.
                lightbar.rainbow_checkbox.click()
                self.assertTrue(lightbar.rainbow_checkbox.isChecked())
                window.apply_lightbar()
                run.assert_not_called()

                # Clicking another option switches to it directly.
                lightbar.color_checkbox.click()
                self.assertEqual(lightbar.selected_mode(), "color")
                self.assertFalse(lightbar.rainbow_checkbox.isChecked())
                self.assertTrue(lightbar.color_sliders[0].isEnabled())
                window.apply_lightbar()
                run.assert_called_once()
                run.call_args.args[0]()
                backend.request.assert_called_with(
                    "set_lightbar", mode="color", red=36, green=20, blue=0
                )
                run.call_args.args[1]({})
                window.apply_lightbar()
                run.assert_called_once()

                lightbar.color_sliders[2].setValue(5)
                self.assertTrue(lightbar._apply_timer.isActive())
                self.assertEqual(lightbar.requested_state(), ("color", (36, 20, 5)))

                lightbar.off_checkbox.click()
                self.assertEqual(lightbar.requested_state(), ("off", (36, 20, 5)))
                self.assertFalse(lightbar.color_sliders[0].isEnabled())
            lightbar.stop_pending_apply()
            window._show_backend_state({"capabilities": {}})
            self.assertFalse(window.lightbar_button.isEnabled())
            window._closing = True
            window.tray_icon.hide()
