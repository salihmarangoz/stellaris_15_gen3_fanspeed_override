import subprocess
import os
import sys
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from backend.fan_control_backend import BackendController
from backend.fan_control_service import ControlCenterService, load_lightbar_choice
from backend.temperature_service import (
    Temperatures,
    read_cpu_power_draw,
    read_gpu_power_draw,
    read_nvidia_temperature,
    read_temperatures,
)


class PowerTelemetryTests(unittest.TestCase):
    def setUp(self):
        cpu_power = patch("backend.temperature_service._cpu_power_meter.sample", return_value=None)
        self.cpu_power = cpu_power.start()
        self.addCleanup(cpu_power.stop)

    def test_standalone_cli_still_reaches_help_without_hardware(self):
        script = Path(__file__).resolve().parents[1] / "backend" / "fan_control.py"
        with TemporaryDirectory() as directory:
            result = subprocess.run(
                [sys.executable, str(script), "--help"], cwd=directory,
                capture_output=True, text=True, timeout=10,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("fixed", result.stdout)

    def test_optional_gpu_power_accepts_zero_and_rejects_bad_values(self):
        for value, expected in ((42.5, 42.5), (0, 0.0), (None, None), ("42", None),
                                (float("nan"), None), (float("inf"), None),
                                (-1, None), (301, None), (True, None)):
            with self.subTest(value=value), patch(
                "backend.temperature_service._telemetry_helper.query",
                return_value={"power_w": value, "sw_thermal": False},
            ) as query:
                result = read_gpu_power_draw()
                self.assertEqual(result["gpu_power_w"], expected)
                query.assert_called_once_with("power", 2)

    def test_thermal_flag_is_optional_and_independent_of_power_draw(self):
        for result, expected in (({"power_w": None, "sw_thermal": True}, True),
                                 ({"power_w": 42, "sw_thermal": False}, False),
                                 ({"power_w": 42, "sw_thermal": None}, None),
                                 ({"power_w": 42, "sw_thermal": "Active"}, None),
                                 ({"power_w": 42}, None), ({"powered_off": True}, None)):
            with self.subTest(result=result), patch(
                "backend.temperature_service._telemetry_helper.query", return_value=result
            ):
                power = read_gpu_power_draw()
                self.assertIs(power["gpu_sw_thermal_throttling"], expected)
                if result.get("power_w") is None:
                    self.assertIsNone(power["gpu_power_w"])

    def test_power_errors_are_optional_and_temperature_reads_do_not_query_power(self):
        for error in (OSError("missing"), RuntimeError("unsupported"), TimeoutError("hung")):
            with patch("backend.temperature_service._telemetry_helper.query", side_effect=error):
                self.assertEqual(read_gpu_power_draw(), {"gpu_power_w": None,
                                                        "gpu_sw_thermal_throttling": None})
        with (
            patch("backend.temperature_service.read_ryzen_temperature", return_value=(50, "CPU")),
            patch("backend.temperature_service.read_nvidia_temperature", return_value=(60, "GPU")),
            patch("backend.temperature_service.read_gpu_power_draw") as power,
        ):
            self.assertEqual(read_temperatures(), Temperatures(50, 60, "CPU", "GPU"))
            power.assert_not_called()

    def test_cpu_and_gpu_power_failures_are_independent(self):
        self.cpu_power.return_value = 15.5
        with patch("backend.temperature_service._telemetry_helper.query",
                   side_effect=RuntimeError("GPU gone")):
            self.assertEqual(read_cpu_power_draw(), {"cpu_power_w": 15.5})
            self.assertEqual(read_gpu_power_draw(), {"gpu_power_w": None,
                                                    "gpu_sw_thermal_throttling": None})
        self.cpu_power.side_effect = RuntimeError("CPU gone")
        with patch("backend.temperature_service._telemetry_helper.query",
                   return_value={"power_w": 30, "sw_thermal": False}):
            self.assertEqual(read_cpu_power_draw(), {"cpu_power_w": None})
            self.assertEqual(read_gpu_power_draw(), {"gpu_power_w": 30.0,
                                                    "gpu_sw_thermal_throttling": False})

    def test_snapshot_enriches_display_and_power_failure_does_not_invalidate_sensors(self):
        with patch("backend.fan_control_backend.FanService.instance", return_value=Mock()):
            controller = BackendController()
        with patch("backend.fan_control_backend.read_temperatures",
                   return_value=Temperatures(50, 60, "CPU", "GPU")):
            with patch("backend.fan_control_backend.read_cpu_power_draw",
                       return_value={"cpu_power_w": 12.5}),                     patch("backend.fan_control_backend.read_gpu_power_draw", return_value={
                        "gpu_power_w": 40, "gpu_sw_thermal_throttling": True,
                    }):
                readings, error = controller._read_temperature_snapshot()
                self.assertEqual(readings["cpu_power_w"], 12.5)
                self.assertEqual(readings["gpu_power_w"], 40)
                self.assertIs(readings["gpu_sw_thermal_throttling"], True)
                self.assertIsNone(error)
            with patch("backend.fan_control_backend.read_cpu_power_draw", side_effect=RuntimeError("gone")),                     patch("backend.fan_control_backend.read_gpu_power_draw", side_effect=RuntimeError("gone")):
                readings, error = controller._read_temperature_snapshot()
                self.assertEqual((readings["cpu_c"], readings["gpu_c"]), (50, 60))
                self.assertIsNone(readings["gpu_power_w"])
                self.assertIsNone(readings["cpu_power_w"])
                self.assertIsNone(readings["gpu_sw_thermal_throttling"])
                self.assertIsNone(error)
                self.assertIsNone(controller._sensor_error())

    def test_stuck_gpu_power_query_never_blocks_temperature_reads(self):
        with patch("backend.fan_control_backend.FanService.instance", return_value=Mock()):
            controller = BackendController()
        controller._sensor_timeout = 0.2
        release = threading.Event()
        self.addCleanup(release.set)
        stuck = Mock(side_effect=lambda: release.wait(5) and {})
        with patch("backend.fan_control_backend.read_temperatures",
                   return_value=Temperatures(50, 60, "CPU", "GPU")), \
                patch("backend.fan_control_backend.read_cpu_power_draw",
                      return_value={"cpu_power_w": None}), \
                patch("backend.fan_control_backend.read_gpu_power_draw", stuck):
            for _ in range(3):
                readings, error = controller._read_temperature_snapshot()
                self.assertIsNone(error)
                self.assertEqual((readings["cpu_c"], readings["gpu_c"]), (50, 60))
                self.assertIsNone(readings["gpu_power_w"])
                self.assertIsNone(controller._sensor_error())
        # The abandoned query is skipped rather than queued or restarted.
        stuck.assert_called_once()

    def test_temperature_nvidia_query_ends_before_the_sensor_deadline(self):
        with patch("backend.fan_control_backend.FanService.instance", return_value=Mock()):
            controller = BackendController()
        with patch("backend.temperature_service._temperature_helper.query",
                   return_value={"name": "RTX", "temperature": 55}) as query:
            self.assertEqual(read_nvidia_temperature(), (55.0, "NVIDIA driver (RTX)"))
        operation, timeout = query.call_args.args
        self.assertEqual(operation, "temperature")
        self.assertLess(timeout, controller._sensor_timeout)

    def test_lightbar_maintenance_survives_an_exception(self):
        with patch("backend.fan_control_backend.FanService.instance", return_value=Mock()):
            controller = BackendController()
        controller._stop_event = Mock()
        controller._stop_event.wait.side_effect = [False, False, True]
        attempts = iter([RuntimeError("write failed"), None])
        def maintain():
            error = next(attempts)
            if error is not None:
                raise error
            self.assertEqual(controller._lightbar_error, "write failed")
        controller._service.maintain_lightbar.side_effect = maintain
        controller._lightbar_loop()
        self.assertEqual(controller._service.maintain_lightbar.call_count, 2)
        self.assertIsNone(controller._lightbar_error)


class ThermalWarningGuiTests(unittest.TestCase):
    def test_warning_shows_only_for_active_flag_and_clears_on_sensor_error(self):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtWidgets import QApplication
        from frontend.fan_control_gui import FanControlWindow

        app = QApplication.instance() or QApplication([])
        with (
            TemporaryDirectory() as directory,
            patch.object(FanControlWindow, "_load_initial_state"),
            patch.object(FanControlWindow, "_settings_path",
                         return_value=Path(directory) / "settings.json"),
        ):
            backend = Mock()
            window = FanControlWindow(backend=backend)
            try:
                window._show_backend_state({"control_method": "direct_ec"})
                readings = {"cpu_c": 50, "gpu_c": 60, "cpu_source": "CPU", "gpu_source": "GPU"}
                self.assertEqual(window.control_method_label.text(), "Direct EC")
                self.assertTrue(window.gpu_thermal_warning.isHidden())
                for active in (True, False, True, None, "Active", 1):
                    window._show_temperatures({**readings, "gpu_sw_thermal_throttling": active})
                    self.assertEqual(window.gpu_thermal_warning.isHidden(), active is not True)
                window._show_temperatures({**readings, "gpu_sw_thermal_throttling": True})
                window._show_temperature_error("GPU unavailable")
                self.assertTrue(window.gpu_thermal_warning.isHidden())
                self.assertIn("NVIDIA", window.gpu_thermal_warning.toolTip())
                self.assertIn("memory", window.gpu_thermal_warning.toolTip())
                backend.request.assert_not_called()
            finally:
                window._closing = True
                window.tray_icon.hide()
                window.deleteLater()
                app.processEvents()


class MaintenanceWarningGuiTests(unittest.TestCase):
    def test_gpu_power_error_survives_telemetry_and_clears_after_recovery(self):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtWidgets import QApplication
        from frontend.fan_control_gui import FanControlWindow

        app = QApplication.instance() or QApplication([])
        with (
            TemporaryDirectory() as directory,
            patch.object(FanControlWindow, "_load_initial_state"),
            patch.object(FanControlWindow, "_settings_path",
                         return_value=Path(directory) / "settings.json"),
        ):
            backend = Mock()
            window = FanControlWindow(backend=backend)
            error = "GPU power readback did not match the write"
            state = {"control_method": "direct_ec", "gpu_power_error": error}
            try:
                window._show_backend_state(state)
                self.assertFalse(window.fault_label.isHidden())
                self.assertEqual(window.fault_label.text(), error)
                window._show_temperatures({
                    "cpu_c": 50, "gpu_c": 60, "cpu_source": "CPU", "gpu_source": "GPU",
                })
                self.assertFalse(window.fault_label.isHidden())
                self.assertEqual(window.fault_label.text(), error)
                window._show_temperature_error("GPU unavailable")
                window._show_backend_state(state)
                self.assertEqual(window.fault_label.text(), "Sensor error: GPU unavailable")
                window._set_fault("sensor", None)
                self.assertEqual(window.fault_label.text(), error)
                window._show_backend_state({**state, "gpu_power_error": None})
                self.assertTrue(window.fault_label.isHidden())
                backend.request.assert_not_called()
            finally:
                window._closing = True
                window.tray_icon.hide()
                window.deleteLater()
                app.processEvents()


class FakeLightbarClient:
    method_name = "direct_ec"

    def __init__(self):
        self.calls = []

    def write_lightbar(self, mode, color, *, force=False):
        self.calls.append((mode, color, force))

    def read_lightbar(self):
        return {"mode": "rainbow", "color": [0, 0, 0]}


class WindowsLightbarServiceTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "lightbar.json"
        settings = patch("backend.fan_control_service.lightbar_settings_path", return_value=self.path)
        settings.start()
        self.addCleanup(settings.stop)
        broker = patch("backend.fan_control_service.oem_broker_available", return_value=False)
        self.broker = broker.start()
        self.addCleanup(broker.stop)
        self.service = object.__new__(ControlCenterService)
        self.service.__init__()
        self.client = FakeLightbarClient()
        self.service._method = "direct_ec"
        self.service._connected_client = Mock(return_value=self.client)

    def test_choice_persists_and_retains_color_while_off(self):
        state = self.service.set_lightbar("off", (36, 20, 5))
        self.assertEqual(state["color"], [36, 20, 5])
        self.assertEqual(load_lightbar_choice(), ("off", (36, 20, 5)))
        restored = object.__new__(ControlCenterService)
        restored.__init__()
        self.assertEqual(restored._desired_lightbar, ("off", (36, 20, 5)))
        self.assertEqual(self.service.lightbar_state()["color"], [36, 20, 5])

    def test_oem_blocks_commands_and_maintenance_without_connecting(self):
        self.broker.return_value = True
        self.service._desired_lightbar = ("color", (1, 2, 3))
        self.assertFalse(self.service.lightbar_state()["available"])
        with self.assertRaisesRegex(RuntimeError, "GCUBridge"):
            self.service.set_lightbar("off", (0, 0, 0))
        self.service.maintain_lightbar()
        self.service._connected_client.assert_not_called()
        self.assertEqual(self.client.calls, [])
        self.service._method = "oem_mqtt"
        self.assertFalse(self.service.capabilities()["lightbar"])
        self.service._method = "direct_ec"
        self.assertTrue(self.service.capabilities()["lightbar"])

    def test_maintenance_only_writes_a_user_choice_and_refreshes_after_15_minutes(self):
        self.service.maintain_lightbar()
        self.service._connected_client.assert_not_called()
        self.service._desired_lightbar = ("color", (1, 2, 3))
        with patch("backend.fan_control_service.time.monotonic", return_value=100):
            self.service.maintain_lightbar()
        with patch("backend.fan_control_service.time.monotonic", return_value=115):
            self.service.maintain_lightbar()
        with patch("backend.fan_control_service.time.monotonic", return_value=1000):
            self.service.maintain_lightbar()
        self.assertEqual([call[2] for call in self.client.calls], [True, False, True])
        self.broker.return_value = True
        self.service.maintain_lightbar()
        self.broker.return_value = False
        self.service.maintain_lightbar()
        self.assertTrue(self.client.calls[-1][2])

    def test_failed_write_does_not_save_choice(self):
        self.client.write_lightbar = Mock(side_effect=RuntimeError("readback failed"))
        with self.assertRaisesRegex(RuntimeError, "readback"):
            self.service.set_lightbar("color", (1, 2, 3))
        self.assertIsNone(self.service._desired_lightbar)
        self.assertFalse(self.path.exists())

    def test_bad_settings_are_ignored(self):
        for text in ("broken", "[]", '{"mode":"color","color":[100,0,0]}'):
            self.path.write_text(text)
            self.assertIsNone(load_lightbar_choice())
