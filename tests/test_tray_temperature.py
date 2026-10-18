import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication
from frontend import fan_control_gui as gui
from frontend.fan_control_gui import FanControlWindow


def readings(cpu, gpu):
    return {"cpu_c": cpu, "gpu_c": gpu, "cpu_source": "CPU", "gpu_source": "GPU"}


class TrayTemperatureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])

    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        for patcher in (
            patch.object(FanControlWindow, "_load_initial_state"),
            patch.object(FanControlWindow, "_settings_path",
                         return_value=Path(directory.name) / "settings.json"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.backend = Mock()
        self.window = FanControlWindow(backend=self.backend)
        self.window._telemetry_timer.stop()
        self.window._backend_watchdog_timer.stop()
        self.messages = patch.object(self.window.tray_icon, "showMessage").start()
        self.addCleanup(patch.stopall)
        self.addCleanup(self.close_window)

    def close_window(self):
        self.window._closing = True
        self.window._status_timer.stop()
        self.window._tray_blink_timer.stop()
        self.window.tray_icon.hide()
        self.window.lightbar_window.stop_pending_apply()
        self.window.deleteLater()
        self.application.processEvents()

    def test_files_exist_for_every_tray_level(self):
        for filename in gui.TRAY_ICON_FILENAMES.values():
            self.assertTrue(FanControlWindow._icon_path(filename).is_file(), filename)

    def test_level_follows_the_hottest_reading_with_strict_thresholds(self):
        cases = [
            ((70, 40), "normal"), ((40, 70.5), "warm"), ((85, 60), "warm"),
            ((60, 85.5), "hot"), ((86, None), "hot"), ((50, None), "normal"),
        ]
        for (cpu, gpu), level in cases:
            with self.subTest(cpu=cpu, gpu=gpu):
                self.window._show_temperatures(readings(cpu, gpu))
                self.assertEqual(self.window._tray_level, level)
        self.assertIn("C", self.window.tray_icon.toolTip())

    def test_hot_blinks_between_two_reds_and_stops_when_it_cools(self):
        icons = self.window._tray_icons
        self.window._show_temperatures(readings(90, 60))
        self.assertTrue(self.window._tray_blink_timer.isActive())
        self.assertEqual(self.window._tray_blink_timer.interval(), 500)
        with patch.object(self.window.tray_icon, "setIcon") as set_icon:
            self.window._blink_tray_icon()
            self.window._blink_tray_icon()
        self.assertEqual([call.args[0] for call in set_icon.call_args_list],
                         [icons["hot_dim"], icons["hot"]])
        with patch.object(self.window.tray_icon, "setIcon") as set_icon:
            self.window._show_temperatures(readings(75, 60))
        set_icon.assert_called_once_with(icons["warm"])
        self.assertFalse(self.window._tray_blink_timer.isActive())

    def test_hot_notifies_once_until_the_reading_returns_to_normal(self):
        for cpu in (90, 88, 75, 92):
            self.window._show_temperatures(readings(cpu, 50))
        self.messages.assert_called_once()
        self.assertIn("CPU is at 90 C", self.messages.call_args.args[1])
        self.window._show_temperatures(readings(60, 50))
        self.window._show_temperatures(readings(50, 87))
        self.assertEqual(self.messages.call_count, 2)
        self.assertIn("GPU is at 87 C", self.messages.call_args.args[1])

    def test_notifications_can_be_turned_off_from_the_tray_and_stay_off(self):
        action = self.window.hot_notifications_action
        self.assertTrue(action.isCheckable())
        self.assertTrue(action.isChecked())  # on by default
        self.assertIn(action, self.window.tray_icon.contextMenu().actions())
        action.setChecked(False)
        self.window._show_temperatures(readings(90, 60))
        self.messages.assert_not_called()
        # The icon still turns red and blinks.
        self.assertEqual(self.window._tray_level, "hot")
        self.assertTrue(self.window._tray_blink_timer.isActive())
        self.assertFalse(FanControlWindow._load_preferences()["hot_notifications"])
        action.setChecked(True)
        self.assertTrue(FanControlWindow._load_preferences()["hot_notifications"])

    def test_sensor_error_keeps_the_last_level(self):
        self.window._show_temperatures(readings(90, 60))
        self.window._show_temperature_error("GPU unavailable")
        self.assertEqual(self.window._tray_level, "hot")
        self.assertTrue(self.window._tray_blink_timer.isActive())
        self.backend.request.assert_not_called()


if __name__ == "__main__":
    unittest.main()
